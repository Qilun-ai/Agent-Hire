//! The agent's document text: uploads of each kind, and reading web pages.

use std::io::{Cursor, Write};

use agent_hire_contract::{Page, PageServer, Session};
use serde_json::json;

fn multipart(boundary: &str, file_name: &str, content_type: &str, data: &[u8]) -> Vec<u8> {
    let mut body = format!(
        "--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{file_name}\"\r\nContent-Type: {content_type}\r\n\r\n"
    )
    .into_bytes();
    body.extend_from_slice(data);
    body.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
    body
}

fn docx() -> Vec<u8> {
    let mut buffer = Cursor::new(Vec::new());
    {
        let mut writer = zip::ZipWriter::new(&mut buffer);
        writer
            .start_file(
                "word/document.xml",
                zip::write::SimpleFileOptions::default(),
            )
            .expect("zip entry");
        writer
            .write_all(
                br#"<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Priya Raman</w:t></w:r></w:p><w:p><w:r><w:t xml:space="preserve">Backend engineer &amp; SRE</w:t></w:r></w:p><w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr><w:r><w:t>Go</w:t></w:r></w:p><w:p/><w:p><w:pPr><w:numPr/></w:pPr><w:r><w:t>Kafka</w:t></w:r></w:p></w:body></w:document>"#,
            )
            .expect("zip write");
        writer.finish().expect("zip finish");
    }
    buffer.into_inner()
}

#[test]
fn agent_docs() {
    let posting = r#"<html><head><title>Careers</title><script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting","title":"Platform Engineer","description":"<h2>About</h2><p>We run <b>fleets</b> &amp; robots.</p><ul><li>Go</li><li>Kafka</li></ul>","datePosted":"2026-11-01","hiringOrganization":{"@type":"Organization","name":"Nimbus Robotics"},"jobLocation":[{"@type":"Place","address":{"addressLocality":"Hyderabad","addressRegion":"TG","addressCountry":"IN"}}]}</script></head><body><p>ignored</p></body></html>"#;
    let plain = "<!DOCTYPE html><html><head><title>Data Analyst &ndash; Ledgerline</title><style>p{}</style></head><body><nav>Home | Jobs</nav><header>Logo</header><h1>Data Analyst</h1><p>Join our <em>finance</em> team.<br>SQL required.</p><ol><li>Dashboards</li><li>Reports</li></ol><footer>(c) Ledgerline</footer><script>track()</script></body></html>";
    let server = PageServer::start(vec![
        Page {
            path: "/posting".to_owned(),
            status: 200,
            content_type: "text/html; charset=utf-8".to_owned(),
            body: posting.to_owned(),
        },
        Page {
            path: "/plain".to_owned(),
            status: 200,
            content_type: "text/html".to_owned(),
            body: plain.to_owned(),
        },
        Page {
            path: "/file.pdf".to_owned(),
            status: 200,
            content_type: "application/pdf".to_owned(),
            body: "%PDF-1.4".to_owned(),
        },
        Page {
            path: "/gone".to_owned(),
            status: 410,
            content_type: "text/html".to_owned(),
            body: "<html>gone</html>".to_owned(),
        },
    ]);
    let mut s = Session::start("agent_docs");
    let base = server.base_url().to_owned();
    s.redact(&base, "<page-server>");
    let boundary = "agentdocsboundary42";
    let content_type = format!("multipart/form-data; boundary={boundary}");
    s.step_raw(
        "text file",
        "POST",
        "/api/v1/agent-docs/extract",
        &content_type,
        &multipart(
            boundary,
            "notes.txt",
            "text/plain",
            b"Priya Raman\r\nBackend engineer   \n\n\n\nGo, Kafka",
        ),
        "multipart: notes.txt (text)",
    );
    s.step_raw(
        "docx",
        "POST",
        "/api/v1/agent-docs/extract",
        &content_type,
        &multipart(
            boundary,
            "cv.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            &docx(),
        ),
        "multipart: cv.docx (two paragraphs, two numbered items)",
    );
    s.step_raw(
        "pdf",
        "POST",
        "/api/v1/agent-docs/extract",
        &content_type,
        &multipart(
            boundary,
            "resume.pdf",
            "application/pdf",
            include_bytes!("../../fixtures/resume.pdf"),
        ),
        "multipart: resume.pdf (the e2e résumé)",
    );
    s.step_raw(
        "broken docx",
        "POST",
        "/api/v1/agent-docs/extract",
        &content_type,
        &multipart(
            boundary,
            "cv.docx",
            "application/octet-stream",
            b"not a zip",
        ),
        "multipart: cv.docx (not a zip)",
    );
    s.step(
        "read a job posting",
        "POST",
        "/api/v1/agent-docs/read-page",
        Some(json!({"url": format!("{base}/posting")})),
    );
    s.step(
        "read a plain page",
        "POST",
        "/api/v1/agent-docs/read-page",
        Some(json!({"url": format!("{base}/plain")})),
    );
    s.step(
        "not a web page",
        "POST",
        "/api/v1/agent-docs/read-page",
        Some(json!({"url": format!("{base}/file.pdf")})),
    );
    s.step(
        "page error",
        "POST",
        "/api/v1/agent-docs/read-page",
        Some(json!({"url": format!("{base}/gone")})),
    );
    s.step(
        "bad link",
        "POST",
        "/api/v1/agent-docs/read-page",
        Some(json!({"url": "ftp://example.com"})),
    );
    s.step(
        "unreachable",
        "POST",
        "/api/v1/agent-docs/read-page",
        Some(json!({"url": "http://127.0.0.1:9/nothing"})),
    );
    s.finish();
}
