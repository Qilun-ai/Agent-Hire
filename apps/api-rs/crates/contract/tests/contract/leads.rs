//! Applications (leads), the cart, events and the extension queue on an empty database.

use agent_hire_contract::Session;
use serde_json::json;

#[test]
fn leads_empty() {
    let mut s = Session::start("leads_empty");
    s.step("legacy list", "GET", "/api/v1/leads", None);
    s.step(
        "paged list",
        "GET",
        "/api/v1/leads?page=1&limit=25&view=all&sort=newest",
        None,
    );
    s.step("bad page size", "GET", "/api/v1/leads?page=1&limit=0", None);
    s.step("summary", "GET", "/api/v1/leads/summary", None);
    s.step("missing lead", "GET", "/api/v1/leads/does-not-exist", None);
    s.step(
        "missing lead pdf",
        "GET",
        "/api/v1/leads/does-not-exist/pdf",
        None,
    );
    s.step(
        "status of a missing lead",
        "PUT",
        "/api/v1/leads/does-not-exist/status",
        Some(json!({ "status": "applied" })),
    );
    s.step(
        "cleanup with nothing to clean",
        "POST",
        "/api/v1/leads/cleanup",
        None,
    );
    s.step("empty cart", "GET", "/api/v1/cart", None);
    s.step(
        "cart with unknown ids",
        "PUT",
        "/api/v1/cart",
        Some(json!({ "job_ids": ["nope-1"] })),
    );
    s.step("cart after put", "GET", "/api/v1/cart", None);
    s.step("events", "GET", "/api/v1/events?limit=20", None);
    s.step("extension queue", "GET", "/api/v1/extension/jobs", None);
    s.step(
        "extension queue incl. done",
        "GET",
        "/api/v1/extension/jobs?include_done=true",
        None,
    );
    s.step(
        "fill context, unknown job",
        "GET",
        "/api/v1/fill-context?job_id=does-not-exist",
        None,
    );
    s.step(
        "fill context, unknown url",
        "GET",
        "/api/v1/fill-context?url=https%3A%2F%2Fexample.com%2Fjobs%2F1",
        None,
    );
    s.step(
        "fill context, no arguments",
        "GET",
        "/api/v1/fill-context",
        None,
    );
    s.step(
        "client error report",
        "POST",
        "/api/v1/errors",
        Some(json!({ "message": "contract test", "source": "web", "stack": "at test" })),
    );
    s.finish();
}
