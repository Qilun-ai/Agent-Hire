//! `GET /api/v1/events`: the activity log.

use std::sync::Arc;

use axum::Json;
use axum::extract::{RawQuery, State};
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};

use crate::app_state::AppState;
use crate::respond;
use crate::validation;

/// Newest events first; `limit` (default 100) is clamped to 1..=1000, `job_id` filters.
pub async fn list(State(state): State<Arc<AppState>>, RawQuery(query): RawQuery) -> Response {
    let pairs = validation::query_pairs(query.as_deref());
    let limit = match validation::query_int(&pairs, "limit", 100) {
        Ok(limit) => limit.clamp(1, 1000),
        Err(response) => return response,
    };
    let job_id = validation::query_value(&pairs, "job_id").map(str::to_owned);
    let log = state.events.clone();
    let events = respond::blocking("read events", move || log.list(limit, job_id.as_deref())).await;
    match events {
        Ok(events) => {
            let rows: Vec<Value> = events
                .into_iter()
                .map(|event| json!({ "job_id": event.job_id, "action": event.action, "ts": event.ts }))
                .collect();
            Json(Value::Array(rows)).into_response()
        }
        Err(response) => response,
    }
}
