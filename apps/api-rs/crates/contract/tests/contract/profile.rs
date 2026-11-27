//! Editing the profile the way the agent's edit_profile tool does.

use agent_hire_contract::Session;
use serde_json::{Value, json};

fn first_id(profile: &Value, section: &str) -> String {
    profile[section][0]["id"]
        .as_str()
        .unwrap_or("missing-id")
        .to_owned()
}

#[test]
fn profile_edits() {
    let mut s = Session::start("profile_edits");
    s.step("empty profile", "GET", "/api/v1/profile", None);
    s.step(
        "candidate",
        "PUT",
        "/api/v1/profile/candidate",
        Some(json!({ "n": "Priya Raman", "s": "Backend engineer, 6 years, Go and Kafka." })),
    );
    s.step(
        "identity",
        "PUT",
        "/api/v1/profile/identity",
        Some(json!({ "email": "priya@example.com", "phone": "+91 98765 43210", "city": "Hyderabad", "country": "India", "linkedin_url": "https://linkedin.com/in/priya", "github_url": "https://github.com/priya", "website_url": "https://priya.dev" })),
    );
    s.step(
        "add skill",
        "POST",
        "/api/v1/profile/skill",
        Some(json!({ "n": "Go", "cat": "language" })),
    );
    s.step(
        "add experience",
        "POST",
        "/api/v1/profile/experience",
        Some(json!({ "role": "Senior Engineer", "co": "Ledgerline", "period": "2021-2026", "d": "Payments platform on Kafka." })),
    );
    s.step(
        "add project",
        "POST",
        "/api/v1/profile/project",
        Some(json!({ "title": "Settlement service", "stack": "Go, Kafka, PostgreSQL", "repo": "", "impact": "3M transactions a day" })),
    );
    s.step(
        "add education",
        "POST",
        "/api/v1/profile/education",
        Some(json!({ "title": "B.Tech CSE, NIT Warangal" })),
    );
    s.step(
        "add certification",
        "POST",
        "/api/v1/profile/certification",
        Some(json!({ "title": "CKA" })),
    );
    s.step(
        "add achievement",
        "POST",
        "/api/v1/profile/achievement",
        Some(json!({ "title": "Cut MTTR 90 to 25 min" })),
    );
    let profile = s
        .step("profile after adds", "GET", "/api/v1/profile", None)
        .json()
        .clone();
    let skill = first_id(&profile, "skills");
    let exp = first_id(&profile, "exp");
    let project = first_id(&profile, "projects");
    s.step(
        "edit skill",
        "PUT",
        &format!("/api/v1/profile/skill/{skill}"),
        Some(json!({ "n": "Golang", "cat": "language" })),
    );
    s.step(
        "edit experience",
        "PUT",
        &format!("/api/v1/profile/experience/{exp}"),
        Some(json!({ "role": "Staff Engineer", "co": "Ledgerline", "period": "2021-2026", "d": "Led the payments platform." })),
    );
    s.step(
        "edit project",
        "PUT",
        &format!("/api/v1/profile/project/{project}"),
        Some(json!({ "title": "Settlement service", "stack": "Go, Kafka", "repo": "https://github.com/priya/settle", "impact": "3M tx/day" })),
    );
    s.step("profile after edits", "GET", "/api/v1/profile", None);
    s.step(
        "edit a missing skill",
        "PUT",
        "/api/v1/profile/skill/nope",
        Some(json!({ "n": "X" })),
    );
    s.step(
        "delete skill",
        "DELETE",
        &format!("/api/v1/profile/skill/{skill}"),
        None,
    );
    s.step(
        "delete experience",
        "DELETE",
        &format!("/api/v1/profile/experience/{exp}"),
        None,
    );
    s.step(
        "delete project",
        "DELETE",
        &format!("/api/v1/profile/project/{project}"),
        None,
    );
    s.step(
        "delete education by title",
        "DELETE",
        "/api/v1/profile/education/B.Tech%20CSE%2C%20NIT%20Warangal",
        None,
    );
    s.step(
        "delete certification by title",
        "DELETE",
        "/api/v1/profile/certification/CKA",
        None,
    );
    s.step(
        "delete achievement by title",
        "DELETE",
        "/api/v1/profile/achievement/Cut%20MTTR%2090%20to%2025%20min",
        None,
    );
    s.step(
        "delete a missing entry",
        "DELETE",
        "/api/v1/profile/education/nope",
        None,
    );
    s.step("profile after deletes", "GET", "/api/v1/profile", None);
    s.finish();
}
