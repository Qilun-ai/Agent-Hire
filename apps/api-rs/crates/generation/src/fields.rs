//! Field access for profile, lead, and structured response objects.

use std::borrow::Cow;

use serde_json::Value;

pub(crate) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|number| number != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(values) => !values.is_empty(),
        Value::Object(values) => !values.is_empty(),
    }
}

pub(crate) fn first<'a>(value: &'a Value, keys: &[&str]) -> &'a Value {
    keys.iter()
        .find_map(|key| value.get(*key).filter(|field| truthy(field)))
        .unwrap_or(&Value::Null)
}

pub(crate) fn string<'a>(value: &'a Value, keys: &[&str]) -> &'a str {
    keys.iter()
        .find_map(|key| value[*key].as_str().filter(|text| !text.is_empty()))
        .unwrap_or_default()
}

pub(crate) fn text(value: &Value) -> Cow<'_, str> {
    match value {
        Value::Null => Cow::Borrowed(""),
        Value::Bool(true) => Cow::Borrowed("True"),
        Value::Bool(false) => Cow::Borrowed("False"),
        Value::String(text) => Cow::Borrowed(text),
        value => Cow::Owned(value.to_string()),
    }
}

pub(crate) fn list<'a>(value: &'a Value, key: &str) -> &'a [Value] {
    value[key].as_array().map_or(&[], Vec::as_slice)
}

pub(crate) fn compact(value: &Value) -> String {
    if let Some(values) = value.as_array() {
        let text: Vec<_> = values.iter().map(text).collect();
        text.join(", ")
    } else {
        text(value).into_owned()
    }
}

pub(crate) fn label(value: &Value) -> &str {
    value
        .as_str()
        .unwrap_or_else(|| string(value, &["title", "name", "n"]))
}
