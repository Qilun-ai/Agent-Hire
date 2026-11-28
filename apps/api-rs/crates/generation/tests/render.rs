//! PDF interoperability and layout checks with Poppler when installed.

use std::io::ErrorKind;
use std::path::Path;
use std::process::Command;

use agent_hire_generation::{render, render_resume_template};

#[test]
fn poppler_reads_resume_and_letter_in_order() {
    let dir = tempfile::tempdir().expect("temporary PDF directory");
    let resume = render_resume_template(
        "# Inès Rivera\ncontact@example.test\n## Skills\nRust and PostgreSQL\n## Experience\n### Backend Engineer - Jan 2023 - Present\n- Designed reliable data services.\n## Projects\n### Ledger\n- Improved transaction visibility.",
        dir.path(), Path::new("resume.pdf"), false,
    ).expect("render résumé");
    let letter = render(
        "Dear Hiring Team,\n\nI wrote **backend services** with *Rust* and PostgreSQL.\n- Tech: PostgreSQL\n\nSincerely,\nInès Rivera",
        dir.path(), Path::new("letter.pdf"), "cover_letter", true,
    ).expect("render cover letter");
    assert!(resume.used_ratio.is_some());
    assert_eq!(letter.used_ratio, None);
    let extract = |path: &Path| match Command::new("pdftotext").arg(path).arg("-").output() {
        Err(error) if error.kind() == ErrorKind::NotFound => None,
        Err(error) => panic!("launch Poppler: {error}"),
        Ok(output) => {
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
            Some(String::from_utf8(output.stdout).expect("UTF-8 extracted text"))
        }
    };
    let Some(text) = extract(&resume.path) else {
        return;
    };
    in_order(
        &text,
        &[
            "Inès Rivera",
            "contact@example.test",
            "SKILLS",
            "Rust and PostgreSQL",
            "EXPERIENCE",
            "Backend Engineer",
            "Jan 2023 - Present",
            "Designed reliable data services.",
            "PROJECTS",
            "Ledger",
            "Improved transaction visibility.",
        ],
    );
    let text = extract(&letter.path).expect("same Poppler executable");
    in_order(
        &text,
        &[
            "Dear Hiring Team,",
            "I wrote backend services with Rust and PostgreSQL.",
            "Tech: PostgreSQL",
            "Sincerely,",
            "Inès Rivera",
        ],
    );
}

fn in_order(mut text: &str, expected: &[&str]) {
    let complete = text;
    for piece in expected {
        let at = text
            .find(piece)
            .unwrap_or_else(|| panic!("missing or out of order: {piece:?}\n{complete}"));
        text = &text[at + piece.len()..];
    }
}

#[test]
fn writes_only_one_page_even_after_fit_attempts_fail() {
    let dir = tempfile::tempdir().expect("temporary PDF directory");
    let text = format!(
        "# Candidate\n## Experience\n### Role\n{} - LAST\n",
        "- VERBATIM detail that should not make the document grow to multiple pages.\n".repeat(300)
    );
    let file = render_resume_template(&text, dir.path(), Path::new("long.pdf"), true)
        .expect("render one page");
    let bytes = std::fs::read(file.path).expect("read PDF bytes");
    let pdf = String::from_utf8_lossy(&bytes);
    assert!(pdf.contains("/Type /Page\n"));
    assert_eq!(pdf.matches("/Type /Page\n").count(), 1);
    assert!(pdf.contains("/BaseFont /Helvetica"));
    assert!(!pdf.contains("/FontFile"));
    assert!(!pdf.contains("LAST"));
}
