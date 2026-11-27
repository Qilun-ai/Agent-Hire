//! Calling the configured model: free text or JSON that follows a schema.
//!
//! Every hosted provider is reached over plain HTTP. Most speak OpenAI Chat Completions;
//! Anthropic speaks Messages; the custom and OpenCode endpoints may speak Chat Completions,
//! Responses or Messages. Transient failures (rate limits, 5xx, timeouts, refused connections)
//! are retried with back-off, and a model that rejects page images is asked again with text only.

use std::fmt;
use std::sync::LazyLock;
use std::time::Duration;

use reqwest::header::HeaderMap;
use serde_json::{Map, Value, json};

use crate::config::Settings;
use crate::endpoint::{EndpointConfig, EndpointType, resolve_endpoints};
use crate::http;
use crate::json_text::extract_json;
use crate::media::{PdfMedia, content_parts};
use crate::provider::{SUBSCRIPTION_CLI_PROVIDERS, provider_needs_key};

/// Retries after the first attempt.
const MAX_RETRIES: usize = 3;
/// Waits before retries 1-3, and the longer waits used when the provider rate-limits.
const BACKOFF_SECONDS: [u64; 3] = [1, 2, 4];
const RATE_LIMIT_BACKOFF_SECONDS: [u64; 3] = [5, 15, 30];

static CLIENT: LazyLock<reqwest::Client> =
    LazyLock::new(|| http::http_client(Duration::from_secs(120)));

/// What to ask.
#[derive(Clone, Copy, Debug, Default)]
pub struct LlmRequest<'a> {
    /// The system instruction; may be empty.
    pub system: &'a str,
    /// The user message.
    pub user: &'a str,
    /// The pipeline step, whose `<step>_provider`/`_api_key`/`_model` settings win when set.
    pub step: Option<&'a str>,
    /// Page images for models that read them.
    pub media: Option<&'a PdfMedia>,
}

/// The JSON shape a structured call must return.
#[derive(Clone, Debug, PartialEq)]
pub struct OutputSchema {
    /// A name for the shape (sent to Responses endpoints).
    pub name: String,
    /// A JSON Schema.
    pub schema: Value,
}

/// A call that did not produce an answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LlmError {
    /// The provider settings cannot work (bad endpoint, unsupported provider).
    Config(String),
    /// The provider answered with an error status.
    Status {
        /// The HTTP status.
        status: u16,
        /// The provider's message, or the start of its body.
        message: String,
    },
    /// The request did not complete (connection, timeout).
    Transport {
        /// What went wrong.
        message: String,
        /// Whether it was a timeout.
        timeout: bool,
    },
    /// The reply was not the JSON asked for.
    Parse(String),
}

impl fmt::Display for LlmError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LlmError::Config(message) | LlmError::Parse(message) => formatter.write_str(message),
            LlmError::Status { status, message } => {
                write!(formatter, "Error code: {status} - {message}")
            }
            LlmError::Transport { message, .. } => formatter.write_str(message),
        }
    }
}

impl std::error::Error for LlmError {}

impl LlmError {
    fn is_transient(&self) -> bool {
        match self {
            LlmError::Status { status, .. } => *status == 429 || (500..600).contains(status),
            LlmError::Transport { .. } => true,
            LlmError::Config(_) | LlmError::Parse(_) => false,
        }
    }

    fn is_rate_limit(&self) -> bool {
        matches!(self, LlmError::Status { status: 429, .. })
            || self.to_string().contains("Too Many Requests")
    }

    /// Whether the provider rejected the page images (a 4xx other than auth, not-found or
    /// rate-limit, or a message about images).
    fn is_media_rejection(&self) -> bool {
        let LlmError::Status { status, message } = self else {
            return false;
        };
        if !(400..500).contains(status) || matches!(status, 401 | 403 | 404 | 429) {
            return false;
        }
        let lowered = message.to_lowercase();
        matches!(status, 400 | 409 | 415 | 422)
            || [
                "image",
                "input_file",
                "input_image",
                "vision",
                "multimodal",
                "media",
                "content part",
            ]
            .iter()
            .any(|token| lowered.contains(token))
    }
}

