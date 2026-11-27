//! The HTTP routes and middleware stack.

use std::sync::Arc;
use std::time::Duration;

use agent_hire_store::ProfileEntryKind;
use axum::extract::{DefaultBodyLimit, Extension, Request, State};
use axum::http::StatusCode;
use axum::response::Response;
use axum::routing::{delete, get, post, put};
use axum::{Json, Router, middleware};
use serde_json::json;

use crate::app_state::AppState;
use crate::{
    agent_docs_routes, agent_routes, apply_routes, auth, cart_routes, data_reset_routes,
    documents_routes, email_routes, environment_settings, events, events_routes, fill_plan,
    fill_routes, generation_routes, health, jobindex_routes, jobindex_runtime_routes, leads_routes,
    misc_routes, profile_import_routes, profile_routes, settings_routes, ui_routes,
};

/// The version reported by `/bootstrap`.
const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Builds the app. Middleware runs, outermost first: request id, token check, trusted host, CORS.
pub fn build(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/bootstrap", get(bootstrap))
        .route("/health", get(health::health))
        .route("/ws", get(events::websocket))
        .route("/api/v1/shutdown", post(shutdown))
        .route(
            "/api/v1/agent-internal/llm-config",
            get(agent_routes::llm_config),
        )
        .route("/api/v1/events", get(events_routes::list))
        .route(
            "/api/v1/settings",
            get(settings_routes::get_settings).post(settings_routes::post_settings),
        )
        .route(
            "/api/v1/settings/environment",
            get(environment_settings::get_environment),
        )
        .route(
            "/api/v1/settings/secret-previews",
            get(settings_routes::secret_previews),
        )
        .route(
            "/api/v1/settings/key/{key}",
            delete(settings_routes::delete_key),
        )
        .route(
            "/api/v1/settings/validate",
            get(settings_routes::validate_saved).post(settings_routes::validate_pending),
        )
        .route(
            "/api/v1/settings/models/{provider}",
            get(settings_routes::models_saved).post(settings_routes::models_pending),
        )
        .route(
            "/api/v1/preferences",
            get(settings_routes::get_preferences).post(settings_routes::post_preferences),
        )
        .route("/api/v1/leads", get(leads_routes::list))
        .route("/api/v1/leads/summary", get(leads_routes::summary))
        .route("/api/v1/leads/cleanup", post(leads_routes::cleanup))
        .route(
            "/api/v1/leads/{job_id}",
            get(leads_routes::detail).delete(leads_routes::remove),
        )
        .route(
            "/api/v1/leads/{job_id}/status",
            put(leads_routes::set_status),
        )
        .route(
            "/api/v1/leads/{job_id}/feedback",
            put(leads_routes::feedback),
        )
        .route(
            "/api/v1/leads/{job_id}/followup",
            put(leads_routes::set_followup),
        )
        .route(
            "/api/v1/leads/{job_id}/generate",
            post(generation_routes::generate),
        )
        .route(
            "/api/v1/leads/{job_id}/suggest-selection",
            post(generation_routes::suggest_selection),
        )
        .route("/api/v1/leads/{job_id}/pdf", get(leads_routes::pdf))
        .route("/api/v1/leads/{job_id}/filled", post(apply_routes::filled))
        .route(
            "/api/v1/leads/{job_id}/applied",
            post(apply_routes::applied),
        )
        .route("/api/v1/fill-context", get(apply_routes::fill_context))
        .route(
            "/api/v1/cart",
            get(cart_routes::get_cart).put(cart_routes::put_cart),
        )
        .route("/api/v1/extension/jobs", get(cart_routes::extension_jobs))
        .route("/api/v1/email/status", get(email_routes::status))
        .route("/api/v1/email/connect", post(email_routes::connect))
        .route("/api/v1/email/disconnect", post(email_routes::disconnect))
        .route("/api/v1/email/check", post(email_routes::check))
        .route("/api/v1/email/poll", post(email_routes::poll))
        .route("/api/v1/fill/plan", post(fill_plan::plan))
        .route(
            "/api/v1/fill/facts",
            get(fill_routes::get_facts).put(fill_routes::put_facts),
        )
        .route(
            "/api/v1/ingest/profile",
            post(profile_import_routes::import_profile),
        )
        .route(
            "/api/v1/documents",
            get(documents_routes::list).post(documents_routes::upload),
        )
        .route(
            "/api/v1/documents/ingest/status",
            get(documents_routes::ingest_status),
        )
        .route("/api/v1/data/reset", post(data_reset_routes::reset))
        .route("/api/v1/profile", get(profile_routes::get_profile))
        .route(
            "/api/v1/profile/candidate",
            put(profile_routes::update_candidate),
        )
        .route(
            "/api/v1/profile/identity",
            put(profile_routes::update_identity),
        )
        .route("/api/v1/profile/skill", post(profile_routes::save_skill))
        .route(
            "/api/v1/profile/skill/{sid}",
            put(profile_routes::save_skill).delete(profile_routes::delete_skill),
        )
        .route(
            "/api/v1/profile/experience",
            post(profile_routes::save_experience),
        )
        .route(
            "/api/v1/profile/experience/{eid}",
            put(profile_routes::save_experience).delete(profile_routes::delete_experience),
        )
        .route(
            "/api/v1/profile/project",
            post(profile_routes::save_project),
        )
        .route(
            "/api/v1/profile/project/{pid}",
            put(profile_routes::save_project).delete(profile_routes::delete_project),
        )
        .route(
            "/api/v1/profile/education",
            post(profile_routes::add_entry).layer(Extension(ProfileEntryKind::Education)),
        )
        .route(
            "/api/v1/profile/education/{*entry}",
            delete(profile_routes::delete_entry).layer(Extension(ProfileEntryKind::Education)),
        )
        .route(
            "/api/v1/profile/education/",
            delete(profile_routes::delete_entry).layer(Extension(ProfileEntryKind::Education)),
        )
        .route(
            "/api/v1/profile/certification",
            post(profile_routes::add_entry).layer(Extension(ProfileEntryKind::Certification)),
        )
        .route(
            "/api/v1/profile/certification/{*entry}",
            delete(profile_routes::delete_entry).layer(Extension(ProfileEntryKind::Certification)),
        )
        .route(
            "/api/v1/profile/certification/",
            delete(profile_routes::delete_entry).layer(Extension(ProfileEntryKind::Certification)),
        )
        .route(
            "/api/v1/profile/achievement",
            post(profile_routes::add_entry).layer(Extension(ProfileEntryKind::Achievement)),
        )
        .route(
            "/api/v1/profile/achievement/{*entry}",
            delete(profile_routes::delete_entry).layer(Extension(ProfileEntryKind::Achievement)),
        )
        .route(
            "/api/v1/profile/achievement/",
            delete(profile_routes::delete_entry).layer(Extension(ProfileEntryKind::Achievement)),
        )
        .merge(job_index_runtime())
        .route("/api/v1/jobindex/search", post(jobindex_routes::search))
        .route(
            "/api/v1/jobindex/jobs/{reference}",
            get(jobindex_routes::get_job),
        )
        .route(
            "/api/v1/jobindex/departments",
            get(jobindex_routes::departments),
        )
        .route(
            "/api/v1/jobindex/companies",
            get(jobindex_routes::companies),
        )
        .route("/api/v1/jobindex/promote", post(jobindex_routes::promote))
        .route("/api/v1/jobindex/custom", post(jobindex_routes::add_custom))
        .route("/api/v1/jobindex/delete", post(jobindex_routes::delete))
        .route("/api/v1/jobindex/reset", post(jobindex_routes::reset))
        .route(
            "/api/v1/jobindex/scrape",
            get(jobindex_routes::scrape_status).post(jobindex_routes::scrape),
        )
        .route("/api/v1/jobindex/scrape/stop", post(jobindex_routes::stop))
        .route("/api/v1/errors", post(misc_routes::report_error))
        .route(
            "/api/v1/llm/chat/completions",
            post(misc_routes::chat_completions),
        )
        .route(
            "/api/v1/agent-docs/extract",
            post(agent_docs_routes::extract),
        )
        .route(
            "/api/v1/agent-docs/read-page",
            post(agent_docs_routes::read_page),
        )
        .route(
            "/api/v1/settings/subscription-status",
            get(settings_routes::subscription_status),
        )
        .route(
            "/api/v1/settings/subscription-login/{provider}",
            post(settings_routes::subscription_login),
        )
        .merge(ui_routes::router())
        .method_not_allowed_fallback(method_not_allowed)
        .fallback(fallback)
        // Uploads go up to 20 MB (documents, résumés); the default limit is 2 MB.
        .layer(DefaultBodyLimit::max(64 * 1024 * 1024))
        .layer(middleware::from_fn(crate::middleware::cors))
        .layer(middleware::from_fn(crate::middleware::trusted_host))
        .layer(middleware::from_fn_with_state(
            Arc::clone(&state),
            auth::require_token,
        ))
        .layer(middleware::from_fn_with_state(
            Arc::clone(&state),
            ui_routes::authorize_ui_action,
        ))
        .layer(middleware::from_fn(crate::middleware::request_id))
        .with_state(state)
}

