//! UI actions share the API's services and validation.

use std::sync::Arc;

use agent_hire_ui::settings;
use axum::Router;
use axum::body::Bytes;
use axum::extract::{Json, State};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::{delete, post, put};
use serde_json::Value;

use super::data;
use crate::app_state::AppState;
use crate::data_reset_routes;
use crate::email_routes;
use crate::fill_routes;
use crate::jobindex_routes;
use crate::jobindex_runtime_routes;
use crate::leads_routes;
use crate::settings_routes;

pub(super) fn router() -> Router<Arc<AppState>> {
    Router::new()
        .route("/ui/actions/jobs/promote", post(jobindex_routes::promote))
        .route("/ui/actions/scrape", post(jobindex_routes::scrape))
        .route("/ui/actions/scrape/stop", post(jobindex_routes::stop))
        .route(
            "/ui/actions/kaggle/connect",
            post(jobindex_runtime_routes::kaggle_connect),
        )
        .route(
            "/ui/actions/kaggle/toggle",
            post(jobindex_runtime_routes::kaggle_toggle),
        )
        .route(
            "/ui/actions/kaggle/encoder-device",
            post(jobindex_runtime_routes::kaggle_encoder_device),
        )
        .route(
            "/ui/actions/kaggle/stop",
            post(jobindex_runtime_routes::kaggle_stop),
        )
        .route(
            "/ui/actions/kaggle/forget",
            post(jobindex_runtime_routes::kaggle_forget),
        )
        .route("/ui/actions/profile/answers", put(fill_routes::put_facts))
        .route("/ui/actions/settings", post(settings_routes::post_settings))
        .route(
            "/ui/actions/settings/key/{key}",
            delete(settings_routes::delete_key),
        )
        .route("/ui/actions/settings/validate", post(validate_settings))
        .route("/ui/actions/settings/probe", post(probe_settings))
        .route("/ui/actions/email/connect", post(email_routes::connect))
        .route("/ui/actions/email/check", post(email_routes::check))
        .route("/ui/actions/email/poll", post(email_routes::poll))
        .route(
            "/ui/actions/email/disconnect",
            post(email_routes::disconnect),
        )
        .route("/ui/actions/data/reset", post(data_reset_routes::reset))
        .route("/ui/leads/{job_id}/status", put(leads_routes::set_status))
        .route("/ui/leads/{job_id}/feedback", put(leads_routes::feedback))
        .route(
            "/ui/leads/{job_id}/followup",
            put(leads_routes::set_followup),
        )
        .route(
            "/ui/leads/{job_id}/pdf",
            axum::routing::get(leads_routes::pdf),
        )
}

async fn validate_settings(
    State(state): State<Arc<AppState>>,
    Json(body): Json<Value>,
) -> Response {
    let provider = body["llm_provider"].as_str().unwrap_or_default();
    let response =
        settings_routes::validate_pending(State(state), Bytes::from(body.to_string())).await;
    if !response.status().is_success() {
        return response;
    }
    let result = data::json_response(response).await;
    Html(settings::result("validate", provider, &result).into_string()).into_response()
}

async fn probe_settings(State(state): State<Arc<AppState>>) -> Response {
    match jobindex_runtime_routes::probe(State(state)).await {
        Ok(Json(result)) => {
            Html(settings::result("probe", "", &result).into_string()).into_response()
        }
        Err(response) => response,
    }
}
