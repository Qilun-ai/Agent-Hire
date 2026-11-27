//! The job index routes served in Rust: search, one job, departments, companies, adding jobs by
//! hand, promoting jobs into the pipeline, deleting and resetting, and scrape runs.

use std::collections::HashSet;
use std::env;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

use agent_hire_jobindex::{self as jobindex, JobIndex, SearchParams};
use agent_hire_store::{LeadStore, SettingsStore, annotate_seniority};
use axum::Json;
use axum::body::Bytes;
use axum::extract::{Path, RawQuery, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{Map, Value, json};

use crate::app_state::AppState;
use crate::cart_routes;
use crate::profile_source;
use crate::respond;
use crate::validation::{self, ModelReader};

fn unprocessable(detail: &str) -> Response {
    respond::detail(StatusCode::UNPROCESSABLE_ENTITY, detail)
}

fn scraper_dir(state: &AppState) -> std::path::PathBuf {
    jobindex::scraper_dir(state.repo_root.as_deref())
}

/// `POST /api/v1/jobindex/search`: a filtered, skill-ranked page of jobs.
pub async fn search(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let role = reader.string("role", Some(200), false).unwrap_or_default();
    let keywords = reader.string_list("keywords", Some(20)).unwrap_or_default();
    let since_hours = reader.number("since_hours", Some(1.0), Some(24.0 * 365.0));
    let levels = reader.string_list("levels", Some(6)).unwrap_or_default();
    let departments = reader
        .string_list("departments", Some(40))
        .unwrap_or_default();
    let locations = reader
        .string_list("locations", Some(10))
        .unwrap_or_default();
    let companies = reader
        .string_list("companies", Some(50))
        .unwrap_or_default();
    let company_slugs = reader
        .string_list("company_slugs", Some(300))
        .unwrap_or_default();
    let title_keywords = reader
        .string_list("title_keywords", Some(20))
        .unwrap_or_default();
    let remote = reader.optional_boolean("remote");
    let match_profile = reader.boolean("match_profile").unwrap_or(false);
    let sort = reader
        .patterned(
            "sort",
            "^(match|recent|level)$",
            |s| matches!(s, "match" | "recent" | "level"),
            false,
        )
        .unwrap_or_else(|| "match".to_owned());
    let page = reader.integer("page", Some(1), Some(10_000)).unwrap_or(1);
    let page_size = reader
        .integer("page_size", Some(1), Some(jobindex::PAGE_MAX))
        .unwrap_or(15);
    let _ = reader.boolean("use_decision_model");
    reader.forbid_extra(&[
        "role",
        "keywords",
        "since_hours",
        "levels",
        "departments",
        "locations",
        "companies",
        "company_slugs",
        "title_keywords",
        "remote",
        "match_profile",
        "sort",
        "page",
        "page_size",
        "use_decision_model",
    ]);
    if let Err(response) = reader.finish() {
        return response;
    }
    let params = SearchParams {
        role,
        keywords,
        since_hours,
        levels,
        departments,
        locations,
        companies,
        company_slugs,
        title_keywords,
        remote,
        match_profile,
        sort,
        page,
        page_size,
    };
    let needs_profile = params.match_profile || params.sort == "match";
    let profile = if needs_profile {
        profile_source::profile(&state).await
    } else {
        Map::new()
    };
    let index = state.jobs.clone();
    match respond::blocking("job search", move || {
        jobindex::search(&index, &params, &profile)
    })
    .await
    {
        Ok(result) => Json(result).into_response(),
        Err(response) => response,
    }
}

/// `GET /api/v1/jobindex/jobs/{ref}`: one job with its full description.
pub async fn get_job(
    State(state): State<Arc<AppState>>,
    Path(reference): Path<String>,
) -> Response {
    let index = state.jobs.clone();
    let result = respond::blocking("read job", move || {
        let connection = index.connect()?;
        let Some(mut job) = jobindex::get_job(&connection, &reference)? else {
            return Ok::<_, rusqlite::Error>(None);
        };
        let id = job
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let verdicts = jobindex::decisions_for(
            &connection,
            jobindex::DEPARTMENT_ROLE_KEY,
            std::slice::from_ref(&id),
        )?;
        let department = verdicts.get(&id).and_then(|verdict| {
            let code = verdict["level"].as_str()?;
            let p = verdict["role_p"].as_f64().unwrap_or(0.0);
            let mut described = jobindex::describe_department(code)?;
            described["p"] = json!(p);
            Some(described)
        });
        job.insert(
            "department_model".to_owned(),
            department.unwrap_or(Value::Null),
        );
        for key in ["skills", "locations"] {
            let parsed = job
                .get(key)
                .and_then(Value::as_str)
                .and_then(|text| serde_json::from_str::<Value>(text).ok());
            job.insert(key.to_owned(), parsed.unwrap_or_else(|| json!([])));
        }
        Ok(Some(job))
    })
    .await;
    match result {
        Ok(Some(job)) => Json(Value::Object(job)).into_response(),
        Ok(None) => respond::detail(
            StatusCode::NOT_FOUND,
            "No job with that reference in the index",
        ),
        Err(response) => response,
    }
}

static DEPARTMENTS_CACHE: LazyLock<Mutex<Option<(Instant, Value)>>> =
    LazyLock::new(|| Mutex::new(None));

fn department_counts(index: &JobIndex) -> rusqlite::Result<Value> {
    if let Ok(cache) = DEPARTMENTS_CACHE.lock()
        && let Some((at, value)) = cache.as_ref()
        && at.elapsed() < Duration::from_secs(60)
    {
        return Ok(value.clone());
    }
    let connection = index.connect()?;
    let mut statement = connection.prepare(
        "SELECT d.level AS code, COUNT(*) AS n FROM jobs j JOIN decisions d ON d.job_id = j.id AND d.role_key = ?1 \
         WHERE j.active = 1 AND d.role_p >= ?2 GROUP BY d.level",
    )?;
    let counts: std::collections::HashMap<String, i64> = statement
        .query_map(
            rusqlite::params![
                jobindex::DEPARTMENT_ROLE_KEY,
                jobindex::DEPARTMENT_MIN_CONFIDENCE
            ],
            |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?.unwrap_or_default(),
                    row.get::<_, i64>(1)?,
                ))
            },
        )?
        .filter_map(Result::ok)
        .collect();
    let departments: Vec<Value> = jobindex::TAXONOMY
        .iter()
        .map(|(name, group)| {
            let subs: Vec<Value> = group
                .iter()
                .map(|(code, sub)| json!({"code": code, "name": sub, "count": counts.get(*code).copied().unwrap_or(0)}))
                .collect();
            let total: i64 = group.iter().map(|(code, _)| counts.get(*code).copied().unwrap_or(0)).sum();
            json!({"name": name, "count": total, "subdepartments": subs})
        })
        .collect();
    let value = json!({
        "model": jobindex::model_available(&jobindex::ModelDirectories::from_jobs_dir(index.dir()).department),
        "departments": departments,
    });
    if let Ok(mut cache) = DEPARTMENTS_CACHE.lock() {
        *cache = Some((Instant::now(), value.clone()));
    }
    Ok(value)
}

