//! The applications list and one application: `/api/v1/leads...`.

use std::collections::HashSet;
use std::path::{Path as FilePath, PathBuf};
use std::sync::{Arc, LazyLock};

use agent_hire_leadtext::Seniority;
use agent_hire_store::{
    LeadError, LeadFilters, LeadSort, LeadView, ProfileStore, SeniorityFilter, UtcTime,
    annotate_seniority, normalize_skill,
};
use axum::Json;
use axum::body::{Body, Bytes};
use axum::extract::{Path, RawQuery, Request, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::app_state::AppState;
use crate::respond;
use crate::validation::{self, ModelBody, ModelReader, QueryReader};

const VIEWS: [&str; 7] = [
    "all",
    "hot",
    "found",
    "evaluated",
    "generated",
    "applied",
    "discarded",
];
const SORTS: [&str; 5] = ["recommended", "newest", "signal", "match", "company"];
const STATUSES: [&str; 14] = [
    "discovered",
    "evaluating",
    "tailoring",
    "approved",
    "applied",
    "interviewing",
    "rejected",
    "accepted",
    "discarded",
    "matched",
    "bidding",
    "proposal_sent",
    "awarded",
    "completed",
];
const EXPERIENCE_LEVELS: [&str; 4] = [
    "no_prior_experience",
    "entry_level",
    "mid_level",
    "senior_level",
];
const EMPLOYMENT_TYPES: [&str; 6] = [
    "full_time",
    "part_time",
    "contract",
    "temporary",
    "internship",
    "per_diem",
];

static JOB_ID: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-zA-Z0-9_\-]{1,128}$").expect("valid pattern"));
static ROLE_ID: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z][a-z0-9_]{1,63}$").expect("valid pattern"));
static CANONICAL_INT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[1-9][0-9]*$").expect("valid pattern"));

fn bad_request(detail: &str) -> Response {
    respond::detail(StatusCode::BAD_REQUEST, detail)
}

fn unprocessable(detail: &str) -> Response {
    respond::detail(StatusCode::UNPROCESSABLE_ENTITY, detail)
}

/// `400 Invalid job ID format` unless the id is 1-128 letters, digits, `_` or `-`.
fn check_job_id(job_id: &str) -> Result<(), Response> {
    if JOB_ID.is_match(job_id) {
        Ok(())
    } else {
        Err(bad_request("Invalid job ID format"))
    }
}

/// A comma-separated filter: trimmed, de-duplicated, bounded.
fn csv(raw: Option<&str>, max_items: usize, max_len: usize) -> Option<Vec<String>> {
    let mut out: Vec<String> = Vec::new();
    for part in raw.unwrap_or("").split(',') {
        let part = part.trim();
        if part.is_empty() || out.iter().any(|seen| seen == part) {
            continue;
        }
        if part.chars().count() > max_len {
            return None;
        }
        out.push(part.to_owned());
    }
    (out.len() <= max_items).then_some(out)
}

