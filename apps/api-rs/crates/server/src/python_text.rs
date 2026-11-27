//! Python's `str()` of JSON values, for settings that the Python backend stored as `str(value)`.

use serde_json::{Number, Value};

/// `str(value)` as Python prints the decoded JSON value: `None` → `""` (settings treat null as
/// empty), booleans `True`/`False`, floats with a `.0`, containers in `repr` form.
pub fn str_of(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(text) => text.clone(),
        other => repr(other),
    }
}

fn repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Number(number) => number_text(number),
        Value::String(text) => string_repr(text),
        Value::Array(items) => {
            let parts: Vec<String> = items.iter().map(repr).collect();
            format!("[{}]", parts.join(", "))
        }
        Value::Object(map) => {
            let parts: Vec<String> = map
                .iter()
                .map(|(key, value)| format!("{}: {}", string_repr(key), repr(value)))
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
    }
}

fn number_text(number: &Number) -> String {
    if number.is_f64() {
        let float = number.as_f64().unwrap_or(0.0);
        if float.is_finite() && float.fract() == 0.0 && float.abs() < 1e16 {
            return format!("{float:.1}");
        }
        let magnitude = float.abs();
        if magnitude != 0.0 && !(1e-4..1e16).contains(&magnitude) {
            // Python switches to exponent form outside this range, with at least two exponent
            // digits and an explicit sign.
            let text = format!("{float:e}");
            if let Some((mantissa, exponent)) = text.split_once('e') {
                let exponent: i32 = exponent.parse().unwrap_or(0);
                let sign = if exponent < 0 { '-' } else { '+' };
                return format!("{mantissa}e{sign}{:02}", exponent.abs());
            }
        }
        return format!("{float}");
    }
    number.to_string()
}

/// Python's `repr` of a string: single quotes unless the text contains one and no double quote.
pub fn string_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for character in text.chars() {
        match character {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn python_str() {
        assert_eq!(str_of(&json!(null)), "");
        assert_eq!(str_of(&json!(true)), "True");
        assert_eq!(str_of(&json!(5)), "5");
        assert_eq!(str_of(&json!(1.0)), "1.0");
        assert_eq!(str_of(&json!(0.5)), "0.5");
        assert_eq!(str_of(&json!(1e20)), "1e+20");
        assert_eq!(str_of(&json!(["a", 1, null])), "['a', 1, None]");
        assert_eq!(str_of(&json!({"k": "it's"})), "{'k': \"it's\"}");
    }
}
