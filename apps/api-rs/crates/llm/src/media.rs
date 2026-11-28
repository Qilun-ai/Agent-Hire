//! Résumé pages as images for models that read them, and which models do.
//!
//! An unknown model is treated as text-only: an OpenAI-compatible endpoint is not evidence that
//! it accepts image parts. Pages are rendered locally with `pdftoppm` and kept in memory only.

use std::path::Path;
use std::process::Command;

use serde_json::{Value, json};

const MAX_PDF_BYTES: u64 = 25 * 1024 * 1024;
const MAX_PAGES: usize = 8;
const MAX_RENDERED_PIXELS: u64 = 3_200_000;
const MAX_IMAGE_BYTES: usize = 3 * 1024 * 1024;
const MAX_TOTAL_BASE64_BYTES: usize = 14 * 1024 * 1024;
const RENDER_DPI: &str = "144";
const RENDER_SCALE: &str = "1800";

/// Whether a model accepts a kind of input.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Capability {
    /// Documented as accepted.
    Supported,
    /// Not documented either way; treated as not accepted.
    Unknown,
    /// Documented as not accepted.
    Unsupported,
}

/// One rendered page.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PageImage {
    /// The PNG bytes, base64-encoded.
    pub data_base64: String,
    /// Always `image/png`.
    pub media_type: String,
    /// Pixel width.
    pub width: u32,
    /// Pixel height.
    pub height: u32,
}

/// The rendered pages of a PDF.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PdfMedia {
    /// The pages that fit the size limits.
    pub images: Vec<PageImage>,
    /// How many pages were rendered.
    pub pages_rendered: usize,
    /// How many pages the PDF has, when known.
    pub pages_available: Option<usize>,
}

fn model_matches(model: &str, prefixes: &[&str]) -> bool {
    prefixes
        .iter()
        .any(|prefix| model == *prefix || model.starts_with(&format!("{prefix}-")))
}

/// Whether `provider`'s `model` accepts images (the first value) and PDF files (the second).
pub fn model_capabilities(provider: &str, model: &str) -> (Capability, Capability) {
    let provider = provider.trim().to_lowercase();
    let model = model.trim().to_lowercase();
    match provider.as_str() {
        "openai" => {
            let vision = [
                "gpt-4o", "gpt-4.1", "gpt-5", "gpt-5.1", "gpt-5.2", "gpt-5.5", "gpt-5.6", "gpt-6",
            ];
            if model_matches(&model, &vision)
                || model.starts_with("o1")
                || model.starts_with("o3")
                || model.starts_with("o4")
            {
                (Capability::Supported, Capability::Supported)
            } else if matches!(
                model.as_str(),
                "gpt-4" | "gpt-3.5-turbo" | "text-davinci-003"
            ) {
                (Capability::Unsupported, Capability::Unsupported)
            } else {
                (Capability::Unknown, Capability::Unknown)
            }
        }
        "anthropic" => {
            let claude = [
                "claude-3",
                "claude-4",
                "claude-sonnet",
                "claude-opus",
                "claude-haiku",
            ];
            if claude.iter().any(|prefix| model.starts_with(prefix)) {
                (Capability::Supported, Capability::Unknown)
            } else {
                (Capability::Unknown, Capability::Unknown)
            }
        }
        "gemini" if model.starts_with("gemini") => (Capability::Supported, Capability::Unknown),
        _ => (Capability::Unknown, Capability::Unknown),
    }
}

fn png_size(data: &[u8]) -> Option<(u32, u32)> {
    if data.len() < 24 || &data[..8] != b"\x89PNG\r\n\x1a\n" || &data[12..16] != b"IHDR" {
        return None;
    }
    let width = u32::from_be_bytes(data[16..20].try_into().ok()?);
    let height = u32::from_be_bytes(data[20..24].try_into().ok()?);
    Some((width, height))
}

fn page_count(path: &Path) -> Option<usize> {
    let output = Command::new("pdfinfo").arg("--").arg(path).output().ok()?;
    let text = String::from_utf8_lossy(&output.stdout);
    text.lines()
        .find(|line| line.to_lowercase().starts_with("pages:"))
        .and_then(|line| line.split_once(':'))
        .and_then(|(_, count)| count.trim().parse().ok())
}