/// The filters shared by the list and the summary, or the 422 for the first bad one.
fn shared_filters(pairs: &[(String, String)]) -> Result<LeadFilters, Response> {
    let value = |name: &str| validation::query_value(pairs, name);
    let role_ids = csv(value("role_ids"), 32, 64)
        .filter(|ids| {
            ids.iter().all(|id| {
                ROLE_ID.is_match(id)
                    && !matches!(id.as_str(), "other" | "unresolved" | "pending" | "none")
            })
        })
        .ok_or_else(|| unprocessable("invalid role_ids"))?;
    let experience_levels = csv(value("experience_levels"), 4, 32)
        .filter(|levels| {
            levels
                .iter()
                .all(|level| EXPERIENCE_LEVELS.contains(&level.as_str()))
        })
        .ok_or_else(|| unprocessable("invalid experience_levels"))?;
    let required_skills = csv(value("required_skills"), 20, 80)
        .filter(|skills| {
            skills
                .iter()
                .all(|skill| !skill.chars().any(|c| (c as u32) < 32))
        })
        .ok_or_else(|| unprocessable("invalid required_skills"))?;
    let include_unresolved = match value("include_unresolved").unwrap_or("true") {
        "true" => true,
        "false" => false,
        _ => return Err(unprocessable("include_unresolved must be true or false")),
    };
    let posted_within_hours = match value("posted_within_hours").filter(|raw| !raw.is_empty()) {
        None => None,
        Some(raw) => {
            let hours: i64 = raw
                .trim()
                .parse()
                .map_err(|_| unprocessable("posted_within_hours must be a whole number"))?;
            if !(1..=8760).contains(&hours) {
                return Err(unprocessable("posted_within_hours must be 1..8760"));
            }
            Some(hours)
        }
    };
    let company: String = value("company")
        .map(str::trim)
        .unwrap_or("")
        .chars()
        .take(200)
        .collect();
    let sponsorship = match value("sponsorship") {
        None => None,
        Some(raw @ ("yes" | "not_no")) => Some(raw.to_owned()),
        Some(_) => return Err(unprocessable("sponsorship must be yes or not_no")),
    };
    let employment_types: Vec<String> = value("employment_types")
        .unwrap_or("")
        .split(',')
        .filter(|part| !part.is_empty())
        .map(str::to_owned)
        .collect();
    if employment_types
        .iter()
        .any(|kind| !EMPLOYMENT_TYPES.contains(&kind.as_str()))
    {
        return Err(unprocessable("invalid employment_types"));
    }
    Ok(LeadFilters {
        session_id: value("session_id")
            .filter(|id| !id.is_empty())
            .map(str::to_owned),
        role_ids,
        experience_levels,
        required_skills,
        include_unresolved,
        posted_within_hours,
        company,
        sponsorship,
        employment_types,
        ..LeadFilters::default()
    })
}

/// A seniority query value for the paged list and the summary (`beginner` allowed).
fn strict_seniority(
    raw: Option<&str>,
    beginner_only: bool,
) -> Result<Option<SeniorityFilter>, Response> {
    if beginner_only {
        return Ok(Some(SeniorityFilter::Beginner));
    }
    let original = raw.unwrap_or("");
    let lowered = original.trim().to_lowercase();
    match lowered.as_str() {
        "" | "all" => Ok(None),
        "beginner" => Ok(Some(SeniorityFilter::Beginner)),
        other => match Seniority::parse(other) {
            Some(band) => Ok(Some(SeniorityFilter::Band(band))),
            None => Err(unprocessable(&format!(
                "invalid seniority: {}",
                python_repr(original)
            ))),
        },
    }
}

/// Python's `repr` of a string.
fn python_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let escaped = text.replace('\\', "\\\\");
    let escaped = if quote == '\'' {
        escaped.replace('\'', "\\'")
    } else {
        escaped
    };
    format!("{quote}{escaped}{quote}")
}

fn lead_error(error: LeadError, context: &'static str) -> Response {
    match error {
        LeadError::UnknownSession => respond::detail(StatusCode::NOT_FOUND, "unknown save session"),
        LeadError::Invalid(message) => unprocessable(&message),
        LeadError::NotFound(_) => respond::detail(StatusCode::NOT_FOUND, "lead not found"),
        LeadError::Store(error) => respond::internal_error(context, &error),
    }
}

