//! Feedback saves, lead scores after promotion, and fill plans without model credentials.

use agent_hire_contract::Session;
use serde_json::json;

#[test]
fn lead_feedback() {
    let mut s = Session::start("lead_feedback");
    s.step(
        "set candidate",
        "PUT",
        "/api/v1/profile/candidate",
        Some(json!({"n": "Asha Rao", "s": "Backend engineer who builds Rust services on PostgreSQL"})),
    );
    s.step(
        "set profile identity",
        "PUT",
        "/api/v1/profile/identity",
        Some(json!({"email": "asha@example.net", "phone": "+91 98765 43210", "country": "India"})),
    );
    for skill in ["Rust", "PostgreSQL"] {
        s.step(
            "add a matching skill",
            "POST",
            "/api/v1/profile/skill",
            Some(json!({"n": skill})),
        );
    }
    let description = "Build backend applications with Rust and PostgreSQL. Maintain distributed systems and                        improve high-volume data processing. Work with the engineering team on reliability                        and operating production APIs. Candidates should have three years of backend experience.";
    let created = s.step(
        "create a custom lead with a matching profile",
        "POST",
        "/api/v1/jobindex/custom",
        Some(json!({"title": "Rust Backend Engineer", "company": "Example", "description": description,
                    "url": "https://example.test/jobs/custom-rust", "add_to_pipeline": true})),
    );
    let id = created.json()["job_ref"].as_str().expect("custom job id");
    let lead = format!("/api/v1/leads/{id}");
    s.step("scores on a newly created lead", "GET", &lead, None);
    let second = s.step(
        "create a job to promote separately",
        "POST",
        "/api/v1/jobindex/custom",
        Some(json!({"title": "Backend Platform Engineer", "company": "Example", "description": description,
                    "url": "https://example.test/jobs/promoted-rust", "add_to_pipeline": false})),
    );
    let second_id = second.json()["job_ref"].as_str().expect("second job id");
    s.step(
        "promote the indexed job",
        "POST",
        "/api/v1/jobindex/promote",
        Some(json!({"job_refs": [second_id]})),
    );
    s.step(
        "scores on a promoted lead",
        "GET",
        &format!("/api/v1/leads/{second_id}"),
        None,
    );
    s.step(
        "invalid lead id",
        "PUT",
        "/api/v1/leads/invalid%40id/feedback",
        Some(json!({"feedback": "good"})),
    );
    s.step(
        "missing lead",
        "PUT",
        "/api/v1/leads/missing/feedback",
        Some(json!({"feedback": "good"})),
    );
    s.step(
        "invalid feedback model",
        "PUT",
        &format!("{lead}/feedback"),
        Some(json!({"feedback": "not_freelance", "extra": true})),
    );
    let contacted = s.step(
        "mark the lead already contacted",
        "PUT",
        &format!("{lead}/feedback"),
        Some(json!({"feedback": "already_contacted"})),
    );
    assert_eq!(contacted.json()["last_contacted_at"], "");
    assert_eq!(contacted.json()["followup_due_at"], "");
    s.step(
        "save feedback and discard",
        "PUT",
        &format!("{lead}/feedback"),
        Some(json!({"feedback": "too_generic", "note": "Needs more detail"})),
    );
    s.step("lead after feedback", "GET", &lead, None);
    s.step(
        "configured model provider without credentials",
        "POST",
        "/api/v1/settings",
        Some(json!({"llm_provider": "nvidia"})),
    );
    s.step(
        "save a fact and an answer default",
        "PUT",
        "/api/v1/fill/facts",
        Some(json!({"facts": {"phone_country_code": "+91", "phone": "+91 98765 43210", "country": "India", "email": "asha@example.net"}, "defaults": {"notice_period": "30 days"}})),
    );
    s.step(
        "fill plan with rules and no decision endpoint",
        "POST",
        "/api/v1/fill/plan",
        Some(json!({"job_id": "unknown", "fields": [
            {"id": "email", "label": "Email"},
            {"id": "phone", "kind": "tel"},
            {"id": "dial", "kind": "select", "label": "Dialing code", "autocomplete": "tel-country-code", "options": ["+1", "+91"]},
            {"id": "country", "kind": "select", "label": "Country", "options": ["India", "Ireland"]},
            {"id": "consent", "kind": "checkbox", "context": "I agree to the terms"},
            {"id": "human", "label": "Are you a human?"},
            {"id": "other", "kind": "textarea", "label": "Why are you interested in this role?"}
        ]})),
    );
    s.step(
        "fill a known job without model credentials",
        "POST",
        "/api/v1/fill/plan",
        Some(json!({"job_id": second_id, "fields": [{"id": "name", "kind": "text", "label": "Email"}]})),
    );
    s.step(
        "validate nested fill fields",
        "POST",
        "/api/v1/fill/plan",
        Some(json!({"job_id": "", "fields": [{"options": [7], "required": "maybe"}, false]})),
    );
    s.finish();
}
