//! Checking that a provider key works ("Validate" in Settings → AI model).

use std::time::{Duration, Instant};

use reqwest::StatusCode;
use reqwest::header::HeaderMap;
use serde_json::{Map, Value, json};

use crate::config::Settings;
use crate::endpoint::{EndpointConfig, EndpointType, resolve_endpoints};
use crate::http;
use crate::provider::{KEYED_PROVIDERS, Provider};

const TIMEOUT: Duration = Duration::from_secs(20);

/// The providers to validate: every keyed provider with a key, in table order, and the active
/// provider first if it has no key.
pub fn providers_to_validate(settings: &Settings) -> Vec<&'static str> {
    let mut out: Vec<&'static str> = KEYED_PROVIDERS
        .iter()
        .filter(|provider| !settings.provider_key(provider.id).is_empty())
        .map(|provider| provider.id)
        .collect();
    let active = settings.get("llm_provider").trim().to_lowercase();
    if let Some(found) = Provider::find(&active)
        && !out.contains(&found.id)
    {
        out.insert(0, found.id);
    }
    out
}

/// Probes one provider with a minimal request and reports `{status, latency_ms, ...}`.
pub async fn probe_provider(provider: &str, settings: &Settings) -> Value {
    let key = settings.provider_key(provider);
    if key.is_empty() {
        return json!({ "status": "not_configured", "latency_ms": 0, "detail": "No API key configured" });
    }
    match provider {
        "anthropic" => {
            let base = settings
                .get("anthropic_base_url")
                .trim()
                .trim_end_matches('/')
                .to_owned();
            let url = if base.is_empty() {
                "https://api.anthropic.com/v1/messages".to_owned()
            } else {
                format!("{base}/messages")
            };
            let model = settings.get_or("anthropic_model", "claude-haiku-4-5-20251001");
            let mut headers = HeaderMap::new();
            http::insert(&mut headers, "x-api-key", &key);
            http::insert(&mut headers, "authorization", &format!("Bearer {key}"));
            http::insert(&mut headers, "anthropic-version", "2023-06-01");
            http::insert(&mut headers, "content-type", "application/json");
            http::insert(&mut headers, "user-agent", "Mozilla/5.0");
            let body = json!({"model": model, "max_tokens": 15, "messages": [{"role": "user", "content": "ping"}]});
            chat_probe(&url, headers, body, model, Flavour::Plain).await
        }
        "openai" => {
            let model = settings.get_or("openai_model", "gpt-4o-mini");
            let mut headers = HeaderMap::new();
            http::insert(&mut headers, "authorization", &format!("Bearer {key}"));
            http::insert(&mut headers, "content-type", "application/json");
            let body = json!({"model": model, "max_tokens": 15, "messages": [{"role": "user", "content": "ping"}]});
            chat_probe(
                "https://api.openai.com/v1/chat/completions",
                headers,
                body,
                model,
                Flavour::Plain,
            )
            .await
        }
        "groq" => {
            list_probe(
                "https://api.groq.com/openai/v1/models",
                bearer(&key),
                ListRule::Only401,
            )
            .await
        }
        "gemini" => {
            list_probe(
                "https://generativelanguage.googleapis.com/v1beta/openai/models",
                bearer(&key),
                ListRule::Auth,
            )
            .await
        }
        "deepseek" => {
            list_probe(
                "https://api.deepseek.com/models",
                bearer(&key),
                ListRule::Auth,
            )
            .await
        }
        "openrouter" => {
            list_probe(
                "https://openrouter.ai/api/v1/auth/key",
                bearer(&key),
                ListRule::Auth,
            )
            .await
        }
        "azure" => {
            let Ok(base) = settings.provider_base_url("azure") else {
                return json!({ "status": "unchecked", "latency_ms": 0 });
            };
            let mut headers = HeaderMap::new();
            http::insert(&mut headers, "api-key", &key);
            list_probe(&format!("{base}/models"), headers, ListRule::Auth).await
        }
        "opencode" => {
            let endpoint = settings.opencode_endpoint();
            let model = settings
                .get_or("opencode_model", "muse-spark-1.3-contributor")
                .to_owned();
            let anthropic = endpoint.endpoint_type == EndpointType::Messages;
            endpoint_probe(
                &endpoint,
                http::opencode_headers(&key, anthropic),
                &model,
                Flavour::OpenCode,
            )
            .await
        }
        "custom" => {
            let url = settings.get_or_env("custom_base_url", "OPENAI_COMPAT_BASE_URL", "");
            if url.trim().is_empty() {
                return json!({ "status": "endpoint_not_found", "latency_ms": 0, "detail": "Custom endpoint URL is missing" });
            }
            let model = settings.get("custom_model").trim().to_owned();
            if model.is_empty() {
                return json!({ "status": "missing_model", "latency_ms": 0, "detail": "Please specify a model name to test" });
            }
            let endpoint = settings.custom_endpoint();
            let mut headers = HeaderMap::new();
            http::insert(&mut headers, "authorization", &format!("Bearer {key}"));
            http::insert(&mut headers, "content-type", "application/json");
            http::insert(&mut headers, "user-agent", http::BROWSER_USER_AGENT);
            if endpoint.endpoint_type == EndpointType::Messages {
                http::insert(&mut headers, "x-api-key", &key);
                http::insert(&mut headers, "anthropic-version", "2023-06-01");
            }
            endpoint_probe(&endpoint, headers, &model, Flavour::Custom).await
        }
        other => match Provider::find(other).and_then(|found| found.base_url) {
            Some(base) => {
                list_probe(
                    &format!("{}/models", base.trim_end_matches('/')),
                    bearer(&key),
                    ListRule::Auth,
                )
                .await
            }
            None => json!({ "status": "unchecked", "latency_ms": 0 }),
        },
    }
}