/// `GET /api/v1/jobindex/departments`: the taxonomy with open-job counts.
pub async fn departments(State(state): State<Arc<AppState>>) -> Response {
    let index = state.jobs.clone();
    match respond::blocking("department counts", move || department_counts(&index)).await {
        Ok(value) => Json(value).into_response(),
        Err(response) => response,
    }
}

/// `GET /api/v1/jobindex/companies`: every company the scraper covers, by scrape speed.
pub async fn companies(State(state): State<Arc<AppState>>) -> Response {
    let index = state.jobs.clone();
    let scraper = scraper_dir(&state);
    let result = respond::blocking("companies", move || {
        let connection = index.connect()?;
        let mut statement = connection.prepare("SELECT * FROM companies")?;
        let indexed: std::collections::HashMap<String, Map<String, Value>> = statement
            .query_map([], jobindex::row_object)?
            .filter_map(Result::ok)
            .map(|row| {
                (
                    row.get("slug")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                    row,
                )
            })
            .collect();
        let active = jobindex::active_run(&connection)?;
        drop(statement);
        Ok::<_, rusqlite::Error>(company_listing(&index, &scraper, &indexed, active.as_ref()))
    })
    .await;
    match result {
        Ok(value) => Json(value).into_response(),
        Err(response) => response,
    }
}

