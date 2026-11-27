//! Bearer-token checks for `/api/*`.

use std::sync::Arc;

use axum::Json;
use axum::extract::{Request, State};
use axum::http::{HeaderMap, Method, StatusCode, header};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use serde_json::json;

use crate::app_state::AppState;

/// The credentials of an `Authorization: Bearer <token>` header; the scheme is case-insensitive.
pub fn bearer_token(headers: &HeaderMap) -> Option<&str> {
    let value = headers.get(header::AUTHORIZATION)?.to_str().ok()?;
    let (scheme, credentials) = value.trim().split_once(' ')?;
    if !scheme.eq_ignore_ascii_case("bearer") {
        return None;
    }
    let credentials = credentials.trim();
    (!credentials.is_empty()).then_some(credentials)
}

/// Whether the request carries this server's token.
pub fn has_valid_token(state: &AppState, headers: &HeaderMap) -> bool {
    bearer_token(headers).is_some_and(|token| state.token.matches(token))
}

/// Rejects `/api/*` requests without the bearer token. `OPTIONS` (CORS preflight) passes.
pub async fn require_token(
    State(state): State<Arc<AppState>>,
    request: Request,
    next: Next,
) -> Response {
    let protected =
        request.uri().path().starts_with("/api/") && request.method() != Method::OPTIONS;
    if protected && !has_valid_token(&state, request.headers()) {
        return unauthorized();
    }
    next.run(request).await
}

/// `401 {"detail": "invalid token"}`.
pub fn unauthorized() -> Response {
    (
        StatusCode::UNAUTHORIZED,
        Json(json!({ "detail": "invalid token" })),
    )
        .into_response()
}