/// `GET /api/v1/leads`: the whole list (no `page`), or one page of it.
pub async fn list(State(state): State<Arc<AppState>>, request: Request) -> Response {
    let pairs = validation::query_pairs(request.uri().query());
    let mut reader = QueryReader::default();
    let beginner_only = reader.boolean(&pairs, "beginner_only", false);
    let min_score = reader.integer(&pairs, "min_score");
    let view = reader.literal(&pairs, "view", &VIEWS, "all").to_owned();
    let sort = reader.literal(&pairs, "sort", &SORTS, "newest").to_owned();
    let collapse_similar = reader.boolean(&pairs, "collapse_similar", false);
    let include_preferred = reader.boolean(&pairs, "include_preferred", false);
    let match_profile = reader.boolean(&pairs, "match_profile", false);
    if let Err(response) = reader.finish() {
        return response;
    }
    let mut filters = match shared_filters(&pairs) {
        Ok(filters) => filters,
        Err(response) => return response,
    };
    filters.include_preferred = include_preferred;
    let value = |name: &str| validation::query_value(&pairs, name);
    let page = match value("page") {
        None => None,
        Some(raw) if CANONICAL_INT.is_match(raw) => match raw.parse::<usize>() {
            Ok(page) => Some(page),
            Err(_) => return unprocessable("page must be a canonical decimal integer >= 1"),
        },
        Some(_) => return unprocessable("page must be a canonical decimal integer >= 1"),
    };
    let limit_raw = value("limit").unwrap_or("200");
    let limit = match limit_raw.parse::<usize>() {
        Ok(limit) if CANONICAL_INT.is_match(limit_raw) && limit <= 1000 => limit,
        _ => return unprocessable("limit must be a canonical decimal integer in [1, 1000]"),
    };
    let status = value("status")
        .filter(|status| !status.is_empty())
        .map(str::to_owned);
    let seniority_raw = value("seniority").map(str::to_owned);
    let store = state.leads.clone();

    let legacy = page.is_none() || status.is_some() || min_score.is_some();
    if legacy {
        // The old in-memory path: only session, seniority, semantic, status and score filters.
        filters.include_preferred = false;
        filters.company.clear();
        filters.sponsorship = None;
        filters.employment_types.clear();
        filters.posted_within_hours = None;
        let lowered = seniority_raw.as_deref().unwrap_or("").trim().to_lowercase();
        filters.seniority = if beginner_only || lowered == "beginner" {
            Some(SeniorityFilter::Beginner)
        } else {
            Seniority::parse(&lowered).map(SeniorityFilter::Band)
        };
        let result = respond::blocking("list leads", move || {
            let leads = store.filtered_leads(&filters)?;
            Ok::<_, LeadError>(leads)
        })
        .await;
        let leads = match result {
            Ok(leads) => leads,
            Err(response) => return response,
        };
        let mut leads: Vec<Value> = leads
            .into_iter()
            .filter(|lead| {
                status.as_deref().is_none_or(|wanted| {
                    lead.get("status").and_then(Value::as_str).unwrap_or("") == wanted
                })
            })
            .filter(|lead| min_score.is_none_or(|minimum| score_of(lead) >= minimum))
            .map(|mut lead| {
                annotate_seniority(&mut lead);
                Value::Object(lead)
            })
            .collect();
        let Some(page) = page else {
            return Json(Value::Array(leads)).into_response();
        };
        let total = leads.len();
        let start = (page - 1).saturating_mul(limit).min(total);
        let items: Vec<Value> = leads.drain(start..).take(limit).collect();
        return Json(json!({ "items": items, "total": total, "page": page, "limit": limit, "pages": total.div_ceil(limit) }))
            .into_response();
    }
    let page = page.unwrap_or(1);
    filters.seniority = match strict_seniority(seniority_raw.as_deref(), beginner_only) {
        Ok(seniority) => seniority,
        Err(response) => return response,
    };
    filters.q = value("q").unwrap_or("").to_owned();
    filters.platform = value("platform").unwrap_or("").to_owned();
    filters.location = value("location").unwrap_or("").to_owned();
    let view = LeadView::parse(&view).unwrap_or_default();
    let sort = LeadSort::parse(&sort).unwrap_or_default();
    let profile = state.profile.clone();
    let result = tokio::task::spawn_blocking(move || {
        if match_profile {
            filters.profile_skills = Some(profile_skills(&profile));
        }
        store.page(&filters, view, sort, page, limit, collapse_similar)
    })
    .await;
    match result {
        Ok(Ok(mut body)) => {
            if let Some(items) = body["items"].as_array_mut() {
                for item in items {
                    if let Value::Object(lead) = item {
                        annotate_seniority(lead);
                    }
                }
            }
            Json(body).into_response()
        }
        Ok(Err(error)) => lead_error(error, "page leads"),
        Err(error) => respond::internal_error("page leads", &error),
    }
}