fn company_listing(
    index: &JobIndex,
    scraper: &std::path::Path,
    indexed: &std::collections::HashMap<String, Map<String, Value>>,
    active: Option<&Map<String, Value>>,
) -> Value {
    let speeds = jobindex::company_speeds(index, scraper);
    let list = |row: Option<&Map<String, Value>>, key: &str| -> HashSet<String> {
        row.and_then(|r| r.get(key))
            .and_then(Value::as_str)
            .and_then(|t| serde_json::from_str::<Vec<String>>(t).ok())
            .unwrap_or_default()
            .into_iter()
            .collect()
    };
    let planned = list(active, "companies");
    let done = list(active, "imported");
    let mut rows: Vec<Map<String, Value>> = Vec::new();
    for row in jobindex::catalog(scraper) {
        let slug = row
            .get("slug")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let info = indexed.get(&slug);
        let speed = speeds.get(&slug).cloned().unwrap_or_else(|| json!({}));
        let state = planned.contains(&slug).then(|| {
            if done.contains(&slug) {
                "done"
            } else {
                "queued"
            }
        });
        let get = |key: &str| row.get(key).cloned().unwrap_or(Value::Null);
        let mut out = Map::new();
        out.insert("company".to_owned(), get("company"));
        out.insert("slug".to_owned(), json!(slug));
        out.insert("status".to_owned(), get("status"));
        out.insert("careers_url".to_owned(), get("careers_url"));
        out.insert(
            "active_jobs".to_owned(),
            info.and_then(|i| i.get("active_jobs"))
                .cloned()
                .unwrap_or(json!(0)),
        );
        out.insert(
            "last_import_at".to_owned(),
            info.and_then(|i| i.get("last_import_at"))
                .cloned()
                .unwrap_or(Value::Null),
        );
        out.insert(
            "est_seconds".to_owned(),
            speed.get("seconds").cloned().unwrap_or(Value::Null),
        );
        out.insert(
            "est_basis".to_owned(),
            speed.get("basis").cloned().unwrap_or(Value::Null),
        );
        out.insert(
            "category".to_owned(),
            speed.get("category").cloned().unwrap_or(json!("unknown")),
        );
        out.insert("run_state".to_owned(), json!(state));
        out.insert("unavailable_group".to_owned(), get("unavailable_group"));
        out.insert("unavailable_reason".to_owned(), get("unavailable_reason"));
        out.insert("review_note".to_owned(), get("review_note"));
        rows.push(out);
    }
    let verified: Vec<&Map<String, Value>> =
        rows.iter().filter(|r| r["status"] == "verified").collect();
    let workers = env::var("AGENT_HIRE_SCRAPE_CONCURRENCY")
        .ok()
        .and_then(|v| v.trim().parse::<i64>().ok())
        .unwrap_or(4)
        .max(1);
    let mut categories = Vec::new();
    let mut specs: Vec<(&str, &str, &str)> = jobindex::SPEED_CATEGORIES
        .iter()
        .map(|(k, l, r, _)| (*k, *l, *r))
        .collect();
    specs.push(("unknown", "Not timed yet", "unknown"));
    for (key, label, range) in specs {
        let members: Vec<&&Map<String, Value>> =
            verified.iter().filter(|r| r["category"] == key).collect();
        if members.is_empty() && key == "unknown" {
            continue;
        }
        let seconds = |r: &Map<String, Value>| r["est_seconds"].as_f64().unwrap_or(0.0);
        let total: f64 = members.iter().map(|r| seconds(r)).sum();
        let longest = members.iter().map(|r| seconds(r)).fold(0.0, f64::max);
        let jobs: i64 = members
            .iter()
            .map(|r| r["active_jobs"].as_i64().unwrap_or(0))
            .sum();
        let wall = (key != "unknown")
            .then(|| python_round(total / workers as f64).max(python_round(longest)));
        categories.push(json!({
            "key": key, "label": label, "range": range, "count": members.len(),
            "total_seconds": python_round(total), "longest_seconds": python_round(longest), "jobs": jobs,
            "est_wall_seconds": wall,
        }));
    }
    json!({
        "companies": rows,
        "categories": categories,
        "concurrency": workers,
        "active_run": active.map_or(Value::Null, |run| jobindex::public_run(Some(run))),
    })
}

/// Python's `round()` to an integer (halves go to the even neighbour).
fn python_round(value: f64) -> i64 {
    let rounded = value.round();
    if (value - value.trunc()).abs() == 0.5 && (rounded as i64) % 2 != 0 {
        (rounded - value.signum()) as i64
    } else {
        rounded as i64
    }
}

