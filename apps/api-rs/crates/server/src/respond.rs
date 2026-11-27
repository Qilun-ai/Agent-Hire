//! Common responses and running blocking storage work off the async threads.

use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::json;

use crate::{log, validation};

/// `500 {"detail": "Internal server error", "request_id": ""}`, after logging the cause.
pub fn internal_error(context: &str, error: &dyn std::fmt::Display) -> Response {
    log::warning("api", &format!("{context}: {error}"));
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({ "detail": "Internal server error", "request_id": "" })),
    )
        .into_response()
}

/// `<status> {"detail": <detail>}`.
pub fn detail(status: StatusCode, detail: &str) -> Response {
    (status, Json(json!({ "detail": detail }))).into_response()
}

/// The legacy-compatible 404 for an unmatched native route.
pub fn not_found(path: &str) -> Response {
    let path = validation::decode_path(path);
    if path.starts_with("/api/") {
        detail(StatusCode::NOT_FOUND, &format!("no such endpoint: {path}"))
    } else {
        detail(StatusCode::NOT_FOUND, "Not Found")
    }
}

/// Runs `work` on the blocking pool; a storage error becomes a logged 500.
pub async fn blocking<T, E, F>(context: &'static str, work: F) -> Result<T, Response>
where
    T: Send + 'static,
    E: std::fmt::Display + Send + 'static,
    F: FnOnce() -> Result<T, E> + Send + 'static,
{
    match tokio::task::spawn_blocking(work).await {
        Ok(Ok(value)) => Ok(value),
        Ok(Err(error)) => Err(internal_error(context, &error)),
        Err(error) => Err(internal_error(context, &error)),
    }
}
