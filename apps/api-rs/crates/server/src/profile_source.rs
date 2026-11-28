//! The canonical profile for routes that consume it.

use serde_json::{Map, Value};

use crate::app_state::AppState;
use crate::respond;

/// The profile as `GET /api/v1/profile` returns it; an empty object when it cannot be read.
pub async fn profile(state: &AppState) -> Map<String, Value> {
    let store = state.profile.clone();
    respond::blocking("read profile", move || store.get_profile())
        .await
        .unwrap_or_default()
}
