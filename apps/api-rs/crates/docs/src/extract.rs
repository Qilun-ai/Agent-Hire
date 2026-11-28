//! Document files to text: PDF (poppler's `pdftotext`, which keeps the word spacing of
//! LaTeX-made résumés), DOCX (the document XML), images and plain text. Scanned PDFs have no text
//! layer, so their first pages come back as images for a vision-capable model.

use std::io::{Cursor, Read, Write};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::LazyLock;

use agent_hire_llm::render_pdf_media;
use quick_xml::Reader;
use quick_xml::events::Event;
use regex::Regex;
use serde_json::{Value, json};

/// The largest file accepted (20 MB).
pub const MAX_BYTES: usize = 20 * 1024 * 1024;
const MAX_IMAGES: usize = 4;

static TRAILING_SPACE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[ \t]+\n").expect("valid pattern"));
static BLANK_RUNS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\n{3,}").expect("valid pattern"));
static WHITESPACE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s+").expect("valid pattern"));

/// Removes carriage returns and trailing spaces, collapses blank-line runs, trims.
pub fn tidy(text: &str) -> String {
    let text = text.replace('\r', "");
    let text = TRAILING_SPACE.replace_all(&text, "\n");
    BLANK_RUNS.replace_all(&text, "\n\n").trim().to_owned()
}

fn run_with_input(program: &str, args: &[&str], input: &[u8]) -> Option<Vec<u8>> {
    let mut child = Command::new(program)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut stdin = child.stdin.take()?;
    let data = input.to_vec();
    let writer = std::thread::spawn(move || {
        let _ = stdin.write_all(&data);
    });
    let output = child.wait_with_output().ok()?;
    let _ = writer.join();
    output.status.success().then_some(output.stdout)
}

fn pdf_page_count(path: &Path) -> Option<usize> {
    let output = Command::new("pdfinfo").arg("--").arg(path).output().ok()?;
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .find(|line| line.to_lowercase().starts_with("pages:"))
        .and_then(|line| line.split_once(':'))
        .and_then(|(_, count)| count.trim().parse().ok())
}

fn pdf(data: &[u8]) -> Result<Value, String> {
    let directory = std::env::temp_dir().join(format!(
        "agent-hire-doc-{}-{}",
        std::process::id(),
        data.len()
    ));
    std::fs::create_dir_all(&directory).map_err(|e| e.to_string())?;
    let file = directory.join("document.pdf");
    std::fs::write(&file, data).map_err(|e| e.to_string())?;
    let result = (|| {
        let pages_count = pdf_page_count(&file).ok_or("PdfReadError")?;
        let raw = run_with_input("pdftotext", &["-layout", "-enc", "UTF-8", "-", "-"], data)
            .ok_or("PdfReadError")?;
        let text = String::from_utf8_lossy(&raw).into_owned();
        let pages: Vec<&str> = text.split('\u{c}').collect();
        let joined: Vec<String> = pages
            .iter()
            .enumerate()
            .filter(|(_, page)| !page.trim().is_empty())
            .map(|(index, page)| format!("--- page {} ---\n{}", index + 1, tidy(page)))
            .collect();
        let text = joined.join("\n\n");
        let mut method = "pdftotext";
        let mut images: Vec<Value> = Vec::new();
        if WHITESPACE.replace_all(&text, "").chars().count() < 200 {
            method = "pdf-scanned";
            if let Ok(media) = render_pdf_media(&file, MAX_IMAGES) {
                images = media
                    .images
                    .into_iter()
                    .map(|image| json!({"mime": image.media_type, "data": image.data_base64}))
                    .collect();
            }
        }
        Ok::<_, String>(
            json!({"text": text, "pages": pages_count, "method": method, "images": images}),
        )
    })();
    let _ = std::fs::remove_dir_all(&directory);
    result
}

