//! Readiness, résumé suggestions, and deterministic application PDFs.

use agent_hire_contract::{Auth, Page, PageServer, Session};
use serde_json::json;

#[test]
fn generation() {
    let mut session = Session::start("generation");
    session.step("authenticated health", "GET", "/health", None);
    session.step(
        "set candidate",
        "PUT",
        "/api/v1/profile/candidate",
        Some(json!({"n":"Maya Rao","s":"Backend engineer specializing in payment systems"})),
    );
    session.step(
        "set identity",
        "PUT",
        "/api/v1/profile/identity",
        Some(json!({"email":"maya@example.test"})),
    );
    session.step(
        "add skill",
        "POST",
        "/api/v1/profile/skill",
        Some(json!({"n":"Rust","cat":"language"})),
    );
    session.step(
        "add experience",
        "POST",
        "/api/v1/profile/experience",
        Some(json!({"role":"Backend Engineer","co":"Harbor Labs","period":"2022-2025","d":"Built reliable payment services with Rust and PostgreSQL."})),
    );
    let url_only = job(&mut session, "link", "https://example.test/posting", "");
    let empty = job(&mut session, "empty", "Backend Engineer", "");
    let ready = job(
        &mut session,
        "ready",
        "Rust Engineer",
        "Build backend payment services in Rust and PostgreSQL. Maintain production APIs and work with the payments team on service reliability.",
    );
    session.step(
        "URL-only blocker",
        "POST",
        &format!("/api/v1/leads/{url_only}/generate"),
        None,
    );
    session.step(
        "missing requirements blocker",
        "POST",
        &format!("/api/v1/leads/{empty}/suggest-selection"),
        None,
    );
    session.step(
        "unknown lead",
        "POST",
        "/api/v1/leads/unknown/generate",
        None,
    );
    let generate = format!("/api/v1/leads/{ready}/generate");
    session.step(
        "validate selection",
        "POST",
        &generate,
        Some(json!({"selection":{"version":"no","sections":[5],"unknown":true},"extra":false})),
    );
    session.step(
        "validate show_location",
        "POST",
        &format!("{generate}?show_location=maybe"),
        None,
    );
    // Python defaults to Ollama and asserts that a remote provider has credentials.
    // A loopback endpoint with empty structured replies exercises its real PDF fallback.
    let profile = session
        .client()
        .request("GET", "/api/v1/profile", None, Auth::Token);
    let profile = profile.json();
    let skill = &profile["skills"][0]["id"];
    let experience = &profile["exp"][0];
    let parent = &experience["id"];
    let point = &experience["points"][0]["id"];
    let rewrite = "Built reliable Rust and PostgreSQL payment services.";
    let reason = "Names the technologies in the posting.";
    let picks = json!({
        "skills_on":[skill,"unknown"],"experience_on":[parent],"projects_on":[],
        "points_on":[point],
        "suggestions":[{"point_id":point,"parent_kind":"experience","parent_id":parent,
                        "suggested_text":rewrite,"reason":reason}],
    });
    let stub = PageServer::start(vec![Page {
        path: "/v1/chat/completions".to_owned(),
        status: 200,
        content_type: "application/json".to_owned(),
        body: json!({
            "id":"chatcmpl-fallback","object":"chat.completion","created":0,"model":"fixture",
            "choices":[{"index":0,"finish_reason":"tool_calls","message":{
                "role":"assistant",
                "tool_calls":[{"id":"call_selection","type":"function","function":{
                    "name":"SelectionSuggestion","arguments":picks.to_string(),
                }}],
                "content":json!({
                    "resume_markdown":"","cover_letter_markdown":"",
                    "selected_projects":[],"founder_message":"","linkedin_note":"","cold_email":"",
                    "selection":{"version":1,"skills_on":[skill,"unknown"],"experience_on":[parent],"points_on":[point]},
                    "rewrites":[{"point_id":point,"text":rewrite,"reason":reason}],
                }).to_string(),
            }}]
        }).to_string(),
    }]);
    let url = format!("{}/v1", stub.base_url());
    session.step(
        "install empty-reply generation fixture",
        "POST",
        "/api/v1/settings",
        Some(json!({"llm_provider":"ollama","ollama_url":url})),
    );
    session.redact(&url, "<model-fixture>");
    let response = session.step("generate a fallback package", "POST", &generate, None);
    assert_eq!(response.status, 200, "{}", response.json());
    let result = response.json();
    session.redact(
        result["generation_job_id"].as_str().expect("generation id"),
        "<generation-id>",
    );
    for (field, name) in [
        ("resume_asset", "<resume>.pdf"),
        ("cover_letter_asset", "<letter>.pdf"),
    ] {
        let file = result["lead"][field].as_str().expect("generated PDF path");
        let filename = file.rsplit(['/', '\\']).next().expect("filename");
        session.redact(filename, name);
    }
    let lead = format!("/api/v1/leads/{ready}");
    session.step_pdf_text("read generated résumé", &format!("{lead}/pdf"));
    session.step_pdf_text(
        "read generated cover letter",
        &format!("{lead}/pdf?kind=cover"),
    );
    session.step("approved lead after generation", "GET", &lead, None);
    session.step(
        "suggest selection",
        "POST",
        &format!("{lead}/suggest-selection"),
        None,
    );
    session.finish();
}

fn job(session: &mut Session, suffix: &str, title: &str, description: &str) -> String {
    let response = session.step(
        &format!("seed {suffix} posting"),
        "POST",
        "/api/v1/jobindex/custom",
        Some(
            json!({"title":title,"company":"Aster Payments","description":description,
            "url":format!("https://example.test/jobs/{suffix}"),"add_to_pipeline":true}),
        ),
    );
    response.json()["job_ref"]
        .as_str()
        .expect("custom job ID")
        .to_owned()
}
