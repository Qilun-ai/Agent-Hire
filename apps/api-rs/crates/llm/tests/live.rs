//! Live calls against a real provider; ignored by default.
//!
//! `LLM_LIVE_PROVIDER=nvidia LLM_LIVE_KEY_FILE=/path LLM_LIVE_MODEL=... cargo test -p agent-hire-llm
//! --test live -- --ignored`

use std::env;
use std::fs;

use agent_hire_llm::{LlmRequest, OutputSchema, Settings, call_raw, call_structured};
use serde_json::json;

fn settings() -> Settings {
    let provider = env::var("LLM_LIVE_PROVIDER").expect("LLM_LIVE_PROVIDER is set");
    let key = fs::read_to_string(env::var("LLM_LIVE_KEY_FILE").expect("LLM_LIVE_KEY_FILE is set"))
        .expect("the key file is readable")
        .trim()
        .to_owned();
    let mut pairs = vec![
        ("llm_provider".to_owned(), provider.clone()),
        (format!("{provider}_api_key"), key),
    ];
    if let Ok(model) = env::var("LLM_LIVE_MODEL") {
        pairs.push((format!("{provider}_model"), model));
    }
    if let Ok(base) = env::var("LLM_LIVE_BASE_URL") {
        pairs.push((format!("{provider}_base_url"), base));
    }
    Settings::new(pairs)
}

#[tokio::test]
#[ignore = "needs a provider key"]
async fn raw_and_structured() {
    let settings = settings();
    let raw = call_raw(
        &settings,
        &LlmRequest {
            system: "Answer in one word.",
            user: "Capital of France?",
            ..LlmRequest::default()
        },
    )
    .await
    .expect("raw call works");
    assert!(raw.to_lowercase().contains("paris"), "raw reply: {raw}");
    let schema = OutputSchema {
        name: "City".to_owned(),
        schema: json!({"type": "object", "properties": {"city": {"type": "string"}, "country": {"type": "string"}}, "required": ["city", "country"]}),
    };
    let value = call_structured(
        &settings,
        &LlmRequest {
            user: "Capital of Japan, with its country.",
            ..LlmRequest::default()
        },
        &schema,
    )
    .await
    .expect("structured call works");
    assert!(
        value["city"]
            .as_str()
            .unwrap_or("")
            .to_lowercase()
            .contains("tokyo"),
        "structured reply: {value}"
    );
}