fn profile_skills(profile: &ProfileStore) -> Vec<String> {
    let profile = profile.get_profile().unwrap_or_default();
    let mut seen = HashSet::new();
    profile
        .get("skills")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|skill| {
            skill.as_str().or_else(|| {
                skill
                    .get("n")
                    .and_then(Value::as_str)
                    .filter(|name| !name.is_empty())
                    .or_else(|| skill.get("name").and_then(Value::as_str))
            })
        })
        .map(normalize_skill)
        .filter(|name| !name.is_empty() && seen.insert(name.clone()))
        .take(400)
        .collect()
}

fn score_of(lead: &Map<String, Value>) -> i64 {
    match lead.get("score") {
        Some(Value::Number(number)) => number
            .as_i64()
            .unwrap_or_else(|| number.as_f64().unwrap_or(0.0) as i64),
        Some(Value::String(text)) => text.trim().parse().unwrap_or(0),
        _ => 0,
    }
}

/// `GET /api/v1/leads/summary`: bucket counts and facets for the filters.
pub async fn summary(State(state): State<Arc<AppState>>, request: Request) -> Response {
    let pairs = validation::query_pairs(request.uri().query());
    let mut reader = QueryReader::default();
    let include_preferred = reader.boolean(&pairs, "include_preferred", false);
    let match_profile = reader.boolean(&pairs, "match_profile", false);
    if let Err(response) = reader.finish() {
        return response;
    }
    let mut filters = match shared_filters(&pairs) {
        Ok(filters) => filters,
        Err(response) => return response,
    };
    filters.include_preferred = include_preferred;
    let value = |name: &str| validation::query_value(&pairs, name);
    filters.seniority = match strict_seniority(value("seniority"), false) {
        Ok(seniority) => seniority,
        Err(response) => return response,
    };
    filters.q = value("q").unwrap_or("").to_owned();
    filters.platform = value("platform").unwrap_or("").to_owned();
    filters.location = value("location").unwrap_or("").to_owned();
    let store = state.leads.clone();
    let profile = state.profile.clone();
    match tokio::task::spawn_blocking(move || {
        if match_profile {
            filters.profile_skills = Some(profile_skills(&profile));
        }
        store.summary(&filters)
    })
    .await
    {
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => lead_error(error, "lead summary"),
        Err(error) => respond::internal_error("lead summary", &error),
    }
}

/// `GET /api/v1/leads/{job_id}`.
pub async fn detail(State(state): State<Arc<AppState>>, Path(job_id): Path<String>) -> Response {
    if let Err(response) = check_job_id(&job_id) {
        return response;
    }
    let store = state.leads.clone();
    match respond::blocking("read lead", move || store.by_id(&job_id)).await {
        Ok(Some(mut lead)) => {
            let kind = lead.get("kind").and_then(Value::as_str).unwrap_or("");
            if kind.is_empty() || kind == "job" {
                annotate_seniority(&mut lead);
            }
            Json(Value::Object(lead)).into_response()
        }
        Ok(None) => respond::detail(StatusCode::NOT_FOUND, "Lead not found"),
        Err(response) => response,
    }
}

/// `DELETE /api/v1/leads/{job_id}`.
pub async fn remove(State(state): State<Arc<AppState>>, Path(job_id): Path<String>) -> Response {
    if let Err(response) = check_job_id(&job_id) {
        return response;
    }
    let store = state.leads.clone();
    match tokio::task::spawn_blocking(move || store.delete(&job_id)).await {
        Ok(Ok(())) => Json(json!({ "ok": true })).into_response(),
        Ok(Err(error)) => lead_error(error, "delete lead"),
        Err(error) => respond::internal_error("delete lead", &error),
    }
}

