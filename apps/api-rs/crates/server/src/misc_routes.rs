//! Small routes: frontend error reports and the extension's LLM relay.

use std::sync::Arc;
use std::time::Duration;

use axum::Json;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{Map, Value, json};

use crate::app_state::AppState;
use crate::log;
use crate::rate_limit::RateLimiter;
use crate::respond;
use crate::settings_routes;
use crate::telemetry;
use crate::validation;

static ERROR_REPORTS: RateLimiter = RateLimiter::new(30, Duration::from_secs(60));
/// One agent run makes about one request per page action; this bounds a runaway loop.
static AGENT_RELAY: RateLimiter = RateLimiter::new(240, Duration::from_secs(60));

fn field_text(body: &Map<String, Value>, key: &str, max: usize) -> String {
    let text = match body.get(key) {
        None | Some(Value::Null) => String::new(),
        Some(value) => crate::python_text::str_of(value),
    };
    text.chars().take(max).collect()
}

/// `POST /api/v1/errors`: a frontend error report, redacted and logged.
pub async fn report_error(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    if let Err(response) = ERROR_REPORTS.check() {
        return response;
    }
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let falsy = |value: &Value| {
        matches!(value, Value::Null | Value::Bool(false))
            || value.as_str() == Some("")
            || value.as_f64() == Some(0.0)
    };
    let error = match body.get("error") {
        Some(value) if !falsy(value) => crate::python_text::str_of(value)
            .chars()
            .take(2000)
            .collect(),
        _ => "Frontend error".to_owned(),
    };
    let mut context = Map::new();
    context.insert("error".to_owned(), json!(error));
    context.insert(
        "componentStack".to_owned(),
        json!(field_text(&body, "componentStack", 8000)),
    );
    context.insert("url".to_owned(), json!(field_text(&body, "url", 1000)));
    context.insert(
        "userAgent".to_owned(),
        json!(field_text(&body, "userAgent", 500)),
    );
    let message = telemetry::redact_text(&error, 2000);
    let data_dir = state.paths.data_dir.clone();
    let _ = tokio::task::spawn_blocking(move || {
        telemetry::log_frontend_error(&data_dir, &message, &context)
    })
    .await;
    Json(json!({ "ok": true })).into_response()
}

/// `POST /api/v1/llm/chat/completions`: the extension agent's tool calls, replayed on the
/// configured provider.
pub async fn chat_completions(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    if let Err(response) = AGENT_RELAY.check() {
        return response;
    }
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let settings = match settings_routes::current(&state).await {
        Ok(settings) => settings,
        Err(response) => return response,
    };
    match agent_hire_llm::relay_chat_completions(&settings, &body).await {
        Ok(value) => Json(value).into_response(),
        Err(error) => {
            log::warning("llm", &format!("tool proxy refused: {error}"));
            respond::detail(StatusCode::BAD_REQUEST, &error.0)
        }
    }
}