/// Free-text answer. A provider that needs a key and has none answers `""` without a call.
pub async fn call_raw(settings: &Settings, request: &LlmRequest<'_>) -> Result<String, LlmError> {
    let result = with_retries(|| attempt_raw(settings, request)).await;
    match result {
        Err(error) if request.media.is_some() && error.is_media_rejection() => {
            let text_only = LlmRequest {
                media: None,
                ..*request
            };
            with_retries(|| attempt_raw(settings, &text_only)).await
        }
        other => other,
    }
}

/// A JSON value following `schema`. A provider that needs a key and has none answers `{}`
/// without a call (callers read missing fields as their defaults).
pub async fn call_structured(
    settings: &Settings,
    request: &LlmRequest<'_>,
    schema: &OutputSchema,
) -> Result<Value, LlmError> {
    let result = with_retries(|| attempt_structured(settings, request, schema)).await;
    match result {
        Err(error) if request.media.is_some() && error.is_media_rejection() => {
            let text_only = LlmRequest {
                media: None,
                ..*request
            };
            with_retries(|| attempt_structured(settings, &text_only, schema)).await
        }
        other => other,
    }
}

async fn with_retries<T, F, Fut>(mut attempt: F) -> Result<T, LlmError>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<T, LlmError>>,
{
    let mut tries = 0;
    loop {
        match attempt().await {
            Ok(value) => return Ok(value),
            Err(error) if error.is_transient() && tries < MAX_RETRIES => {
                let wait = if error.is_rate_limit() {
                    RATE_LIMIT_BACKOFF_SECONDS[tries]
                } else {
                    BACKOFF_SECONDS[tries]
                };
                tries += 1;
                tokio::time::sleep(Duration::from_secs(wait)).await;
            }
            Err(error) => return Err(error),
        }
    }
}

/// How one call reaches its provider.
enum Route {
    /// OpenAI Chat Completions at `url`.
    Chat {
        url: String,
        headers: HeaderMap,
        json_mode: bool,
        extra: Map<String, Value>,
    },
    /// Anthropic Messages at `url`.
    Messages {
        url: String,
        headers: HeaderMap,
        max_tokens: u64,
        thinking: Option<u64>,
    },
    /// OpenAI Responses at `url`.
    Responses {
        url: String,
        headers: HeaderMap,
        reasoning: Option<String>,
    },
}

fn reasoning_effort(endpoint: &EndpointConfig) -> Option<String> {
    matches!(endpoint.reasoning.as_str(), "low" | "medium" | "high")
        .then(|| endpoint.reasoning.clone())
}

fn thinking_budget(effort: &str) -> u64 {
    match effort {
        "low" => 1024,
        "medium" => 2048,
        _ => 4096,
    }
}