/// How a chat-style probe reports success and timeouts.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Flavour {
    Plain,
    OpenCode,
    Custom,
}

/// Which statuses a model-list probe maps to `invalid_key`.
#[derive(Clone, Copy, PartialEq, Eq)]
enum ListRule {
    Only401,
    Auth,
}

fn bearer(key: &str) -> HeaderMap {
    let mut headers = HeaderMap::new();
    http::insert(&mut headers, "authorization", &format!("Bearer {key}"));
    headers
}

async fn endpoint_probe(
    endpoint: &EndpointConfig,
    headers: HeaderMap,
    model: &str,
    flavour: Flavour,
) -> Value {
    let target = resolve_endpoints(&endpoint.url, endpoint.endpoint_type).target_url;
    let effort = matches!(endpoint.reasoning.as_str(), "low" | "medium" | "high")
        .then_some(endpoint.reasoning.as_str());
    let mut body = Map::new();
    body.insert("model".to_owned(), json!(model));
    match endpoint.endpoint_type {
        EndpointType::ChatCompletions => {
            body.insert("max_tokens".to_owned(), json!(15));
            body.insert(
                "messages".to_owned(),
                json!([{"role": "user", "content": "ping"}]),
            );
            if let Some(effort) = effort {
                body.insert("reasoning_effort".to_owned(), json!(effort));
            }
        }
        EndpointType::Responses => {
            body.insert("input".to_owned(), json!("ping"));
            body.insert("max_output_tokens".to_owned(), json!(16));
            if let Some(effort) = effort {
                body.insert("reasoning".to_owned(), json!({ "effort": effort }));
            }
        }
        EndpointType::Messages => {
            body.insert(
                "messages".to_owned(),
                json!([{"role": "user", "content": "ping"}]),
            );
            let mut max_tokens = 15;
            if let Some(effort) = effort {
                let budget = match effort {
                    "low" => 1024,
                    "medium" => 2048,
                    _ => 4096,
                };
                body.insert(
                    "thinking".to_owned(),
                    json!({ "type": "enabled", "budget_tokens": budget }),
                );
                max_tokens = budget + 500;
            }
            body.insert("max_tokens".to_owned(), json!(max_tokens));
        }
    }
    chat_probe(&target, headers, Value::Object(body), model, flavour).await
}

async fn chat_probe(
    url: &str,
    headers: HeaderMap,
    body: Value,
    model: &str,
    flavour: Flavour,
) -> Value {
    let started = Instant::now();
    let sent = http::http_client(TIMEOUT)
        .post(url)
        .headers(headers)
        .json(&body)
        .send()
        .await;
    let response = match sent {
        Ok(response) => response,
        Err(error) => {
            let detail = if error.is_timeout() {
                match flavour {
                    Flavour::OpenCode => {
                        "Connection timed out (server did not respond in 20s)".to_owned()
                    }
                    Flavour::Plain | Flavour::Custom => "Connection timed out".to_owned(),
                }
            } else if error.is_connect() && flavour == Flavour::OpenCode {
                "Could not connect to host (connection refused or DNS failed)".to_owned()
            } else {
                truncate(&error.to_string(), 120)
            };
            return json!({ "status": "unreachable", "latency_ms": latency(started), "model": model, "detail": detail });
        }
    };
    let status = response.status();
    let text = response.text().await.unwrap_or_default();
    let latency_ms = latency(started);
    if status.is_success() {
        let parsed: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
        let mut result = json!({ "status": "ok", "latency_ms": latency_ms, "model": model, "reply": reply_snippet(&parsed) });
        if flavour != Flavour::Plain {
            result["detail"] = json!(format!("Model '{model}' responded successfully"));
        }
        return result;
    }
    let (detail, kind) = error_detail(status, &text);
    json!({ "status": classify(status, &detail, &kind), "latency_ms": latency_ms, "model": model, "detail": detail })
}