/// `PUT /api/v1/leads/{job_id}/status` with `{"status": ...}`.
pub async fn set_status(
    State(state): State<Arc<AppState>>,
    Path(job_id): Path<String>,
    body: Bytes,
) -> Response {
    if let Err(response) = check_job_id(&job_id) {
        return response;
    }
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let status = reader.one_of("status", &STATUSES, true);
    reader.forbid_extra(&["status"]);
    if let Err(response) = reader.finish() {
        return response;
    }
    let Some(status) = status else {
        return unprocessable("status is required");
    };
    let store = state.leads.clone();
    let (id, new_status) = (job_id.clone(), status.clone());
    match tokio::task::spawn_blocking(move || store.update_status(&id, &new_status)).await {
        Ok(Ok(())) => {}
        Ok(Err(LeadError::Invalid(message))) => return bad_request(&message),
        Ok(Err(error)) => return lead_error(error, "update lead status"),
        Err(error) => return respond::internal_error("update lead status", &error),
    }
    state
        .hub
        .broadcast(
            json!({ "type": "LEAD_UPDATED", "data": { "job_id": job_id, "status": status } }),
        )
        .await;
    Json(json!({ "ok": true })).into_response()
}

/// `PUT /api/v1/leads/{job_id}/feedback` saves feedback and queues a ranking pass.
pub async fn feedback(
    State(state): State<Arc<AppState>>,
    Path(job_id): Path<String>,
    ModelBody(body): ModelBody,
) -> Result<Json<Value>, Response> {
    const FEEDBACK: &[&str] = &[
        "good",
        "trash",
        "too_generic",
        "not_ai",
        "already_contacted",
        "relevant",
        "not_relevant",
        "duplicate",
        "low_quality",
        "incorrect_category",
    ];
    let mut reader = ModelReader::new(&body);
    let feedback = reader.one_of("feedback", FEEDBACK, true);
    let note = reader.string("note", Some(1000), false).unwrap_or_default();
    reader.forbid_extra(&["feedback", "note"]);
    reader.finish()?;
    check_job_id(&job_id)?;
    let feedback = feedback.expect("required feedback was validated");
    let store = state.leads.clone();
    let saved =
        tokio::task::spawn_blocking(move || store.save_feedback(&job_id, &feedback, &note)).await;
    let lead = match saved {
        Ok(Ok(Some(lead))) => Value::Object(lead),
        Ok(Ok(None)) => return Err(respond::detail(StatusCode::NOT_FOUND, "Lead not found")),
        Ok(Err(LeadError::Invalid(message))) => return Err(bad_request(&message)),
        Ok(Err(error)) => return Err(lead_error(error, "save lead feedback")),
        Err(error) => return Err(respond::internal_error("save lead feedback", &error)),
    };
    state
        .hub
        .broadcast(json!({"type": "LEAD_UPDATED", "data": lead}))
        .await;
    // The retired scraper_api_v1 corpus no longer consumes feedback.
    crate::feedback_relearn::schedule(state);
    Ok(Json(lead))
}

/// `PUT /api/v1/leads/{job_id}/followup` with `{"days": 1..60}` (default 5).
pub async fn set_followup(
    State(state): State<Arc<AppState>>,
    Path(job_id): Path<String>,
    body: Bytes,
) -> Response {
    if let Err(response) = check_job_id(&job_id) {
        return response;
    }
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let days = reader.integer("days", Some(1), Some(60)).unwrap_or(5);
    reader.forbid_extra(&["days"]);
    if let Err(response) = reader.finish() {
        return response;
    }
    let days = if days == 0 { 5 } else { days.clamp(1, 60) };
    let now = UtcTime::now();
    let due = now.plus_seconds(days * 86_400);
    let store = state.leads.clone();
    let (now, due) = (now.to_iso_micros(), due.to_iso_micros());
    match respond::blocking("set follow-up", move || {
        store.set_followup(&job_id, &due, &now)
    })
    .await
    {
        Ok(Some(lead)) => {
            let lead = Value::Object(lead);
            state
                .hub
                .broadcast(json!({ "type": "LEAD_UPDATED", "data": lead.clone() }))
                .await;
            Json(lead).into_response()
        }
        Ok(None) => respond::detail(StatusCode::NOT_FOUND, "Lead not found"),
        Err(response) => response,
    }
}

