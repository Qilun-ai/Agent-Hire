//! Provider choices, endpoint controls and safe key previews.

use maud::{Markup, html};
use serde_json::Value;

use super::Settings;
use crate::view;

const SECRET_SET: &str = "__AGENT_HIRE_SECRET_SET__";

pub(super) fn panel(settings: &Settings) -> Markup {
    let provider = settings.values["llm_provider"]
        .as_str()
        .filter(|name| !name.is_empty())
        .unwrap_or("ollama");
    let environment = view::list(&settings.environment, "providers");
    let mut providers = vec!["custom", "anthropic", "opencode"];
    for found in environment {
        let id = view::text(found, "id");
        if !id.is_empty() && !providers.contains(&id) {
            providers.push(id);
        }
    }
    if !providers.contains(&provider) {
        providers.push(provider);
    }
    html! {
        section.card.setting-card {
            p { "Choose the model that writes résumés, fills forms and reads unclear emails." }
            form method="post" data-action="/ui/actions/settings" data-settings="ai" data-save data-busy="Saving…" {
                fieldset.provider-list {
                    legend { "Provider" }
                    @for &id in &providers {
                        @let variable = variable(environment, id);
                        label.choice.provider {
                            input type="radio" name="llm_provider" value=(id) checked[id == provider];
                            span { strong { (friendly(id)) }
                                small { @if !variable.is_empty() { "key from " (variable) }
                                    @else if let Some(note) = hint(id) { (note) }
                                }
                            }
                        }
                    }
                }
                @for id in providers {
                    fieldset.provider-fields data-provider=(id) hidden[id != provider] disabled[id != provider] {
                        (provider_fields(settings, environment, id))
                    }
                }
                div.inline {
                    button.primary { "Save settings" }
                    button.secondary data-validate { "Check keys" }
                }
                output aria-live="polite" {}
                div #settings-result {}
            }
        }
    }
}

fn variable<'a>(environment: &'a [Value], id: &str) -> &'a str {
    environment
        .iter()
        .find(|found| found["id"] == id)
        .map_or("", |found| view::text(found, "variable"))
}

fn friendly(id: &str) -> &str {
    match id {
        "custom" => "OpenAI-compatible",
        "anthropic" => "Anthropic",
        "opencode" => "OpenCode",
        "openai" => "OpenAI",
        "gemini" => "Gemini",
        "groq" => "Groq",
        "nvidia" => "NVIDIA",
        "deepseek" => "DeepSeek",
        "xai" => "xAI",
        "kimi" => "Kimi",
        "mistral" => "Mistral",
        "openrouter" => "OpenRouter",
        "together" => "Together",
        "fireworks" => "Fireworks",
        "cerebras" => "Cerebras",
        "perplexity" => "Perplexity",
        "huggingface" => "Hugging Face",
        "cohere" => "Cohere",
        "sambanova" => "SambaNova",
        "qwen" => "Qwen",
        "ollama" => "Ollama",
        "claude_code" | "claude-code" => "Claude Code",
        "codex" | "codex-cli" => "Codex CLI",
        "gemini_cli" | "gemini-cli" => "Gemini CLI",
        "opencode-cli" => "OpenCode CLI",
        _ => id,
    }
}

fn hint(id: &str) -> Option<&'static str> {
    match id {
        "custom" => Some("Any compatible /v1 endpoint"),
        "anthropic" => Some("Claude"),
        "opencode" => Some("Zen / Go API"),
        _ => None,
    }
}

