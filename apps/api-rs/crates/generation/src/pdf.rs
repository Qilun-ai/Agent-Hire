//! PDF serialization and safe staging for résumé and letter assets.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use pdf_writer::{Name, Pdf, Rect, Ref};

use crate::layout::{self, Layout};
use crate::{letter, resume};

/// A PDF file and the résumé's used vertical ratio, when available.
#[derive(Clone, Debug, PartialEq)]
pub struct Rendered {
    /// The written PDF path.
    pub path: PathBuf,
    /// The selected résumé layout's used height ratio.
    pub used_ratio: Option<f64>,
}

/// Renders a one-page PDF using the résumé template when `kind == "resume"`.
///
/// Relative filenames resolve under `output_dir`. Absolute filenames take precedence.
/// The renderer overwrites an existing file at the resolved path.
pub fn render(
    text: &str,
    output_dir: &Path,
    filename: &Path,
    kind: &str,
    verbatim: bool,
) -> io::Result<Rendered> {
    fs::create_dir_all(output_dir)?;
    let path = output_dir.join(filename);
    let (data, used_ratio) = bytes(text, kind, verbatim);
    stage(&path, &data)?.persist(&path)?;
    Ok(Rendered { path, used_ratio })
}

/// Renders the recruiter's Letter résumé template and returns its fit ratio.
pub fn render_resume_template(
    text: &str,
    output_dir: &Path,
    filename: &Path,
    verbatim: bool,
) -> io::Result<Rendered> {
    render(text, output_dir, filename, "resume", verbatim)
}

pub(crate) fn bytes(text: &str, kind: &str, verbatim: bool) -> (Vec<u8>, Option<f64>) {
    let (layout, used_ratio) = if kind == "resume" {
        let layout = resume::render(text, verbatim);
        let ratio = layout.ratio();
        (layout, Some(ratio))
    } else {
        (letter::render(text), None)
    };
    (pdf(layout), used_ratio)
}

pub(crate) fn stage(path: &Path, data: &[u8]) -> io::Result<tempfile::NamedTempFile> {
    use std::io::Write as _;

    let parent = path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let mut staged = tempfile::NamedTempFile::new_in(parent)?;
    staged.write_all(data)?;
    staged.flush()?;
    Ok(staged)
}

fn pdf(layout: Layout) -> Vec<u8> {
    // pdf-writer supplies the standard Type 1 font dictionaries and content operators
    // directly, so no layout defaults, font embedding, or system font discovery are involved.
    let mut pdf = Pdf::new();
    pdf.set_version(1, 5);
    let catalog = Ref::new(1);
    let pages = Ref::new(2);
    let page = Ref::new(3);
    let contents = Ref::new(4);
    let fonts = [
        (Name(b"F1"), Name(b"Helvetica"), Ref::new(5)),
        (Name(b"F2"), Name(b"Helvetica-Bold"), Ref::new(6)),
        (Name(b"F3"), Name(b"Helvetica-Oblique"), Ref::new(7)),
    ];
    pdf.catalog(catalog).pages(pages);
    pdf.pages(pages).kids([page]).count(1);
    {
        let mut page = pdf.page(page);
        page.parent(pages)
            .media_box(Rect::new(
                0.0,
                0.0,
                layout::points(layout.width),
                layout::points(layout.height),
            ))
            .contents(contents);
        let mut resources = page.resources();
        let mut font_dict = resources.fonts();
        for (resource, _, reference) in fonts {
            font_dict.pair(resource, reference);
        }
    }
    for (_, name, reference) in fonts {
        pdf.type1_font(reference)
            .base_font(name)
            .encoding_predefined(Name(b"WinAnsiEncoding"));
    }
    pdf.stream(contents, &layout.content.finish());
    pdf.finish()
}
