//! The apply cart shared by the website and the extension, and the extension's job list.
//!
//! The cart lives in the settings table (`apply_cart`) as a JSON list of job ids, so the website
//! and the extension (a different origin) see the same jobs.

use std::path::Path;
use std::sync::Arc;

use agent_hire_store::{LeadStore, SettingsStore, python_json_dumps};
use axum::Json;
use axum::body::Bytes;
use axum::extract::{RawQuery, State};
use axum::response::{IntoResponse, Response};
use serde_json::{Map, Value, json};

use crate::app_state::AppState;
use crate::respond;
use crate::validation::{self, ModelReader, QueryReader};

const CART_SETTING: &str = "apply_cart";
const MAX_CART: usize = 200;
const DONE_STATUSES: [&str; 5] = [
    "applied",
    "interviewing",
    "accepted",
    "rejected",
    "discarded",
];

/// The job ids in the cart, in the user's order.
pub fn read_cart(settings: &SettingsStore) -> Vec<String> {
    let raw = settings.get(CART_SETTING, "[]").unwrap_or_default();
    let raw = if raw.is_empty() { "[]".to_owned() } else { raw };
    let Ok(Value::Array(items)) = serde_json::from_str::<Value>(&raw) else {
        return Vec::new();
    };
    let mut out: Vec<String> = Vec::new();
    for item in items {
        if let Some(id) = item.as_str().filter(|id| !id.is_empty())
            && !out.iter().any(|seen| seen == id)
        {
            out.push(id.to_owned());
        }
    }
    out.truncate(MAX_CART);
    out
}

/// Replaces the cart; returns what was stored (trimmed, de-duplicated, at most 200).
pub fn write_cart(
    settings: &SettingsStore,
    job_ids: &[String],
) -> Result<Vec<String>, agent_hire_store::SettingsError> {
    let mut cleaned: Vec<String> = Vec::new();
    for id in job_ids {
        let id = id.trim();
        if !id.is_empty() && !cleaned.iter().any(|seen| seen == id) {
            cleaned.push(id.to_owned());
        }
    }
    cleaned.truncate(MAX_CART);
    let stored = python_json_dumps(&json!(cleaned), true);
    settings.save(&[(CART_SETTING.to_owned(), stored)])?;
    Ok(cleaned)
}

/// Drops one job from the cart (when it is marked applied). Never fails the caller.
pub fn remove_from_cart(settings: &SettingsStore, job_id: &str) {
    let current = read_cart(settings);
    if current.iter().any(|id| id == job_id) {
        let rest: Vec<String> = current.into_iter().filter(|id| id != job_id).collect();
        if let Err(error) = write_cart(settings, &rest) {
            crate::log::warning(
                "cart",
                &format!("could not update the cart for {job_id}: {error}"),
            );
        }
    }
}

/// `GET /api/v1/cart`.
pub async fn get_cart(State(state): State<Arc<AppState>>) -> Response {
    let settings = state.settings.clone();
    match tokio::task::spawn_blocking(move || read_cart(&settings)).await {
        Ok(ids) => Json(json!({ "job_ids": ids })).into_response(),
        Err(error) => respond::internal_error("read cart", &error),
    }
}

/// `PUT /api/v1/cart` with `{"job_ids": [...]}`; the website's copy wins.
pub async fn put_cart(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let job_ids = reader
        .string_list("job_ids", Some(MAX_CART))
        .unwrap_or_default();
    if let Err(response) = reader.finish() {
        return response;
    }
    let settings = state.settings.clone();
    match respond::blocking("write cart", move || write_cart(&settings, &job_ids)).await {
        Ok(ids) => Json(json!({ "job_ids": ids })).into_response(),
        Err(response) => response,
    }
}

/// One row of the extension's job list.
fn extension_job(lead: &Map<String, Value>, in_cart: bool) -> Value {
    let field = |key: &str| -> String {
        match lead.get(key) {
            Some(Value::String(text)) => text.clone(),
            Some(Value::Null) | None => String::new(),
            Some(other) => other.to_string(),
        }
    };
    let meta = lead.get("source_meta").and_then(Value::as_object);
    let context = meta
        .and_then(|meta| meta.get("prepared_fill_context"))
        .and_then(Value::as_object);
    let context_text = |key: &str| {
        context
            .and_then(|c| c.get(key))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned()
    };
    let mut url = context_text("apply_url");
    if url.is_empty() {
        url = field("url");
    }
    let resume = {
        let primary = field("resume_asset");
        let path = if primary.is_empty() {
            field("asset")
        } else {
            primary
        };
        path.trim().to_owned()
    };
    json!({
        "job_id": field("job_id"),
        "title": field("title"),
        "company": field("company"),
        "url": url,
        "location": field("location"),
        "status": field("status"),
        "resume_ready": !resume.is_empty() && Path::new(&resume).is_file(),
        "prepared": context.is_some(),
        "prepared_at": context_text("prepared_at"),
        "in_cart": in_cart,
    })
}

fn extension_jobs_list(
    settings: &SettingsStore,
    leads: &LeadStore,
    include_done: bool,
) -> Result<Vec<Value>, agent_hire_store::StoreError> {
    let cart = read_cart(settings);
    let mut rows: Vec<Value> = Vec::new();
    let mut listed: Vec<String> = Vec::new();
    for job_id in &cart {
        if let Some(lead) = leads.by_id(job_id)? {
            rows.push(extension_job(&lead, true));
            listed.push(job_id.clone());
        }
    }
    let mut prepared: Vec<Value> = Vec::new();
    for lead in leads.all()? {
        let job_id = lead.get("job_id").and_then(Value::as_str).unwrap_or("");
        if job_id.is_empty() || listed.iter().any(|id| id == job_id) {
            continue;
        }
        let has_context = lead
            .get("source_meta")
            .and_then(|meta| meta.get("prepared_fill_context"))
            .is_some_and(Value::is_object);
        if has_context {
            prepared.push(extension_job(&lead, false));
        }
    }
    prepared.sort_by(|a, b| {
        b["prepared_at"]
            .as_str()
            .unwrap_or("")
            .cmp(a["prepared_at"].as_str().unwrap_or(""))
    });
    rows.extend(prepared);
    if !include_done {
        rows.retain(|row| !DONE_STATUSES.contains(&row["status"].as_str().unwrap_or("")));
    }
    Ok(rows)
}

/// `GET /api/v1/extension/jobs`: cart jobs (in cart order), then prepared jobs newest first.
/// Finished jobs are hidden unless `include_done=true`.
pub async fn extension_jobs(
    State(state): State<Arc<AppState>>,
    RawQuery(query): RawQuery,
) -> Response {
    let pairs = validation::query_pairs(query.as_deref());
    let mut reader = QueryReader::default();
    let include_done = reader.boolean(&pairs, "include_done", false);
    if let Err(response) = reader.finish() {
        return response;
    }
    let (settings, leads) = (state.settings.clone(), state.leads.clone());
    match respond::blocking("extension jobs", move || {
        extension_jobs_list(&settings, &leads, include_done)
    })
    .await
    {
        Ok(jobs) => Json(json!({ "jobs": jobs })).into_response(),
        Err(response) => response,
    }
}