/// The pipeline lead for an index job.
fn lead_from_job(job: &Map<String, Value>, note: &str) -> Map<String, Value> {
    let text = |key: &str| {
        job.get(key)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned()
    };
    let skills: Vec<Value> =
        serde_json::from_str::<Vec<Value>>(&text("skills")).unwrap_or_default();
    let url = {
        let apply = text("apply_url");
        if apply.is_empty() { text("url") } else { apply }
    };
    let date_posted = job
        .get("date_posted")
        .filter(|v| !v.is_null() && v.as_str() != Some(""))
        .or_else(|| job.get("first_seen_at"))
        .cloned()
        .unwrap_or(Value::Null);
    let published = job
        .get("date_posted")
        .is_some_and(|v| !v.is_null() && v.as_str() != Some(""));
    let mut meta = Map::new();
    meta.insert("source".to_owned(), json!("agent"));
    meta.insert(
        "index_job_id".to_owned(),
        job.get("id").cloned().unwrap_or(Value::Null),
    );
    meta.insert(
        "company_slug".to_owned(),
        job.get("company_slug").cloned().unwrap_or(Value::Null),
    );
    meta.insert("date_posted".to_owned(), date_posted);
    meta.insert(
        "posted_basis".to_owned(),
        json!(if published { "published" } else { "first_seen" }),
    );
    meta.insert("full_description".to_owned(), json!(text("description")));
    meta.insert("apply_url".to_owned(), json!(url));
    meta.insert(
        "experience_level".to_owned(),
        job.get("rule_level").cloned().unwrap_or(Value::Null),
    );
    meta.insert(
        "min_years_experience".to_owned(),
        job.get("min_years").cloned().unwrap_or(Value::Null),
    );
    meta.insert(
        "workplace_type".to_owned(),
        job.get("workplace_type").cloned().unwrap_or(Value::Null),
    );
    meta.insert("agent_note".to_owned(), json!(note));
    meta.insert(
        "added_at".to_owned(),
        json!(agent_hire_store::UtcTime::now().to_iso_micros()),
    );
    let mut lead = Map::new();
    lead.insert(
        "job_id".to_owned(),
        json!(jobindex::lead_id_for(&text("id"))),
    );
    lead.insert(
        "title".to_owned(),
        json!(text("title").chars().take(220).collect::<String>()),
    );
    lead.insert(
        "company".to_owned(),
        json!(text("company").chars().take(200).collect::<String>()),
    );
    lead.insert("url".to_owned(), json!(url));
    lead.insert("platform".to_owned(), json!("careers"));
    lead.insert("description".to_owned(), json!(text("description")));
    lead.insert(
        "location".to_owned(),
        json!(text("location_text").chars().take(200).collect::<String>()),
    );
    lead.insert("kind".to_owned(), json!("job"));
    lead.insert("status".to_owned(), json!("discovered"));
    lead.insert(
        "tech_stack".to_owned(),
        Value::Array(skills.into_iter().take(40).collect()),
    );
    lead.insert("source_meta".to_owned(), Value::Object(meta));
    lead
}

/// Copies jobs into the pipeline and optionally the cart; returns what happened to each.
fn promote_jobs(
    index: &JobIndex,
    leads: &LeadStore,
    settings: &SettingsStore,
    refs: &[String],
    add_to_cart: bool,
    note: &str,
) -> Result<Value, String> {
    let connection = index.connect().map_err(|e| e.to_string())?;
    let mut added: Vec<String> = Vec::new();
    let mut existing: Vec<String> = Vec::new();
    let mut missing: Vec<String> = Vec::new();
    let mut seen: Vec<&String> = Vec::new();
    for reference in refs {
        if seen.contains(&reference) {
            continue;
        }
        seen.push(reference);
        let Some(job) = jobindex::get_job(&connection, reference).map_err(|e| e.to_string())?
        else {
            missing.push(reference.clone());
            continue;
        };
        let mut lead_id = job
            .get("lead_id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .map_or_else(
                || jobindex::lead_id_for(job["id"].as_str().unwrap_or("")),
                str::to_owned,
            );
        if leads.by_id(&lead_id).map_err(|e| e.to_string())?.is_some() {
            existing.push(lead_id.clone());
        } else {
            let mut lead = lead_from_job(&job, note);
            annotate_seniority(&mut lead);
            leads.save(&lead).map_err(|e| e.to_string())?;
            lead_id = lead["job_id"].as_str().unwrap_or("").to_owned();
            added.push(lead_id.clone());
        }
        connection
            .execute(
                "UPDATE jobs SET lead_id=?1 WHERE id=?2",
                rusqlite::params![lead_id, reference],
            )
            .map_err(|e| e.to_string())?;
    }
    let mut cart_size = Value::Null;
    if add_to_cart && (!added.is_empty() || !existing.is_empty()) {
        let mut cart = cart_routes::read_cart(settings);
        for id in added.iter().chain(existing.iter()) {
            if !cart.contains(id) {
                cart.push(id.clone());
            }
        }
        let stored = cart_routes::write_cart(settings, &cart).map_err(|e| e.to_string())?;
        if !stored.is_empty() {
            cart_size = json!(stored.len());
        }
    }
    Ok(
        json!({ "added": added, "already_in_pipeline": existing, "missing": missing, "cart_size": cart_size }),
    )
}

