//! Request parsing that fails the way FastAPI does: `422 {"detail": [<pydantic errors>]}`.

use axum::Json;
use axum::body::Bytes;
use axum::extract::{FromRequest, Request};
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde_json::{Map, Value, json};

use crate::respond;

/// Pydantic-style errors collected while reading one request.
#[derive(Debug, Default)]
pub struct Errors {
    items: Vec<Value>,
}

impl Errors {
    /// Adds one error. `context` becomes the `ctx` member when given.
    pub fn push(
        &mut self,
        kind: &str,
        loc: &[Value],
        message: &str,
        input: &Value,
        context: Option<Value>,
    ) {
        let mut item = Map::new();
        item.insert("type".to_owned(), json!(kind));
        item.insert("loc".to_owned(), Value::Array(loc.to_vec()));
        item.insert("msg".to_owned(), json!(message));
        item.insert("input".to_owned(), input.clone());
        if let Some(context) = context {
            item.insert("ctx".to_owned(), context);
        }
        self.items.push(Value::Object(item));
    }

    /// `missing` for a required field.
    pub fn missing(&mut self, loc: &[Value], input: &Value) {
        self.push("missing", loc, "Field required", input, None);
    }

    /// The 422 response, or `None` when there were no errors.
    pub fn into_response(self) -> Option<Response> {
        if self.items.is_empty() {
            return None;
        }
        Some(
            (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({ "detail": self.items })),
            )
                .into_response(),
        )
    }
}

/// One `422` with a single error.
pub fn single(kind: &str, loc: &[Value], message: &str, input: &Value) -> Response {
    let mut errors = Errors::default();
    errors.push(kind, loc, message, input, None);
    errors
        .into_response()
        .unwrap_or_else(|| StatusCode::UNPROCESSABLE_ENTITY.into_response())
}

/// Parses a required JSON body.
pub fn json_body(bytes: &Bytes) -> Result<Value, Response> {
    if bytes.iter().all(u8::is_ascii_whitespace) {
        return Err(single(
            "missing",
            &[json!("body")],
            "Field required",
            &Value::Null,
        ));
    }
    serde_json::from_slice(bytes).map_err(|error| {
        let mut errors = Errors::default();
        errors.push(
            "json_invalid",
            &[json!("body"), json!(error.column().saturating_sub(1))],
            "JSON decode error",
            &json!({}),
            Some(json!({ "error": "Expecting value" })),
        );
        errors
            .into_response()
            .unwrap_or_else(|| StatusCode::UNPROCESSABLE_ENTITY.into_response())
    })
}

/// Parses a required JSON body that must be an object (a Pydantic model).
pub fn object_body(bytes: &Bytes) -> Result<Map<String, Value>, Response> {
    match json_body(bytes)? {
        Value::Object(map) => Ok(map),
        other => Err(single(
            "model_attributes_type",
            &[json!("body")],
            "Input should be a valid dictionary or object to extract fields from",
            &other,
        )),
    }
}

/// A required FastAPI model body, including its JSON media type and decoding checks.
pub struct ModelBody(pub Map<String, Value>);

impl<S: Send + Sync> FromRequest<S> for ModelBody {
    type Rejection = Response;

    async fn from_request(request: Request, state: &S) -> Result<Self, Self::Rejection> {
        match request_json(request, state).await? {
            Value::Null => Err(missing_body()),
            Value::Object(map) => Ok(ModelBody(map)),
            other => Err(single(
                "model_attributes_type",
                &[json!("body")],
                "Input should be a valid dictionary or object to extract fields from",
                &other,
            )),
        }
    }
}

/// A required FastAPI body annotated as `dict`.
pub struct DictBody(pub Map<String, Value>);

impl<S: Send + Sync> FromRequest<S> for DictBody {
    type Rejection = Response;

