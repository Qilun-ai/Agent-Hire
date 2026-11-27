//! Saved form answers (fill facts), documents and the email monitor without a mailbox.

use agent_hire_contract::Session;
use serde_json::json;

#[test]
fn fill_facts() {
    let mut s = Session::start("fill_facts");
    s.step("empty facts", "GET", "/api/v1/fill/facts", None);
    s.step(
        "save facts and defaults",
        "PUT",
        "/api/v1/fill/facts",
        Some(json!({
            "facts": { "first_name": "Priya", "last_name": "Raman", "notice_period": "30 days", "needs_sponsorship": "No" },
            "defaults": { "current_company": "Ledgerline", "total_years_experience": "6" },
        })),
    );
    s.step("facts saved", "GET", "/api/v1/fill/facts", None);
    s.step(
        "partial update",
        "PUT",
        "/api/v1/fill/facts",
        Some(json!({ "facts": { "city": "Hyderabad" } })),
    );
    s.step("facts merged", "GET", "/api/v1/fill/facts", None);
    s.step(
        "unknown fact key",
        "PUT",
        "/api/v1/fill/facts",
        Some(json!({ "facts": { "not_a_fact": "x" } })),
    );
    s.step(
        "plan for an unknown job",
        "POST",
        "/api/v1/fill/plan",
        Some(json!({ "job_id": "does-not-exist", "fields": [] })),
    );
    s.finish();
}

#[test]
fn documents_and_email() {
    let mut s = Session::start("documents_and_email");
    s.step("no documents", "GET", "/api/v1/documents", None);
    s.step(
        "unknown ingest task",
        "GET",
        "/api/v1/documents/ingest/status?task_id=nope",
        None,
    );
    let boundary = "contractboundary7MA4YWxk";
    let body = format!(
        "--{boundary}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\nresume\r\n\
         --{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"resume.txt\"\r\n\
         Content-Type: text/plain\r\n\r\nPriya Raman\nBackend engineer\nGo, Kafka, PostgreSQL\r\n\
         --{boundary}--\r\n"
    );
    let uploaded = s.step_raw(
        "upload a text resume",
        "POST",
        "/api/v1/documents",
        &format!("multipart/form-data; boundary={boundary}"),
        body.as_bytes(),
        "multipart: kind=resume, file=resume.txt (text/plain, 3 lines)",
    );
    let document_id = uploaded.json()["id"]
        .as_str()
        .unwrap_or_default()
        .to_owned();
    let stored_name = uploaded.json()["file_path"]
        .as_str()
        .and_then(|p| p.rsplit(['/', '\\']).next())
        .and_then(|name| name.split('.').next())
        .unwrap_or_default()
        .to_owned();
    s.redact(&document_id, "<document-id>");
    s.redact(&stored_name, "<stored-file>");
    s.step("documents after upload", "GET", "/api/v1/documents", None);
    s.step(
        "documents by kind",
        "GET",
        "/api/v1/documents?kind=resume",
        None,
    );
    s.step("email not connected", "GET", "/api/v1/email/status", None);
    s.step(
        "poll without a mailbox",
        "POST",
        "/api/v1/email/poll?since_days=14",
        None,
    );
    s.step(
        "check without a mailbox",
        "POST",
        "/api/v1/email/check",
        None,
    );
    s.step(
        "disconnect without a mailbox",
        "POST",
        "/api/v1/email/disconnect",
        None,
    );
    s.step(
        "connect is validated",
        "POST",
        "/api/v1/email/connect",
        Some(json!({ "host": "", "username": "" })),
    );
    s.finish();
}
