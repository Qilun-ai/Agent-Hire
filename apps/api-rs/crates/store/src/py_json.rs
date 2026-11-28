//! JSON text written the way Python's `json.dumps` writes it by default (`", "` and `": "`
//! separators), so stored JSON looks the same whichever backend wrote it.

use serde_json::Value;

/// `json.dumps(value)` with `ensure_ascii` as given.
pub fn dumps(value: &Value, ensure_ascii: bool) -> String {
    let mut out = String::new();
    write(value, ensure_ascii, &mut out);
    out
}

fn write(value: &Value, ensure_ascii: bool, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => {
            if number.is_f64() {
                let float = number.as_f64().unwrap_or(0.0);
                if float.fract() == 0.0 && float.abs() < 1e16 {
                    out.push_str(&format!("{float:.1}"));
                } else {
                    out.push_str(&number.to_string());
                }
            } else {
                out.push_str(&number.to_string());
            }
        }
        Value::String(text) => write_string(text, ensure_ascii, out),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                write(item, ensure_ascii, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (index, (key, item)) in map.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                write_string(key, ensure_ascii, out);
                out.push_str(": ");
                write(item, ensure_ascii, out);
            }
            out.push('}');
        }
    }
}

fn write_string(text: &str, ensure_ascii: bool, out: &mut String) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if ensure_ascii && !c.is_ascii() => {
                let mut units = [0_u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn matches_python_defaults() {
        assert_eq!(dumps(&json!(["a", "b"]), true), r#"["a", "b"]"#);
        assert_eq!(
            dumps(&json!({"k": 1.0, "é": "→"}), false),
            r#"{"k": 1.0, "é": "→"}"#
        );
        assert_eq!(dumps(&json!("é😀"), true), r#""\u00e9\ud83d\ude00""#);
    }
}