async fn promote_and_announce(
    state: &AppState,
    refs: Vec<String>,
    add_to_cart: bool,
    note: String,
) -> Response {
    let (index, leads, settings) = (
        state.jobs.clone(),
        state.leads.clone(),
        state.settings.clone(),
    );
    let result = respond::blocking("promote jobs", move || {
        promote_jobs(&index, &leads, &settings, &refs, add_to_cart, &note)
    })
    .await;
    match result {
        Ok(value) => {
            // Linking a job that is already in the pipeline leaves the encoder workload intact.
            if value["added"]
                .as_array()
                .is_some_and(|added| !added.is_empty())
            {
                state.notify_job_index();
            }
            state
                .hub
                .broadcast(
                    json!({"type": "LEADS_CHANGED", "data": {"added": value["added"].clone()}}),
                )
                .await;
            Json(value).into_response()
        }
        Err(response) => response,
    }
}

/// `POST /api/v1/jobindex/promote`.
pub async fn promote(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let refs = reader.required_list("job_refs", 1, 100);
    let add_to_cart = reader.boolean("add_to_cart").unwrap_or(true);
    let note = reader.string("note", Some(500), false).unwrap_or_default();
    reader.forbid_extra(&["job_refs", "add_to_cart", "note"]);
    if let Err(response) = reader.finish() {
        return response;
    }
    promote_and_announce(&state, refs.unwrap_or_default(), add_to_cart, note).await
}

/// `POST /api/v1/jobindex/custom`: adds a job by hand, optionally straight into the pipeline.
pub async fn add_custom(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let title = reader.bounded_string("title", 2, 220, true);
    let company = reader.bounded_string("company", 1, 200, true);
    let description = reader
        .string("description", Some(60_000), false)
        .unwrap_or_default();
    let url = reader.string("url", Some(2000), false).unwrap_or_default();
    let location = reader
        .string("location", Some(200), false)
        .unwrap_or_default();
    let workplace_type = reader
        .string("workplace_type", Some(60), false)
        .unwrap_or_default();
    let add_to_pipeline = reader.boolean("add_to_pipeline").unwrap_or(true);
    reader.forbid_extra(&[
        "title",
        "company",
        "description",
        "url",
        "location",
        "workplace_type",
        "add_to_pipeline",
    ]);
    if let Err(response) = reader.finish() {
        return response;
    }
    let (title, company) = (title.unwrap_or_default(), company.unwrap_or_default());
    let index = state.jobs.clone();
    let added = respond::blocking("add custom job", move || {
        let connection = index.connect()?;
        jobindex::add_custom_job(
            &connection,
            title.trim(),
            company.trim(),
            description.trim(),
            url.trim(),
            location.trim(),
            workplace_type.trim(),
        )
    })
    .await;
    let reference = match added {
        Ok(reference) => reference,
        Err(response) => return response,
    };
    state.notify_job_index();
    let mut result = json!({ "job_ref": reference, "added_to_index": true });
    if add_to_pipeline {
        let promoted =
            promote_and_announce(&state, vec![reference], true, "added by hand".to_owned()).await;
        let (parts, body) = promoted.into_parts();
        if !parts.status.is_success() {
            return Response::from_parts(parts, body);
        }
        let bytes = axum::body::to_bytes(body, usize::MAX)
            .await
            .unwrap_or_default();
        result["pipeline"] = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    }
    Json(result).into_response()
}