/// Renders up to eight pages of the PDF at `path` (blocking; run it off the async threads).
pub fn render_pdf_media(path: &Path, max_pages: usize) -> Result<PdfMedia, String> {
    let size = std::fs::metadata(path)
        .map_err(|_| "document is not available for visual ingestion".to_owned())?
        .len();
    if size == 0 || size > MAX_PDF_BYTES {
        return Err("document exceeds the visual-ingestion size limit".to_owned());
    }
    let available = page_count(path);
    let mut upper = max_pages.clamp(1, MAX_PAGES);
    if let Some(count) = available {
        upper = upper.min(count);
    }
    if upper == 0 {
        return Ok(PdfMedia {
            images: Vec::new(),
            pages_rendered: 0,
            pages_available: available,
        });
    }
    let directory = std::env::temp_dir().join(format!(
        "agent-hire-pdf-{}-{}",
        std::process::id(),
        random_suffix()
    ));
    std::fs::create_dir_all(&directory)
        .map_err(|_| "could not render document pages".to_owned())?;
    let rendered = Command::new("pdftoppm")
        .args([
            "-png",
            "-r",
            RENDER_DPI,
            "-scale-to",
            RENDER_SCALE,
            "-f",
            "1",
            "-l",
            &upper.to_string(),
            "--",
        ])
        .arg(path)
        .arg(directory.join("page"))
        .output();
    let result = match rendered {
        Ok(output) if output.status.success() => collect_pages(&directory, upper, available),
        _ => Err("could not render document pages".to_owned()),
    };
    let _ = std::fs::remove_dir_all(&directory);
    result
}

fn random_suffix() -> String {
    let mut bytes = [0_u8; 6];
    let _ = getrandom::fill(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn collect_pages(
    directory: &Path,
    upper: usize,
    available: Option<usize>,
) -> Result<PdfMedia, String> {
    let mut files: Vec<_> = std::fs::read_dir(directory)
        .map_err(|_| "could not render document pages".to_owned())?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with("page-") && name.ends_with(".png"))
        })
        .collect();
    files.sort();
    let mut images = Vec::new();
    let mut total = 0;
    for file in files.into_iter().take(upper) {
        let Ok(raw) = std::fs::read(&file) else {
            continue;
        };
        if raw.is_empty() || raw.len() > MAX_IMAGE_BYTES {
            continue;
        }
        let Some((width, height)) = png_size(&raw) else {
            continue;
        };
        if width == 0 || height == 0 || u64::from(width) * u64::from(height) > MAX_RENDERED_PIXELS {
            continue;
        }
        let encoded = base64_encode(&raw);
        if total + encoded.len() > MAX_TOTAL_BASE64_BYTES {
            break;
        }
        total += encoded.len();
        images.push(PageImage {
            data_base64: encoded,
            media_type: "image/png".to_owned(),
            width,
            height,
        });
    }
    Ok(PdfMedia {
        pages_rendered: images.len(),
        images,
        pages_available: available,
    })
}

/// Standard base64 with padding.
pub fn base64_encode(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        out.push(char::from(ALPHABET[(n >> 18) as usize & 63]));
        out.push(char::from(ALPHABET[(n >> 12) as usize & 63]));
        out.push(if chunk.len() > 1 {
            char::from(ALPHABET[(n >> 6) as usize & 63])
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            char::from(ALPHABET[n as usize & 63])
        } else {
            '='
        });
    }
    out
}

/// The user content for one API: the text alone, or text plus page images.
pub fn content_parts(protocol: &str, text: &str, media: Option<&PdfMedia>) -> Value {
    let Some(media) = media.filter(|media| !media.images.is_empty()) else {
        return json!(text);
    };
    let data_url =
        |image: &PageImage| format!("data:{};base64,{}", image.media_type, image.data_base64);
    let mut parts = Vec::new();
    match protocol.trim().to_lowercase().as_str() {
        "responses" => {
            parts.push(json!({"type": "input_text", "text": text}));
            for image in &media.images {
                parts.push(
                    json!({"type": "input_image", "image_url": data_url(image), "detail": "auto"}),
                );
            }
        }
        "messages" | "anthropic" => {
            parts.push(json!({"type": "text", "text": text}));
            for image in &media.images {
                parts.push(json!({"type": "image", "source": {"type": "base64", "media_type": image.media_type, "data": image.data_base64}}));
            }
        }
        _ => {
            parts.push(json!({"type": "text", "text": text}));
            for image in &media.images {
                parts.push(json!({"type": "image_url", "image_url": {"url": data_url(image), "detail": "auto"}}));
            }
        }
    }
    Value::Array(parts)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_matches_the_standard() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn capabilities_are_conservative() {
        assert_eq!(
            model_capabilities("openai", "gpt-4o-mini").0,
            Capability::Supported
        );
        assert_eq!(
            model_capabilities("nvidia", "anything").0,
            Capability::Unknown
        );
        assert_eq!(
            model_capabilities("openai", "gpt-4").0,
            Capability::Unsupported
        );
    }
}
