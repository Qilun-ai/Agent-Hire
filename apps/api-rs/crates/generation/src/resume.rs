//! The recruiter résumé template and its one-page fit attempts.

use std::sync::LazyLock;

use pdf_writer::{Name, TextStr};
use regex::Regex;

use crate::layout::{self, Align, Color, Layout, Style};
use crate::markdown::{self, Face};

const FIT_FACTORS: [f64; 10] = [1.28, 1.22, 1.16, 1.10, 1.04, 0.98, 0.92, 0.86, 0.80, 0.76];
const INK: Color = [28, 31, 35];
const ACCENT: Color = [31, 78, 121];
const MUTED: Color = [92, 98, 108];
const GRAY: Color = [183, 194, 207];

struct Resume<'a> {
    name: String,
    contact: String,
    sections: Vec<Section<'a>>,
}

struct Section<'a> {
    heading: String,
    lines: Vec<&'a str>,
}

#[derive(Clone, Copy)]
struct Fit {
    factor: f64,
    spacing: f64,
}

#[derive(Debug, PartialEq, Eq)]
enum Line {
    Blank,
    Entry(String),
    Bullet(String),
    Plain(String),
}

struct Limits {
    entries: usize,
    bullets: Option<usize>,
    chars: usize,
    plain_entries: bool,
}

impl Limits {
    fn for_heading(heading: &str) -> Self {
        let (entries, bullets, chars, plain_entries) = match heading {
            "SKILLS" => (6, None, 128, true),
            "PROJECTS" => (3, Some(2), 155, false),
            "EXPERIENCE" => (2, Some(2), 155, false),
            "CERTIFICATES" | "CERTS" => (3, None, 120, true),
            "ACHIEVEMENTS" => (3, None, 130, true),
            "EDUCATION" => (3, None, 130, false),
            _ => (4, Some(2), 145, false),
        };
        Self {
            entries,
            bullets,
            chars,
            plain_entries,
        }
    }
}

impl Fit {
    fn new(factor: f64) -> Self {
        Self {
            factor,
            spacing: 1.0,
        }
    }

    fn style(self, nominal: f64, face: Face, color: Color) -> Style {
        let size = (nominal * self.factor).max(6.2);
        let height = (size * 0.48 * self.spacing.min(1.45)).max(3.35);
        Style {
            size,
            height,
            face,
            color,
        }
    }
}

pub(crate) fn render(text: &str, verbatim: bool) -> Layout {
    let cleaned = markdown::clean(text);
    let document = parse(&cleaned);
    let mut factors = FIT_FACTORS.into_iter().map(Fit::new);
    let mut fit = factors.next().expect("at least one résumé fit factor");
    let mut selected = draw(&document, verbatim, fit);
    for next in factors {
        if !selected.failed {
            break;
        }
        fit = next;
        selected = draw(&document, verbatim, fit);
    }
    let ratio = selected.ratio();
    if ratio < 0.90 {
        fit.spacing = (1.0 + (0.90 - ratio) * 2.2).min(2.20);
        let adjusted = draw(&document, verbatim, fit);
        if !adjusted.failed {
            selected = adjusted;
        }
    }
    selected
}

fn parse(text: &str) -> Resume<'_> {
    let mut name = "Candidate".to_owned();
    let mut contact = Vec::new();
    let mut sections: Vec<Section<'_>> = Vec::new();
    for line in text.lines().map(str::trim) {
        if let Some(heading) = line.strip_prefix("## ") {
            sections.push(Section {
                heading: markdown::strip_inline(heading).to_uppercase(),
                lines: Vec::new(),
            });
        } else if name == "Candidate"
            && let Some(candidate) = line.strip_prefix("# ")
        {
            let candidate = markdown::strip_inline(candidate);
            if !candidate.is_empty() {
                name = candidate;
            }
        } else if let Some(section) = sections.last_mut() {
            section.lines.push(line);
        } else {
            let line = markdown::strip_inline(line);
            if !line.is_empty() {
                contact.push(line);
            }
        }
    }
    Resume {
        name,
        contact: contact.join("  |  "),
        sections,
    }
}

