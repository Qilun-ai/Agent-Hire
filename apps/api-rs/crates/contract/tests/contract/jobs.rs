//! The job index: status and catalogue routes, then a custom job's whole life as an application.

use agent_hire_contract::Session;
use serde_json::json;

#[test]
fn jobindex_empty() {
    let mut s = Session::start("jobindex_empty");
    s.step("status", "GET", "/api/v1/jobindex/status", None);
    s.step("companies", "GET", "/api/v1/jobindex/companies", None);
    s.step("departments", "GET", "/api/v1/jobindex/departments", None);
    s.step(
        "encoder progress",
        "GET",
        "/api/v1/jobindex/encoders/progress",
        None,
    );
    s.step("no scrape running", "GET", "/api/v1/jobindex/scrape", None);
    s.step(
        "unknown scrape run",
        "GET",
        "/api/v1/jobindex/scrape?run_id=nope",
        None,
    );
    s.step(
        "stop with nothing running",
        "POST",
        "/api/v1/jobindex/scrape/stop",
        Some(json!({})),
    );
    s.step(
        "kaggle not connected",
        "GET",
        "/api/v1/jobindex/kaggle",
        None,
    );
    s.step(
        "search an empty index",
        "POST",
        "/api/v1/jobindex/search",
        Some(json!({})),
    );
    s.step(
        "search with filters",
        "POST",
        "/api/v1/jobindex/search",
        Some(
            json!({ "role": "backend engineer", "levels": ["senior"], "remote": true, "page": 1 }),
        ),
    );
    s.step("missing job", "GET", "/api/v1/jobindex/jobs/nope", None);
    s.step(
        "classify nothing",
        "POST",
        "/api/v1/jobindex/classify",
        Some(json!({ "limit": 8 })),
    );
    s.step(
        "reset needs confirmation",
        "POST",
        "/api/v1/jobindex/reset",
        Some(json!({ "scope": "index", "confirm": "no" })),
    );
    s.finish();
}

#[test]
fn custom_job_lifecycle() {
    let mut s = Session::start("custom_job_lifecycle");
    let added = s.step(
        "add a custom job",
        "POST",
        "/api/v1/jobindex/custom",
        Some(json!({
            "title": "Senior Backend Engineer, Fleet Platform",
            "company": "Nimbus Robotics",
            "location": "Hyderabad, Telangana, India",
            "workplace_type": "hybrid",
            "url": "http://127.0.0.1:8099/jobs/nr-1042/",
            "description": "Build event-driven services in Go with Kafka and PostgreSQL. 5+ years \
                            of backend experience. Own on-call and SLOs.",
            "add_to_pipeline": true,
        })),
    );
    // The index and the pipeline share one reference for custom jobs: `custom-<hash of the URL>`.
    let job_id = added.json()["job_ref"]
        .as_str()
        .expect("custom job returns job_ref")
        .to_owned();
    let index_ref = job_id.clone();
    s.step(
        "index status counts it",
        "GET",
        "/api/v1/jobindex/status",
        None,
    );
    s.step(
        "read it from the index",
        "GET",
        &format!("/api/v1/jobindex/jobs/{index_ref}"),
        None,
    );
    s.step(
        "search finds it",
        "POST",
        "/api/v1/jobindex/search",
        Some(json!({ "role": "backend" })),
    );
    s.step(
        "lead detail",
        "GET",
        &format!("/api/v1/leads/{job_id}"),
        None,
    );
    s.step("leads list", "GET", "/api/v1/leads", None);
    s.step("summary", "GET", "/api/v1/leads/summary", None);
    s.step("extension queue", "GET", "/api/v1/extension/jobs", None);
    s.step("cart", "GET", "/api/v1/cart", None);
    s.step(
        "fill context by job",
        "GET",
        &format!("/api/v1/fill-context?job_id={job_id}"),
        None,
    );
    s.step(
        "fill context by url",
        "GET",
        "/api/v1/fill-context?url=http%3A%2F%2F127.0.0.1%3A8099%2Fjobs%2Fnr-1042%2F",
        None,
    );
    s.step(
        "promote again (idempotent)",
        "POST",
        "/api/v1/jobindex/promote",
        Some(json!({ "job_refs": [index_ref], "add_to_cart": true })),
    );
    s.step(
        "feedback",
        "PUT",
        &format!("/api/v1/leads/{job_id}/feedback"),
        Some(json!({ "feedback": "good", "note": "great stack" })),
    );
    s.step(
        "follow-up",
        "PUT",
        &format!("/api/v1/leads/{job_id}/followup"),
        Some(json!({ "days": 7 })),
    );
    s.step(
        "extension reports a fill",
        "POST",
        &format!("/api/v1/leads/{job_id}/filled"),
        Some(json!({ "filled": ["First name", "Email"], "skipped": ["Gender"], "url": "http://127.0.0.1:8099/jobs/nr-1042/apply.html" })),
    );
    s.step(
        "extension marks applied",
        "POST",
        &format!("/api/v1/leads/{job_id}/applied"),
        None,
    );
    s.step(
        "status change",
        "PUT",
        &format!("/api/v1/leads/{job_id}/status"),
        Some(json!({ "status": "interviewing" })),
    );
    s.step(
        "invalid status",
        "PUT",
        &format!("/api/v1/leads/{job_id}/status"),
        Some(json!({ "status": "bogus" })),
    );
    s.step(
        "events for the job",
        "GET",
        &format!("/api/v1/events?job_id={job_id}"),
        None,
    );
    s.step(
        "applied view",
        "GET",
        "/api/v1/leads?page=1&limit=25&view=applied&sort=newest",
        None,
    );
    s.step(
        "pdf before tailoring",
        "GET",
        &format!("/api/v1/leads/{job_id}/pdf"),
        None,
    );
    s.step(
        "delete the lead",
        "DELETE",
        &format!("/api/v1/leads/{job_id}"),
        None,
    );
    s.step(
        "lead is gone",
        "GET",
        &format!("/api/v1/leads/{job_id}"),
        None,
    );
    s.step(
        "delete from the index",
        "POST",
        "/api/v1/jobindex/delete",
        Some(json!({ "job_refs": [index_ref], "scope": "both" })),
    );
    s.step("index after delete", "GET", "/api/v1/jobindex/status", None);
    s.finish();
}
