//! Bootstrap, health, bearer auth and the agent-secret route.

use agent_hire_contract::{Auth, Session};

#[test]
fn auth() {
    let mut s = Session::start("auth");
    s.step_as("health is public", "GET", "/health", None, Auth::Anonymous);
    s.step_as(
        "bootstrap is public",
        "GET",
        "/bootstrap",
        None,
        Auth::Anonymous,
    );
    s.step_as("no token", "GET", "/api/v1/settings", None, Auth::Anonymous);
    s.step_as(
        "wrong token",
        "GET",
        "/api/v1/settings",
        None,
        Auth::WrongToken,
    );
    s.step(
        "right token",
        "GET",
        "/api/v1/settings/secret-previews",
        None,
    );
    s.step("unknown route", "GET", "/api/v1/no-such-route", None);
    s.step(
        "llm-config without the secret",
        "GET",
        "/api/v1/agent-internal/llm-config",
        None,
    );
    s.step_as(
        "llm-config, nothing configured",
        "GET",
        "/api/v1/agent-internal/llm-config",
        None,
        Auth::AgentSecret,
    );
    s.step_as(
        "llm-config secret without token",
        "GET",
        "/api/v1/agent-internal/llm-config",
        None,
        Auth::Anonymous,
    );
    s.finish();
}
