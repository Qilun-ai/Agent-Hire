//! Shared text rules for canonical profile rows and their deletion tokens.

use std::borrow::Cow;
use std::sync::LazyLock;

use md5::Md5;
use regex::Regex;
use serde_json::Value;

/// Python's whitespace includes the ASCII information separators.
pub(super) fn whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}

pub(super) fn trim(text: &str) -> &str {
    text.trim_matches(whitespace)
}

pub(super) fn collapse(text: &str) -> String {
    text.split(whitespace)
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

pub(super) fn unquote(text: &str) -> Cow<'_, str> {
    percent_encoding::percent_decode_str(text).decode_utf8_lossy()
}

pub(super) fn id(text: &str) -> String {
    use md5::Digest;

    let digest = Md5::digest(text.as_bytes());
    format!("{digest:x}")[..12].to_owned()
}

fn string(value: &Value) -> Cow<'_, str> {
    match value {
        Value::Null => Cow::Borrowed(""),
        Value::String(text) => Cow::Borrowed(text),
        Value::Bool(true) => Cow::Borrowed("True"),
        Value::Bool(false) => Cow::Borrowed("False"),
        other => Cow::Owned(other.to_string()),
    }
}

pub(super) fn truthy_string(value: &Value) -> Cow<'_, str> {
    let empty = match value {
        Value::Bool(flag) => !flag,
        Value::Number(number) => number.as_f64() == Some(0.0),
        Value::Array(items) => items.is_empty(),
        Value::Object(fields) => fields.is_empty(),
        _ => false,
    };
    if empty {
        Cow::Borrowed("")
    } else {
        string(value)
    }
}

pub(super) fn stack(value: &Value) -> Vec<String> {
    match value {
        Value::Array(items) => items
            .iter()
            .map(string)
            .map(|item| trim(&item).to_owned())
            .filter(|item| !item.is_empty())
            .collect(),
        other => truthy_string(other)
            .split(',')
            .map(trim)
            .filter(|item| !item.is_empty())
            .map(str::to_owned)
            .collect(),
    }
}

pub(super) fn summary(text: &str) -> String {
    const CONTACT_PREFIXES: [&str; 10] = [
        "email",
        "phone",
        "mobile",
        "links",
        "linkedin",
        "github",
        "portfolio",
        "website",
        "contact",
        "targeting ",
    ];
    static URL: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?i)\b(?:https?://|www\.)\S+").expect("valid URL pattern"));
    static EMAIL: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}").expect("valid email pattern")
    });
    static PHONE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\+?\d[\d(). -]{7,}\d").expect("valid phone pattern"));

    let cleaned = text
        .split([
            '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
            '\u{2029}',
        ])
        .map(collapse)
        .filter_map(|line| {
            let lower = line.to_lowercase();
            if CONTACT_PREFIXES
                .iter()
                .any(|prefix| lower.starts_with(prefix))
            {
                return None;
            }
            let line = URL.replace_all(&line, "");
            let line = EMAIL.replace_all(&line, "");
            let line = PHONE.replace_all(&line, "");
            let line = collapse(line.trim_matches([' ', '.', ';', '-', '|']));
            (!line.is_empty()).then_some(line)
        })
        .collect::<Vec<_>>()
        .join(" ");
    let lower = cleaned.to_lowercase();
    let markers = ["email", "phone", "links", "linkedin", "github", "http"];
    if markers
        .iter()
        .filter(|marker| lower.contains(**marker))
        .count()
        >= 2
    {
        String::new()
    } else {
        cleaned
    }
}