    async fn from_request(request: Request, state: &S) -> Result<Self, Self::Rejection> {
        match request_json(request, state).await? {
            Value::Null => Err(missing_body()),
            Value::Object(map) => Ok(DictBody(map)),
            other => Err(single(
                "dict_type",
                &[json!("body")],
                "Input should be a valid dictionary",
                &other,
            )),
        }
    }
}

/// An optional FastAPI model body. Null and an empty request both use defaults.
pub struct OptionalModelBody(pub Map<String, Value>);

impl<S: Send + Sync> FromRequest<S> for OptionalModelBody {
    type Rejection = Response;

    async fn from_request(request: Request, state: &S) -> Result<Self, Self::Rejection> {
        match request_json(request, state).await? {
            Value::Null => Ok(OptionalModelBody(Map::new())),
            Value::Object(map) => Ok(OptionalModelBody(map)),
            other => Err(single(
                "model_attributes_type",
                &[json!("body")],
                "Input should be a valid dictionary or object to extract fields from",
                &other,
            )),
        }
    }
}

fn missing_body() -> Response {
    single("missing", &[json!("body")], "Field required", &Value::Null)
}

async fn request_json<S: Send + Sync>(request: Request, state: &S) -> Result<Value, Response> {
    let is_json = request
        .headers()
        .get(header::CONTENT_TYPE)
        .is_none_or(|header| {
            let media_type = header
                .to_str()
                .unwrap_or_default()
                .split(';')
                .next()
                .unwrap_or_default();
            let media_type = media_type.trim().to_ascii_lowercase();
            media_type
                .strip_prefix("application/")
                .is_some_and(|subtype| subtype == "json" || subtype.ends_with("+json"))
        });
    let bytes = Bytes::from_request(request, state)
        .await
        .map_err(IntoResponse::into_response)?;
    let value = if bytes.is_empty() {
        Value::Null
    } else if !is_json {
        json!(String::from_utf8_lossy(&bytes))
    } else {
        let payload = bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&bytes);
        let text = std::str::from_utf8(payload).map_err(|_| {
            respond::detail(
                StatusCode::BAD_REQUEST,
                "There was an error parsing the body",
            )
        })?;
        serde_json::from_str(text).map_err(|error| invalid_model_json(text, &error))?
    };
    Ok(value)
}

/// Python-style character position and diagnostic for an invalid JSON body.
pub fn json_decode_problem(text: &str, error: &serde_json::Error) -> (usize, &'static str) {
    let byte_offset = if error.is_eof() {
        text.len()
    } else {
        let line_offset: usize = text
            .split_inclusive('\n')
            .take(error.line().saturating_sub(1))
            .map(str::len)
            .sum();
        line_offset + error.column().saturating_sub(1)
    };
    let position = text
        .char_indices()
        .take_while(|&(offset, _)| offset < byte_offset)
        .count();
    let message = error.to_string();
    let reason = match message.split_once(" at line ").map(|(reason, _)| reason) {
        Some("key must be a string") => "Expecting property name enclosed in double quotes",
        Some("EOF while parsing an object") if text.trim_end().ends_with(['{', ',']) => {
            "Expecting property name enclosed in double quotes"
        }
        Some("expected `:`") => "Expecting ':' delimiter",
        Some("expected `,` or `}`" | "expected `,` or `]`" | "EOF while parsing an object") => {
            "Expecting ',' delimiter"
        }
        Some("trailing characters") => "Extra data",
        Some("trailing comma") if text.trim_end().ends_with('}') => {
            "Expecting property name enclosed in double quotes"
        }
        _ => "Expecting value",
    };
    (position, reason)
}

fn invalid_model_json(text: &str, error: &serde_json::Error) -> Response {
    let (position, reason) = json_decode_problem(text, error);
    let mut errors = Errors::default();
    errors.push(
        "json_invalid",
        &[json!("body"), json!(position)],
        "JSON decode error",
        &json!({}),
        Some(json!({ "error": reason })),
    );
    errors.into_response().expect("a JSON error was added")
}

