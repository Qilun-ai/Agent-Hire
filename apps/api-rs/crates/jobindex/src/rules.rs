//! Deterministic job rules: posting dates, experience level and the software role family.
//!
//! They are the fallback when no decision model is configured, and they pre-filter candidates
//! before a model is asked (asking a remote model about 200k postings is not an option).

use std::collections::HashSet;
use std::sync::LazyLock;

use regex::{Regex, RegexBuilder};
use serde_json::Value;

use crate::datetime::DateTime;
use crate::levels;

/// The levels a job can have.
pub const LEVELS: [&str; 6] = ["intern", "fresher", "junior", "mid", "senior", "lead"];

fn insensitive(pattern: &str) -> Regex {
    RegexBuilder::new(pattern)
        .case_insensitive(true)
        .build()
        .expect("rule patterns are valid")
}

static RELATIVE: LazyLock<Regex> = LazyLock::new(|| {
    insensitive(r"(?P<n>\d+|an?|one)\s*\+?\s*(?P<unit>minute|min|hour|hr|day|week|month)s?\s+ago")
});
static NOW_WORDS: LazyLock<Regex> =
    LazyLock::new(|| insensitive(r"\b(just now|today|just posted|few hours)\b"));

/// The first string of a value sources publish as a string, number, object or list.
fn scalar(value: &Value) -> Option<String> {
    match value {
        Value::Null => None,
        Value::String(text) => Some(text.clone()),
        Value::Number(number) => Some(match number.as_i64() {
            Some(n) => n.to_string(),
            None => format!("{}", number.as_f64().unwrap_or(0.0).trunc() as i64),
        }),
        Value::Object(map) => ["value", "date", "iso", "label", "text", "raw"]
            .iter()
            .find_map(|key| match map.get(*key) {
                Some(inner @ (Value::String(_) | Value::Number(_))) => scalar(inner),
                _ => None,
            }),
        Value::Array(items) => items.first().and_then(scalar),
        Value::Bool(_) => None,
    }
}

/// `(iso, source)` for a posting date: `source` for an absolute date the company published,
/// `relative` for a label like "Posted 3 days ago" resolved against the fetch time. Labels that do
/// not pin a date ("30+ days ago") give nothing rather than an invented precise date.
pub fn parse_posted(
    value: &Value,
    raw: &Value,
    fetched_at: Option<&str>,
) -> (Option<String>, Option<String>) {
    let value = scalar(value);
    let raw = scalar(raw);
    if let Some(value) = value.as_deref().filter(|value| !value.is_empty())
        && let Some(parsed) = DateTime::parse_absolute(value)
    {
        return (Some(parsed.isoformat()), Some("source".to_owned()));
    }
    let text = raw.as_deref().unwrap_or("").trim().to_lowercase();
    if text.is_empty() {
        return (None, None);
    }
    let base = fetched_at
        .and_then(DateTime::parse_absolute)
        .unwrap_or_else(DateTime::now_utc);
    if text.contains("30+") || text.contains("+ days") {
        return (None, None);
    }
    let relative = |datetime: DateTime| (Some(datetime.isoformat()), Some("relative".to_owned()));
    if NOW_WORDS.is_match(&text) {
        return relative(base);
    }
    if text.contains("yesterday") {
        return relative(base.minus_seconds(86_400));
    }
    if let Some(captures) = RELATIVE.captures(&text) {
        let n_raw = captures["n"].to_lowercase();
        let n: i64 = if matches!(n_raw.as_str(), "a" | "an" | "one") {
            1
        } else {
            n_raw.parse().unwrap_or(0)
        };
        let seconds = match &captures["unit"].to_lowercase()[..] {
            "minute" | "min" => n * 60,
            "hour" | "hr" => n * 3600,
            "day" => n * 86_400,
            "week" => n * 7 * 86_400,
            _ => n * 30 * 86_400,
        };
        return relative(base.minus_seconds(seconds));
    }
    if let Some(parsed) = raw.as_deref().and_then(DateTime::parse_absolute) {
        return (Some(parsed.isoformat()), Some("source".to_owned()));
    }
    (None, None)
}

/// `(level, min_years)` from the stated experience requirement and the title.
pub fn rule_level(title: &str, description: &str) -> (Option<String>, Option<i64>) {
    let result = levels::classify_level(title, description);
    (
        result.level.map(str::to_owned),
        result.years_min.map(|years| years.trunc() as i64),
    )
}

