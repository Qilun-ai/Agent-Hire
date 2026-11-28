//! OpenAI-compatible tool-calling relay for the extension's in-page agent.
//!
//! The extension holds no model keys, so its agent posts OpenAI chat-completions requests (with
//! function tools) to the local API and this module replays them on the configured provider:
//! Responses-API gateways get the request translated to `input`/`function_call` items and back;
//! OpenAI-compatible chat endpoints get it forwarded almost unchanged; providers that cannot do
//! function calls (Anthropic Messages, subscription CLIs) fail with an actionable error.

use std::fmt;
use std::sync::LazyLock;
use std::time::Duration;

use reqwest::header::HeaderMap;
use serde_json::{Map, Value, json};

use crate::config::Settings;
use crate::endpoint::{EndpointType, resolve_endpoints};
use crate::http;
use crate::provider::{Provider, provider_needs_key};

const MAX_MESSAGES: usize = 200;
const MAX_TOOLS: usize = 64;

static CLIENT: LazyLock<reqwest::Client> =
    LazyLock::new(|| http::http_client(Duration::from_secs(120)));

/// The provider cannot serve the request; the message is safe to show the page.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolProxyError(pub String);

impl fmt::Display for ToolProxyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for ToolProxyError {}

fn is_chat_provider(provider: &str) -> bool {
    matches!(
        provider,
        "azure"
            | "opencode"
            | "custom"
            | "openai"
            | "deepseek"
            | "groq"
            | "gemini"
            | "nvidia"
            | "ollama"
    ) || Provider::find(provider).is_some_and(|found| found.base_url.is_some())
}