/// Reads the fields of one model, collecting errors in field order.
pub struct ModelReader<'a> {
    body: &'a Map<String, Value>,
    errors: Errors,
}

impl<'a> ModelReader<'a> {
    pub fn new(body: &'a Map<String, Value>) -> ModelReader<'a> {
        ModelReader {
            body,
            errors: Errors::default(),
        }
    }

    fn loc(name: &str) -> [Value; 2] {
        [json!("body"), json!(name)]
    }

    /// A string field; `None` means absent, which is an error when `required`.
    pub fn string(
        &mut self,
        name: &str,
        max_length: Option<usize>,
        required: bool,
    ) -> Option<String> {
        let Some(value) = self.body.get(name) else {
            if required {
                self.errors
                    .missing(&Self::loc(name), &Value::Object(self.body.clone()));
            }
            return None;
        };
        let Some(text) = value.as_str() else {
            self.errors.push(
                "string_type",
                &Self::loc(name),
                "Input should be a valid string",
                value,
                None,
            );
            return None;
        };
        if let Some(max) = max_length
            && text.chars().count() > max
        {
            let suffix = if max == 1 { "" } else { "s" };
            self.errors.push(
                "string_too_long",
                &Self::loc(name),
                &format!("String should have at most {max} character{suffix}"),
                value,
                Some(json!({ "max_length": max })),
            );
            return None;
        }
        Some(text.to_owned())
    }

    /// A boolean field in Pydantic's lax mode (`true`/`1`/`"yes"`/`"on"` and so on).
    pub fn boolean(&mut self, name: &str) -> Option<bool> {
        let value = self.body.get(name)?;
        let parsed = match value {
            Value::Bool(flag) => Some(*flag),
            Value::Number(number) => {
                let n = number.as_f64().unwrap_or(f64::NAN);
                if n == 0.0 {
                    Some(false)
                } else if n == 1.0 {
                    Some(true)
                } else {
                    None
                }
            }
            Value::String(text) => match text.to_lowercase().as_str() {
                "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
                "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
                _ => None,
            },
            _ => {
                self.errors.push(
                    "bool_type",
                    &Self::loc(name),
                    "Input should be a valid boolean",
                    value,
                    None,
                );
                return None;
            }
        };
        if parsed.is_none() {
            self.errors.push(
                "bool_parsing",
                &Self::loc(name),
                "Input should be a valid boolean, unable to interpret input",
                value,
                None,
            );
        }
        parsed
    }

    /// A Pydantic boolean field with no default.
    pub fn required_boolean(&mut self, name: &str) -> Option<bool> {
        if !self.body.contains_key(name) {
            self.errors
                .missing(&Self::loc(name), &Value::Object(self.body.clone()));
            return None;
        }
        self.boolean(name)
    }

    // TODO: used once `data/reset` is ported.
    #[allow(dead_code)]
    /// A field that must equal one string literal.
    pub fn literal(&mut self, name: &str, expected: &str, required: bool) -> Option<String> {
        let Some(value) = self.body.get(name) else {
            if required {
                self.errors
                    .missing(&Self::loc(name), &Value::Object(self.body.clone()));
            }
            return None;
        };
        if value.as_str() == Some(expected) {
            return Some(expected.to_owned());
        }
        self.errors.push(
            "literal_error",
            &Self::loc(name),
            &format!("Input should be '{expected}'"),
            value,
            Some(json!({ "expected": format!("'{expected}'") })),
        );
        None
    }

    /// An integer field in Pydantic's lax mode, with inclusive bounds.
    pub fn integer(&mut self, name: &str, min: Option<i64>, max: Option<i64>) -> Option<i64> {
        let value = self.body.get(name)?;
        let number = match parse_integer(value) {
            Ok(number) => number,
            Err((kind, message)) => {
                self.errors
                    .push(kind, &Self::loc(name), message, value, None);
                return None;
            }
        };
        if let Some(min) = min
            && number < min
        {
            self.errors.push(
                "greater_than_equal",
                &Self::loc(name),
                &format!("Input should be greater than or equal to {min}"),
                value,
                Some(json!({ "ge": min })),
            );
            return None;
        }
        if let Some(max) = max
            && number > max
        {
            self.errors.push(
                "less_than_equal",
                &Self::loc(name),
                &format!("Input should be less than or equal to {max}"),
                value,
                Some(json!({ "le": max })),
            );
            return None;
        }
        Some(number)
    }

    /// Reads an optional nested model, preserving the parent path for child errors.
    pub fn optional_model<T>(
        &mut self,
        name: &str,
        read: impl FnOnce(&mut ModelReader<'_>) -> T,
    ) -> Option<T> {
        let value = self.body.get(name)?;
        if value.is_null() {
            return None;
        }
        let Some(object) = value.as_object() else {
            self.errors.push(
                "model_attributes_type",
                &Self::loc(name),
                "Input should be a valid dictionary or object to extract fields from",
                value,
                None,
            );
            return None;
        };
        let mut child = ModelReader::new(object);
        let model = read(&mut child);
        for mut error in child.errors.items {
            if let Some(loc) = error["loc"].as_array_mut() {
                loc.insert(1, json!(name));
            }
            self.errors.items.push(error);
        }
        Some(model)
    }

    /// A required list of Pydantic models, preserving each item's path in validation errors.
    pub fn model_list<T>(
        &mut self,
        name: &str,
        max_items: usize,
        mut read: impl FnMut(&mut ModelReader<'_>) -> T,
    ) -> Vec<T> {
        let path = Self::loc(name);
        let Some(value) = self.body.get(name) else {
            self.errors
                .missing(&path, &Value::Object(self.body.clone()));
            return Vec::new();
        };
        let Some(items) = value.as_array() else {
            self.errors.push(
                "list_type",
                &path,
                "Input should be a valid list",
                value,
                None,
            );
            return Vec::new();
        };
        if items.len() > max_items {
            self.errors.push(
                "too_long",
                &path,
                &format!("List should have at most {max_items} items after validation, not {}", items.len()),
                value,
                Some(json!({"field_type": "List", "max_length": max_items, "actual_length": items.len()})),
            );
            return Vec::new();
        }
        let mut out = Vec::with_capacity(items.len());
        for (index, item) in items.iter().enumerate() {
            let Some(object) = item.as_object() else {
                self.errors.push(
                    "model_attributes_type",
                    &[json!("body"), json!(name), json!(index)],
                    "Input should be a valid dictionary or object to extract fields from",
                    item,
                    None,
                );
                continue;
            };
            let mut child = ModelReader::new(object);
            out.push(read(&mut child));
            for mut error in child.errors.items {
                if let Some(loc) = error["loc"].as_array_mut() {
                    loc.splice(1..1, [json!(name), json!(index)]);
                }
                self.errors.items.push(error);
            }
        }
        out
    }

    /// A list of strings, at most `max_items` long.
    pub fn string_list(&mut self, name: &str, max_items: Option<usize>) -> Option<Vec<String>> {
        let value = self.body.get(name)?;
        let Some(items) = value.as_array() else {
            self.errors.push(
                "list_type",
                &Self::loc(name),
                "Input should be a valid list",
                value,
                None,
            );
            return None;
        };
        let mut out = Vec::with_capacity(items.len());
        let mut ok = true;
        for (index, item) in items.iter().enumerate() {
            match item.as_str() {
                Some(text) => out.push(text.to_owned()),
                None => {
                    ok = false;
                    self.errors.push(
                        "string_type",
                        &[json!("body"), json!(name), json!(index)],
                        "Input should be a valid string",
                        item,
                        None,
                    );
                }
            }
        }
        if let Some(max) = max_items
            && items.len() > max
        {
            let noun = if max == 1 { "item" } else { "items" };
            self.errors.push(
                "too_long",
                &Self::loc(name),
                &format!("List should have at most {max} {noun} after validation, not {}", items.len()),
                value,
                Some(json!({ "field_type": "List", "max_length": max, "actual_length": items.len() })),
            );
            return None;
        }
        ok.then_some(out)
    }

    /// A `str | None` field: absent and `null` are both `None`.
    pub fn optional_string(&mut self, name: &str) -> Option<String> {
        match self.body.get(name) {
            None | Some(Value::Null) => None,
            Some(_) => self.string(name, None, false),
        }
    }

    /// A `Literal[...] | None` field.
    pub fn optional_one_of(&mut self, name: &str, allowed: &[&str]) -> Option<String> {
        match self.body.get(name) {
            None | Some(Value::Null) => None,
            Some(_) => self.one_of(name, allowed, false),
        }
    }

    /// A string field limited to `allowed` values (a Pydantic `Literal`).
    pub fn one_of(&mut self, name: &str, allowed: &[&str], required: bool) -> Option<String> {
        let Some(value) = self.body.get(name) else {
            if required {
                self.errors
                    .missing(&Self::loc(name), &Value::Object(self.body.clone()));
            }
            return None;
        };
        if let Some(text) = value.as_str()
            && allowed.contains(&text)
        {
            return Some(text.to_owned());
        }
        let message = literal_message(allowed);
        self.errors.push(
            "literal_error",
            &Self::loc(name),
            &format!("Input should be {message}"),
            value,
            Some(json!({ "expected": message })),
        );
        None
    }

    /// A `float | None` field with inclusive bounds.
    pub fn number(&mut self, name: &str, min: Option<f64>, max: Option<f64>) -> Option<f64> {
        let value = self.body.get(name)?;
        let parsed = match value {
            Value::Null => return None,
            Value::Number(number) => number.as_f64(),
            Value::String(text) => match text.trim().parse::<f64>() {
                Ok(number) => Some(number),
                Err(_) => {
                    self.errors.push(
                        "float_parsing",
                        &Self::loc(name),
                        "Input should be a valid number, unable to parse string as a number",
                        value,
                        None,
                    );
                    return None;
                }
            },
            _ => None,
        };
        let Some(number) = parsed else {
            self.errors.push(
                "float_type",
                &Self::loc(name),
                "Input should be a valid number",
                value,
                None,
            );
            return None;
        };
        let render = |bound: f64| {
            if bound.fract() == 0.0 {
                json!(bound as i64)
            } else {
                json!(bound)
            }
        };
        if let Some(min) = min
            && number < min
        {
            self.errors.push(
                "greater_than_equal",
                &Self::loc(name),
                &format!("Input should be greater than or equal to {}", render(min)),
                value,
                Some(json!({ "ge": min })),
            );
            return None;
        }
        if let Some(max) = max
            && number > max
        {
            self.errors.push(
                "less_than_equal",
                &Self::loc(name),
                &format!("Input should be less than or equal to {}", render(max)),
                value,
                Some(json!({ "le": max })),
            );
            return None;
        }
        Some(number)
    }

    /// A `bool | None` field.
    pub fn optional_boolean(&mut self, name: &str) -> Option<bool> {
        match self.body.get(name) {
            None | Some(Value::Null) => None,
            Some(_) => self.boolean(name),
        }
    }

    /// A string field that must match `pattern` (checked by `accepts`), as `Field(pattern=...)`.
    pub fn patterned(
        &mut self,
        name: &str,
        pattern: &str,
        accepts: impl Fn(&str) -> bool,
        required: bool,
    ) -> Option<String> {
        let text = self.string(name, None, required)?;
        if accepts(&text) {
            return Some(text);
        }
        self.errors.push(
            "string_pattern_mismatch",
            &Self::loc(name),
            &format!("String should match pattern '{pattern}'"),
            &json!(text),
            Some(json!({ "pattern": pattern })),
        );
        None
    }

    /// A string field with a minimum length too.
    pub fn bounded_string(
        &mut self,
        name: &str,
        min: usize,
        max: usize,
        required: bool,
    ) -> Option<String> {
        let text = self.string(name, Some(max), required)?;
        if text.chars().count() < min {
            let suffix = if min == 1 { "" } else { "s" };
            self.errors.push(
                "string_too_short",
                &Self::loc(name),
                &format!("String should have at least {min} character{suffix}"),
                &json!(text),
                Some(json!({ "min_length": min })),
            );
            return None;
        }
        Some(text)
    }

    /// A required list of strings with a minimum length.
    pub fn required_list(&mut self, name: &str, min: usize, max: usize) -> Option<Vec<String>> {
        if !self.body.contains_key(name) {
            self.errors
                .missing(&Self::loc(name), &Value::Object(self.body.clone()));
            return None;
        }
        let items = self.string_list(name, Some(max))?;
        if items.len() < min {
            let noun = if min == 1 { "item" } else { "items" };
            self.errors.push(
                "too_short",
                &Self::loc(name),
                &format!("List should have at least {min} {noun} after validation, not {}", items.len()),
                &json!(items),
                Some(json!({ "field_type": "List", "min_length": min, "actual_length": items.len() })),
            );
            return None;
        }
        Some(items)
    }

    /// Flags every key not in `allowed` (`extra="forbid"` models).
    pub fn forbid_extra(&mut self, allowed: &[&str]) {
        for (key, value) in self.body {
            if !allowed.contains(&key.as_str()) {
                self.errors.push(
                    "extra_forbidden",
                    &Self::loc(key),
                    "Extra inputs are not permitted",
                    value,
                    None,
                );
            }
        }
    }

    /// The collected errors as a 422, if any.
    pub fn finish(self) -> Result<(), Response> {
        match self.errors.into_response() {
            Some(response) => Err(response),
            None => Ok(()),
        }
    }
}

fn parse_integer(value: &Value) -> Result<i64, (&'static str, &'static str)> {
    const SIZE: (&str, &str) = (
        "int_parsing_size",
        "Unable to parse input string as an integer, exceeded maximum size",
    );
    const PARSING: (&str, &str) = (
        "int_parsing",
        "Input should be a valid integer, unable to parse string as an integer",
    );
    let saturated = |text: &str| {
        if text.starts_with('-') {
            i64::MIN
        } else {
            i64::MAX
        }
    };
    match value {
        Value::Bool(flag) => Ok(i64::from(*flag)),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                return Ok(integer);
            }
            let repr = number.to_string();
            if !repr.contains(['.', 'e', 'E']) {
                // Preserve the original integer in the validation error even when it cannot
                // fit in i64. Saturation suffices for the bounded model fields.
                return Ok(saturated(&repr));
            }
            let float = number.as_f64().ok_or(SIZE)?;
            if !(i64::MIN as f64..-(i64::MIN as f64)).contains(&float) {
                return Err(SIZE);
            }
            if float.fract() != 0.0 {
                return Err((
                    "int_from_float",
                    "Input should be a valid integer, got a number with a fractional part",
                ));
            }
            Ok(float as i64)
        }
        Value::String(text) => {
            use std::sync::LazyLock;

            use regex::Regex;

            static INTEGER: LazyLock<Regex> = LazyLock::new(|| {
                Regex::new(r"^[+-]?[0-9]+(?:_[0-9]+)*(?:\.0+)?$").expect("valid integer pattern")
            });
            let text = text.trim();
            if !INTEGER.is_match(text) {
                return Err(PARSING);
            }
            let integer = text.split('.').next().unwrap_or_default();
            let integer = integer.replace('_', "");
            Ok(integer
                .parse::<i64>()
                .unwrap_or_else(|_| saturated(&integer)))
        }
        _ => Err(("int_type", "Input should be a valid integer")),
    }
}

/// Decodes `%XX` escapes; invalid escapes are kept as written.
pub fn decode_path(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%'
            && let Some(hex) = input.get(index + 1..index + 3)
            && let Ok(byte) = u8::from_str_radix(hex, 16)
        {
            out.push(byte);
            index += 3;
            continue;
        }
        out.push(bytes[index]);
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Query-string pairs, percent-decoded, in order.
pub fn query_pairs(query: Option<&str>) -> Vec<(String, String)> {
    url::form_urlencoded::parse(query.unwrap_or("").as_bytes())
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect()
}

/// The last value of a query parameter (FastAPI keeps the last for scalar parameters).
pub fn query_value<'a>(pairs: &'a [(String, String)], name: &str) -> Option<&'a str> {
    pairs
        .iter()
        .rev()
        .find(|(key, _)| key == name)
        .map(|(_, value)| value.as_str())
}

