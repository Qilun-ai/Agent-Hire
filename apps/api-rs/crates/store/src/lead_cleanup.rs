//! Spotting rows that are not job postings (Hacker News commentary, empty rows).

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value};

const ROLE_TERMS: &[&str] = &[
    "engineer",
    "developer",
    "software",
    "backend",
    "frontend",
    "full stack",
    "devops",
    "sre",
    "data",
    "analyst",
    "designer",
    "product",
    "security",
    "machine learning",
    "ml",
    "ai",
    "research",
    "infrastructure",
    "platform",
    "mobile",
    "ios",
    "android",
    "qa",
    "support",
    "solutions",
    "sales",
    "marketing",
    "operations",
    "founding",
];

const POSTING_TERMS: &[&str] = &[
    "remote",
    "onsite",
    "on-site",
    "hybrid",
    "visa",
    "salary",
    "apply",
    "full-time",
    "part-time",
    "contract",
    "intern",
    "hiring",
    "equity",
    "location",
    "relocation",
];

const HIRING_PHRASES: &[&str] = &[
    "we are hiring",
    "we're hiring",
    "is hiring",
    "are hiring",
    "hiring for",
];

const DISCUSSION_TERMS: &[&str] = &[
    "maybe ",
    "i think",
    "why ",
    "what ",
    "how ",
    "should ",
    "deprecate",
    "tutorial",
    "blog post",
    "newsletter",
    "podcast",
    "comment thread",
    "this thread",
    "discussion",
    "upvote",
    "downvote",
    "karma",
];

const HIRING_TERMS: &[&str] = &[
    "apply",
    "hiring",
    "full-time",
    "part-time",
    "contract",
    "salary",
    "equity",
    "remote",
    "onsite",
    "hybrid",
    "visa",
    "recruiter",
];

static BREAK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)<br\s*/?>").expect("valid pattern"));
static PARAGRAPH_END: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)</p\s*>").expect("valid pattern"));
static TAG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<[^>]+>").expect("valid pattern"));
static SPACES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[ \t]+").expect("valid pattern"));

/// Decodes the HTML entities job feeds use.
pub fn unescape_html(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find('&') {
        out.push_str(&rest[..start]);
        let after = &rest[start + 1..];
        let decoded = after.find(';').filter(|end| *end <= 10).and_then(|end| {
            let name = &after[..end];
            let character =
                if let Some(hex) = name.strip_prefix("#x").or_else(|| name.strip_prefix("#X")) {
                    u32::from_str_radix(hex, 16).ok().and_then(char::from_u32)
                } else if let Some(decimal) = name.strip_prefix('#') {
                    decimal.parse::<u32>().ok().and_then(char::from_u32)
                } else {
                    match name {
                        "amp" => Some('&'),
                        "lt" => Some('<'),
                        "gt" => Some('>'),
                        "quot" => Some('"'),
                        "apos" => Some('\''),
                        "nbsp" => Some('\u{a0}'),
                        "ndash" => Some('\u{2013}'),
                        "mdash" => Some('\u{2014}'),
                        "hellip" => Some('\u{2026}'),
                        "rsquo" => Some('\u{2019}'),
                        "lsquo" => Some('\u{2018}'),
                        "rdquo" => Some('\u{201d}'),
                        "ldquo" => Some('\u{201c}'),
                        "bull" => Some('\u{2022}'),
                        _ => None,
                    }
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

/// HTML turned into plain lines: entities decoded, breaks as newlines, tags dropped, blank lines
/// removed.
pub fn clean_html(text: &str) -> String {
    let text = unescape_html(text);
    let text = BREAK.replace_all(&text, "\n");
    let text = PARAGRAPH_END.replace_all(&text, "\n");
    let text = TAG.replace_all(&text, " ");
    let text = SPACES.replace_all(&text, " ");
    text.lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn any(text: &str, terms: &[&str]) -> bool {
    terms.iter().any(|term| text.contains(term))
}

/// Whether cleaned HN text reads like a job posting.
fn looks_like_posting(cleaned: &str) -> bool {
    if cleaned.chars().count() < 80 {
        return false;
    }
    let lowered = cleaned.to_lowercase();
    if !(any(&lowered, ROLE_TERMS) && any(&lowered, POSTING_TERMS)) {
        return false;
    }
    let first_line = cleaned.lines().next().unwrap_or("");
    first_line.matches('|').count() >= 2 || any(&lowered, HIRING_PHRASES)
}

fn field<'a>(fields: &'a Map<String, Value>, key: &str) -> &'a str {
    fields.get(key).and_then(Value::as_str).unwrap_or("")
}

/// Why a row is junk, sorted; empty when it looks like a real posting.
pub fn reasons(fields: &Map<String, Value>) -> Vec<String> {
    let title = field(fields, "title").trim();
    let url = field(fields, "url").trim();
    let platform = field(fields, "platform").trim().to_lowercase();
    let joined: Vec<&str> = [
        "title",
        "company",
        "platform",
        "url",
        "description",
        "reason",
        "signal_reason",
    ]
    .iter()
    .map(|key| field(fields, key))
    .collect();
    let cleaned = clean_html(&joined.join("\n"));
    let lowered = cleaned.to_lowercase();
    let mut out: Vec<String> = Vec::new();
    if title.is_empty() {
        out.push("missing title".to_owned());
    }
    if url.is_empty() {
        out.push("missing source url".to_owned());
    }
    let lowered_title = title.to_lowercase();
    let hn_story = ["ask hn:", "show hn:", "tell hn:", "launch hn:"]
        .iter()
        .any(|prefix| lowered_title.starts_with(prefix));
    if hn_story && !lowered_title.contains("who is hiring") {
        out.push("HN story/commentary title, not a job".to_owned());
    }
    let from_hn = matches!(platform.as_str(), "hn" | "hackernews" | "hn_hiring")
        || url.contains("news.ycombinator.com/item?id=");
    if from_hn && !looks_like_posting(&clean_html(field(fields, "description"))) {
        out.push("HN item does not match a job-posting pattern".to_owned());
    }
    if any(&lowered, DISCUSSION_TERMS) && !any(&lowered, HIRING_TERMS) {
        out.push("discussion/tutorial content without hiring signal".to_owned());
    }
    out.sort();
    out.dedup();
    out
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn html_cleaning() {
        assert_eq!(clean_html("a &amp; b<br>c</p><b>d</b>  e"), "a & b\nc\nd e");
        assert_eq!(unescape_html("&#39;x&#x27; &bogus;"), "'x' &bogus;");
    }

    #[test]
    fn junk_reasons() {
        let row =
            json!({"title": "Ask HN: why rust?", "url": "", "description": "I think it is good"});
        let reasons = reasons(row.as_object().expect("object"));
        assert_eq!(
            reasons,
            [
                "HN story/commentary title, not a job",
                "discussion/tutorial content without hiring signal",
                "missing source url"
            ]
        );
        let ok = json!({"title": "Backend Engineer", "url": "https://x/jobs/1", "description": "Apply now, remote"});
        assert!(super::reasons(ok.as_object().expect("object")).is_empty());
    }
}
