//! Agent profile imports and résumé storage with no model credentials.

use agent_hire_contract::Session;
use serde_json::json;

#[test]
fn profile_import() {
    let mut s = Session::start_with_legacy_parser("profile_import");
    import(
        &mut s,
        "import grouped skills and JSON Resume aliases",
        json!({
            "basics": {"name": "Éva Martin", "summary": "Builds reliable payment systems.", "email": "eva@example.net", "location": {"city": "Lyon"}},
            "skills": {"language": ["Go", {"n": "Python"}], "database": "PostgreSQL"},
            "work": [{"position": "Backend Engineer", "name": "Ledgerline", "startDate": "2022-01", "endDate": "2025-01", "highlights": ["Implemented reliable payment processing."]}],
            "projects": [{"name": "Payment Gateway", "technologies": ["Go", "PostgreSQL"], "description": "Processed payments across multiple regions."}],
            "certs": [{"name": "Cloud Practitioner", "issuer": "AWS"}],
            "education": [{"studyType": "Bachelor", "area": "Computer Science", "institution": "Example University"}]
        }),
    );
    import(
        &mut s,
        "import flat and object skills and a string stack",
        json!({
            "skills": ["Docker", {"name": "Apache Kafka", "category": "tool"}, {"name": "Docker"}],
            "projects": [{"title": "Queue Monitor", "stack": "Go, Docker", "impact": "Improved visibility into message processing."}],
            "awards": ["Won the internal engineering award"]
        }),
    );
    s.step_raw(
        "malformed JSON",
        "POST",
        "/api/v1/ingest/profile",
        "application/json",
        b"{",
        "{",
    );
    s.step(
        "non-object JSON",
        "POST",
        "/api/v1/ingest/profile",
        Some(json!([])),
    );
    s.step_raw(
        "oversize JSON",
        "POST",
        "/api/v1/ingest/profile",
        "application/json",
        &vec![b' '; 5 * 1024 * 1024 + 1],
        "5 MiB plus one byte",
    );
    s.step("imported profile", "GET", "/api/v1/profile", None);
    s.step(
        "activity after profile import",
        "GET",
        "/api/v1/events",
        None,
    );

    s.step(
        "select an ingestor with no API key",
        "POST",
        "/api/v1/settings",
        Some(json!({"ingestor_provider": "nvidia"})),
    );

    let boundary = "profile-import-boundary";
    let body = format!(
        "--{boundary}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\nresume\r\n\
         --{boundary}\r\nContent-Disposition: form-data; name=\"ingest\"\r\n\r\ntrue\r\n\
         --{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"resume.txt\"\r\n\
         Content-Type: text/plain\r\n\r\nÉva Martin\nSkills\nGo, Docker\r\n\
         --{boundary}--\r\n"
    );
    let uploaded = s.step_raw(
        "upload a resume without model credentials",
        "POST",
        "/api/v1/documents",
        &format!("multipart/form-data; boundary={boundary}"),
        body.as_bytes(),
        "multipart: resume.txt, kind=resume, ingest=true",
    );
    let document = uploaded.json();
    s.redact(
        document["id"].as_str().expect("document id"),
        "<document-id>",
    );
    let stored = document["file_path"].as_str().expect("stored path");
    let basename = stored.rsplit(['/', '\\']).next().expect("stored filename");
    s.redact(basename.trim_end_matches(".txt"), "<stored-file>");
    s.step(
        "documents after resume upload",
        "GET",
        "/api/v1/documents",
        None,
    );
    s.step(
        "unknown ingest task",
        "GET",
        "/api/v1/documents/ingest/status?task_id=missing",
        None,
    );
    s.step(
        "no asynchronous task",
        "GET",
        "/api/v1/documents/ingest/status",
        None,
    );
    let parsed = s.step(
        "profile after local resume parsing",
        "GET",
        "/api/v1/profile",
        None,
    );
    let resume_id = parsed.json()["skills"][0]["source_resume_ids"][0]
        .as_str()
        .expect("local parsing attached the résumé to a canonical skill");
    s.redact(resume_id, "<resume-id>");
    s.finish();
}

fn import(s: &mut Session, label: &str, payload: serde_json::Value) {
    let reply = s.step(label, "POST", "/api/v1/ingest/profile", Some(payload));
    // Python reports Kùzu and LanceDB work here. SQLite has no equivalent operations.
    for field in ["graph_sync", "vector_sync"] {
        assert!(reply.json()["stats"][field]["status"].is_string());
        s.mask_response_field(&format!("/stats/{field}"), "<backend sync>");
    }
}