/// Where and how to send a call for `provider`.
fn route(settings: &Settings, provider: &str, key: &str) -> Result<Route, LlmError> {
    if SUBSCRIPTION_CLI_PROVIDERS.contains(&provider) {
        return Err(LlmError::Config(format!(
            "{provider} runs through a local CLI, which this server does not support yet"
        )));
    }
    let mut headers = HeaderMap::new();
    match provider {
        "anthropic" => {
            let configured = settings.get_or_env("anthropic_base_url", "ANTHROPIC_BASE_URL", "");
            let mut base = configured.trim().trim_end_matches('/').to_owned();
            if let Some(stripped) = base
                .strip_suffix("/v1/messages")
                .or_else(|| base.strip_suffix("/messages"))
            {
                base = stripped.to_owned();
            }
            if base.is_empty() {
                base = "https://api.anthropic.com".to_owned();
            }
            let base = base.strip_suffix("/v1").unwrap_or(&base).to_owned();
            http::insert(&mut headers, "x-api-key", key);
            http::insert(&mut headers, "anthropic-version", "2023-06-01");
            http::insert(&mut headers, "content-type", "application/json");
            Ok(Route::Messages {
                url: format!("{base}/v1/messages"),
                headers,
                max_tokens: 4096,
                thinking: None,
            })
        }
        "custom" | "opencode" => {
            let endpoint = if provider == "custom" {
                settings.custom_endpoint()
            } else {
                settings.opencode_endpoint()
            };
            let target = resolve_endpoints(&endpoint.url, endpoint.endpoint_type).target_url;
            let effort = reasoning_effort(&endpoint);
            let anthropic = endpoint.endpoint_type == EndpointType::Messages;
            if provider == "opencode" {
                headers = http::opencode_headers(key, anthropic);
            } else {
                http::insert(&mut headers, "authorization", &format!("Bearer {key}"));
                http::insert(&mut headers, "content-type", "application/json");
                http::insert(&mut headers, "user-agent", http::BROWSER_USER_AGENT);
                if anthropic {
                    http::insert(&mut headers, "x-api-key", key);
                    http::insert(&mut headers, "anthropic-version", "2023-06-01");
                }
            }
            Ok(match endpoint.endpoint_type {
                EndpointType::ChatCompletions => {
                    let mut extra = Map::new();
                    if let Some(effort) = effort {
                        extra.insert("reasoning_effort".to_owned(), json!(effort));
                    }
                    Route::Chat {
                        url: target,
                        headers,
                        json_mode: true,
                        extra,
                    }
                }
                EndpointType::Responses => Route::Responses {
                    url: target,
                    headers,
                    reasoning: effort,
                },
                EndpointType::Messages => {
                    let budget = effort.as_deref().map(thinking_budget);
                    let max_tokens = match (provider, budget) {
                        ("opencode", Some(budget)) => budget + 4096,
                        _ => 4096,
                    };
                    Route::Messages {
                        url: target,
                        headers,
                        max_tokens,
                        thinking: budget,
                    }
                }
            })
        }
        "ollama" => {
            let base = settings
                .get_or("ollama_url", "http://localhost:11434/v1")
                .trim_end_matches('/')
                .to_owned();
            http::insert(&mut headers, "authorization", "Bearer ollama");
            http::insert(&mut headers, "content-type", "application/json");
            Ok(Route::Chat {
                url: format!("{base}/chat/completions"),
                headers,
                json_mode: true,
                extra: Map::new(),
            })
        }
        other => {
            let base = settings
                .provider_base_url(other)
                .map_err(LlmError::Config)?;
            let base = base.trim_end_matches('/');
            http::insert(&mut headers, "authorization", &format!("Bearer {key}"));
            http::insert(&mut headers, "content-type", "application/json");
            if other != "openai" {
                http::insert(&mut headers, "user-agent", http::BROWSER_USER_AGENT);
            }
            if other == "azure" {
                http::insert(&mut headers, "api-key", key);
            }
            let mut extra = Map::new();
            if other == "nvidia" {
                extra.insert("max_tokens".to_owned(), json!(16384));
                extra.insert(
                    "chat_template_kwargs".to_owned(),
                    json!({"enable_thinking": false}),
                );
            }
            // Perplexity rejects a response format, so it only gets the schema instruction.
            let json_mode = other != "perplexity";
            Ok(Route::Chat {
                url: format!("{base}/chat/completions"),
                headers,
                json_mode,
                extra,
            })
        }
    }
}

/// The schema instruction added to the system prompt when a provider cannot enforce a schema.
fn schema_instruction(schema: &OutputSchema) -> String {
    let rendered = serde_json::to_string(&schema.schema).unwrap_or_default();
    format!(
        "Answer with one JSON value that validates against this JSON Schema, and nothing else: no prose, \
         no Markdown fences.\nJSON Schema:\n{rendered}"
    )
}

fn joined_system(system: &str, extra: &str) -> String {
    if system.is_empty() {
        extra.to_owned()
    } else {
        format!("{system}\n\n{extra}")
    }
}

/// The provider, key and model, or `None` when a needed key is missing.
fn resolved(settings: &Settings, step: Option<&str>) -> Option<(String, String, String)> {
    let config = settings.resolve(step);
    if provider_needs_key(&config.provider) && config.api_key.is_empty() {
        return None;
    }
    Some((config.provider, config.api_key, config.model))
}

async fn attempt_raw(settings: &Settings, request: &LlmRequest<'_>) -> Result<String, LlmError> {
    let Some((provider, key, model)) = resolved(settings, request.step) else {
        return Ok(String::new());
    };
    let route = route(settings, &provider, &key)?;
    send(
        &route,
        &model,
        request.system,
        request.user,
        request.media,
        None,
    )
    .await
}

