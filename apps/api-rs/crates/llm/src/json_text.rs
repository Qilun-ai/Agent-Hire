//! Pulling a JSON value out of model text.

use serde_json::Value;

/// The JSON in `text`: the first fenced block if there is one, else the span from the first `{`
/// or `[` to the last matching closer.
pub fn extract_json(text: &str) -> Result<Value, String> {
    let mut body = text.trim();
    if let Some(start) = body.find("```") {
        let after = &body[start + 3..];
        let after = after
            .strip_prefix("json")
            .or_else(|| after.strip_prefix("JSON"))
            .unwrap_or(after);
        if let Some(end) = after.find("```") {
            body = after[..end].trim();
        }
    }
    if let Ok(value) = serde_json::from_str::<Value>(body) {
        return Ok(value);
    }
    let open = body
        .find(['{', '['])
        .ok_or_else(|| "the reply contains no JSON".to_owned())?;
    let closer = if body[open..].starts_with('{') {
        '}'
    } else {
        ']'
    };
    let close = body
        .rfind(closer)
        .filter(|close| *close > open)
        .ok_or_else(|| "the reply's JSON is unterminated".to_owned())?;
    serde_json::from_str::<Value>(&body[open..=close])
        .map_err(|error| format!("the reply's JSON is invalid: {error}"))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn finds_json_in_replies() {
        assert_eq!(extract_json(" {\"a\": 1} "), Ok(json!({"a": 1})));
        assert_eq!(
            extract_json("Sure:\n```json\n{\"a\": [1]}\n```\nDone"),
            Ok(json!({"a": [1]}))
        );
        assert_eq!(
            extract_json("Here you go {\"a\": {\"b\": 2}} thanks"),
            Ok(json!({"a": {"b": 2}}))
        );
        assert!(extract_json("no json here").is_err());
    }
}
