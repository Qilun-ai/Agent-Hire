//! Validation for resetting application data.

use std::sync::Arc;

use axum::Json;
use axum::extract::State;
use axum::response::{IntoResponse, Response};
use serde_json::json;

use crate::app_state::AppState;
use crate::respond;
use crate::validation::{ModelBody, ModelReader};

/// Clears the native stores after the user confirms.
pub async fn reset(State(state): State<Arc<AppState>>, ModelBody(body): ModelBody) -> Response {
    let mut reader = ModelReader::new(&body);
    reader.literal("confirm", "DELETE", true);
    let clear = reader.boolean("clear_settings").unwrap_or(false);
    reader.forbid_extra(&["confirm", "clear_settings"]);
    if let Err(response) = reader.finish() {
        return response;
    }
    let _exclusive = state.profile_writes.write().await;
    let database = state.database.clone();
    let assets = state.paths.assets_dir();
    match tokio::task::spawn_blocking(move || {
        agent_hire_store::reset_data(&database, &assets, clear)
    })
    .await
    {
        Ok(summary) => Json(json!({"ok": true, "summary": summary})).into_response(),
        Err(error) => respond::internal_error("reset data", &error),
    }
}