fn docx(data: &[u8]) -> Result<Value, String> {
    let mut archive =
        zip::ZipArchive::new(Cursor::new(data)).map_err(|_| "BadZipFile".to_owned())?;
    let mut xml = String::new();
    archive
        .by_name("word/document.xml")
        .map_err(|_| "KeyError".to_owned())?
        .read_to_string(&mut xml)
        .map_err(|_| "ParseError".to_owned())?;
    let mut reader = Reader::from_str(&xml);
    let mut lines: Vec<String> = Vec::new();
    let (mut paragraph, mut numbered, mut in_text, mut in_paragraph) =
        (String::new(), false, false, false);
    loop {
        match reader.read_event() {
            Ok(Event::Start(tag)) => match tag.name().as_ref() {
                b"w:p" => {
                    in_paragraph = true;
                    paragraph.clear();
                    numbered = false;
                }
                b"w:t" => in_text = true,
                b"w:numPr" if in_paragraph => numbered = true,
                _ => {}
            },
            Ok(Event::Empty(tag)) => match tag.name().as_ref() {
                b"w:p" => lines.push(String::new()),
                b"w:numPr" if in_paragraph => numbered = true,
                _ => {}
            },
            Ok(Event::Text(text)) if in_text => {
                paragraph.push_str(&text.decode().map(|t| t.into_owned()).unwrap_or_default());
            }
            Ok(Event::GeneralRef(reference)) if in_text => {
                let name = reference
                    .decode()
                    .map(|t| t.into_owned())
                    .unwrap_or_default();
                paragraph.push_str(&crate::html::unescape(&format!("&{name};")));
            }
            Ok(Event::End(tag)) => match tag.name().as_ref() {
                b"w:t" => in_text = false,
                b"w:p" => {
                    in_paragraph = false;
                    let line = if numbered && !paragraph.is_empty() {
                        format!("• {paragraph}")
                    } else {
                        paragraph.clone()
                    };
                    lines.push(line);
                }
                _ => {}
            },
            Ok(Event::Eof) => break,
            Err(_) => return Err("ParseError".to_owned()),
            _ => {}
        }
    }
    Ok(json!({"text": tidy(&lines.join("\n")), "pages": null, "method": "docx-xml", "images": []}))
}

fn base64(bytes: &[u8]) -> String {
    agent_hire_llm::base64_encode(bytes)
}

/// `{text, pages, chars, method, images[]}` for an uploaded file; the error names what failed.
pub fn extract(name: &str, data: &[u8]) -> Result<Value, String> {
    let lower = name.to_lowercase();
    let mut result = if lower.ends_with(".pdf") || data.starts_with(b"%PDF-") {
        pdf(data)?
    } else if lower.ends_with(".docx") {
        docx(data)?
    } else if lower.ends_with(".png") || lower.ends_with(".jpg") || lower.ends_with(".jpeg") {
        let mime = if lower.ends_with(".png") {
            "image/png"
        } else {
            "image/jpeg"
        };
        json!({"text": "", "pages": 1, "method": "image", "images": [{"mime": mime, "data": base64(data)}]})
    } else {
        json!({"text": tidy(&String::from_utf8_lossy(data)), "pages": null, "method": "text", "images": []})
    };
    let chars = result["text"]
        .as_str()
        .map_or(0, |text| text.chars().count());
    result["chars"] = json!(chars);
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain_text_and_docx() {
        let text = extract("notes.txt", b"Hello  \r\n\n\n\nWorld").expect("text");
        assert_eq!(text["text"], json!("Hello\n\nWorld"));
        assert_eq!(text["chars"], json!(12));
        let mut buffer = Cursor::new(Vec::new());
        {
            let mut writer = zip::ZipWriter::new(&mut buffer);
            writer
                .start_file(
                    "word/document.xml",
                    zip::write::SimpleFileOptions::default(),
                )
                .expect("file");
            writer
                .write_all(br#"<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>Priya &amp; Co</w:t></w:r></w:p><w:p><w:pPr><w:numPr/></w:pPr><w:r><w:t>Go</w:t></w:r></w:p></w:body></w:document>"#)
                .expect("write");
            writer.finish().expect("finish");
        }
        let docx = extract("cv.docx", buffer.get_ref()).expect("docx");
        assert_eq!(docx["text"], json!("Priya & Co\n• Go"));
        assert!(extract("cv.docx", b"not a zip").is_err());
    }
}
