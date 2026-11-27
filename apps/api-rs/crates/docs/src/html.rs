//! Readable text from a fetched page: a schema.org `JobPosting` when the page publishes one,
//! else the visible page as Markdown-ish text (the style of the `html2text` library: `#`
//! headings, `**bold**`, `_italic_`, `  * ` list items, links and images dropped).

use std::sync::LazyLock;

use regex::{Regex, RegexBuilder};
use serde_json::{Value, json};

use crate::extract::tidy;

static TITLE: LazyLock<Regex> = LazyLock::new(|| {
    RegexBuilder::new(r"<title[^>]*>(.*?)</title>")
        .case_insensitive(true)
        .dot_matches_new_line(true)
        .build()
        .expect("valid")
});
static LD_JSON: LazyLock<Regex> = LazyLock::new(|| {
    RegexBuilder::new(r#"<script[^>]+application/ld\+json[^>]*>(.*?)</script>"#)
        .case_insensitive(true)
        .dot_matches_new_line(true)
        .build()
        .expect("valid")
});
static TAG: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)<!--.*?-->|<[^>]*>").expect("valid"));
static DROPPED_BLOCKS: LazyLock<Vec<Regex>> = LazyLock::new(|| {
    [
        "script", "style", "noscript", "svg", "nav", "header", "footer", "form",
    ]
    .iter()
    .map(|tag| {
        RegexBuilder::new(&format!(r"<{tag}\b.*?</{tag}>"))
            .case_insensitive(true)
            .dot_matches_new_line(true)
            .build()
            .expect("valid")
    })
    .collect()
});
static HEAD: LazyLock<Regex> = LazyLock::new(|| {
    RegexBuilder::new(r"<head\b.*?</head>")
        .case_insensitive(true)
        .dot_matches_new_line(true)
        .build()
        .expect("valid")
});

