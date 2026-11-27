//! Normalising responses and comparing transcripts with golden files.
//!
//! Golden files hold what the Python backend answered. Values that legitimately change between
//! runs (timestamps, UUIDs, the session token, temp paths, durations) are replaced with
//! placeholders so a byte-for-byte comparison is meaningful.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

/// Object keys whose numeric or string values vary from run to run.
const VOLATILE_KEYS: &[&str] = &[
    "ts",
    "timestamp",
    "time",
    "uptime",
    "uptime_s",
    "uptime_seconds",
    "elapsed",
    "elapsed_s",
    "elapsed_ms",
    "duration_ms",
    "duration_s",
    "pid",
    "port",
    "token",
];

/// What to replace when normalising one run's output.
pub(crate) struct Redactions {
    /// Literal strings and the placeholder that replaces them wherever they appear.
    pub literals: Vec<(String, String)>,
}

/// Normalises `value` with no run-specific literals; see the module docs.
pub fn normalise(value: &Value) -> Value {
    normalise_with(
        value,
        &Redactions {
            literals: Vec::new(),
        },
    )
}

pub(crate) fn normalise_with(value: &Value, redactions: &Redactions) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = Map::new();
            for (key, inner) in map {
                let normalised = if is_volatile_key(key) && !inner.is_null() {
                    Value::String(placeholder_for(inner).to_owned())
                } else {
                    normalise_with(inner, redactions)
                };
                out.insert(key.clone(), normalised);
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(
            items
                .iter()
                .map(|v| normalise_with(v, redactions))
                .collect(),
        ),
        Value::String(text) => Value::String(normalise_text(text, redactions)),
        Value::Number(number) => match number.as_f64() {
            Some(float) if !number.is_i64() && !number.is_u64() => {
                // Six decimals: float formatting differs between implementations past that.
                let rounded = (float * 1e6).round() / 1e6;
                serde_json::Number::from_f64(rounded).map_or(Value::Null, Value::Number)
            }
            _ => value.clone(),
        },
        Value::Null | Value::Bool(_) => value.clone(),
    }
}

fn is_volatile_key(key: &str) -> bool {
    VOLATILE_KEYS.contains(&key) || key.ends_with("_at") || key.ends_with("_at_ms")
}

fn placeholder_for(value: &Value) -> &'static str {
    match value {
        Value::Number(_) => "<number>",
        Value::String(_) => "<string>",
        _ => "<value>",
    }
}

fn normalise_text(text: &str, redactions: &Redactions) -> String {
    let mut out = text.to_owned();
    for (literal, placeholder) in &redactions.literals {
        if !literal.is_empty() && out.contains(literal.as_str()) {
            out = out.replace(literal.as_str(), placeholder.as_str());
        }
    }
    if is_uuid(&out) {
        return "<uuid>".to_owned();
    }
    replace_datetimes(&out)
}

/// `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` in hex.
fn is_uuid(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 36
        && bytes.iter().enumerate().all(|(i, b)| match i {
            8 | 13 | 18 | 23 => *b == b'-',
            _ => b.is_ascii_hexdigit(),
        })
}

/// Replaces every ISO 8601 date-time (`YYYY-MM-DD[T ]HH:MM[:SS[.frac]][Z|±HH:MM]`) inside
/// `text` with `<datetime>`, including ones embedded in longer strings such as
/// `followup_due=2026-12-02T22:38:48+00:00`. Plain dates are left alone: they are usually data
/// (posting dates), not the clock.
fn replace_datetimes(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut copied_to = 0;
    let mut i = 0;
    while i < bytes.len() {
        match datetime_len(&bytes[i..]) {
            Some(len) if i == 0 || !bytes[i - 1].is_ascii_digit() => {
                out.push_str(&text[copied_to..i]);
                out.push_str("<datetime>");
                i += len;
                copied_to = i;
            }
            _ => i += 1,
        }
    }
    out.push_str(&text[copied_to..]);
    out
}

/// The length of the date-time starting at `bytes[0]`, if one does.
fn datetime_len(bytes: &[u8]) -> Option<usize> {
    let digit = |i: usize| bytes.get(i).is_some_and(u8::is_ascii_digit);
    let is = |i: usize, c: u8| bytes.get(i) == Some(&c);
    let head = (0..4).all(digit)
        && is(4, b'-')
        && digit(5)
        && digit(6)
        && is(7, b'-')
        && digit(8)
        && digit(9)
        && (is(10, b'T') || is(10, b' '))
        && digit(11)
        && digit(12)
        && is(13, b':')
        && digit(14)
        && digit(15);
    if !head {
        return None;
    }
    let mut end = 16;
    if is(end, b':') && digit(end + 1) && digit(end + 2) {
        end += 3;
        if is(end, b'.') && digit(end + 1) {
            end += 1;
            while digit(end) {
                end += 1;
            }
        }
    }
    if is(end, b'Z') {
        end += 1;
    } else if (is(end, b'+') || is(end, b'-'))
        && digit(end + 1)
        && digit(end + 2)
        && is(end + 3, b':')
        && digit(end + 4)
        && digit(end + 5)
    {
        end += 6;
    }
    Some(end)
}

