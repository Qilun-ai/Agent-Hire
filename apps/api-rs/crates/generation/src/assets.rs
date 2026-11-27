//! Versioned asset naming and cover-letter text resolution (§3.3, §11).

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;
use sha1::Sha1;

use crate::fallback::Draft;
use crate::{fields, markdown, pdf};

/// Versioned PDF paths and the résumé fit statistic.
#[derive(Clone, Debug, PartialEq)]
pub struct AssetFiles {
    /// Résumé PDF.
    pub resume: PathBuf,
    /// Cover-letter PDF.
    pub cover_letter: PathBuf,
    /// Version used for both files.
    pub version: u64,
    /// Job ID stem used for naming and version persistence.
    pub safe_job_id: String,
    /// Résumé fit ratio rounded to four decimals.
    pub render_ratio: f64,
}

/// Creates the filename stem, including a hash suffix if the supplied ID changed.
#[must_use]
pub fn safe_job_id(raw: &str) -> String {
    use sha1::Digest as _;

    static UNSAFE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"[^A-Za-z0-9_.-]+").expect("valid filename sanitizer"));
    let raw = raw.trim();
    let safe = UNSAFE.replace_all(raw, "_");
    let safe = safe.trim_matches(['.', '_', '-']);
    let safe = if safe.is_empty() { "manual" } else { safe };
    if raw.is_empty() || safe == raw {
        safe.to_owned()
    } else {
        let digest = format!("{:x}", Sha1::digest(raw.as_bytes()));
        format!("{safe}_{}", &digest[..8])
    }
}

/// Builds the two versioned filenames for a lead ID.
#[must_use]
pub fn asset_filenames(id: &str, version: u64) -> (String, String) {
    let stem = safe_job_id(id);
    (
        format!("{stem}_v{version}.pdf"),
        format!("{stem}_cl_v{version}.pdf"),
    )
}

pub(crate) fn write(
    output: &Path,
    id: &str,
    version: u64,
    draft: &Draft,
    verbatim: bool,
) -> io::Result<AssetFiles> {
    let (resume_name, letter_name) = asset_filenames(id, version);
    let resume = output.join(resume_name);
    let cover_letter = output.join(letter_name);
    fs::create_dir_all(output)?;
    let (resume_data, ratio) = pdf::bytes(&draft.resume, "resume", verbatim);
    let (letter_data, _) = pdf::bytes(&draft.letter, "cover_letter", false);
    let resume_file = pdf::stage(&resume, &resume_data)?;
    let letter_file = pdf::stage(&cover_letter, &letter_data)?;
    letter_file.persist(&cover_letter)?;
    resume_file.persist(&resume)?;
    let ratio = ratio.expect("résumé rendering reports a layout ratio");
    let render_ratio = format!("{ratio:.4}").parse().unwrap_or(ratio);
    Ok(AssetFiles {
        resume,
        cover_letter,
        version,
        safe_job_id: safe_job_id(id),
        render_ratio,
    })
}

/// Resolves the tag-scoped rewrite base from the latest cover-letter document.
#[must_use]
pub fn resolve_cover_letter_base(tag_id: &str, document: Option<&Value>) -> String {
    if tag_id.is_empty() {
        return String::new();
    }
    let Some(document) = document else {
        return String::new();
    };
    let excerpt = fields::string(document, &["excerpt"]);
    if !excerpt.is_empty() {
        return excerpt.to_owned();
    }
    let path = Path::new(fields::string(document, &["file_path"]));
    if !text_file(path) {
        return String::new();
    }
    match fs::read(path) {
        Ok(data) => markdown::clip(&String::from_utf8_lossy(&data), 8000).to_owned(),
        Err(error) => {
            eprintln!("cover-letter base could not be read: {error}");
            String::new()
        }
    }
}

/// Reads the Markdown or text associated with a stored cover-letter asset (§11.6).
#[must_use]
pub fn resolve_cover_letter_text(path: &Path) -> String {
    if path.as_os_str().is_empty() {
        return String::new();
    }
    for candidate in [path.with_extension("md"), path.to_owned()] {
        if !text_file(&candidate) || !candidate.is_file() {
            continue;
        }
        return match fs::read_to_string(&candidate) {
            Ok(text) => text,
            Err(error) => {
                eprintln!(
                    "could not read cover letter {}: {error}",
                    candidate.display()
                );
                String::new()
            }
        };
    }
    let is_pdf = path.extension().is_some_and(|ext| ext == "pdf") && path.is_file();
    eprintln!(
        "no readable cover letter text for asset {} (.pdf exists={})",
        path.display(),
        if is_pdf { "True" } else { "False" }
    );
    String::new()
}

fn text_file(path: &Path) -> bool {
    path.extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| matches!(ext, "md" | "txt"))
}

#[cfg(test)]
mod tests {
    use super::{asset_filenames, safe_job_id};

    #[test]
    fn pins_safe_stems_and_version_suffixes() {
        assert_eq!(
            asset_filenames("run.12-west", 3),
            (
                "run.12-west_v3.pdf".to_owned(),
                "run.12-west_cl_v3.pdf".to_owned()
            )
        );
        assert_eq!(safe_job_id("  "), "manual");
        assert_eq!(safe_job_id("a/b"), "a_b_3ec69c85");
        assert_eq!(safe_job_id("///"), "manual_c64b8480");
    }
}
