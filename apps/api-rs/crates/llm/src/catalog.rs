//! The model catalogue from models.dev, with a bundled snapshot when it cannot be fetched.

use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::http;

const SOURCE_URL: &str = "https://models.dev/api.json";
const CACHE_FOR: Duration = Duration::from_secs(24 * 60 * 60);
const SNAPSHOT: &str = include_str!("../data/models_snapshot.json");

static LIVE: LazyLock<Mutex<Option<(Instant, Value)>>> = LazyLock::new(|| Mutex::new(None));
static BUNDLED: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str::<Value>(SNAPSHOT)
        .map(|value| value["providers"].clone())
        .unwrap_or(Value::Null)
});

/// The models.dev provider key for an app provider id.
fn models_dev_key(provider: &str) -> Option<&'static str> {
    let key = match provider {
        "anthropic" => "anthropic",
        "openai" => "openai",
        "gemini" => "google",
        "groq" => "groq",
        "deepseek" => "deepseek",
        "xai" => "xai",
        "kimi" => "moonshotai",
        "mistral" => "mistral",
        "openrouter" => "openrouter",
        "together" => "togetherai",
        "fireworks" => "fireworks-ai",
        "cerebras" => "cerebras",
        "perplexity" => "perplexity",
        "huggingface" => "huggingface",
        "cohere" => "cohere",
        "qwen" => "alibaba",
        "nvidia" => "nvidia",
        "azure" => "azure",
        _ => return None,
    };
    Some(key)
}

/// Catalogue lookups.
#[derive(Clone, Copy, Debug, Default)]
pub struct Catalog;

impl Catalog {
    /// Whether `provider` has catalogue entries at all.
    pub fn has(provider: &str) -> bool {
        models_dev_key(provider).is_some()
    }

    /// The provider's models, newest release first: `{id, name, release_date, reasoning,
    /// context, input, output}`. Live models.dev data (cached for a day) is preferred.
    pub async fn rows(provider: &str) -> Vec<Value> {
        let Some(key) = models_dev_key(provider) else {
            return Vec::new();
        };
        let live = live_providers().await;
        let source = live
            .as_ref()
            .and_then(|providers| providers.get(key))
            .filter(|entry| {
                entry["models"]
                    .as_object()
                    .is_some_and(|models| !models.is_empty())
            })
            .or_else(|| BUNDLED.get(key));
        source.map(normalise).unwrap_or_default()
    }
}

async fn live_providers() -> Option<Value> {
    if let Ok(cache) = LIVE.lock()
        && let Some((fetched, value)) = cache.as_ref()
        && fetched.elapsed() < CACHE_FOR
    {
        return Some(value.clone());
    }
    let response = http::http_client(Duration::from_secs(10))
        .get(SOURCE_URL)
        .header("user-agent", "Agent-Hire model catalog")
        .send()
        .await
        .ok()?;
    if !response.status().is_success() {
        return None;
    }
    let value: Value = response.json().await.ok()?;
    if let Ok(mut cache) = LIVE.lock() {
        *cache = Some((Instant::now(), value.clone()));
    }
    Some(value)
}

fn normalise(entry: &Value) -> Vec<Value> {
    let Some(models) = entry["models"].as_object() else {
        return Vec::new();
    };
    let mut rows: Vec<Value> = models
        .iter()
        .map(|(key, model)| {
            json!({
                "id": model["id"].as_str().unwrap_or(key),
                "name": model["name"].as_str().unwrap_or(key),
                "release_date": model["release_date"].as_str().unwrap_or(""),
                "reasoning": model["reasoning"].as_bool().unwrap_or(false),
                "context": model["limit"]["context"].clone(),
                "input": model["cost"]["input"].clone(),
                "output": model["cost"]["output"].clone(),
            })
        })
        .collect();
    // Stable, newest first; a missing date sorts as the oldest.
    rows.sort_by(|a, b| release(b).cmp(release(a)));
    rows
}

fn release(row: &Value) -> &str {
    match row["release_date"].as_str() {
        Some(date) if !date.is_empty() => date,
        _ => "0000",
    }
}