static SOFTWARE_TITLE: LazyLock<Regex> = LazyLock::new(|| {
    insensitive(concat!(
        r"\b(software|developer|programmer|sde|swe|devops|dev ops|sre|site reliability|full[\s-]?stack|",
        r"back[\s-]?end|front[\s-]?end|web developer|mobile|ios|android|flutter|react|node|java|python|golang|",
        r"\.net|c\+\+|machine learning|ml engineer|ai engineer|deep learning|data engineer|data platform|",
        r"platform engineer|cloud engineer|infrastructure engineer|qa engineer|sdet|test automation|automation engineer|",
        r"security engineer|application engineer|systems engineer|embedded software|firmware|blockchain|",
        r"solutions? architect|technical architect|member of technical staff|mts|engineering manager)\b"
    ))
});
static NOT_SOFTWARE_TITLE: LazyLock<Regex> = LazyLock::new(|| {
    insensitive(concat!(
        r"\b(sales|account executive|account manager|business development|recruit(er|ing)|talent acquisition|",
        r"human resources|\bhr\b|marketing|finance|accountant|accounting|legal|counsel|nurse|physician|pharmac|",
        r"mechanical|civil|chemical|electrical engineer|hvac|technician|driver|warehouse|cashier|store|",
        r"customer service|call cent(er|re)|teacher|chef|cook|security guard|receptionist|clerk)\b"
    ))
});
static BUILDER_TITLE: LazyLock<Regex> = LazyLock::new(|| {
    insensitive(
        r"\b(software (engineer|developer|development engineer)|developer|programmer|sde|swe|sdet|devops|site reliability)\b",
    )
});
static AMBIGUOUS_TITLE: LazyLock<Regex> =
    LazyLock::new(|| insensitive(r"\b(engineer|engineering|analyst|scientist|technolog)"));
static BUILDER_WORD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\b(engineer|developer|coder|programming)\b").expect("valid"));

/// Whether a posting is a software or tech-engineering role: the title first, the skills it
/// mentions second (for ambiguous titles like "Engineer" or "Analyst II").
pub fn rule_is_software(title: &str, skills: &[String], software_vocab: &HashSet<String>) -> bool {
    if NOT_SOFTWARE_TITLE.is_match(title) {
        // "Account Executive - Enterprise Software" names software but is a sales job.
        return BUILDER_TITLE.is_match(title);
    }
    if SOFTWARE_TITLE.is_match(title) {
        return true;
    }
    if AMBIGUOUS_TITLE.is_match(title) {
        return skills
            .iter()
            .filter(|skill| software_vocab.contains(*skill))
            .count()
            >= 3;
    }
    false
}

/// The known role family a free-text target role belongs to (only `software` today).
pub fn role_family(role: &str) -> Option<&'static str> {
    let lowered = role.to_lowercase();
    if lowered.is_empty() {
        return None;
    }
    let builder = SOFTWARE_TITLE.is_match(&lowered) || BUILDER_WORD.is_match(&lowered);
    (builder && !NOT_SOFTWARE_TITLE.is_match(&lowered)).then_some("software")
}

/// Maps user words (fresher, entry, experienced, staff ...) onto [`LEVELS`].
pub fn normalize_level(value: &str) -> Option<&'static str> {
    let text = value.trim().to_lowercase();
    let mapped = match text.as_str() {
        "intern" | "internship" | "trainee" => "intern",
        "fresher" | "freshers" | "fresh graduate" | "graduate" | "new grad" | "entry"
        | "entry level" | "entry-level" => "fresher",
        "junior" | "jr" => "junior",
        "mid" | "mid level" | "mid-level" | "intermediate" => "mid",
        "senior" | "sr" | "experienced" => "senior",
        "lead" | "staff" | "principal" | "architect" | "manager" => "lead",
        other => return LEVELS.iter().find(|level| **level == other).copied(),
    };
    Some(mapped)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn posting_dates() {
        let (iso, source) = parse_posted(&json!("2026-11-20"), &Value::Null, None);
        assert_eq!(
            (iso.as_deref(), source.as_deref()),
            (Some("2026-11-20T00:00:00+00:00"), Some("source"))
        );
        let (iso, source) = parse_posted(
            &Value::Null,
            &json!("Posted 3 days ago"),
            Some("2026-11-26T06:00:00+00:00"),
        );
        assert_eq!(
            (iso.as_deref(), source.as_deref()),
            (Some("2026-11-23T06:00:00+00:00"), Some("relative"))
        );
        assert_eq!(
            parse_posted(&Value::Null, &json!("30+ days ago"), None),
            (None, None)
        );
        let (iso, _) = parse_posted(&json!({"value": 1795674408}), &Value::Null, None);
        assert_eq!(iso.as_deref(), Some("2026-11-26T06:26:48+00:00"));
    }

    #[test]
    fn software_titles() {
        let empty = HashSet::new();
        assert!(rule_is_software("Senior Backend Engineer", &[], &empty));
        assert!(!rule_is_software(
            "Account Executive - Software",
            &[],
            &empty
        ));
        assert!(rule_is_software(
            "Sales Engineer, Software Developer",
            &[],
            &empty
        ));
        assert_eq!(role_family("backend dev engineer"), Some("software"));
        assert_eq!(normalize_level("Entry-Level"), Some("fresher"));
    }
}