/// `POST /api/v1/jobindex/delete`: removes jobs from the index, the pipeline, or both.
pub async fn delete(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let refs = reader.required_list("job_refs", 1, 1000);
    let scope = reader
        .patterned(
            "scope",
            "^(index|pipeline|both)$",
            |s| matches!(s, "index" | "pipeline" | "both"),
            false,
        )
        .unwrap_or_else(|| "both".to_owned());
    reader.forbid_extra(&["job_refs", "scope"]);
    if let Err(response) = reader.finish() {
        return response;
    }
    let mut refs_unique: Vec<String> = Vec::new();
    for reference in refs.unwrap_or_default() {
        if !refs_unique.contains(&reference) {
            refs_unique.push(reference);
        }
    }
    let (index, leads, settings) = (
        state.jobs.clone(),
        state.leads.clone(),
        state.settings.clone(),
    );
    let result = respond::blocking("delete jobs", move || {
        let connection = index.connect().map_err(|e| e.to_string())?;
        let mut out = json!({ "index_deleted": 0, "pipeline_deleted": 0, "not_found": [] });
        if scope == "pipeline" || scope == "both" {
            let marks = vec!["?"; refs_unique.len()].join(",");
            let mut statement = connection
                .prepare(&format!(
                    "SELECT id, lead_id FROM jobs WHERE id IN ({marks})"
                ))
                .map_err(|e| e.to_string())?;
            let linked: std::collections::HashMap<String, Option<String>> = statement
                .query_map(rusqlite::params_from_iter(refs_unique.iter()), |row| {
                    Ok((row.get(0)?, row.get(1)?))
                })
                .map_err(|e| e.to_string())?
                .filter_map(Result::ok)
                .collect();
            drop(statement);
            let mut deleted = 0;
            let mut not_found: Vec<String> = Vec::new();
            for reference in &refs_unique {
                let lead_id = linked
                    .get(reference)
                    .cloned()
                    .flatten()
                    .filter(|id| !id.is_empty())
                    .unwrap_or_else(|| reference.clone());
                if leads.by_id(&lead_id).map_err(|e| e.to_string())?.is_some() {
                    leads.delete(&lead_id).map_err(|e| e.to_string())?;
                    deleted += 1;
                } else if scope == "pipeline" {
                    not_found.push(lead_id);
                }
            }
            out["pipeline_deleted"] = json!(deleted);
            out["not_found"] = json!(not_found);
            let cart: Vec<String> = cart_routes::read_cart(&settings)
                .into_iter()
                .filter(|id| !refs_unique.contains(id))
                .collect();
            cart_routes::write_cart(&settings, &cart).map_err(|e| e.to_string())?;
            for reference in &refs_unique {
                connection
                    .execute(
                        "UPDATE jobs SET lead_id=NULL WHERE id=?1 OR lead_id=?1",
                        [reference],
                    )
                    .map_err(|e| e.to_string())?;
            }
        }
        if scope == "index" || scope == "both" {
            out["index_deleted"] =
                json!(jobindex::delete_jobs(&connection, &refs_unique).map_err(|e| e.to_string())?);
        }
        Ok::<_, String>(out)
    })
    .await;
    match result {
        Ok(value) => {
            state.notify_job_index();
            Json(value).into_response()
        }
        Err(response) => response,
    }
}

