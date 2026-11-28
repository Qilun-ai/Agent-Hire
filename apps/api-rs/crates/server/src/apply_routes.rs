//! The apply hand-off with the extension: the fill context for a tab, what the extension
//! filled, and the human's confirmation that they applied.
//!
//! Opening a form is not applying: only `POST /leads/{id}/applied`, which a human triggers,
//! sets the `applied` status.

use std::sync::Arc;

use agent_hire_store::LeadError;
use axum::Json;
use axum::body::Bytes;
use axum::extract::{Path, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};
use url::Url;

use crate::app_state::AppState;
use crate::cart_routes;
use crate::fill_context as handoff;
use crate::log;
use crate::respond;
use crate::validation::{self, ModelReader};

fn url_host(value: &str) -> String {
    Url::parse(value)
        .ok()
        .and_then(|url| url.host_str().map(str::to_lowercase))
        .unwrap_or_default()
}

/// `GET /api/v1/fill-context?job_id=...|url=...`: the hand-off the extension fills from.
///
/// An explicit `job_id` wins over the tab URL (a posting found on one site often applies on
/// another). Without one, the newest prepared hand-off whose apply URL has the tab's host is used.
pub async fn fill_context(State(state): State<Arc<AppState>>, request: Request) -> Response {
    let pairs = validation::query_pairs(request.uri().query());
    let job_id = validation::query_value(&pairs, "job_id")
        .filter(|id| !id.is_empty())
        .map(str::to_owned);
    let url = validation::query_value(&pairs, "url").map(str::to_owned);
    let store = state.leads.clone();
    if let Some(job_id) = job_id {
        let profile = state.profile.clone();
        return match respond::blocking("read application context", move || {
            let Some(lead) = store.by_id(&job_id)? else {
                return Ok(None);
            };
            let lead = Value::Object(lead);
            let cached = &lead["source_meta"]["prepared_fill_context"];
            if cached
                .as_object()
                .is_some_and(|context| !context.is_empty())
            {
                return Ok(Some(cached.clone()));
            }
            let context = handoff::build(&lead, &profile.get_profile()?);
            // A draft without a résumé is only a preview. Keep URL matching limited to
            // hand-offs with a generated document, as in the legacy application flow.
            if context["resume_path"]
                .as_str()
                .is_some_and(|path| !path.is_empty())
                && let Err(error) = store.save_prepared_fill_context(&job_id, &context)
            {
                log::warning("apply", &format!("save fill context for {job_id}: {error}"));
            }
            Ok::<_, agent_hire_store::StoreError>(Some(context))
        })
        .await
        {
            Ok(Some(context)) => Json(context).into_response(),
            Ok(None) => respond::detail(StatusCode::NOT_FOUND, "job not found"),
            Err(response) => response,
        };
    }
    let requested_host = url.as_deref().map(url_host).unwrap_or_default();
    let leads = match respond::blocking("list leads", move || store.all()).await {
        Ok(leads) => leads,
        Err(response) => return response,
    };
    let best = leads
        .iter()
        .filter_map(|lead| {
            lead.get("source_meta")?
                .get("prepared_fill_context")?
                .as_object()
        })
        .filter(|context| {
            requested_host.is_empty()
                || url_host(
                    context
                        .get("apply_url")
                        .and_then(Value::as_str)
                        .unwrap_or(""),
                ) == requested_host
        })
        .max_by(|a, b| {
            let at = |context: &serde_json::Map<String, Value>| {
                context
                    .get("prepared_at")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_owned()
            };
            at(a).cmp(&at(b))
        });
    match best {
        Some(context) => Json(Value::Object(context.clone())).into_response(),
        None => respond::detail(
            StatusCode::NOT_FOUND,
            "no prepared application matches that URL",
        ),
    }
}

/// `POST /api/v1/leads/{job_id}/filled`: the extension reports what it filled.
pub async fn filled(
    State(state): State<Arc<AppState>>,
    Path(job_id): Path<String>,
    body: Bytes,
) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let filled = reader.string_list("filled", None).unwrap_or_default();
    let skipped = reader.string_list("skipped", None).unwrap_or_default();
    if let Err(response) = reader.finish() {
        return response;
    }
    let (filled, skipped) = (filled.len(), skipped.len());
    let action = format!("extension_filled:{filled}/{}", filled + skipped);
    let leads = state.leads.clone();
    let id = job_id.clone();
    let recorded = tokio::task::spawn_blocking(move || leads.record_event(&id, &action)).await;
    if !matches!(recorded, Ok(Ok(()))) {
        crate::log::warning("apply", &format!("could not record the fill for {job_id}"));
    }
    state
        .hub
        .broadcast(json!({ "type": "agent", "event": "fill_done", "msg": format!("Extension filled {filled} fields") }))
        .await;
    Json(json!({ "ok": true, "filled": filled, "skipped": skipped })).into_response()
}

/// `POST /api/v1/leads/{job_id}/applied`: the human confirms they submitted.
pub async fn applied(State(state): State<Arc<AppState>>, Path(job_id): Path<String>) -> Response {
    let store = state.leads.clone();
    let id = job_id.clone();
    match tokio::task::spawn_blocking(move || store.update_status(&id, "applied")).await {
        Ok(Ok(())) => {}
        Ok(Err(LeadError::NotFound(_))) => {
            return respond::detail(StatusCode::NOT_FOUND, "lead not found");
        }
        Ok(Err(LeadError::Invalid(message))) => {
            return respond::detail(StatusCode::BAD_REQUEST, &message);
        }
        Ok(Err(error)) => return respond::internal_error("mark applied", &error),
        Err(error) => return respond::internal_error("mark applied", &error),
    }
    let (settings, store, id) = (state.settings.clone(), state.leads.clone(), job_id.clone());
    let lead = tokio::task::spawn_blocking(move || {
        cart_routes::remove_from_cart(&settings, &id);
        store.by_id(&id)
    })
    .await;
    if let Ok(Ok(Some(lead))) = lead {
        state
            .hub
            .broadcast(json!({ "type": "LEAD_UPDATED", "data": Value::Object(lead) }))
            .await;
    }
    state
        .hub
        .broadcast(json!({ "type": "agent", "event": "applied", "msg": format!("Marked applied: {job_id}") }))
        .await;
    Json(json!({ "ok": true, "job_id": job_id, "status": "applied" })).into_response()
}