fn job_index_runtime() -> Router<Arc<AppState>> {
    Router::new()
        .route(
            "/api/v1/jobindex/status",
            get(jobindex_runtime_routes::status),
        )
        .route(
            "/api/v1/jobindex/encoders/progress",
            get(jobindex_runtime_routes::progress),
        )
        .route(
            "/api/v1/jobindex/classify",
            post(jobindex_runtime_routes::classify),
        )
        .route(
            "/api/v1/jobindex/decision/probe",
            post(jobindex_runtime_routes::probe),
        )
        .route(
            "/api/v1/jobindex/kaggle",
            get(jobindex_runtime_routes::kaggle),
        )
        .route(
            "/api/v1/jobindex/kaggle/connect",
            post(jobindex_runtime_routes::kaggle_connect),
        )
        .route(
            "/api/v1/jobindex/kaggle/toggle",
            post(jobindex_runtime_routes::kaggle_toggle),
        )
        .route(
            "/api/v1/jobindex/kaggle/encoder-device",
            post(jobindex_runtime_routes::kaggle_encoder_device),
        )
        .route(
            "/api/v1/jobindex/kaggle/stop",
            post(jobindex_runtime_routes::kaggle_stop),
        )
        .route(
            "/api/v1/jobindex/kaggle/forget",
            post(jobindex_runtime_routes::kaggle_forget),
        )
}

async fn method_not_allowed() -> Response {
    crate::respond::detail(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed")
}

/// Hands the browser UI its token; the server only listens on loopback.
async fn bootstrap(State(state): State<Arc<AppState>>) -> Json<serde_json::Value> {
    Json(json!({ "token": state.token.as_str(), "version": VERSION, "mode": "web" }))
}

/// Answers, then shuts the server down shortly after.
async fn shutdown(State(state): State<Arc<AppState>>) -> Json<serde_json::Value> {
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(100)).await;
        state.request_shutdown();
    });
    Json(json!({ "ok": true }))
}

/// Reports unknown routes using the API's existing response format.
async fn fallback(request: Request) -> Response {
    crate::respond::not_found(request.uri().path())
}
