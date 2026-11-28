//! Routes only the agent (apps/agent) calls.

use std::env;
use std::sync::Arc;

use agent_hire_llm::{SUBSCRIPTION_CLI_PROVIDERS, Settings, provider_needs_key};
use axum::Json;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};

use crate::app_state::AppState;
use crate::respond;
use crate::settings_routes;
use crate::token;

/// Providers whose agent base URL is fixed (the agent speaks OpenAI Chat Completions to them).
const FIXED_BASE_URLS: [(&str, &str); 5] = [
    ("openai", "https://api.openai.com/v1"),
    ("groq", "https://api.groq.com/openai/v1"),
    (
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
    ),
    ("nvidia", "https://integrate.api.nvidia.com/v1"),
    ("deepseek", "https://api.deepseek.com/v1"),
];

/// `GET /api/v1/agent-internal/llm-config`: the resolved provider endpoint and key for the agent.
///
/// This is the one route that returns a secret, so besides the bearer token it needs the
/// `X-Agent-Secret` header to match `AGENT_HIRE_AGENT_SECRET`; otherwise it does not exist (404).
pub async fn llm_config(State(state): State<Arc<AppState>>, headers: HeaderMap) -> Response {
    let expected = env::var("AGENT_HIRE_AGENT_SECRET").unwrap_or_default();
    let offered = headers
        .get("x-agent-secret")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    if expected.is_empty()
        || offered.is_empty()
        || !token::constant_time_eq(expected.as_bytes(), offered.as_bytes())
    {
        return respond::detail(StatusCode::NOT_FOUND, "Not Found");
    }
    let settings = match settings_routes::current(&state).await {
        Ok(settings) => settings,
        Err(response) => return response,
    };
    // Base-URL checks resolve host names, so they run on the blocking pool.
    let resolved = tokio::task::spawn_blocking(move || resolve_agent_llm(&settings)).await;
    match resolved {
        Ok(value) => Json(value).into_response(),
        Err(error) => respond::internal_error("llm-config", &error),
    }
}

/// `{provider, api, base_url, api_key, model, reasoning}`, or `{error, provider?, message}`.
fn resolve_agent_llm(settings: &Settings) -> Value {
    let config = settings.resolve(Some("agent"));
    let explicit = settings.get_or("agent_provider", settings.get("llm_provider"));
    if explicit.is_empty() {
        // The resolver falls back to Ollama when nothing was chosen; for the agent that silent
        // default reads as "configured" while every request fails. Ask for a real choice instead.
        return json!({ "error": "not_configured", "message": "Choose an AI provider and paste its API key in Settings → AI model." });
    }
    let provider = config.provider.as_str();
    if SUBSCRIPTION_CLI_PROVIDERS.contains(&provider) {
        return json!({
            "error": "subscription_cli_unsupported",
            "provider": provider,
            "message": "The agent needs an API-key provider (or Ollama); subscription CLIs cannot stream tool calls.",
        });
    }
    if provider_needs_key(provider) && config.api_key.is_empty() {
        return json!({
            "error": "missing_key",
            "provider": provider,
            "message": format!("No API key is configured for {provider}. Add it in Settings → AI provider."),
        });
    }
    let mut api = "openai-completions";
    let mut reasoning = String::new();
    let base = match provider {
        "anthropic" => {
            api = "anthropic-messages";
            Ok("https://api.anthropic.com".to_owned())
        }
        "ollama" => Ok(settings
            .get_or("ollama_url", "http://localhost:11434/v1")
            .trim_end_matches('/')
            .to_owned()),
        "custom" | "opencode" => {
            let endpoint = if provider == "custom" {
                settings.custom_endpoint()
            } else {
                settings.opencode_endpoint()
            };
            reasoning = endpoint.reasoning.clone();
            settings.provider_base_url(provider).map(|base| {
                let anthropic = endpoint.protocol == "anthropic"
                    || endpoint.endpoint_type == agent_hire_llm::EndpointType::Messages;
                if anthropic {
                    api = "anthropic-messages";
                    base.strip_suffix("/v1").map(str::to_owned).unwrap_or(base)
                } else {
                    base
                }
            })
        }
        other => match FIXED_BASE_URLS.iter().find(|(id, _)| *id == other) {
            Some((_, base)) => Ok((*base).to_owned()),
            None => settings.provider_base_url(other),
        },
    };
    let base = match base {
        Ok(base) => base,
        Err(message) => {
            return json!({ "error": "bad_endpoint", "provider": provider, "message": message });
        }
    };
    let api_key = if config.api_key.is_empty() {
        "ollama".to_owned()
    } else {
        config.api_key.clone()
    };
    json!({
        "provider": provider,
        "api": api,
        "base_url": base,
        "api_key": api_key,
        "model": config.model,
        "reasoning": reasoning,
    })
}
