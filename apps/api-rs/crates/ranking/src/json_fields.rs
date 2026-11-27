//! JSON field access and text conversions shared by ranking and feedback.

use serde_json::Value;

pub(crate) fn string<'a>(value: &'a Value, keys: &[&str]) -> &'a str {
    keys.iter()
        .find_map(|key| value[*key].as_str().filter(|text| !text.is_empty()))
        .unwrap_or_default()
}

pub(crate) fn first<'a>(value: &'a Value, keys: &[&str]) -> &'a Value {
    keys.iter()
        .find_map(|key| value.get(key).filter(|field| truthy(field)))
        .unwrap_or(&Value::Null)
}

pub(crate) fn items<'a>(value: &'a Value, key: &str) -> &'a [Value] {
    value[key].as_array().map_or(&[], Vec::as_slice)
}

pub(crate) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

pub(crate) fn stringify(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "None".to_owned(),
        Value::Bool(flag) => if *flag { "True" } else { "False" }.to_owned(),
        other => other.to_string(),
    }
}

pub(crate) fn number(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .or_else(|| value.as_bool().map(|flag| f64::from(u8::from(flag))))
        .or_else(|| value.as_str()?.trim().parse().ok())
        .filter(|number| number.is_finite())
}

pub(crate) fn integer(value: &Value) -> i64 {
    if let Some(text) = value.as_str() {
        text.trim().parse().unwrap_or_default()
    } else {
        number(value).unwrap_or_default() as i64
    }
}

pub(crate) fn clip(text: &str, limit: usize) -> &str {
    text.char_indices()
        .nth(limit)
        .map_or(text, |(end, _)| &text[..end])
}

pub(crate) fn rounded(number: f64, decimals: usize) -> f64 {
    format!("{number:.decimals$}").parse().unwrap_or(number)
}

pub(crate) fn squash(text: &str) -> String {
    text.split(|character: char| character.is_whitespace() || matches!(character, '\x1c'..='\x1f'))
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}
