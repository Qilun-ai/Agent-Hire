//! Settings, preferences, saved keys and the agent's resolved LLM config.

use agent_hire_contract::{Auth, Session};
use serde_json::json;

#[test]
fn settings() {
    let mut s = Session::start("settings");
    s.step("defaults", "GET", "/api/v1/settings", None);
    s.step(
        "no saved keys",
        "GET",
        "/api/v1/settings/secret-previews",
        None,
    );
    s.step(
        "validate defaults",
        "GET",
        "/api/v1/settings/validate",
        None,
    );
    s.step(
        "subscription CLIs absent",
        "GET",
        "/api/v1/settings/subscription-status",
        None,
    );
    s.step(
        "save provider, model and key",
        "POST",
        "/api/v1/settings",
        Some(json!({
            "llm_provider": "nvidia",
            "nvidia_model": "minimaxai/minimax-m3.1-flash",
            "nvidia_api_key": "nvapi-contract-test-key-0123456789",
            "scrape_refresh_hours": "0",
        })),
    );
    s.step("saved values", "GET", "/api/v1/settings", None);
    s.step(
        "key preview is masked",
        "GET",
        "/api/v1/settings/secret-previews",
        None,
    );
    s.step_as(
        "agent sees the resolved config",
        "GET",
        "/api/v1/agent-internal/llm-config",
        None,
        Auth::AgentSecret,
    );
    s.step(
        "delete the key",
        "DELETE",
        "/api/v1/settings/key/nvidia_api_key",
        None,
    );
    s.step(
        "delete an unknown key",
        "DELETE",
        "/api/v1/settings/key/not_a_key",
        None,
    );
    s.step(
        "previews after delete",
        "GET",
        "/api/v1/settings/secret-previews",
        None,
    );
    s.step_as(
        "agent config without the key",
        "GET",
        "/api/v1/agent-internal/llm-config",
        None,
        Auth::AgentSecret,
    );
    s.step(
        "custom provider",
        "POST",
        "/api/v1/settings",
        Some(json!({
            "llm_provider": "custom",
            "custom_base_url": "http://127.0.0.1:9/v1",
            "custom_model": "stub-model",
            "custom_api_key": "custom-contract-key",
        })),
    );
    s.step_as(
        "agent config for a custom endpoint",
        "GET",
        "/api/v1/agent-internal/llm-config",
        None,
        Auth::AgentSecret,
    );
    s.step(
        "login without the CLI",
        "POST",
        "/api/v1/settings/subscription-login/claude_cli",
        None,
    );
    s.step(
        "login for an unknown provider",
        "POST",
        "/api/v1/settings/subscription-login/nope_cli",
        None,
    );
    s.step(
        "preferences start empty",
        "GET",
        "/api/v1/preferences",
        None,
    );
    s.step(
        "save preferences",
        "POST",
        "/api/v1/preferences",
        Some(json!({ "preferences": "Remote backend roles in India; no crypto." })),
    );
    s.step("preferences saved", "GET", "/api/v1/preferences", None);
    s.step(
        "reject a non-object body",
        "POST",
        "/api/v1/settings",
        Some(json!(["not", "an", "object"])),
    );
    s.finish();
}