fn provider_fields(settings: &Settings, environment: &[Value], id: &str) -> Markup {
    let endpoint = matches!(id, "custom" | "opencode");
    let key = if id == "anthropic" {
        "anthropic_key".to_owned()
    } else if let Some(found) = environment.iter().find(|found| found["id"] == id) {
        view::text(found, "key_setting").to_owned()
    } else {
        format!("{id}_api_key")
    };
    let base = format!("{id}_base_url");
    let model = format!("{id}_model");
    let values = &settings.values;
    html! {
        @if endpoint {
            (view::input("Endpoint URL", &base, view::text(values, &base), if id == "custom" { "https://api.example.com/v1" } else { "https://opencode.ai/zen/go/v1" }))
        }
        @if id != "ollama" && !id.ends_with("_cli") && !id.ends_with("-cli") && !matches!(id, "codex" | "claude_code" | "claude-code") {
            (key_input(settings, &key, variable(environment, id), match id {
                "anthropic" => "sk-ant-****", "gemini" => "AIza****", "nvidia" => "nvapi-****", "openrouter" => "sk-or-****", "opencode" => "OpenCode API key", _ => "API key",
            }))
        }
        @if id == "anthropic" {
            (view::input("Base URL", &base, view::text(values, &base), "https://api.anthropic.com · leave empty for official Anthropic"))
        }
        (view::input(if endpoint { "Model name" } else { "Global model" }, &model, view::text(values, &model), "Model ID"))
        @let models: &[&str] = match id {
            "anthropic" => &["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
            "opencode" => &["muse-spark-1.3-contributor"],
            _ => &[],
        };
        @if !models.is_empty() {
            div.chips { @for &name in models { button.chip type="button" data-model=(name) data-model-field=(&model) { (name) } } }
        }
        @if endpoint { (advanced(values, id)) }
    }
}

fn advanced(values: &Value, id: &str) -> Markup {
    let protocol_key = format!("{id}_protocol");
    let format_key = format!("{id}_endpoint_type");
    let effort_key = format!("{id}_reasoning_effort");
    let protocol = values[&protocol_key].as_str().unwrap_or("openai");
    let default_format = if id == "custom" {
        "chat/completions"
    } else {
        "responses"
    };
    let format = if protocol == "anthropic" {
        "messages"
    } else {
        values[&format_key]
            .as_str()
            .filter(|value| *value != "messages")
            .unwrap_or(default_format)
    };
    html! {
        details.advanced data-format-default=(default_format) {
            summary { "Advanced: protocol, request format, thinking" }
            div.field-grid {
                (view::select("Protocol", &protocol_key, protocol, &[("openai", "OpenAI"), ("anthropic", "Anthropic")]))
                (view::select("Request format", &format_key, format, &[("chat/completions", "Chat"), ("responses", "Responses"), ("messages", "Messages")]))
                (view::select("Reasoning / thinking", &effort_key, values[&effort_key].as_str().unwrap_or("none"), &[("none", "None (Standard)"), ("low", "Low"), ("medium", "Medium"), ("high", "High")]))
            }
            p.muted { "Maps to the Anthropic thinking budget, Responses reasoning effort, or Chat reasoning_effort." }
        }
    }
}

struct KeyField {
    saved: bool,
    placeholder: String,
    preview: String,
}

fn key_field(value: &str, preview: &Value, variable: &str, placeholder: &str) -> KeyField {
    let saved = value == SECRET_SET;
    let placeholder = if saved {
        "Paste a new key to replace the saved one".to_owned()
    } else if !variable.is_empty() {
        format!("Using {variable} from your environment · paste a key to override")
    } else {
        placeholder.to_owned()
    };
    let preview = if saved {
        format!("{}••••", preview.as_str().unwrap_or_default())
    } else {
        String::new()
    };
    KeyField {
        saved,
        placeholder,
        preview,
    }
}

pub(super) fn key_input(
    settings: &Settings,
    key: &str,
    variable: &str,
    placeholder: &str,
) -> Markup {
    let field = key_field(
        view::text(&settings.values, key),
        &settings.previews[key],
        variable,
        placeholder,
    );
    let empty = key_field("", &Value::Null, variable, placeholder);
    html! {
        div.key-field {
            label.field { span { "API key" }
                input type="password" name=(key) autocomplete="new-password" placeholder=(field.placeholder) data-empty-key=(empty.placeholder) data-secret;
            }
            @if field.saved {
                div.inline.saved-key {
                    span.badge { "Saved " (field.preview) }
                    button.quiet type="button" data-delete-key=(format!("/ui/actions/settings/key/{}", view::segment(key))) { "Delete saved key" }
                }
            }
        }
    }
}

pub(super) fn validation(selected: &str, data: &Value) -> Markup {
    let mut reports: Vec<(&String, &Value)> = data.as_object().into_iter().flatten().collect();
    reports.sort_by_key(|(provider, _)| (*provider != selected, friendly(provider).to_lowercase()));
    html! {
        div.validation-list {
            @for (provider, row) in reports {
                @let (tone, label) = validation_status(view::text(row, "status"));
                section.notice {
                    h4 { (friendly(provider)) " " span class=(format!("badge {tone}")) { (label) } }
                    p.muted {
                        (view::text(row, "model"))
                        @if let Some(ms) = row["latency_ms"].as_u64() { " · " (ms) " ms" }
                    }
                    @if let Some(reply) = row["reply"].as_str() { blockquote { (reply) } }
                    @if tone != "ok" && let Some(detail) = row["detail"].as_str() { p.bad { (detail) } }
                }
            }
        }
    }
}

fn validation_status(status: &str) -> (&str, &str) {
    match status {
        "ok" => ("ok", "OK"),
        "invalid_key" => ("bad", "Invalid key"),
        "unreachable" => ("warn", "Unreachable"),
        "not_set" | "not_configured" | "missing_key" => ("", "Not set"),
        "unchecked" => ("", "Unchecked"),
        "model_not_found" => ("bad", "Model not found"),
        "bad_endpoint" => ("bad", "Bad endpoint"),
        "format_error" => ("bad", "Format error"),
        "missing_model" => ("bad", "Missing model"),
        "rate_limited" => ("warn", "Rate limited"),
        "server_error" => ("bad", "Server error"),
        other => ("bad", other),
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{SECRET_SET, key_field};

    #[test]
    fn stored_keys_take_precedence_without_rendering_the_secret() {
        let field = key_field(SECRET_SET, &json!("sk-a"), "AI_KEY", "Other");
        assert!(field.saved);
        assert_eq!(field.preview, "sk-a••••");
        assert!(!field.placeholder.contains("AI_KEY"));
        assert!(!field.placeholder.contains(SECRET_SET));
    }

    #[test]
    fn environment_and_new_keys_have_distinct_placeholders() {
        let field = key_field("", &json!(null), "AI_KEY", "Other");
        assert!(!field.saved);
        assert_eq!(field.preview, "");
        assert!(field.placeholder.contains("AI_KEY"));
        assert_eq!(
            key_field("", &json!(null), "", "Other").placeholder,
            "Other"
        );
    }
}