async fn attempt_structured(
    settings: &Settings,
    request: &LlmRequest<'_>,
    schema: &OutputSchema,
) -> Result<Value, LlmError> {
    let Some((provider, key, model)) = resolved(settings, request.step) else {
        return Ok(json!({}));
    };
    let mut route = route(settings, &provider, &key)?;
    let first = send(
        &route,
        &model,
        request.system,
        request.user,
        request.media,
        Some(schema),
    )
    .await;
    let text = match first {
        Ok(text) => text,
        // Some endpoints refuse a response format or a native schema; ask once more without it,
        // keeping the schema in the instructions.
        Err(LlmError::Status { status: 400, .. }) if !matches!(route, Route::Messages { .. }) => {
            route = without_native_schema(route);
            send(
                &route,
                &model,
                request.system,
                request.user,
                request.media,
                Some(schema),
            )
            .await?
        }
        Err(error) => return Err(error),
    };
    match extract_json(&text) {
        Ok(value) => Ok(value),
        Err(_) => {
            // One re-ask with the schema spelled out, as a structured-output library would.
            let retry = send(
                &route,
                &model,
                request.system,
                request.user,
                request.media,
                Some(schema),
            )
            .await?;
            extract_json(&retry).map_err(LlmError::Parse)
        }
    }
}

fn without_native_schema(route: Route) -> Route {
    match route {
        Route::Chat {
            url,
            headers,
            extra,
            ..
        } => Route::Chat {
            url,
            headers,
            json_mode: false,
            extra,
        },
        Route::Responses {
            url,
            headers,
            reasoning,
        } => {
            let mut plain = headers;
            plain.insert(
                "x-agent-hire-plain",
                reqwest::header::HeaderValue::from_static("1"),
            );
            Route::Responses {
                url,
                headers: plain,
                reasoning,
            }
        }
        other => other,
    }
}

/// Sends one request and returns the reply text.
async fn send(
    route: &Route,
    model: &str,
    system: &str,
    user: &str,
    media: Option<&PdfMedia>,
    schema: Option<&OutputSchema>,
) -> Result<String, LlmError> {
    let (url, headers, body) = match route {
        Route::Chat {
            url,
            headers,
            json_mode,
            extra,
        } => {
            let system = match schema {
                Some(schema) => joined_system(system, &schema_instruction(schema)),
                None => system.to_owned(),
            };
            let mut messages = Vec::new();
            if !system.is_empty() {
                messages.push(json!({"role": "system", "content": system}));
            }
            messages.push(
                json!({"role": "user", "content": content_parts("chat/completions", user, media)}),
            );
            let mut body = Map::new();
            body.insert("model".to_owned(), json!(model));
            body.insert("messages".to_owned(), Value::Array(messages));
            body.insert("stream".to_owned(), json!(false));
            if schema.is_some() && *json_mode {
                body.insert("response_format".to_owned(), json!({"type": "json_object"}));
            }
            body.extend(extra.clone());
            (url, headers, Value::Object(body))
        }
        Route::Messages {
            url,
            headers,
            max_tokens,
            thinking,
        } => {
            let system = match schema {
                Some(schema) => joined_system(system, &schema_instruction(schema)),
                None => system.to_owned(),
            };
            let mut body = Map::new();
            body.insert("model".to_owned(), json!(model));
            body.insert("max_tokens".to_owned(), json!(max_tokens));
            if !system.is_empty() {
                body.insert("system".to_owned(), json!(system));
            }
            body.insert(
                "messages".to_owned(),
                json!([{"role": "user", "content": content_parts("messages", user, media)}]),
            );
            if let Some(budget) = thinking {
                body.insert(
                    "thinking".to_owned(),
                    json!({"type": "enabled", "budget_tokens": budget}),
                );
            }
            (url, headers, Value::Object(body))
        }
        Route::Responses {
            url,
            headers,
            reasoning,
        } => {
            let plain = headers.contains_key("x-agent-hire-plain");
            let mut body = Map::new();
            body.insert("model".to_owned(), json!(model));
            body.insert(
                "input".to_owned(),
                json!([{"role": "user", "content": content_parts("responses", user, media)}]),
            );
            let instructions = match schema {
                Some(schema) if plain => joined_system(system, &schema_instruction(schema)),
                _ => system.to_owned(),
            };
            if !instructions.is_empty() {
                body.insert("instructions".to_owned(), json!(instructions));
            }
            if let Some(effort) = reasoning {
                body.insert("reasoning".to_owned(), json!({"effort": effort}));
            }
            if let Some(schema) = schema
                && !plain
            {
                body.insert(
                    "text".to_owned(),
                    json!({"format": {"type": "json_schema", "name": schema.name, "schema": schema.schema}}),
                );
            }
            (url, headers, Value::Object(body))
        }
    };
    let response = CLIENT
        .post(url)
        .headers(headers.clone())
        .json(&body)
        .send()
        .await
        .map_err(|error| LlmError::Transport {
            timeout: error.is_timeout(),
            message: error.to_string(),
        })?;
    let status = response.status();
    let text = response.text().await.map_err(|error| LlmError::Transport {
        timeout: error.is_timeout(),
        message: error.to_string(),
    })?;
    if !status.is_success() {
        return Err(LlmError::Status {
            status: status.as_u16(),
            message: error_message(&text),
        });
    }
    let reply: Value = serde_json::from_str(&text)
        .map_err(|_| LlmError::Parse("the provider's reply is not JSON".to_owned()))?;
    Ok(reply_text(route, &reply))
}