/// `POST /api/v1/jobindex/reset`: empties the pipeline and/or the index (needs `confirm: "RESET"`).
pub async fn reset(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let scope = reader.patterned(
        "scope",
        "^(pipeline|index|all)$",
        |s| matches!(s, "pipeline" | "index" | "all"),
        true,
    );
    let confirm = reader.string("confirm", None, true);
    reader.forbid_extra(&["scope", "confirm"]);
    if let Err(response) = reader.finish() {
        return response;
    }
    let (scope, confirm) = (scope.unwrap_or_default(), confirm.unwrap_or_default());
    if confirm != "RESET" {
        return unprocessable(
            "Resetting is irreversible: send confirm='RESET' after the user has agreed",
        );
    }
    let (index, leads, settings) = (
        state.jobs.clone(),
        state.leads.clone(),
        state.settings.clone(),
    );
    let result = tokio::task::spawn_blocking(move || {
        let mut out = Map::new();
        if scope == "pipeline" || scope == "all" {
            let all = leads.all().map_err(|e| (500, e.to_string()))?;
            for lead in &all {
                if let Some(id) = lead.get("job_id").and_then(Value::as_str) {
                    let _ = leads.delete(id);
                }
            }
            cart_routes::write_cart(&settings, &[]).map_err(|e| (500, e.to_string()))?;
            let connection = index.connect().map_err(|e| (500, e.to_string()))?;
            connection
                .execute("UPDATE jobs SET lead_id=NULL WHERE lead_id IS NOT NULL", [])
                .map_err(|e| (500, e.to_string()))?;
            out.insert("pipeline_deleted".to_owned(), json!(all.len()));
        }
        if scope == "index" || scope == "all" {
            let connection = index.connect().map_err(|e| (500, e.to_string()))?;
            if jobindex::active_run(&connection)
                .map_err(|e| (500, e.to_string()))?
                .is_some()
            {
                return Err((
                    409,
                    "A scrape is running; stop it before resetting the job index".to_owned(),
                ));
            }
            out.insert(
                "index_deleted".to_owned(),
                json!(jobindex::reset_index(&connection).map_err(|e| (500, e.to_string()))?),
            );
        }
        Ok(Value::Object(out))
    })
    .await;
    match result {
        Ok(Ok(value)) => {
            state.notify_job_index();
            Json(value).into_response()
        }
        Ok(Err((409, message))) => respond::detail(StatusCode::CONFLICT, &message),
        Ok(Err((_, message))) => respond::internal_error("reset", &message),
        Err(error) => respond::internal_error("reset", &error),
    }
}

fn refresh_hours(settings: &SettingsStore) -> f64 {
    let stored = settings.get("scrape_refresh_hours", "12").ok();
    jobindex::refresh_hours(stored.as_deref())
}

/// `GET /api/v1/jobindex/scrape?run_id=...`.
pub async fn scrape_status(
    State(state): State<Arc<AppState>>,
    RawQuery(query): RawQuery,
) -> Response {
    let pairs = validation::query_pairs(query.as_deref());
    let run_id = validation::query_value(&pairs, "run_id").map(str::to_owned);
    let (index, settings) = (state.jobs.clone(), state.settings.clone());
    match respond::blocking("scrape status", move || {
        jobindex::run_status(&index, run_id.as_deref(), refresh_hours(&settings))
    })
    .await
    {
        Ok(value) => Json(value).into_response(),
        Err(response) => response,
    }
}

/// `POST /api/v1/jobindex/scrape`: starts a background scrape.
pub async fn scrape(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let companies = reader
        .string_list("companies", Some(300))
        .unwrap_or_default();
    let categories = reader
        .string_list("categories", Some(6))
        .unwrap_or_default();
    let concurrency = match body.get("concurrency") {
        None | Some(Value::Null) => None,
        Some(_) => reader.integer("concurrency", Some(1), Some(12)),
    };
    reader.forbid_extra(&["companies", "categories", "concurrency"]);
    if let Err(response) = reader.finish() {
        return response;
    }
    let bad: Vec<&String> = categories
        .iter()
        .filter(|c| !jobindex::SPEED_KEYS.contains(&c.as_str()))
        .collect();
    if !bad.is_empty() {
        let names: Vec<&str> = bad.iter().map(|s| s.as_str()).collect();
        return unprocessable(&format!(
            "Unknown speed categories: {} (use {})",
            names.join(", "),
            jobindex::SPEED_KEYS.join(", ")
        ));
    }
    let index = state.jobs.clone();
    let scraper = scraper_dir(&state);
    match respond::blocking("start scrape", move || {
        jobindex::start_run(
            &index,
            &scraper,
            &companies,
            &categories,
            concurrency,
            "manual",
        )
    })
    .await
    {
        Ok(value) => {
            state.notify_job_index();
            Json(value).into_response()
        }
        Err(response) => response,
    }
}

/// `POST /api/v1/jobindex/scrape/stop` with `{"run_id": optional}`.
pub async fn stop(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let run_id = reader.optional_string("run_id");
    reader.forbid_extra(&["run_id"]);
    if let Err(response) = reader.finish() {
        return response;
    }
    let index = state.jobs.clone();
    match respond::blocking("stop scrape", move || {
        jobindex::stop_run(&index, run_id.as_deref())
    })
    .await
    {
        Ok(value) => Json(value).into_response(),
        Err(response) => response,
    }
}
