//! The generic single-page letter layout and its fit attempts.

use std::sync::LazyLock;

use regex::Regex;

use crate::layout::{self, Align, Layout, Style};
use crate::markdown::{self, Face};

const FIT: [f64; 6] = [1.0, 0.94, 0.88, 0.82, 0.76, 0.70];

pub(crate) fn render(text: &str) -> Layout {
    let cleaned = markdown::clean(text);
    let mut chosen = draw(&cleaned, FIT[0]);
    for factor in FIT.into_iter().skip(1) {
        if !chosen.failed {
            break;
        }
        chosen = draw(&cleaned, factor);
    }
    chosen
}

fn style(nominal: f64, factor: f64, face: Face) -> Style {
    let size = (nominal * factor).max(6.0);
    Style {
        size,
        height: (size * 0.42).max(2.8),
        face,
        color: [0, 0, 0],
    }
}

fn draw(text: &str, factor: f64) -> Layout {
    static NUMBERED: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^\d+\.\s").expect("valid numbered item pattern"));
    let margin = (15.0 * factor).max(8.0);
    let mut layout = Layout::new(210.0, 297.0, margin, margin);
    for line in text.lines().map(str::trim) {
        if line.is_empty() {
            if !layout.fit(1.4) {
                break;
            }
            layout.y += 1.4;
            continue;
        }
        let separator =
            line.len() >= 3 && (line.bytes().all(|b| b == b'-') || line.bytes().all(|b| b == b'*'));
        if separator {
            rule(&mut layout, 1.0, 1.0);
        } else if let Some(heading) = heading(line) {
            let style = style(heading.size, factor, Face::Bold);
            let plain = markdown::strip_inline(heading.text);
            if layout.fit(
                heading.before
                    + estimate(&plain, layout.usable(), style) * style.height
                    + heading.after,
            ) {
                layout.y += heading.before;
                layout.text(&plain, 0.0, layout.usable(), style, Align::Left);
                layout.y += heading.after;
                if heading.rule {
                    rule(&mut layout, 0.0, 0.8);
                }
            }
        } else if let Some(quote) = line.strip_prefix("> ") {
            body(
                &mut layout,
                markdown::styled(quote),
                7.0,
                style(9.4, factor, Face::Regular),
            );
        } else if let Some(bullet) = markdown::bullet(line) {
            let runs = match bullet
                .strip_prefix("Tech:")
                .filter(|rest| rest.starts_with(markdown::whitespace))
            {
                Some(rest) => vec![
                    ("- ".to_owned(), Face::Regular),
                    ("Tech:".to_owned(), Face::Bold),
                    (format!(" {}", markdown::strip_inline(rest)), Face::Regular),
                ],
                None => markdown::styled(&format!("- {bullet}")),
            };
            body(&mut layout, runs, 5.0, style(10.0, factor, Face::Regular));
        } else {
            let indent = if NUMBERED.is_match(line) { 5.0 } else { 0.0 };
            body(
                &mut layout,
                markdown::styled(line),
                indent,
                style(10.0, factor, Face::Regular),
            );
        }
        if layout.failed {
            break;
        }
    }
    layout
}

struct Heading<'a> {
    text: &'a str,
    size: f64,
    before: f64,
    after: f64,
    rule: bool,
}

fn heading(line: &str) -> Option<Heading<'_>> {
    for (prefix, size, before, after, rule) in [
        ("#### ", 10.0, 0.0, 0.4, false),
        ("### ", 10.5, 0.8, 0.4, false),
        ("## ", 12.0, 1.2, 0.6, true),
        ("# ", 15.0, 0.4, 1.0, false),
    ] {
        if let Some(text) = line.strip_prefix(prefix) {
            return Some(Heading {
                text,
                size,
                before,
                after,
                rule,
            });
        }
    }
    None
}

fn body(layout: &mut Layout, runs: Vec<(String, Face)>, indent: f64, style: Style) {
    let text = runs
        .iter()
        .map(|(text, _)| text.as_str())
        .collect::<String>();
    let width = layout.usable() - indent;
    if layout.fit(estimate(&text, width, style) * style.height) {
        layout.styled(&runs, indent, width, style);
    }
}

fn estimate(line: &str, width: f64, style: Style) -> f64 {
    let mut count = 1.0;
    let mut current = 0.0;
    let space = layout::measure(" ", style);
    for word in line
        .split(markdown::whitespace)
        .filter(|word| !word.is_empty())
    {
        let length = layout::measure(word, style);
        if current > 0.0 {
            if current + space + length <= width {
                current += space + length;
                continue;
            }
            count += 1.0;
        }
        count += (length / width).floor();
        current = length % width;
    }
    count
}

fn rule(layout: &mut Layout, before: f64, after: f64) {
    if layout.fit(before + after + 0.3) {
        layout.y += before;
        layout.rule(0.0, 0.2, [135, 135, 135]);
        layout.y += after;
    }
}

#[cfg(test)]
mod tests {
    use super::{Face, style};

    #[test]
    fn pins_generic_size_formula() {
        let body = style(10.0, 0.70, Face::Regular);
        assert_eq!(body.size, 7.0);
        assert!((body.height - 2.94).abs() < 1e-9);
        assert_eq!(style(4.0, 0.70, Face::Regular).height, 2.8);
    }
}