fn normalize(section: &Section<'_>, verbatim: bool) -> Vec<Line> {
    let limits = Limits::for_heading(&section.heading);
    let mut entries = 0;
    let mut bullets = 0;
    let mut lines = Vec::new();
    let mut omitted_entry = false;
    let shorten = |text: &str, limit| {
        if verbatim {
            text.to_owned()
        } else {
            markdown::shorten(text, limit)
        }
    };
    for &line in &section.lines {
        if let Some(title) = line.strip_prefix("### ") {
            entries += 1;
            bullets = 0;
            omitted_entry = !verbatim && entries > limits.entries;
            if !omitted_entry {
                lines.push(Line::Entry(shorten(title, 95)));
            }
        } else if omitted_entry {
            continue;
        } else if line.is_empty() {
            if lines.last().is_some_and(|last| *last != Line::Blank) {
                lines.push(Line::Blank);
            }
        } else if let Some(text) = markdown::bullet(line) {
            if verbatim || limits.bullets.is_none_or(|limit| bullets < limit) {
                bullets += 1;
                lines.push(Line::Bullet(shorten(text, limits.chars)));
            }
        } else {
            entries += usize::from(limits.plain_entries);
            if verbatim || !limits.plain_entries || entries <= limits.entries {
                lines.push(Line::Plain(shorten(line, limits.chars)));
            }
        }
    }
    lines
}

fn draw(document: &Resume<'_>, verbatim: bool, fit: Fit) -> Layout {
    let Fit {
        factor: f,
        spacing: m,
    } = fit;
    let mut layout = Layout::new(215.9, 279.4, 11.0 * f, 10.0 * f);
    let width = layout.usable();
    let name = fit.style(19.0, Face::Bold, ACCENT);
    layout.text(&document.name, 0.0, width, name, Align::Center);
    layout.y += 0.6 * m;
    if !document.contact.is_empty() {
        let contact = fit.style(7.8, Face::Regular, MUTED);
        layout.text(&document.contact, 0.0, width, contact, Align::Center);
        layout.y += 1.2 * f * m;
    }
    layout.rule(10.0 * f, 0.55, ACCENT);
    layout.y += 2.3 * f * m;

    for section in &document.sections {
        if !layout.fit(7.0 * f) {
            break;
        }
        layout.y += f * m;
        layout.text(
            &section.heading,
            0.0,
            width,
            fit.style(8.4, Face::Bold, ACCENT),
            Align::Left,
        );
        layout.y += 0.35 * m;
        layout.rule(0.0, 0.25, GRAY);
        layout.y += 1.1 * f * m;
        for line in normalize(section, verbatim) {
            match line {
                Line::Blank => {
                    if layout.fit(f * m) {
                        layout.y += 0.6 * f * m;
                    }
                }
                Line::Entry(text) => entry(&mut layout, &text, fit, verbatim),
                Line::Bullet(text) => body(&mut layout, &text, fit, true),
                Line::Plain(text) => body(&mut layout, &text, fit, false),
            }
            if layout.failed {
                return layout;
            }
        }
    }
    layout
}

fn entry(layout: &mut Layout, text: &str, fit: Fit, verbatim: bool) {
    if !layout.fit(5.2 * fit.factor) {
        return;
    }
    let shortened;
    let text = if verbatim {
        text
    } else {
        shortened = markdown::shorten(text, 105);
        &shortened
    };
    let text = markdown::strip_inline(text);
    let width = layout.usable();
    let style = fit.style(8.6, Face::Bold, INK);
    if let Some((left, right)) = date_parts(&text) {
        let meta = fit.style(7.8, Face::Regular, MUTED);
        let measured = layout::measure(right, meta);
        let meta_width = (42.0 * fit.factor).min(measured + 2.0);
        let top = layout.y;
        // A wide visual gap must not turn the dates into a separate column in text extraction.
        let text = format!("{left} {right}");
        layout
            .content
            .begin_marked_content_with_properties(Name(b"Span"))
            .properties()
            .actual_text(TextStr(text.trim()));
        layout.text(left, 0.0, width - meta_width - 3.0, style, Align::Left);
        let left_bottom = layout.y;
        layout.y = top;
        let right_width = meta_width.max(measured);
        layout.text(right, width - right_width, right_width, meta, Align::Right);
        layout.content.end_marked_content();
        layout.y = left_bottom.max(top + meta.height) + 0.6 * fit.spacing;
    } else {
        layout.text(&text, 0.0, width, style, Align::Left);
        layout.y += 0.3 * fit.spacing;
    }
}