/// Pydantic's message for a value outside a set of literals: `Input should be 'a', 'b' or 'c'`.
pub fn literal_message(allowed: &[&str]) -> String {
    let quoted: Vec<String> = allowed.iter().map(|value| format!("'{value}'")).collect();
    match quoted.split_last() {
        Some((last, rest)) if !rest.is_empty() => format!("{} or {last}", rest.join(", ")),
        Some((last, _)) => last.clone(),
        None => String::new(),
    }
}

/// Query-parameter checks that collect every error, as FastAPI reports them together.
#[derive(Debug, Default)]
pub struct QueryReader {
    errors: Errors,
}

impl QueryReader {
    /// A boolean query parameter (`true`/`1`/`yes`/`on`, `false`/`0`/`no`/`off`, ...).
    pub fn boolean(&mut self, pairs: &[(String, String)], name: &str, default: bool) -> bool {
        let Some(raw) = query_value(pairs, name) else {
            return default;
        };
        match raw.to_lowercase().as_str() {
            "0" | "off" | "f" | "false" | "n" | "no" => false,
            "1" | "on" | "t" | "true" | "y" | "yes" => true,
            _ => {
                self.errors.push(
                    "bool_parsing",
                    &[json!("query"), json!(name)],
                    "Input should be a valid boolean, unable to interpret input",
                    &json!(raw),
                    None,
                );
                default
            }
        }
    }

