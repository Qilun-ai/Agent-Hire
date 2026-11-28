//! Job index search over several hand-added jobs: roles, levels, places, keywords, title words,
//! sorting, paging, duplicates collapsing, and the validation errors.

use std::thread;
use std::time::Duration;

use agent_hire_contract::Session;
use serde_json::json;

#[test]
fn job_search() {
    let mut s = Session::start("job_search");
    for (title, company, location, workplace, description) in [
        (
            "Senior Backend Engineer",
            "Nimbus Robotics",
            "Hyderabad, Telangana, India",
            "hybrid",
            "Go, Kafka and PostgreSQL. 6+ years of experience building distributed systems.",
        ),
        (
            "Backend Engineer",
            "Ledgerline",
            "Bengaluru, India",
            "remote",
            "Python, Django and AWS. 2+ years of professional experience with REST APIs.",
        ),
        (
            "Junior Frontend Developer",
            "Pixel Forge",
            "Remote",
            "remote",
            "React and TypeScript. Freshers welcome, no experience required.",
        ),
        (
            "Data Analyst",
            "Ledgerline",
            "Pune, India",
            "onsite",
            "SQL, Excel and Tableau dashboards for finance.",
        ),
        (
            "Staff Software Engineer",
            "Orbit Labs",
            "Austin, TX",
            "onsite",
            "Rust, Kubernetes and gRPC. Lead the platform team.",
        ),
        (
            "Software Engineering Intern",
            "Orbit Labs",
            "Austin, TX",
            "onsite",
            "Summer internship working with Java and Spring Boot.",
        ),
        (
            "Backend Engineer",
            "Ledgerline",
            "Chennai, India",
            "remote",
            "Python, Django and AWS. 2+ years of professional experience with REST APIs.",
        ),
    ] {
        // `first_seen_at` has 1 s resolution; newest-first order needs distinct seconds.
        thread::sleep(Duration::from_millis(1100));
        s.step(
            &format!("add {title} at {company}"),
            "POST",
            "/api/v1/jobindex/custom",
            Some(json!({"title": title, "company": company, "location": location, "workplace_type": workplace,
                        "description": description, "add_to_pipeline": false})),
        );
    }
    for (label, body) in [
        ("everything", json!({"sort": "recent"})),
        (
            "software family",
            json!({"role": "software engineer", "sort": "recent"}),
        ),
        ("free-text role", json!({"role": "data analyst"})),
        ("seniors", json!({"levels": ["senior"], "sort": "recent"})),
        (
            "beginners",
            json!({"levels": ["fresher", "junior", "intern"], "sort": "recent"}),
        ),
        ("sort by level", json!({"sort": "level"})),
        ("india", json!({"locations": ["India"], "sort": "recent"})),
        (
            "texas city",
            json!({"locations": ["austin"], "sort": "recent"}),
        ),
        ("remote only", json!({"remote": true, "sort": "recent"})),
        (
            "keywords",
            json!({"keywords": ["kafka", "rust"], "sort": "recent"}),
        ),
        (
            "title words",
            json!({"title_keywords": ["backend", "-senior"], "sort": "recent"}),
        ),
        (
            "companies",
            json!({"companies": ["ledger"], "sort": "recent"}),
        ),
        (
            "company slugs",
            json!({"company_slugs": ["custom"], "sort": "recent", "page_size": 2}),
        ),
        (
            "second page",
            json!({"company_slugs": ["custom"], "sort": "recent", "page_size": 2, "page": 2}),
        ),
        (
            "departments without a model",
            json!({"departments": ["Technology", "nonsense"], "sort": "recent"}),
        ),
        (
            "recent window",
            json!({"since_hours": 24, "sort": "recent"}),
        ),
        ("bad sort", json!({"sort": "best"})),
        ("bad page size", json!({"page_size": 26})),
        ("extra field", json!({"role": "x", "colour": "blue"})),
        ("bad since", json!({"since_hours": 0.5})),
    ] {
        s.step(label, "POST", "/api/v1/jobindex/search", Some(body));
    }
    s.step("departments", "GET", "/api/v1/jobindex/departments", None);
    s.step(
        "promote two",
        "POST",
        "/api/v1/jobindex/promote",
        Some(json!({"job_refs": ["missing-1"], "add_to_cart": false})),
    );
    s.step(
        "promote nothing",
        "POST",
        "/api/v1/jobindex/promote",
        Some(json!({"job_refs": []})),
    );
    s.step(
        "custom title too short",
        "POST",
        "/api/v1/jobindex/custom",
        Some(json!({"title": "x", "company": "y"})),
    );
    s.step(
        "delete bad scope",
        "POST",
        "/api/v1/jobindex/delete",
        Some(json!({"job_refs": ["a"], "scope": "nowhere"})),
    );
    s.step(
        "reset bad scope",
        "POST",
        "/api/v1/jobindex/reset",
        Some(json!({"scope": "everything", "confirm": "RESET"})),
    );
    s.step(
        "scrape unknown category",
        "POST",
        "/api/v1/jobindex/scrape",
        Some(json!({"categories": ["warp"]})),
    );
    s.step(
        "scrape unknown company",
        "POST",
        "/api/v1/jobindex/scrape",
        Some(json!({"companies": ["no-such-company-xyz"]})),
    );
    s.step(
        "reset the index",
        "POST",
        "/api/v1/jobindex/reset",
        Some(json!({"scope": "index", "confirm": "RESET"})),
    );
    s.step(
        "empty after reset",
        "POST",
        "/api/v1/jobindex/search",
        Some(json!({"sort": "recent"})),
    );
    s.finish();
}