fn date_parts(title: &str) -> Option<(&str, &str)> {
    static DATE: LazyLock<Regex> = LazyLock::new(|| {
        const MONTH: &str =
            r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+'?\d{2,4}";
        Regex::new(&format!(r"(?i)\s+({MONTH}(?: - (?:present|current|{MONTH}))?|\d{{4}}\s*-\s*(?:present|current|\d{{4}})|\d{{4}})$"))
            .expect("valid résumé date suffix pattern")
    });
    let hit = DATE.find(title)?;
    Some((
        title[..hit.start()].trim_matches([' ', '-', ':']),
        hit.as_str().trim(),
    ))
}

fn body(layout: &mut Layout, text: &str, fit: Fit, bullet: bool) {
    let text = markdown::strip_inline(text);
    if text.is_empty() {
        return;
    }
    let style = fit.style(7.8, Face::Regular, INK);
    let indent = if bullet { 7.0 * fit.factor } else { 0.0 };
    let width = layout.usable() - indent;
    let required_lines = (layout::measure(&text, style) / width + 1.25)
        .floor()
        .max(1.0);
    if !layout.fit(required_lines * style.height) {
        return;
    }
    if bullet {
        let y = layout.y;
        layout.text(
            "-",
            4.0 * fit.factor,
            2.5 * fit.factor,
            fit.style(8.0, Face::Bold, ACCENT),
            Align::Left,
        );
        layout.y = y;
    }
    layout.text(&text, indent, width, style, Align::Left);
    layout.y += if bullet { 0.25 } else { 0.35 } * fit.spacing;
}

#[cfg(test)]
mod tests {
    use super::{ACCENT, Face, Fit, Line, date_parts, normalize, parse};

    #[test]
    fn pins_caps_and_verbatim_behavior() {
        let input = "# A\nmail\nweb\n## Projects\n### 1\n- x\n* y\n+ skip\n### 2\n### 3\n### 4\n- extra\n## Skills\na\nb\nc\nd\ne\nf\ng\n## Education\na\nb\nc\nd";
        let document = parse(input);
        assert_eq!(document.contact, "mail  |  web");
        assert_eq!(
            normalize(&document.sections[0], false),
            [
                Line::Entry("1".into()),
                Line::Bullet("x".into()),
                Line::Bullet("y".into()),
                Line::Entry("2".into()),
                Line::Entry("3".into()),
            ]
        );
        assert_eq!(normalize(&document.sections[0], true).len(), 8);
        assert_eq!(normalize(&document.sections[1], false).len(), 6);
        assert_eq!(normalize(&document.sections[2], false).len(), 4);
    }

    #[test]
    fn pins_resume_size_spacing_and_trailing_dates() {
        let small = Fit {
            factor: 0.76,
            spacing: 2.20,
        }
        .style(7.8, Face::Regular, ACCENT);
        assert_eq!(small.size, 6.2);
        assert!((small.height - (6.2 * 0.48 * 1.45)).abs() < 1e-9);
        assert_eq!(
            Fit::new(0.76).style(7.8, Face::Regular, ACCENT).height,
            3.35
        );
        assert_eq!(
            date_parts("Engineer: Jan '24 - Present"),
            Some(("Engineer", "Jan '24 - Present"))
        );
        assert_eq!(
            date_parts("Engineer - 2021 - 2024"),
            Some(("Engineer", "2021 - 2024"))
        );
        assert_eq!(date_parts("Version 4"), None);
    }
}