fn flatten_text(content: &Value) -> String {
    match content {
        Value::String(text) => text.clone(),
        Value::Array(items) => items
            .iter()
            .filter_map(|item| match item {
                Value::String(text) => Some(text.clone()),
                Value::Object(map) => map
                    .get("text")
                    .and_then(Value::as_str)
                    .filter(|t| !t.is_empty())
                    .map(str::to_owned),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("\n"),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

fn text_of(value: Option<&Value>, default: &str) -> String {
    match value {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        Some(Value::Null) | None => default.to_owned(),
        Some(Value::String(_)) => default.to_owned(),
        Some(other) => other.to_string(),
    }
}

fn tool_to_responses(tool: &Map<String, Value>) -> Value {
    let function = tool
        .get("function")
        .and_then(Value::as_object)
        .unwrap_or(tool);
    json!({
        "type": "function",
        "name": text_of(function.get("name"), ""),
        "description": text_of(function.get("description"), ""),
        "parameters": function.get("parameters").filter(|p| !p.is_null()).cloned().unwrap_or_else(|| json!({"type": "object", "properties": {}})),
    })
}

/// A chat-completions request as a Responses API request.
pub fn chat_to_responses(
    body: &Map<String, Value>,
    model: &str,
    reasoning: &str,
) -> Result<Value, ToolProxyError> {
    let messages = body
        .get("messages")
        .and_then(Value::as_array)
        .ok_or_else(|| ToolProxyError("messages must be a list".to_owned()))?;
    let mut instructions: Vec<String> = Vec::new();
    let mut input: Vec<Value> = Vec::new();
    for message in messages
        .iter()
        .take(MAX_MESSAGES)
        .filter_map(Value::as_object)
    {
        let role = text_of(message.get("role"), "user");
        match role.as_str() {
            "system" | "developer" => {
                let text = flatten_text(message.get("content").unwrap_or(&Value::Null));
                if !text.is_empty() {
                    instructions.push(text);
                }
            }
            "tool" => input.push(json!({
                "type": "function_call_output",
                "call_id": text_of(message.get("tool_call_id"), ""),
                "output": flatten_text(message.get("content").unwrap_or(&Value::Null)),
            })),
            "assistant" => {
                let text = flatten_text(message.get("content").unwrap_or(&Value::Null));
                if !text.is_empty() {
                    input.push(json!({"role": "assistant", "content": text}));
                }
                let calls = message.get("tool_calls").and_then(Value::as_array).cloned().unwrap_or_default();
                for call in calls.iter().take(MAX_TOOLS).filter_map(Value::as_object) {
                    let empty = Map::new();
                    let function = call.get("function").and_then(Value::as_object).unwrap_or(&empty);
                    input.push(json!({
                        "type": "function_call",
                        "name": text_of(function.get("name"), ""),
                        "arguments": text_of(function.get("arguments"), "{}"),
                        "call_id": text_of(call.get("id"), ""),
                    }));
                }
            }
            _ => input.push(json!({"role": "user", "content": flatten_text(message.get("content").unwrap_or(&Value::Null))})),
        }
    }
    let mut payload = Map::new();
    payload.insert("model".to_owned(), json!(model));
    payload.insert("input".to_owned(), Value::Array(input));
    if !instructions.is_empty() {
        payload.insert("instructions".to_owned(), json!(instructions.join("\n\n")));
    }
    if let Some(tools) = body
        .get("tools")
        .and_then(Value::as_array)
        .filter(|tools| !tools.is_empty())
    {
        let converted: Vec<Value> = tools
            .iter()
            .take(MAX_TOOLS)
            .filter_map(Value::as_object)
            .map(tool_to_responses)
            .collect();
        payload.insert("tools".to_owned(), Value::Array(converted));
    }
    // The Responses gateways accept only "auto"; the agent's prompt and retries make it call tools.
    if body
        .get("tool_choice")
        .is_some_and(|choice| !choice.is_null())
    {
        payload.insert("tool_choice".to_owned(), json!("auto"));
    }
    if matches!(reasoning, "low" | "medium" | "high") {
        payload.insert("reasoning".to_owned(), json!({"effort": reasoning}));
    }
    Ok(Value::Object(payload))
}

/// A Responses API answer as a chat-completions answer.
pub fn responses_to_chat(data: &Value, model: &str) -> Value {
    let mut text = String::new();
    let mut calls: Vec<Value> = Vec::new();
    for item in data["output"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_object)
    {
        match item.get("type").and_then(Value::as_str) {
            Some("message") => {
                for content in item.get("content").and_then(Value::as_array).into_iter().flatten() {
                    if let Some(part) = content.get("text").filter(|t| !t.is_null() && t.as_str() != Some("")) {
                        text.push_str(&text_of(Some(part), ""));
                    }
                }
            }
            Some("function_call") => calls.push(json!({
                "id": text_of(item.get("call_id").filter(|v| v.as_str() != Some("")).or_else(|| item.get("id")), ""),
                "type": "function",
                "function": {"name": text_of(item.get("name"), ""), "arguments": text_of(item.get("arguments"), "{}")},
            })),
            _ => {}
        }
    }
    let mut message = json!({"role": "assistant", "content": text});
    let finish = if calls.is_empty() {
        "stop"
    } else {
        "tool_calls"
    };
    if !calls.is_empty() {
        message["tool_calls"] = Value::Array(calls);
    }
    json!({
        "id": text_of(data.get("id"), ""),
        "object": "chat.completion",
        "created": data["created_at"].as_i64().or_else(|| data["created_at"].as_f64().map(|f| f as i64)).unwrap_or(0),
        "model": text_of(data.get("model"), model),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": data.get("usage").filter(|u| !u.is_null()).cloned().unwrap_or_else(|| json!({})),
    })
}

fn safe_error(provider: &str, status: Option<u16>, kind: &str) -> ToolProxyError {
    let label = status.map_or_else(|| kind.to_owned(), |status| format!("HTTP {status}"));
    ToolProxyError(format!(
        "provider '{provider}' rejected the agent request ({label})"
    ))
}

/// Serves one chat-completions tool call on the configured provider.
pub async fn chat_completions(
    settings: &Settings,
    body: &Map<String, Value>,
) -> Result<Value, ToolProxyError> {
    if body
        .get("messages")
        .and_then(Value::as_array)
        .is_none_or(Vec::is_empty)
    {
        return Err(ToolProxyError("messages are required".to_owned()));
    }
    if body.get("stream") == Some(&Value::Bool(true)) {
        return Err(ToolProxyError("streaming is not supported".to_owned()));
    }
    let config = settings.resolve(None);
    let provider = config.provider.as_str();
    if provider_needs_key(provider) && config.api_key.is_empty() {
        return Err(ToolProxyError(format!(
            "No API key configured for provider '{provider}'."
        )));
    }
    if !is_chat_provider(provider) {
        return Err(ToolProxyError(format!(
            "Provider '{provider}' does not speak the OpenAI tool-calling protocol. Choose an OpenAI-compatible provider \
             (or a different model) in Settings to use agent fill."
        )));
    }
    let endpoint = match provider {
        "opencode" => Some(settings.opencode_endpoint()),
        "custom" => Some(settings.custom_endpoint()),
        _ => None,
    };
    let key = &config.api_key;
    let (url, headers, payload, translate) =
        match endpoint.filter(|endpoint| endpoint.endpoint_type == EndpointType::Responses) {
            Some(endpoint) => {
                let payload = chat_to_responses(body, &config.model, &endpoint.reasoning)?;
                let url = resolve_endpoints(&endpoint.url, EndpointType::Responses).target_url;
                let headers = if provider == "opencode" {
                    http::opencode_headers(key, false)
                } else {
                    let mut headers = HeaderMap::new();
                    http::insert(&mut headers, "authorization", &format!("Bearer {key}"));
                    http::insert(&mut headers, "content-type", "application/json");
                    http::insert(&mut headers, "user-agent", http::BROWSER_USER_AGENT);
                    headers
                };
                (url, headers, payload, true)
            }
            None => {
                let base = match provider {
                    "openai" => "https://api.openai.com/v1".to_owned(),
                    "deepseek" => "https://api.deepseek.com".to_owned(),
                    "ollama" => settings
                        .get_or("ollama_url", "http://localhost:11434/v1")
                        .to_owned(),
                    other => settings
                        .provider_base_url(other)
                        .map_err(|_| safe_error(provider, None, "ValueError"))?,
                };
                let mut headers = HeaderMap::new();
                let bearer = if provider == "ollama" {
                    "ollama".to_owned()
                } else {
                    key.clone()
                };
                http::insert(&mut headers, "authorization", &format!("Bearer {bearer}"));
                http::insert(&mut headers, "content-type", "application/json");
                if !matches!(provider, "openai" | "deepseek" | "groq" | "ollama") {
                    http::insert(&mut headers, "user-agent", http::BROWSER_USER_AGENT);
                }
                if provider == "opencode" {
                    headers = http::opencode_headers(key, false);
                }
                if provider == "azure" {
                    http::insert(&mut headers, "api-key", key);
                }
                let mut payload = Map::new();
                payload.insert("model".to_owned(), json!(config.model));
                payload.insert("messages".to_owned(), body["messages"].clone());
                if let Some(tools) = body
                    .get("tools")
                    .and_then(Value::as_array)
                    .filter(|tools| !tools.is_empty())
                {
                    payload.insert(
                        "tools".to_owned(),
                        Value::Array(tools.iter().take(MAX_TOOLS).cloned().collect()),
                    );
                }
                for key in ["tool_choice", "temperature"] {
                    if let Some(value) = body.get(key).filter(|v| !v.is_null()) {
                        payload.insert(key.to_owned(), value.clone());
                    }
                }
                if provider == "nvidia" {
                    payload.insert("max_tokens".to_owned(), json!(16384));
                    payload.insert(
                        "chat_template_kwargs".to_owned(),
                        json!({"enable_thinking": false}),
                    );
                }
                (
                    format!("{}/chat/completions", base.trim_end_matches('/')),
                    headers,
                    Value::Object(payload),
                    false,
                )
            }
        };
    let response = CLIENT
        .post(url)
        .headers(headers)
        .json(&payload)
        .send()
        .await
        .map_err(|error| {
            safe_error(
                provider,
                None,
                if error.is_timeout() {
                    "APITimeoutError"
                } else {
                    "APIConnectionError"
                },
            )
        })?;
    let status = response.status();
    if !status.is_success() {
        return Err(safe_error(provider, Some(status.as_u16()), ""));
    }
    let data: Value = response
        .json()
        .await
        .map_err(|_| safe_error(provider, None, "JSONDecodeError"))?;
    if translate {
        return Ok(responses_to_chat(&data, &config.model));
    }
    let mut data = data;
    if data["choices"].as_array().is_none_or(Vec::is_empty) {
        return Err(ToolProxyError(format!(
            "provider '{provider}' returned no choices"
        )));
    }
    if data.get("object").is_none() {
        data["object"] = json!("chat.completion");
    }
    Ok(data)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn translates_both_ways() {
        let body = json!({
            "messages": [
                {"role": "system", "content": "Fill the form."},
                {"role": "user", "content": [{"type": "text", "text": "Page: ..."}]},
                {"role": "assistant", "content": null, "tool_calls": [{"id": "c1", "function": {"name": "click", "arguments": "{\"i\":1}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "ok"}
            ],
            "tools": [{"type": "function", "function": {"name": "click", "parameters": {"type": "object"}}}],
            "tool_choice": "required"
        });
        let payload =
            chat_to_responses(body.as_object().expect("object"), "m", "high").expect("translates");
        assert_eq!(payload["instructions"], json!("Fill the form."));
        assert_eq!(payload["input"][1]["type"], json!("function_call"));
        assert_eq!(payload["input"][2]["output"], json!("ok"));
        assert_eq!(payload["tool_choice"], json!("auto"));
        assert_eq!(payload["reasoning"], json!({"effort": "high"}));
        let answer = json!({"id": "r", "output": [{"type": "function_call", "call_id": "c2", "name": "type", "arguments": "{}"}]});
        let chat = responses_to_chat(&answer, "m");
        assert_eq!(chat["choices"][0]["finish_reason"], json!("tool_calls"));
        assert_eq!(
            chat["choices"][0]["message"]["tool_calls"][0]["id"],
            json!("c2")
        );
    }
}