async fn list_probe(url: &str, headers: HeaderMap, rule: ListRule) -> Value {
    let started = Instant::now();
    let sent = http::http_client(TIMEOUT)
        .get(url)
        .headers(headers)
        .send()
        .await;
    let response = match sent {
        Ok(response) => response,
        Err(error) => {
            let detail = if error.is_timeout() {
                "Connection timed out".to_owned()
            } else {
                truncate(&error.to_string(), 120)
            };
            return json!({ "status": "unreachable", "latency_ms": latency(started), "detail": detail });
        }
    };
    let status = response.status();
    let text = response.text().await.unwrap_or_default();
    let latency_ms = latency(started);
    if status == StatusCode::OK {
        return json!({ "status": "ok", "latency_ms": latency_ms, "detail": "" });
    }
    let invalid = match rule {
        ListRule::Only401 => status == StatusCode::UNAUTHORIZED,
        ListRule::Auth => status == StatusCode::UNAUTHORIZED || status == StatusCode::FORBIDDEN,
    };
    let (detail, _) = error_detail(status, &text);
    json!({ "status": if invalid { "invalid_key" } else { "unreachable" }, "latency_ms": latency_ms, "detail": detail })
}

fn latency(started: Instant) -> u64 {
    // Rounded to the nearest millisecond.
    let micros = started.elapsed().as_micros();
    u64::try_from((micros + 500) / 1000).unwrap_or(u64::MAX)
}

/// The first 70 characters of the reply text, whitespace collapsed.
fn reply_snippet(body: &Value) -> String {
    let mut text = String::new();
    if let Some(content) = body["choices"][0]["message"]["content"].as_str() {
        text.push_str(content);
    }
    if text.is_empty()
        && let Some(output) = body["output"].as_array()
    {
        for item in output {
            for part in item["content"].as_array().into_iter().flatten() {
                text.push_str(part["text"].as_str().unwrap_or(""));
            }
        }
    }
    if text.is_empty()
        && let Some(content) = body["content"].as_array()
    {
        for part in content {
            text.push_str(
                part["text"]
                    .as_str()
                    .or_else(|| part["thinking"].as_str())
                    .unwrap_or(""),
            );
        }
    }
    let collapsed = text.split_whitespace().collect::<Vec<_>>().join(" ");
    collapsed.chars().take(70).collect()
}

/// A readable error from a failed response, and the error type when the body names one.
fn error_detail(status: StatusCode, text: &str) -> (String, String) {
    if let Ok(body) = serde_json::from_str::<Value>(text) {
        let error = &body["error"];
        let detail = [
            &error["message"],
            &error["detail"],
            &body["detail"],
            &body["message"],
        ]
        .into_iter()
        .find_map(|value| {
            value
                .as_str()
                .filter(|text| !text.is_empty())
                .map(str::to_owned)
        })
        .or_else(|| error.as_str().map(str::to_owned))
        .unwrap_or_else(|| truncate(text, 150));
        let kind = error["type"]
            .as_str()
            .or_else(|| body["type"].as_str())
            .unwrap_or("")
            .to_owned();
        return (detail, kind);
    }
    let trimmed = text.trim_start().to_lowercase();
    if trimmed.starts_with("<!doctype") || trimmed.starts_with("<html") {
        return (
            format!(
                "HTTP {} (Endpoint returned non-API HTML page)",
                status.as_u16()
            ),
            String::new(),
        );
    }
    (truncate(text, 150), String::new())
}

fn classify(status: StatusCode, detail: &str, kind: &str) -> &'static str {
    let combined = format!("{detail} {kind}").to_lowercase();
    let model_missing = combined.contains("model")
        && [
            "not found",
            "not supported",
            "does not exist",
            "unknown model",
            "invalid model",
        ]
        .iter()
        .any(|marker| combined.contains(marker));
    if model_missing {
        return "model_not_found";
    }
    match status.as_u16() {
        401 | 403 => "invalid_key",
        404 => "endpoint_not_found",
        429 => "rate_limited",
        400 => "invalid_format",
        500.. => "server_error",
        _ => "unreachable",
    }
}

fn truncate(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classification() {
        assert_eq!(
            classify(StatusCode::NOT_FOUND, "The model `x` does not exist", ""),
            "model_not_found"
        );
        assert_eq!(
            classify(StatusCode::UNAUTHORIZED, "bad key", ""),
            "invalid_key"
        );
        assert_eq!(classify(StatusCode::BAD_GATEWAY, "", ""), "server_error");
        assert_eq!(classify(StatusCode::IM_A_TEAPOT, "", ""), "unreachable");
    }

    #[test]
    fn details() {
        let (detail, kind) = error_detail(
            StatusCode::UNAUTHORIZED,
            r#"{"error":{"message":"nope","type":"auth"}}"#,
        );
        assert_eq!((detail.as_str(), kind.as_str()), ("nope", "auth"));
        let (detail, _) = error_detail(StatusCode::NOT_FOUND, "<!DOCTYPE html><html>");
        assert_eq!(detail, "HTTP 404 (Endpoint returned non-API HTML page)");
    }

    #[test]
    fn snippets() {
        let body = json!({"choices": [{"message": {"content": "  pong \n pong  "}}]});
        assert_eq!(reply_snippet(&body), "pong pong");
    }
}
