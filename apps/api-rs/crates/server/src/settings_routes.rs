//! Settings → AI model and General: stored settings, key previews, provider validation and model
//! lists, and the job-preferences text.

use std::sync::Arc;

use agent_hire_llm::{Catalog, Settings};
use axum::Json;
use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{Map, Value, json};

use crate::app_state::AppState;
use crate::python_text;
use crate::respond;
use crate::validation::{self, ModelReader};

/// What `GET /settings` shows instead of a stored secret.
const SECRET_SET: &str = "__AGENT_HIRE_SECRET_SET__";

/// Values the UI sends back for a secret it only showed masked; they keep the stored value.
fn is_mask(value: &str) -> bool {
    value == SECRET_SET
        || value == "•".repeat(20)
        || value == "â€¢".repeat(20)
        || value == "Ã¢â‚¬Â¢".repeat(20)
}

/// Whether a setting holds a secret.
pub fn is_sensitive(key: &str) -> bool {
    matches!(key, "anthropic_key" | "linkedin_cookie" | "x_bearer_token")
        || key.ends_with("_api_key")
        || key.ends_with("_key")
        || key.ends_with("_token")
}

/// `GET /api/v1/settings`: every setting, secrets masked.
pub async fn get_settings(State(state): State<Arc<AppState>>) -> Response {
    let store = state.settings.clone();
    let pairs = match respond::blocking("read settings", move || store.all()).await {
        Ok(pairs) => pairs,
        Err(response) => return response,
    };
    let mut out = Map::new();
    for (key, value) in pairs {
        let shown = if is_sensitive(&key) && !value.is_empty() {
            SECRET_SET.to_owned()
        } else {
            value
        };
        out.insert(key, Value::String(shown));
    }
    Json(Value::Object(out)).into_response()
}

/// `POST /api/v1/settings`: stores every field as text; masked secrets keep their stored value.
pub async fn post_settings(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let store = state.settings.clone();
    let saved = respond::blocking("save settings", move || {
        let stored: Map<String, Value> = store
            .all()?
            .into_iter()
            .map(|(key, value)| (key, Value::String(value)))
            .collect();
        let pairs: Vec<(String, String)> = body
            .iter()
            .map(|(key, value)| {
                let mut text = python_text::str_of(value);
                if is_sensitive(key) && is_mask(&text) {
                    text = stored
                        .get(key)
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned();
                }
                (key.clone(), text)
            })
            .collect();
        Ok::<_, agent_hire_store::SettingsError>(store.save(&pairs))
    })
    .await;
    match saved {
        Ok(Ok(())) => Json(json!({ "ok": true })).into_response(),
        Ok(Err(agent_hire_store::SettingsError::Invalid(message))) => {
            respond::detail(StatusCode::BAD_REQUEST, &message)
        }
        Ok(Err(error)) => respond::internal_error("save settings", &error),
        Err(response) => response,
    }
}

/// `GET /api/v1/settings/secret-previews`: the first four characters of each stored secret that
/// is at least 12 long (shorter ones preview as `""`), sorted by key. Kaggle secrets are left out.
pub async fn secret_previews(State(state): State<Arc<AppState>>) -> Response {
    let store = state.settings.clone();
    let mut pairs = match respond::blocking("read settings", move || store.all()).await {
        Ok(pairs) => pairs,
        Err(response) => return response,
    };
    pairs.sort_by(|a, b| a.0.cmp(&b.0));
    let mut out = Map::new();
    for (key, value) in pairs {
        if !is_sensitive(&key) || key.starts_with("kaggle_") || value.is_empty() || is_mask(&value)
        {
            continue;
        }
        let preview: String = if value.chars().count() >= 12 {
            value.chars().take(4).collect()
        } else {
            String::new()
        };
        out.insert(key, Value::String(preview));
    }
    Json(Value::Object(out)).into_response()
}

/// `DELETE /api/v1/settings/key/{key}`.
pub async fn delete_key(State(state): State<Arc<AppState>>, Path(key): Path<String>) -> Response {
    let store = state.settings.clone();
    let name = key.clone();
    match respond::blocking("delete setting", move || store.delete(&name)).await {
        Ok(deleted) => Json(json!({ "ok": true, "key": key, "deleted": deleted })).into_response(),
        Err(response) => response,
    }
}

/// `GET /api/v1/preferences`.
pub async fn get_preferences(State(state): State<Arc<AppState>>) -> Response {
    let store = state.settings.clone();
    match respond::blocking("read preferences", move || store.get("job_preferences", "")).await {
        Ok(text) => Json(json!({ "preferences": text })).into_response(),
        Err(response) => response,
    }
}

/// `POST /api/v1/preferences` with `{"preferences": "<up to 2000 characters>"}`.
pub async fn post_preferences(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let preferences = reader
        .string("preferences", Some(2000), false)
        .unwrap_or_default();
    reader.forbid_extra(&["preferences"]);
    if let Err(response) = reader.finish() {
        return response;
    }
    let store = state.settings.clone();
    let pairs = vec![("job_preferences".to_owned(), preferences)];
    match respond::blocking("save preferences", move || store.save(&pairs)).await {
        Ok(()) => Json(json!({ "ok": true })).into_response(),
        Err(response) => response,
    }
}

