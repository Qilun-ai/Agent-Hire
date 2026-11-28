//! Listing a provider's models live from its API.

use std::time::Duration;

use reqwest::header::HeaderMap;
use serde_json::Value;

use crate::config::Settings;
use crate::endpoint::resolve_endpoints;
use crate::http;
use crate::provider::Provider;

const TIMEOUT: Duration = Duration::from_secs(8);

/// The model ids `provider` reports for `key`, sorted case-insensitively. Every failure (no
/// endpoint, network error, non-2xx answer) gives an empty list.
pub async fn list_model_ids(provider: &str, settings: &Settings, key: &str) -> Vec<String> {
    let Some((url, headers)) = models_request(provider, settings, key) else {
        return Vec::new();
    };
    let Ok(response) = http::http_client(TIMEOUT)
        .get(url)
        .headers(headers)
        .send()
        .await
    else {
        return Vec::new();
    };
    if !response.status().is_success() {
        return Vec::new();
    }
    let Ok(body) = response.json::<Value>().await else {
        return Vec::new();
    };
    extract_ids(&body)
}

fn models_request(provider: &str, settings: &Settings, key: &str) -> Option<(String, HeaderMap)> {
    let mut headers = HeaderMap::new();
    let bearer = format!("Bearer {key}");
    let url = match provider {
        "anthropic" => {
            let base = settings
                .get("anthropic_base_url")
                .trim()
                .trim_end_matches('/');
            let base = base
                .strip_suffix("/v1/messages")
                .map(|root| format!("{root}/v1"))
                .or_else(|| base.strip_suffix("/messages").map(str::to_owned))
                .unwrap_or_else(|| base.to_owned());
            http::insert(&mut headers, "x-api-key", key);
            http::insert(&mut headers, "authorization", &bearer);
            http::insert(&mut headers, "anthropic-version", "2023-06-01");
            http::insert(&mut headers, "user-agent", "Mozilla/5.0");
            if base.is_empty() {
                "https://api.anthropic.com/v1/models".to_owned()
            } else {
                format!("{base}/models")
            }
        }
        "custom" => {
            let endpoint = settings.custom_endpoint();
            http::insert(&mut headers, "authorization", &bearer);
            http::insert(&mut headers, "x-api-key", key);
            format!(
                "{}/models",
                resolve_endpoints(&endpoint.url, endpoint.endpoint_type).base_url
            )
        }
        "opencode" => {
            let endpoint = settings.opencode_endpoint();
            headers = http::opencode_headers(key, false);
            format!(
                "{}/models",
                resolve_endpoints(&endpoint.url, endpoint.endpoint_type).base_url
            )
        }
        "azure" => {
            let base = settings.provider_base_url("azure").ok()?;
            http::insert(&mut headers, "api-key", key);
            format!("{base}/models")
        }
        "openai" => {
            http::insert(&mut headers, "authorization", &bearer);
            "https://api.openai.com/v1/models".to_owned()
        }
        "gemini" => {
            http::insert(&mut headers, "authorization", &bearer);
            "https://generativelanguage.googleapis.com/v1beta/openai/models".to_owned()
        }
        "deepseek" => {
            http::insert(&mut headers, "authorization", &bearer);
            "https://api.deepseek.com/models".to_owned()
        }
        other => {
            let base = Provider::find(other)?.base_url?;
            http::insert(&mut headers, "authorization", &bearer);
            format!("{}/models", base.trim_end_matches('/'))
        }
    };
    Some((url, headers))
}

/// Ids from `{"data": [...]}`, `{"models": [...]}` or a bare list; rows may be strings or
/// objects with `id`, `name` or `model`.
fn extract_ids(body: &Value) -> Vec<String> {
    let rows = body
        .get("data")
        .or_else(|| body.get("models"))
        .unwrap_or(body)
        .as_array()
        .cloned()
        .unwrap_or_default();
    let mut ids: Vec<String> = Vec::new();
    for row in rows {
        let id = match &row {
            Value::String(id) => id.clone(),
            Value::Object(fields) => ["id", "name", "model"]
                .iter()
                .find_map(|key| fields.get(*key).and_then(Value::as_str))
                .unwrap_or("")
                .to_owned(),
            _ => String::new(),
        };
        let id = id.trim().to_owned();
        if !id.is_empty() && !ids.iter().any(|seen| seen.eq_ignore_ascii_case(&id)) {
            ids.push(id);
        }
    }
    ids.sort_by_key(|id| id.to_lowercase());
    ids
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn extracts_ids_from_common_shapes() {
        let body = json!({"data": [{"id": "b"}, "A", {"name": "c"}, {"id": "a"}, {}]});
        assert_eq!(extract_ids(&body), ["A", "b", "c"]);
        assert_eq!(extract_ids(&json!(["x"])), ["x"]);
    }
}