/// A copy of `value` with every object's keys in sorted order, so golden files do not depend on
/// whether `serde_json` keeps insertion order (a feature other workspace crates switch on).
fn sorted(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let mut out = serde_json::Map::new();
            for key in keys {
                out.insert(key.clone(), sorted(&map[key]));
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(sorted).collect()),
        other => other.clone(),
    }
}

/// Compares a transcript with `golden/<group>.json`, or rewrites it when recording.
///
/// # Panics
///
/// Panics when the transcript differs from the golden file (writing `<group>.actual.json` next to
/// it for diffing), when the golden file is missing, or when recording against a non-Python target.
pub(crate) fn check(group: &str, transcript: &Value, recording_allowed: bool) {
    let golden_path = golden_dir().join(format!("{group}.json"));
    let actual_path = golden_dir().join(format!("{group}.actual.json"));
    let rendered = format!(
        "{}\n",
        serde_json::to_string_pretty(&sorted(transcript)).expect("rendering JSON")
    );
    if env::var("CONTRACT_RECORD").as_deref() == Ok("1") {
        assert!(
            recording_allowed,
            "golden files are recorded from the Python backend only"
        );
        fs::write(&golden_path, &rendered).expect("writing the golden file");
        let _ = fs::remove_file(&actual_path);
        return;
    }
    let expected = fs::read_to_string(&golden_path).unwrap_or_else(|_| {
        panic!(
            "{} is missing; record it with CONTRACT_RECORD=1 against Python",
            golden_path.display()
        )
    });
    if expected == rendered {
        let _ = fs::remove_file(&actual_path);
        return;
    }
    fs::write(&actual_path, &rendered).expect("writing the actual transcript");
    let expected_value: Value = serde_json::from_str(&expected).expect("golden file is valid JSON");
    let where_ = first_difference(&expected_value, transcript, "")
        .unwrap_or_else(|| "(formatting only)".to_owned());
    panic!(
        "contract `{group}` differs at {where_}\n  diff {} {}",
        relative(&golden_path),
        relative(&actual_path)
    );
}

/// The JSON-pointer-like path of the first difference, with both values.
fn first_difference(expected: &Value, actual: &Value, at: &str) -> Option<String> {
    match (expected, actual) {
        (Value::Object(e), Value::Object(a)) => {
            for key in e.keys().chain(a.keys()) {
                let path = format!("{at}/{key}");
                match (e.get(key), a.get(key)) {
                    (Some(ev), Some(av)) => {
                        if let Some(found) = first_difference(ev, av, &path) {
                            return Some(found);
                        }
                    }
                    (ev, av) => return Some(format!("{path}: expected {ev:?}, got {av:?}")),
                }
            }
            None
        }
        (Value::Array(e), Value::Array(a)) => {
            for (i, (ev, av)) in e.iter().zip(a).enumerate() {
                if let Some(found) = first_difference(ev, av, &format!("{at}/{i}")) {
                    return Some(found);
                }
            }
            (e.len() != a.len())
                .then(|| format!("{at}: expected {} items, got {}", e.len(), a.len()))
        }
        _ => (expected != actual).then(|| format!("{at}: expected {expected}, got {actual}")),
    }
}

fn golden_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("golden")
}

fn relative(path: &Path) -> String {
    path.strip_prefix(env!("CARGO_MANIFEST_DIR"))
        .unwrap_or(path)
        .display()
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn placeholders_replace_volatile_values() {
        let input = json!({
            "created_at": "2026-11-26T03:00:00Z",
            "id": "0b9a6a52-6a4e-4f7b-9d3c-6a1f2b3c4d5e",
            "posted": "2026-11-20",
            "score": 0.123456789,
            "uptime": 12.5,
            "nested": [{"when": "2026-11-26 03:00:00"}],
        });
        let expected = json!({
            "created_at": "<string>",
            "id": "<uuid>",
            "posted": "2026-11-20",
            "score": 0.123457,
            "uptime": "<number>",
            "nested": [{"when": "<datetime>"}],
        });
        assert_eq!(normalise(&input), expected);
    }

    #[test]
    fn literals_are_redacted_inside_strings() {
        let redactions = Redactions {
            literals: vec![("/tmp/run-1".to_owned(), "<data>".to_owned())],
        };
        let input = json!({"path": "/tmp/run-1/jobs/jobs.db"});
        assert_eq!(
            normalise_with(&input, &redactions),
            json!({"path": "<data>/jobs/jobs.db"})
        );
    }

    #[test]
    fn embedded_datetimes_are_replaced() {
        let input = json!(
            "followup_due=2026-12-02T22:38:48.671993+00:00; next 2026-12-03 09:00Z, on 2026-12-04"
        );
        assert_eq!(
            normalise(&input),
            json!("followup_due=<datetime>; next <datetime>, on 2026-12-04")
        );
    }

    #[test]
    fn first_difference_names_the_path() {
        let found = first_difference(&json!({"a": [1, 2]}), &json!({"a": [1, 3]}), "");
        assert_eq!(found.as_deref(), Some("/a/1: expected 2, got 3"));
    }
}