fn error_message(body: &str) -> String {
    if let Ok(value) = serde_json::from_str::<Value>(body) {
        let error = &value["error"];
        if let Some(message) = error["message"]
            .as_str()
            .or_else(|| error.as_str())
            .or_else(|| value["detail"].as_str())
        {
            return message.to_owned();
        }
    }
    body.chars().take(300).collect()
}

/// The answer text of a reply.
fn reply_text(route: &Route, reply: &Value) -> String {
    match route {
        Route::Chat { .. } => reply["choices"][0]["message"]["content"]
            .as_str()
            .unwrap_or("")
            .to_owned(),
        Route::Messages { .. } => {
            let mut text = String::new();
            for block in reply["content"].as_array().into_iter().flatten() {
                if block["type"] == "text" || block.get("text").is_some() {
                    text.push_str(block["text"].as_str().unwrap_or(""));
                }
            }
            text.trim().to_owned()
        }
        Route::Responses { .. } => {
            let mut text = String::new();
            for item in reply["output"].as_array().into_iter().flatten() {
                if item["type"] != "message" {
                    continue;
                }
                for part in item["content"].as_array().into_iter().flatten() {
                    if matches!(part["type"].as_str(), Some("output_text" | "text")) {
                        text.push_str(part["text"].as_str().unwrap_or(""));
                    }
                }
            }
            text.trim().to_owned()
        }
    }
}

/// Whether the active provider (or the step's) needs a key it does not have.
pub fn missing_key(settings: &Settings, step: Option<&str>) -> Option<String> {
    let config = settings.resolve(step);
    (provider_needs_key(&config.provider) && config.api_key.is_empty()).then_some(config.provider)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transient_errors() {
        assert!(
            LlmError::Status {
                status: 429,
                message: String::new()
            }
            .is_transient()
        );
        assert!(
            LlmError::Status {
                status: 503,
                message: String::new()
            }
            .is_transient()
        );
        assert!(
            !LlmError::Status {
                status: 401,
                message: String::new()
            }
            .is_transient()
        );
        assert!(
            LlmError::Status {
                status: 400,
                message: "bad".to_owned()
            }
            .is_media_rejection()
        );
        assert!(
            !LlmError::Status {
                status: 429,
                message: "image".to_owned()
            }
            .is_media_rejection()
        );
    }

    #[tokio::test]
    async fn missing_key_answers_empty_without_a_call() {
        let settings = Settings::new([("llm_provider".to_owned(), "openai".to_owned())]);
        let request = LlmRequest {
            user: "hi",
            ..LlmRequest::default()
        };
        let schema = OutputSchema {
            name: "X".to_owned(),
            schema: json!({"type": "object"}),
        };
        assert_eq!(call_raw(&settings, &request).await, Ok(String::new()));
        assert_eq!(
            call_structured(&settings, &request, &schema).await,
            Ok(json!({}))
        );
    }
}
