//! Redacting secrets and personal data from logged text, and the frontend error log
//! (`errors.jsonl`, capped at the last 500 lines).

use std::env;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};

use agent_hire_store::{UtcTime, python_json_dumps};
use fancy_regex::Regex;
use serde_json::{Map, Value, json};

const MAX_TEXT: usize = 2000;
const MAX_LINES: usize = 500;

static SECRETS: LazyLock<Vec<Regex>> = LazyLock::new(|| {
    [
        r"ghp_[A-Za-z0-9_]+",
        r"github_pat_[A-Za-z0-9_]+",
        r"sk-[A-Za-z0-9_-]{16,}",
        r"AIza[0-9A-Za-z_-]{20,}",
        r"Bearer\s+[A-Za-z0-9._~+/=-]{10,}",
    ]
    .iter()
    .map(|pattern| Regex::new(pattern).expect("secret patterns are valid"))
    .collect()
});
static ASSIGNMENT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\b(authorization|cookie|password|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*[^\s,;]+")
        .expect("valid pattern")
});
static EMAIL: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b").expect("valid pattern")
});
static PHONE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)").expect("valid pattern"));
static SENSITIVE_KEY: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)authorization|bearer|cookie|password|secret|token|api[_-]?key|private[_-]?key|resume|cover[_-]?letter|profile|email|phone")
        .expect("valid pattern")
});
static FILE_LOCK: Mutex<()> = Mutex::new(());

/// Text with secrets, emails and phone numbers replaced, cut to `max_len` characters.
pub fn redact_text(text: &str, max_len: usize) -> String {
    let mut out = text.to_owned();
    for pattern in SECRETS.iter() {
        out = pattern.replace_all(&out, "[REDACTED_SECRET]").into_owned();
    }
    out = ASSIGNMENT
        .replace_all(&out, "$1=[REDACTED_SECRET]")
        .into_owned();
    out = EMAIL.replace_all(&out, "[REDACTED_EMAIL]").into_owned();
    out = PHONE.replace_all(&out, "[REDACTED_PHONE]").into_owned();
    if out.chars().count() > max_len {
        let head: String = out.chars().take(max_len).collect();
        return format!("{head}...[truncated]");
    }
    out
}

/// A value with sensitive keys masked and text redacted (lists cut at 50, depth at 6).
pub fn redact_value(value: &Value, depth: usize) -> Value {
    if depth > 6 {
        return json!("[REDACTED_DEPTH]");
    }
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) => value.clone(),
        Value::String(text) => json!(redact_text(text, MAX_TEXT)),
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(key, item)| {
                    let masked = SENSITIVE_KEY.is_match(key).unwrap_or(false);
                    (
                        key.clone(),
                        if masked {
                            json!("[REDACTED]")
                        } else {
                            redact_value(item, depth + 1)
                        },
                    )
                })
                .collect(),
        ),
        Value::Array(items) => {
            let mut out: Vec<Value> = items
                .iter()
                .take(50)
                .map(|item| redact_value(item, depth + 1))
                .collect();
            if items.len() > 50 {
                out.push(json!("[TRUNCATED]"));
            }
            Value::Array(out)
        }
    }
}

/// `AGENT_HIRE_ERRORS_JSONL`, else `<data dir>/errors.jsonl`.
pub fn errors_path(data_dir: &Path) -> PathBuf {
    env::var_os("AGENT_HIRE_ERRORS_JSONL")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| data_dir.join("errors.jsonl"))
}

/// Appends a frontend error and keeps the file to its last 500 lines. Failures are ignored.
pub fn log_frontend_error(data_dir: &Path, message: &str, context: &Map<String, Value>) {
    let path = errors_path(data_dir);
    let mut event = Map::new();
    event.insert("ts".to_owned(), json!(UtcTime::now().to_iso_seconds_z()));
    event.insert("type".to_owned(), json!("FrontendError"));
    event.insert("message".to_owned(), json!(redact_text(message, MAX_TEXT)));
    event.insert("traceback".to_owned(), json!(""));
    event.insert(
        "context".to_owned(),
        redact_value(&Value::Object(context.clone()), 0),
    );
    let line = python_json_dumps(&Value::Object(event), false);
    let _guard = FILE_LOCK.lock();
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let Ok(mut file) = fs::OpenOptions::new().create(true).append(true).open(&path) else {
        return;
    };
    let _ = writeln!(file, "{line}");
    drop(file);
    if let Ok(text) = fs::read_to_string(&path) {
        let lines: Vec<&str> = text.lines().collect();
        if lines.len() > MAX_LINES {
            let kept = lines[lines.len() - MAX_LINES..].join("\n");
            let _ = fs::write(&path, format!("{kept}\n"));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redaction() {
        assert_eq!(
            redact_text("key sk-abcdefghijklmnop1234 ok", 2000),
            "key [REDACTED_SECRET] ok"
        );
        assert_eq!(
            redact_text("password: hunter2, next", 2000),
            "password=[REDACTED_SECRET], next"
        );
        assert_eq!(
            redact_text("mail a.b@example.com now", 2000),
            "mail [REDACTED_EMAIL] now"
        );
        assert_eq!(
            redact_text("call +91 98765 43210", 2000),
            "call [REDACTED_PHONE]"
        );
        assert_eq!(redact_text("abcdef", 3), "abc...[truncated]");
        let masked = redact_value(&json!({"email": "x", "url": "y"}), 0);
        assert_eq!(masked, json!({"email": "[REDACTED]", "url": "y"}));
    }
}
