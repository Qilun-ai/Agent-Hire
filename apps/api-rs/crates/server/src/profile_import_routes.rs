//! Agent-supplied JSON profile imports.

use std::error::Error;
use std::sync::Arc;
use std::time::Duration;

use axum::Json;
use axum::body;
use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};

use crate::app_state::AppState;
use crate::rate_limit::RateLimiter;
use crate::{respond, validation};

const MAX_PROFILE_BYTES: usize = 5 * 1024 * 1024;
static INGEST: RateLimiter = RateLimiter::new(5, Duration::from_secs(60));

/// Normalizes and saves the profile that the agent extracted.
pub async fn import_profile(State(state): State<Arc<AppState>>, request: Request) -> Response {
    if let Err(response) = INGEST.check() {
        return response;
    }
    let bytes = match body::to_bytes(request.into_body(), MAX_PROFILE_BYTES).await {
        Ok(bytes) => bytes,
        Err(error)
            if error
                .source()
                .is_some_and(|source| source.to_string() == "length limit exceeded") =>
        {
            return respond::detail(
                StatusCode::PAYLOAD_TOO_LARGE,
                "Profile JSON too large (max 5 MB)",
            );
        }
        Err(error) => return invalid_json(&error.to_string()),
    };
    let value = match parse(&bytes) {
        Ok(value) => value,
        Err(error) => return invalid_json(&error),
    };
    if !value.is_object() {
        return respond::detail(
            StatusCode::BAD_REQUEST,
            "Profile JSON must be a JSON object (e.g. {\"skills\": ...})",
        );
    }
    let _profile = state.profile_writes.read().await;
    let store = state.profile.clone();
    let result = match tokio::task::spawn_blocking(move || store.import_profile(value)).await {
        Ok(Ok(response)) => response,
        Ok(Err(error)) => partial(&error.to_string()),
        Err(error) => partial(&error.to_string()),
    };
    Json(result).into_response()
}

fn partial(error: &str) -> Value {
    json!({
        "status": "partial",
        "stats": {"skills": 0, "experience": 0, "projects": 0, "education": 0,
                  "certifications": 0, "achievements": 0, "vector_sync": "skipped"},
        "errors": [error],
    })
}

fn parse(bytes: &[u8]) -> Result<Value, String> {
    if bytes.is_empty() {
        return Ok(json!({}));
    }
    let text = std::str::from_utf8(bytes).map_err(|error| {
        let position = error.valid_up_to();
        let byte = bytes[position];
        let reason = if error.error_len().is_none() {
            "unexpected end of data"
        } else if (0xc2..=0xf4).contains(&byte) {
            "invalid continuation byte"
        } else {
            "invalid start byte"
        };
        format!("'utf-8' codec can't decode byte 0x{byte:02x} in position {position}: {reason}")
    })?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    serde_json::from_str(text).map_err(|error| {
        let (position, reason) = validation::json_decode_problem(text, &error);
        let prefix = text.chars().take(position).collect::<String>();
        let line = prefix.bytes().filter(|&byte| byte == b'\n').count() + 1;
        let column = prefix
            .rsplit('\n')
            .next()
            .map_or(1, |line| line.chars().count() + 1);
        format!("{reason}: line {line} column {column} (char {position})")
    })
}

fn invalid_json(error: &str) -> Response {
    respond::detail(
        StatusCode::BAD_REQUEST,
        &format!("Body is not valid JSON: {error}"),
    )
}