    /// An optional integer query parameter.
    pub fn integer(&mut self, pairs: &[(String, String)], name: &str) -> Option<i64> {
        let raw = query_value(pairs, name)?;
        match raw.trim().parse::<i64>() {
            Ok(number) => Some(number),
            Err(_) => {
                self.errors.push(
                    "int_parsing",
                    &[json!("query"), json!(name)],
                    "Input should be a valid integer, unable to parse string as an integer",
                    &json!(raw),
                    None,
                );
                None
            }
        }
    }

    /// A query parameter limited to `allowed` values.
    pub fn literal<'a>(
        &mut self,
        pairs: &'a [(String, String)],
        name: &str,
        allowed: &[&str],
        default: &'a str,
    ) -> &'a str {
        let Some(raw) = query_value(pairs, name) else {
            return default;
        };
        if allowed.contains(&raw) {
            return raw;
        }
        let message = literal_message(allowed);
        self.errors.push(
            "literal_error",
            &[json!("query"), json!(name)],
            &format!("Input should be {message}"),
            &json!(raw),
            Some(json!({ "expected": message })),
        );
        default
    }

    /// The collected errors as a 422, if any.
    pub fn finish(self) -> Result<(), Response> {
        match self.errors.into_response() {
            Some(response) => Err(response),
            None => Ok(()),
        }
    }
}

/// An integer query parameter, or a 422 in FastAPI's format.
pub fn query_int(pairs: &[(String, String)], name: &str, default: i64) -> Result<i64, Response> {
    let Some(raw) = query_value(pairs, name) else {
        return Ok(default);
    };
    raw.trim().parse::<i64>().map_err(|_| {
        single(
            "int_parsing",
            &[json!("query"), json!(name)],
            "Input should be a valid integer, unable to parse string as an integer",
            &json!(raw),
        )
    })
}