/// `POST /api/v1/leads/cleanup`: discards rows that are clearly not job postings.
pub async fn cleanup(State(state): State<Arc<AppState>>) -> Response {
    let store = state.leads.clone();
    match respond::blocking("clean up leads", move || store.cleanup(1000, false)).await {
        Ok(body) => Json(body).into_response(),
        Err(response) => response,
    }
}

/// `GET /api/v1/leads/{job_id}/pdf?kind=resume|cover&version=N`: the generated PDF.
pub async fn pdf(
    State(state): State<Arc<AppState>>,
    Path(job_id): Path<String>,
    RawQuery(query): RawQuery,
) -> Response {
    if let Err(response) = check_job_id(&job_id) {
        return response;
    }
    let pairs = validation::query_pairs(query.as_deref());
    let mut reader = QueryReader::default();
    let version = reader.integer(&pairs, "version");
    if let Err(response) = reader.finish() {
        return response;
    }
    let cover = matches!(
        validation::query_value(&pairs, "kind").unwrap_or("resume"),
        "cover" | "cover_letter" | "cover-letter"
    );
    let store = state.leads.clone();
    let id = job_id.clone();
    let lead = match respond::blocking("read lead", move || store.by_id(&id)).await {
        Ok(Some(lead)) => lead,
        Ok(None) => return respond::detail(StatusCode::NOT_FOUND, "Lead not found"),
        Err(response) => return response,
    };
    let field = |key: &str| {
        lead.get(key)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned()
    };
    let missing = if cover {
        "Cover letter not generated yet"
    } else {
        "Resume not generated yet"
    };
    let (path, filename) = match version {
        Some(version) => {
            let anchor = ["resume_asset", "asset", "cover_letter_asset"]
                .iter()
                .map(|key| field(key))
                .find(|value| !value.is_empty());
            let directory = anchor
                .and_then(|path| FilePath::new(&path).parent().map(FilePath::to_path_buf))
                .unwrap_or_else(|| state.paths.assets_dir());
            let stem = agent_hire_generation::safe_job_id(&job_id);
            let name = if cover {
                format!("{stem}_cl_v{version}.pdf")
            } else {
                format!("{stem}_v{version}.pdf")
            };
            (directory.join(&name), Some(name))
        }
        None if cover => (
            PathBuf::from(field("cover_letter_asset")),
            Some(format!("{job_id}_cover_letter.pdf")),
        ),
        None => {
            let resume = field("resume_asset");
            let path = if resume.is_empty() {
                field("asset")
            } else {
                resume
            };
            (PathBuf::from(path), Some(format!("{job_id}_resume.pdf")))
        }
    };
    if path.as_os_str().is_empty() || !path.is_file() {
        return respond::detail(StatusCode::NOT_FOUND, missing);
    }
    match tokio::fs::read(&path).await {
        Ok(bytes) => pdf_response(bytes, filename.as_deref()),
        Err(_) => respond::detail(StatusCode::NOT_FOUND, missing),
    }
}

fn pdf_response(bytes: Vec<u8>, filename: Option<&str>) -> Response {
    let mut response = Response::new(Body::from(bytes));
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/pdf"),
    );
    if let Some(name) = filename
        && let Ok(value) = HeaderValue::from_str(&format!("attachment; filename=\"{name}\""))
    {
        headers.insert(header::CONTENT_DISPOSITION, value);
    }
    response
}
