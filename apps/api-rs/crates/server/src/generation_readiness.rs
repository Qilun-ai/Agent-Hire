//! Readiness checks for application document generation.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

use crate::python_text;

static URL: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)https?://\S+|www\.\S+").expect("valid URL pattern"));

/// Returns the first blocker for this lead, in display order.
pub fn blocker(lead: &Value) -> Option<&'static str> {
    if ["input_url_only", "needs_job_description"]
        .iter()
        .any(|key| truthy(&lead["source_meta"][key]))
    {
        return Some(
            "Paste the job description before generating. A URL alone is not enough evidence for a truthful resume.",
        );
    }
    let scalar = |key: &str| collapse(&str_or_empty(&lead[key]));
    let title = scalar("title");
    let description = scalar("description");
    let reason = scalar("reason");
    let points = lead["match_points"]
        .as_array()
        .map_or_else(String::new, |items| {
            items.iter().map(str_or_empty).collect::<Vec<_>>().join(" ")
        });
    let url_only = |text: &str| {
        !text.is_empty()
            && URL
                .replace_all(text, "")
                .chars()
                .all(|c| whitespace(c) || "|,;:(){}[]-_/".contains(c))
    };
    if url_only(&title) && (description.is_empty() || url_only(&description)) {
        return Some(
            "Paste the job description before generating. The current lead only contains a URL.",
        );
    }
    if description.is_empty() && reason.is_empty() && points.is_empty() {
        return Some(
            "Paste the job description before generating. The current lead has no role requirements to tailor against.",
        );
    }
    let context = [title, scalar("company"), description, reason, points].join("\n");
    let context = collapse(&URL.replace_all(&context, ""));
    if context.chars().count() < 40 || context.split(' ').count() < 10 {
        return Some(
            "Paste a fuller job description before generating so the resume can be tailored without guessing.",
        );
    }
    None
}

fn collapse(text: &str) -> String {
    text.split(whitespace)
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

fn whitespace(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}')
}

fn str_or_empty(value: &Value) -> String {
    if truthy(value) {
        python_text::str_of(value)
    } else {
        String::new()
    }
}

pub(super) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}
