//! Public liveness and authenticated health details.

use std::env;
use std::sync::Arc;

use agent_hire_llm::{self as llm, Provider, SUBSCRIPTION_CLI_PROVIDERS, Settings};
use agent_hire_store::UtcTime;
use axum::Json;
use axum::extract::State;
use axum::http::{HeaderMap, header};
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};

use crate::app_state::AppState;
use crate::{generation_readiness, respond};

/// Reports liveness and adds diagnostics for authenticated callers.
pub async fn health(State(state): State<Arc<AppState>>, headers: HeaderMap) -> Response {
    let authorized = health_token(&state, &headers);
    let uptime = (state.started.elapsed().as_secs_f64() * 100.0).round_ties_even() / 100.0;
    let mut body = json!({
        "status": "alive",
        "uptime_seconds": uptime,
        "timestamp": UtcTime::now().to_iso_micros(),
        "details_available": false,
    });
    if authorized {
        match respond::blocking("health details", move || {
            Ok::<_, std::convert::Infallible>(details(&state))
        })
        .await
        {
            Ok(detail) => {
                body.as_object_mut().expect("health is an object").extend(
                    detail
                        .as_object()
                        .expect("health details are an object")
                        .clone(),
                );
            }
            Err(response) => return response,
        }
    }
    Json(body).into_response()
}

fn health_token(state: &AppState, headers: &HeaderMap) -> bool {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .is_some_and(|token| state.token.matches(token))
}

fn details(state: &AppState) -> Value {
    let sqlite = match state.database.connect().and_then(|connection| {
        Ok(connection.query_row("SELECT COUNT(*) FROM leads", [], |row| row.get::<_, i64>(0))?)
    }) {
        Ok(count) => json!({"status":"ok","lead_count":count}),
        Err(error) => component_error(&error),
    };
    // The old graph's profile nodes are lists in the SQLite snapshot. CRM lead counts live
    // under sqlite; native storage has no separate graph JobLead nodes.
    let (graph, profile) = match state.profile.get_profile() {
        Ok(profile) => {
            let count = |field: &str| profile[field].as_array().map_or(0, Vec::len);
            let candidate = ["n", "s"]
                .iter()
                .any(|field| generation_readiness::truthy(&profile[*field]));
            let counts = json!({
                "candidate":usize::from(candidate),"skill":count("skills"),"project":count("projects"),
                "experience":count("exp"),"joblead":0,
            });
            let has_profile = candidate
                || ["skills", "projects", "exp"]
                    .iter()
                    .any(|key| count(key) != 0);
            (
                json!({"status":"ok","counts":counts}),
                json!({"status":"ok","has_profile":has_profile}),
            )
        }
        Err(error) => (component_error(&error), component_error(&error)),
    };
    // Embeddings are generated on demand from the snapshot. Report the embedder's active
    // mode and degradation details; there are no persisted vector tables.
    let mut vector = state
        .embedder
        .runtime_status()
        .unwrap_or_else(|| json!({"status":"ok","mode":"not_loaded"}));
    vector["tables"] = json!([]);
    let llm = match state.settings.all() {
        Ok(settings) => llm_status(&Settings::new(settings)),
        Err(error) => component_error(&error),
    };
    let status = if sqlite["status"] == "ok" && graph["status"] == "ok" {
        "alive"
    } else {
        "degraded"
    };
    let components =
        json!({"sqlite":sqlite,"graph":graph,"vector":vector,"profile":profile,"llm":llm});
    let scan = state
        .settings
        .get("last_scan_finished_at", "")
        .unwrap_or_default();
    json!({
        "status":status,"details_available":true,
        "log_level":env::var("AGENT_HIRE_LOG_LEVEL").unwrap_or_else(|_| "INFO".to_owned()),
        "last_scan_finished_at":scan,"components":components,"checks":components,"services":{},
    })
}

fn llm_status(settings: &Settings) -> Value {
    let resolved = settings.resolve(None);
    let provider = &resolved.provider;
    let status = if llm::provider_needs_key(provider) && resolved.api_key.is_empty() {
        "missing_key"
    } else {
        "ok"
    };
    let source = if provider == "ollama" {
        "local"
    } else if SUBSCRIPTION_CLI_PROVIDERS.contains(&provider.as_str()) {
        "subscription"
    } else if resolved.api_key.is_empty() {
        "none"
    } else if Provider::find(provider)
        .is_some_and(|p| !settings.get(p.key_setting).trim().is_empty())
    {
        "settings"
    } else {
        "environment"
    };
    json!({"status":status,"provider":provider,"model":resolved.model,"key_source":source})
}

fn component_error(error: &dyn std::fmt::Display) -> Value {
    json!({"status":"error","error":error.to_string()})
}