/// The stored settings, optionally merged with unsaved values from the Settings form.
async fn settings_snapshot(
    state: &AppState,
    pending: Option<Map<String, Value>>,
) -> Result<Settings, Response> {
    let store = state.settings.clone();
    let mut pairs = respond::blocking("read settings", move || store.all()).await?;
    if let Some(pending) = pending {
        for (key, value) in pending {
            let mut text = python_text::str_of(&value);
            let stored = pairs.iter().position(|(stored_key, _)| *stored_key == key);
            if is_sensitive(&key) && is_mask(&text) {
                text = stored
                    .map(|index| pairs[index].1.clone())
                    .unwrap_or_default();
            }
            match stored {
                Some(index) => pairs[index].1 = text,
                None => pairs.push((key, text)),
            }
        }
    }
    Ok(Settings::new(pairs))
}

async fn validate_with(settings: Settings) -> Response {
    let providers = tokio::task::spawn_blocking({
        let settings = settings.clone();
        move || agent_hire_llm::providers_to_validate(&settings)
    })
    .await
    .unwrap_or_default();
    let mut out = Map::new();
    for provider in providers {
        out.insert(
            provider.to_owned(),
            agent_hire_llm::probe_provider(provider, &settings).await,
        );
    }
    Json(Value::Object(out)).into_response()
}

/// `GET /api/v1/settings/validate`: probes every configured provider.
pub async fn validate_saved(State(state): State<Arc<AppState>>) -> Response {
    match settings_snapshot(&state, None).await {
        Ok(settings) => validate_with(settings).await,
        Err(response) => response,
    }
}

/// `POST /api/v1/settings/validate`: the same with unsaved form values; nothing is stored.
pub async fn validate_pending(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    match settings_snapshot(&state, Some(body)).await {
        Ok(settings) => validate_with(settings).await,
        Err(response) => response,
    }
}

async fn models_with(provider: &str, settings: Settings) -> Response {
    let provider = provider.trim().to_lowercase();
    if provider == "ollama" {
        return Json(json!({ "provider": "ollama", "models": [], "catalog": [] })).into_response();
    }
    let key = settings.provider_key(&provider);
    let live = if key.is_empty() {
        Vec::new()
    } else {
        agent_hire_llm::list_model_ids(&provider, &settings, &key).await
    };
    let catalog = Catalog::rows(&provider).await;
    let mut models: Vec<String> = Vec::new();
    let catalog_ids = catalog
        .iter()
        .filter_map(|row| row["id"].as_str())
        .map(str::to_owned);
    for id in live.into_iter().chain(catalog_ids) {
        if !id.is_empty() && !models.iter().any(|seen| seen.eq_ignore_ascii_case(&id)) {
            models.push(id);
        }
    }
    let mut out = json!({ "provider": provider, "models": models, "catalog": catalog });
    if out["models"].as_array().is_some_and(Vec::is_empty) {
        out["error"] = json!(if key.is_empty() {
            "not_configured"
        } else {
            "unreachable"
        });
    }
    Json(out).into_response()
}

/// `GET /api/v1/settings/models/{provider}`: live model ids first, then the catalogue.
pub async fn models_saved(
    State(state): State<Arc<AppState>>,
    Path(provider): Path<String>,
) -> Response {
    match settings_snapshot(&state, None).await {
        Ok(settings) => models_with(&provider, settings).await,
        Err(response) => response,
    }
}

/// `POST /api/v1/settings/models/{provider}` with unsaved form values.
pub async fn models_pending(
    State(state): State<Arc<AppState>>,
    Path(provider): Path<String>,
    body: Bytes,
) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    match settings_snapshot(&state, Some(body)).await {
        Ok(settings) => models_with(&provider, settings).await,
        Err(response) => response,
    }
}

/// `GET /api/v1/settings/subscription-status`: each subscription CLI, installed and logged in?
pub async fn subscription_status() -> Response {
    let statuses = tokio::task::spawn_blocking(|| {
        let mut providers: Vec<&str> = agent_hire_llm::SUBSCRIPTION_CLI_PROVIDERS.to_vec();
        providers.sort_unstable();
        let mut out = Map::new();
        for provider in providers {
            let mut status = agent_hire_llm::subscription_status(provider);
            if status["installed"] != json!(true) {
                status["install_hint"] = agent_hire_llm::install_hint(provider);
            }
            out.insert(provider.to_owned(), status);
        }
        Value::Object(out)
    })
    .await;
    match statuses {
        Ok(value) => Json(value).into_response(),
        Err(error) => respond::internal_error("subscription status", &error),
    }
}

/// `POST /api/v1/settings/subscription-login/{provider}`: starts the CLI's own login.
pub async fn subscription_login(Path(provider): Path<String>) -> Response {
    if !agent_hire_llm::SUBSCRIPTION_CLI_PROVIDERS.contains(&provider.as_str()) {
        return respond::detail(StatusCode::BAD_REQUEST, "unknown subscription provider");
    }
    let started = tokio::task::spawn_blocking(move || {
        agent_hire_llm::subscription_login(&provider).unwrap_or_else(|detail| {
            json!({"started": false, "error": "not_installed", "hint": agent_hire_llm::install_hint(&provider), "detail": detail})
        })
    })
    .await;
    match started {
        Ok(value) => Json(value).into_response(),
        Err(error) => respond::internal_error("subscription login", &error),
    }
}

/// The stored settings, for other routes.
pub async fn current(state: &AppState) -> Result<Settings, Response> {
    settings_snapshot(state, None).await
}
