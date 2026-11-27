//! The applications list with several leads: views, filters, sort orders, similar-posting
//! grouping and the summary facets.

use std::thread;
use std::time::Duration;

use agent_hire_contract::Session;
use serde_json::json;

/// Adds a custom job. Waits a little over a second first: `created_at` has 1 s resolution, and
/// newest-first order is only deterministic when no two leads share a second.
fn add(
    s: &mut Session,
    title: &str,
    company: &str,
    location: &str,
    slug: &str,
    description: &str,
) -> String {
    thread::sleep(Duration::from_millis(1100));
    let added = s.step(
        &format!("add {slug}"),
        "POST",
        "/api/v1/jobindex/custom",
        Some(json!({
            "title": title,
            "company": company,
            "location": location,
            "url": format!("http://127.0.0.1:8099/jobs/{slug}/"),
            "description": description,
            "add_to_pipeline": true,
        })),
    );
    added.json()["job_ref"]
        .as_str()
        .unwrap_or("missing")
        .to_owned()
}

#[test]
fn lead_filters() {
    let mut s = Session::start("lead_filters");
    let backend = add(
        &mut s,
        "Senior Backend Engineer",
        "Nimbus Robotics",
        "Bangalore, Karnataka, India",
        "nb-1",
        "Go, Kafka and PostgreSQL. 6+ years. Remote friendly.",
    );
    let twin = add(
        &mut s,
        "Senior Backend Engineer",
        "Nimbus Robotics",
        "Bengaluru, India",
        "nb-2",
        "Same role, second posting. Go and Kafka. 6+ years.",
    );
    let junior = add(
        &mut s,
        "Junior Frontend Developer",
        "Pixel Forge",
        "Remote",
        "pf-1",
        "React and TypeScript. 0-2 years of experience, new grads welcome.",
    );
    let data = add(
        &mut s,
        "Data Analyst",
        "Ledgerline",
        "Pune, India",
        "ll-1",
        "SQL and dashboards for the finance team.",
    );
    s.step(
        "interviewing",
        "PUT",
        &format!("/api/v1/leads/{backend}/status"),
        Some(json!({ "status": "interviewing" })),
    );
    s.step(
        "discard one",
        "PUT",
        &format!("/api/v1/leads/{data}/status"),
        Some(json!({ "status": "discarded" })),
    );
    s.step(
        "accepted after interview",
        "PUT",
        &format!("/api/v1/leads/{backend}/status"),
        Some(json!({ "status": "accepted" })),
    );
    for (label, query) in [
        ("page all newest", "page=1&limit=10&view=all&sort=newest"),
        ("page applied", "page=1&limit=10&view=applied"),
        ("page discarded", "page=1&limit=10&view=discarded"),
        ("page found", "page=1&limit=10&view=found"),
        ("sort company", "page=1&limit=10&sort=company"),
        ("sort recommended", "page=1&limit=10&sort=recommended"),
        ("second page", "page=2&limit=2&sort=company"),
        ("search kafka", "page=1&limit=10&q=KAFKA"),
        ("search tech stack", "page=1&limit=10&q=typescript"),
        ("platform", "page=1&limit=10&platform=careers"),
        (
            "location bengaluru",
            "page=1&limit=10&location=Bengaluru%2C%20Karnataka",
        ),
        ("location pune", "page=1&limit=10&location=pune"),
        (
            "company exact",
            "page=1&limit=10&company=%20pixel%20%20forge%20",
        ),
        ("beginner only", "page=1&limit=10&beginner_only=true"),
        ("seniority senior", "page=1&limit=10&seniority=Senior"),
        ("seniority bad", "page=1&limit=10&seniority=wizard"),
        ("collapse similar", "page=1&limit=10&collapse_similar=true"),
        ("posted recently", "page=1&limit=10&posted_within_hours=24"),
        ("bad posted", "page=1&limit=10&posted_within_hours=0"),
        ("sponsorship yes", "page=1&limit=10&sponsorship=yes"),
        ("sponsorship not_no", "page=1&limit=10&sponsorship=not_no"),
        ("bad sponsorship", "page=1&limit=10&sponsorship=maybe"),
        (
            "role filter unresolved",
            "page=1&limit=10&role_ids=backend_engineer",
        ),
        (
            "role filter strict",
            "page=1&limit=10&role_ids=backend_engineer&include_unresolved=false",
        ),
        ("bad role ids", "page=1&limit=10&role_ids=Other"),
        ("bad view", "page=1&limit=10&view=nope"),
        ("legacy status filter", "status=discarded"),
        ("legacy min score", "page=1&limit=10&min_score=0"),
        ("legacy beginner", "beginner_only=true"),
    ] {
        s.step(label, "GET", &format!("/api/v1/leads?{query}"), None);
    }
    s.step("summary", "GET", "/api/v1/leads/summary", None);
    s.step(
        "summary remote",
        "GET",
        "/api/v1/leads/summary?location=remote",
        None,
    );
    s.step(
        "summary beginner",
        "GET",
        "/api/v1/leads/summary?seniority=beginner",
        None,
    );
    s.step(
        "summary bad seniority",
        "GET",
        "/api/v1/leads/summary?seniority=x",
        None,
    );
    s.step(
        "follow-up out of range",
        "PUT",
        &format!("/api/v1/leads/{junior}/followup"),
        Some(json!({ "days": 90 })),
    );
    s.step(
        "follow-up extra field",
        "PUT",
        &format!("/api/v1/leads/{junior}/followup"),
        Some(json!({ "days": 3, "x": 1 })),
    );
    s.step(
        "status missing",
        "PUT",
        &format!("/api/v1/leads/{junior}/status"),
        Some(json!({})),
    );
    s.step("bad job id", "GET", "/api/v1/leads/bad%20id", None);
    s.step(
        "cart too long",
        "PUT",
        "/api/v1/cart",
        Some(json!({ "job_ids": (0..201).map(|i| format!("j{i}")).collect::<Vec<_>>() })),
    );
    s.step(
        "cart wrong type",
        "PUT",
        "/api/v1/cart",
        Some(json!({ "job_ids": [1, "a"] })),
    );
    s.step(
        "cart replaced",
        "PUT",
        "/api/v1/cart",
        Some(json!({ "job_ids": [twin, " ", junior, twin] })),
    );
    s.step("extension queue", "GET", "/api/v1/extension/jobs", None);
    s.step(
        "applied removes from cart",
        "POST",
        &format!("/api/v1/leads/{twin}/applied"),
        None,
    );
    s.step("cart after applied", "GET", "/api/v1/cart", None);
    s.step("queue hides done", "GET", "/api/v1/extension/jobs", None);
    s.step(
        "queue with done",
        "GET",
        "/api/v1/extension/jobs?include_done=true",
        None,
    );
    s.step("activity", "GET", "/api/v1/events?limit=50", None);
    s.finish();
}
