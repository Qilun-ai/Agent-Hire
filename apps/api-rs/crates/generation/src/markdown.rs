//! Latin-1 normalization and the renderer's inline Markdown subset.

use std::sync::LazyLock;

use regex::Regex;

static BOLD: LazyLock<Regex> = LazyLock::new(|| regex(r"\*\*([^*\n]+)\*\*"));
static ITALIC: LazyLock<Regex> = LazyLock::new(|| regex(r"\*([^*\n]+)\*"));
static CODE: LazyLock<Regex> = LazyLock::new(|| regex(r"`([^`\n]+)`"));
static LINK: LazyLock<Regex> = LazyLock::new(|| regex(r"\[([^\]\n]+)\]\([^)\n]*\)"));
static STYLED: LazyLock<Regex> = LazyLock::new(|| regex(r"\*\*([^*\n]+)\*\*|\*([^*\n]+)\*"));

fn regex(pattern: &str) -> Regex {
    Regex::new(pattern).expect("valid inline Markdown pattern")
}

/// Applies the renderer's substitutions and keeps only Latin-1-reducible text.
#[must_use]
pub fn clean(text: &str) -> String {
    use unicode_normalization::UnicodeNormalization as _;

    let mut output = String::with_capacity(text.len());
    for character in text.chars() {
        let substitute = match character {
            '•' | '‣' | '●' | '▪' | '■' | '▫' | '–' | '‐' | '‑' | '‒' | '·' => {
                "-"
            }
            '▶' => ">",
            '—' | '―' => "--",
            '‘' | '’' => "'",
            '‚' => ",",
            '“' | '”' | '„' => "\"",
            '→' => "->",
            '←' => "<-",
            '↔' => "<->",
            '…' => "...",
            '✓' | '✔' | '✅' => "(check)",
            '✗' | '✘' | '❌' => "(x)",
            '®' => "(R)",
            '©' => "(C)",
            '™' => "(TM)",
            '\u{200b}' | '\u{200c}' | '\u{200d}' | '\u{fffd}' | '\u{1f1e6}'..='\u{1faff}' => "",
            '\u{00a0}' | '\u{202f}' | '\u{2009}' | '\u{2008}' => " ",
            '★' | '☆' => "*",
            '⚠' => "Warning:",
            _ => {
                if character <= '\u{ff}' {
                    output.push(character);
                } else {
                    output.extend(
                        std::iter::once(character)
                            .nfkd()
                            .filter(|part| *part <= '\u{ff}'),
                    );
                }
                continue;
            }
        };
        output.push_str(substitute);
    }
    output
}

/// Strips bold, italic, code and link markers in that order.
#[must_use]
pub fn strip_inline(text: &str) -> String {
    let bold = BOLD.replace_all(text, "$1");
    let italic = ITALIC.replace_all(&bold, "$1");
    let code = CODE.replace_all(&italic, "$1");
    LINK.replace_all(&code, "$1").trim().to_owned()
}

pub(crate) fn squash(text: &str) -> String {
    text.split(whitespace)
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

pub(crate) fn whitespace(value: char) -> bool {
    value.is_whitespace() || matches!(value, '\x1c'..='\x1f')
}

pub(crate) fn clip(text: &str, limit: usize) -> &str {
    text.char_indices()
        .nth(limit)
        .map_or(text, |(end, _)| &text[..end])
}

pub(crate) fn shorten(text: &str, limit: usize) -> String {
    let compact = squash(text);
    if compact.chars().count() <= limit {
        compact
    } else {
        let head = clip(&compact, limit.saturating_sub(1))
            .trim_end_matches([' ', ',', '.', ';', ':', '-']);
        format!("{head}.")
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Face {
    Regular,
    Bold,
    Oblique,
}

pub(crate) fn styled(text: &str) -> Vec<(String, Face)> {
    if !BOLD.is_match(text) {
        return vec![(strip_inline(text), Face::Regular)];
    }
    let links = LINK.replace_all(text, "$1");
    let text = CODE.replace_all(&links, "$1");
    let mut end = 0;
    let mut parts = Vec::new();
    for captures in STYLED.captures_iter(&text) {
        let full = captures.get(0).expect("whole styled span");
        if full.start() > end {
            parts.push((text[end..full.start()].to_owned(), Face::Regular));
        }
        if let Some(part) = captures.get(1) {
            parts.push((part.as_str().to_owned(), Face::Bold));
        } else if let Some(part) = captures.get(2) {
            parts.push((part.as_str().to_owned(), Face::Oblique));
        }
        end = full.end();
    }
    if end < text.len() {
        parts.push((text[end..].to_owned(), Face::Regular));
    }
    parts
}

pub(crate) fn bullet(line: &str) -> Option<&str> {
    line.strip_prefix("- ")
        .or_else(|| line.strip_prefix("* "))
        .or_else(|| line.strip_prefix("+ "))
}

#[cfg(test)]
mod tests {
    use super::{clean, shorten, strip_inline};

    #[test]
    fn pins_substitutions_and_reduction() {
        let text = "•‣●▪■▫▶–‐‑‒—― ‘x’‚ “x”„ →←↔ … ✓✔✅✗✘❌ ®©™ \u{200b}\u{200c}\u{200d}\u{00a0}\u{202f}\u{2009}\u{2008}·★☆⚠ 😀\u{fffd}中文 élève e\u{301} ﬃ";
        assert_eq!(
            clean(text),
            "------>-------- 'x', \"x\"\" -><-<-> ... (check)(check)(check)(x)(x)(x) (R)(C)(TM)     -**Warning:  élève e ffi"
        );
    }

    #[test]
    fn inline_and_shortening_preserve_structural_markers() {
        assert_eq!(
            strip_inline(" - **bold** and *italics* with `code` and [link](https://example.org) "),
            "- bold and italics with code and link"
        );
        assert_eq!(shorten("  first,  second  ", 8), "first.");
        assert_eq!(shorten("---- hello", 4), ".");
    }
}