/// Decodes HTML entities (numeric and the common named ones).
pub fn unescape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find('&') {
        out.push_str(&rest[..start]);
        let after = &rest[start + 1..];
        let end = after.find(';').filter(|end| *end <= 12);
        let decoded = end.and_then(|end| {
            let name = &after[..end];
            let character =
                if let Some(hex) = name.strip_prefix("#x").or_else(|| name.strip_prefix("#X")) {
                    u32::from_str_radix(hex, 16).ok().and_then(char::from_u32)
                } else if let Some(number) = name.strip_prefix('#') {
                    number.parse().ok().and_then(char::from_u32)
                } else {
                    named_entity(name)
                };
            character.map(|character| (character, end))
        });
        match decoded {
            Some((character, end)) => {
                out.push(character);
                rest = &after[end + 1..];
            }
            None => {
                out.push('&');
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

fn named_entity(name: &str) -> Option<char> {
    Some(match name {
        "amp" => '&',
        "lt" => '<',
        "gt" => '>',
        "quot" => '"',
        "apos" => '\'',
        "nbsp" => '\u{a0}',
        "ndash" => '–',
        "mdash" => '—',
        "hellip" => '…',
        "rsquo" => '’',
        "lsquo" => '‘',
        "rdquo" => '”',
        "ldquo" => '“',
        "bull" => '•',
        "middot" => '·',
        "copy" => '©',
        "reg" => '®',
        "trade" => '™',
        "euro" => '€',
        "pound" => '£',
        "rupee" => '₹',
        "deg" => '°',
        "times" => '×',
        _ => return None,
    })
}

/// One list being written: ordered lists count their items.
enum List {
    Bulleted,
    Numbered(usize),
}

/// HTML to Markdown-ish text.
pub fn html_to_markdown(html: &str) -> String {
    let html = HEAD.replace_all(html, " ");
    let mut out = String::new();
    let mut lists: Vec<List> = Vec::new();
    let mut quote_depth = 0_usize;
    let mut preformatted = false;
    let mut pending_space = false;
    let mut close_emphasis_pending = false;
    let mut cells_in_row = 0_usize;
    let mut header_row = false;
    let mut header_cells = 0_usize;
    let mut last = 0;
    let at_line_start = |out: &String| out.is_empty() || out.ends_with('\n');
    let block_break = |out: &mut String| {
        let trimmed = out.trim_end_matches(' ').len();
        out.truncate(trimmed);
        if !out.is_empty() {
            out.push_str("\n\n");
        }
    };
    for found in TAG.find_iter(&html) {
        let text = &html[last..found.start()];
        last = found.end();
        if !text.is_empty() {
            let decoded = unescape(text);
            if preformatted {
                for (index, line) in decoded.split('\n').enumerate() {
                    if index > 0 {
                        out.push('\n');
                    }
                    if !line.is_empty() {
                        out.push_str("    ");
                        out.push_str(line);
                    }
                }
            } else {
                let mut words = decoded.split_whitespace().peekable();
                let starts_with_space = decoded.starts_with(char::is_whitespace);
                if words.peek().is_some() {
                    let first_alnum = decoded.trim_start().starts_with(char::is_alphanumeric);
                    if close_emphasis_pending && !starts_with_space && first_alnum {
                        out.push(' ');
                    }
                    if (pending_space || starts_with_space)
                        && !at_line_start(&out)
                        && !out.ends_with(' ')
                    {
                        out.push(' ');
                    }
                    if at_line_start(&out) && quote_depth > 0 {
                        out.push_str(&"> ".repeat(quote_depth));
                    }
                    let joined: Vec<&str> = words.collect();
                    out.push_str(&joined.join(" "));
                    pending_space = decoded.ends_with(char::is_whitespace);
                } else if !decoded.is_empty() {
                    pending_space = true;
                }
                close_emphasis_pending = false;
            }
        }
        let tag = found.as_str();
        if tag.starts_with("<!--") {
            continue;
        }
        let closing = tag.starts_with("</");
        let name: String = tag
            .trim_start_matches("</")
            .trim_start_matches('<')
            .chars()
            .take_while(|c| c.is_ascii_alphanumeric())
            .collect::<String>()
            .to_lowercase();
        match (name.as_str(), closing) {
            ("p" | "div" | "section" | "article" | "table" | "main", _) => {
                block_break(&mut out);
                pending_space = false;
            }
            ("h1" | "h2" | "h3" | "h4" | "h5" | "h6", false) => {
                block_break(&mut out);
                let level: usize = name[1..].parse().unwrap_or(1);
                out.push_str(&"#".repeat(level));
                out.push(' ');
                pending_space = false;
            }
            ("h1" | "h2" | "h3" | "h4" | "h5" | "h6", true) => {
                block_break(&mut out);
                pending_space = false;
            }
            ("br", _) => {
                out.push('\n');
                pending_space = false;
            }
            ("b" | "strong", _) => {
                if !closing && pending_space && !at_line_start(&out) && !out.ends_with(' ') {
                    out.push(' ');
                }
                out.push_str("**");
                pending_space = false;
                close_emphasis_pending = closing;
            }
            ("i" | "em", _) => {
                if !closing && pending_space && !at_line_start(&out) && !out.ends_with(' ') {
                    out.push(' ');
                }
                out.push('_');
                pending_space = false;
                close_emphasis_pending = closing;
            }
            ("ul", false) => {
                if lists.is_empty() {
                    block_break(&mut out);
                }
                lists.push(List::Bulleted);
            }
            ("ol", false) => {
                if lists.is_empty() {
                    block_break(&mut out);
                }
                lists.push(List::Numbered(0));
            }
            ("ul" | "ol", true) => {
                lists.pop();
                if lists.is_empty() {
                    block_break(&mut out);
                }
            }
            ("li", false) => {
                if !at_line_start(&out) {
                    out.push('\n');
                }
                let indent = "  ".repeat(lists.len().max(1));
                let marker = match lists.last_mut() {
                    Some(List::Numbered(count)) => {
                        *count += 1;
                        format!("{count}. ")
                    }
                    _ => "* ".to_owned(),
                };
                out.push_str(&indent);
                out.push_str(&marker);
                pending_space = false;
            }
            ("li", true) if !at_line_start(&out) => out.push('\n'),
            ("blockquote", false) => {
                block_break(&mut out);
                quote_depth += 1;
            }
            ("blockquote", true) => {
                quote_depth = quote_depth.saturating_sub(1);
                block_break(&mut out);
            }
            ("pre", false) => {
                block_break(&mut out);
                preformatted = true;
            }
            ("pre", true) => {
                preformatted = false;
                block_break(&mut out);
            }
            ("tr", false) => {
                cells_in_row = 0;
                header_row = false;
            }
            ("tr", true) => {
                out.push('\n');
                if header_row {
                    out.push_str(&vec!["---"; header_cells.max(1)].join("|"));
                    out.push('\n');
                }
            }
            ("td" | "th", false) => {
                if cells_in_row > 0 {
                    out.push_str("| ");
                }
                if name == "th" {
                    header_row = true;
                }
                cells_in_row += 1;
                header_cells = cells_in_row;
                pending_space = false;
            }
            _ => {}
        }
    }
    let tail = unescape(&html[last..]);
    let words: Vec<&str> = tail.split_whitespace().collect();
    if !words.is_empty() {
        let first_alnum = tail.trim_start().starts_with(char::is_alphanumeric);
        if (close_emphasis_pending && first_alnum && !tail.starts_with(char::is_whitespace))
            || ((pending_space || tail.starts_with(char::is_whitespace)) && !at_line_start(&out))
        {
            out.push(' ');
        }
        out.push_str(&words.join(" "));
    }
    out
}

/// The page's readable text and what kind of source it came from.
pub fn page_text(url: &str, html: &str) -> Value {
    let title = TITLE
        .captures(html)
        .map(|c| unescape(&c[1]).trim().to_owned())
        .unwrap_or_default();
    let mut posting: Option<serde_json::Map<String, Value>> = None;
    for captures in LD_JSON.captures_iter(html) {
        let Ok(data) = serde_json::from_str::<Value>(captures[1].trim()) else {
            continue;
        };
        let items: Vec<Value> = match &data {
            Value::Array(items) => items.clone(),
            Value::Object(map) => map
                .get("@graph")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_else(|| vec![data.clone()]),
            _ => Vec::new(),
        };
        posting = items
            .into_iter()
            .filter_map(|item| item.as_object().cloned())
            .find(|item| {
                let kind = match item.get("@type") {
                    Some(Value::String(text)) => text.clone(),
                    Some(other) => other.to_string(),
                    None => String::new(),
                };
                kind.to_lowercase() == "jobposting"
            });
        if posting.is_some() {
            break;
        }
    }
    if let Some(posting) = posting {
        let organization = match posting.get("hiringOrganization") {
            Some(Value::Object(map)) => map.get("name").cloned().unwrap_or(Value::Null),
            Some(other) => other.clone(),
            None => Value::Null,
        };
        let location = match posting.get("jobLocation") {
            Some(Value::Array(items)) => items.first().cloned(),
            other => other.cloned(),
        };
        let address = location
            .as_ref()
            .and_then(|loc| loc.get("address"))
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let place: Vec<String> = ["addressLocality", "addressRegion", "addressCountry"]
            .iter()
            .filter_map(|key| {
                address
                    .get(*key)
                    .filter(|v| !v.is_null() && v.as_str() != Some(""))
            })
            .map(|value| match value {
                Value::String(text) => text.clone(),
                other => other.to_string(),
            })
            .collect();
        let description = match posting.get("description") {
            Some(Value::String(text)) => text.clone(),
            Some(Value::Null) | None => String::new(),
            Some(other) => other.to_string(),
        };
        let posting_title = posting
            .get("title")
            .filter(|t| !t.is_null() && t.as_str() != Some(""))
            .cloned()
            .unwrap_or(json!(title));
        return json!({
            "url": url,
            "title": posting_title,
            "source": "jobposting",
            "company": organization,
            "location": if place.is_empty() { Value::Null } else { json!(place.join(", ")) },
            "date_posted": posting.get("datePosted").cloned().unwrap_or(Value::Null),
            "text": tidy(&html_to_markdown(&description)),
        });
    }
    let mut body = html.to_owned();
    for pattern in DROPPED_BLOCKS.iter() {
        body = pattern.replace_all(&body, " ").into_owned();
    }
    json!({"url": url, "title": title, "source": "page", "text": tidy(&html_to_markdown(&body))})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn markdown_like_html2text() {
        let html = "<h2>About</h2><p>We build <b>robots</b> &amp; <i>fleets</i>.<br>Join us.</p><ul><li>Go</li><li>Kafka <a href=\"x\">link</a></li></ul><ol><li>One</li><li>Two</li></ol><p>Last</p><div>Div text</div><h3>Req</h3><p>  spaced   words </p>";
        assert_eq!(
            tidy(&html_to_markdown(html)),
            "## About\n\nWe build **robots** & _fleets_.\nJoin us.\n\n  * Go\n  * Kafka link\n\n  1. One\n  2. Two\n\nLast\n\nDiv text\n\n### Req\n\nspaced words"
        );
        assert_eq!(
            tidy(&html_to_markdown("<p>x<strong>y</strong>z</p>")),
            "x**y** z"
        );
        assert_eq!(
            tidy(&html_to_markdown("<blockquote>quoted</blockquote>")),
            "> quoted"
        );
    }

    #[test]
    fn job_posting_json_ld() {
        let page = r#"<html><head><title>Jobs</title><script type="application/ld+json">{"@type":"JobPosting","title":"Engineer","description":"<p>Build things</p>","hiringOrganization":{"name":"Acme"},"jobLocation":{"address":{"addressLocality":"Pune","addressCountry":"IN"}},"datePosted":"2026-11-01"}</script></head><body>x</body></html>"#;
        let result = page_text("https://x", page);
        assert_eq!(result["source"], json!("jobposting"));
        assert_eq!(result["company"], json!("Acme"));
        assert_eq!(result["location"], json!("Pune, IN"));
        assert_eq!(result["text"], json!("Build things"));
        let plain = page_text(
            "https://y",
            "<html><head><title>T &amp; C</title></head><body><nav>menu</nav><h1>Hi</h1></body></html>",
        );
        assert_eq!(plain["title"], json!("T & C"));
        assert_eq!(plain["text"], json!("# Hi"));
    }
}
