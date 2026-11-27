//! Raw document and page text for the agent, which reads documents itself and saves the profile
//! through `POST /api/v1/ingest/profile`.

use std::sync::{Arc, LazyLock};
use std::time::Duration;

use axum::Json;
use axum::body::Bytes;
use axum::extract::{Multipart, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use futures_util::StreamExt;
use serde_json::{Value, json};

use crate::app_state::AppState;
use crate::respond;
use crate::validation;

const PAGE_MAX_BYTES: usize = 3 * 1024 * 1024;
const PAGE_TEXT_CHARS: usize = 60_000;

static PAGE_CLIENT: LazyLock<reqwest::Client> = LazyLock::new(|| {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(25))
        .user_agent("Mozilla/5.0 (X11; Linux x86_64) Agent-Hire/1.0")
        .no_proxy()
        .build()
        .unwrap_or_default()
});

/// `POST /api/v1/agent-docs/extract` (multipart `file`): `{text, pages, chars, method, images[]}`.
pub async fn extract(State(_state): State<Arc<AppState>>, mut multipart: Multipart) -> Response {
    let mut upload: Option<(String, Vec<u8>)> = None;
    while let Ok(Some(field)) = multipart.next_field().await {
        if field.name() != Some("file") {
            continue;
        }
        let name = field.file_name().unwrap_or("document").to_owned();
        match field.bytes().await {
            Ok(bytes) => upload = Some((name, bytes.to_vec())),
            Err(_) => {
                return respond::detail(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "File too large (max 20 MB)",
                );
            }
        }
    }
    let Some((name, data)) = upload else {
        return validation::single(
            "missing",
            &[json!("body"), json!("file")],
            "Field required",
            &Value::Null,
        );
    };
    if data.len() > agent_hire_docs::MAX_BYTES {
        return respond::detail(StatusCode::PAYLOAD_TOO_LARGE, "File too large (max 20 MB)");
    }
    let result = tokio::task::spawn_blocking(move || agent_hire_docs::extract(&name, &data)).await;
    match result {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(kind)) if matches!(kind.as_str(), "BadZipFile" | "KeyError" | "ParseError") => {
            respond::detail(
                StatusCode::UNPROCESSABLE_ENTITY,
                &format!("Could not read this document: {kind}"),
            )
        }
        Ok(Err(kind)) => respond::detail(
            StatusCode::UNPROCESSABLE_ENTITY,
            &format!("Could not read this document ({kind}). Try a DOCX, TXT, or an image of it."),
        ),
        Err(error) => respond::internal_error("extract document", &error),
    }
}

/// `POST /api/v1/agent-docs/read-page` with `{"url": ...}`: a page's readable text.
pub async fn read_page(State(_state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let url = match body.get("url") {
        Some(Value::String(text)) => text.trim().to_owned(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    };
    let lowered = url.to_lowercase();
    if !(lowered.starts_with("http://") || lowered.starts_with("https://")) {
        return respond::detail(StatusCode::BAD_REQUEST, "Give a full http(s) link");
    }
    let response = match PAGE_CLIENT.get(&url).send().await {
        Ok(response) => response,
        Err(error) => {
            let kind = if error.is_timeout() {
                "ReadTimeout"
            } else if error.is_connect() {
                "ConnectError"
            } else {
                "RemoteProtocolError"
            };
            return respond::detail(
                StatusCode::BAD_GATEWAY,
                &format!("Could not open the page: {kind}"),
            );
        }
    };
    let status = response.status().as_u16();
    let final_url = response.url().to_string();
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_owned();
    let mut raw: Vec<u8> = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        match chunk {
            Ok(chunk) => {
                raw.extend_from_slice(&chunk);
                if raw.len() > PAGE_MAX_BYTES {
                    break;
                }
            }
            Err(_) => {
                return respond::detail(
                    StatusCode::BAD_GATEWAY,
                    "Could not open the page: ReadError",
                );
            }
        }
    }
    if status >= 400 {
        return respond::detail(
            StatusCode::BAD_GATEWAY,
            &format!("The page answered HTTP {status}"),
        );
    }
    raw.truncate(PAGE_MAX_BYTES);
    let head: String = String::from_utf8_lossy(&raw)
        .trim_start()
        .chars()
        .take(15)
        .collect::<String>()
        .to_lowercase();
    if !(content_type.contains("html")
        || head.starts_with("<!doctype")
        || head.starts_with("<html"))
    {
        let shown = if content_type.is_empty() {
            "unknown type"
        } else {
            content_type.as_str()
        };
        return respond::detail(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            &format!("Not a web page ({shown})"),
        );
    }
    let html = String::from_utf8_lossy(&raw).into_owned();
    let result =
        tokio::task::spawn_blocking(move || agent_hire_docs::page_text(&final_url, &html)).await;
    match result {
        Ok(mut value) => {
            let text = value["text"].as_str().unwrap_or("").to_owned();
            value["truncated"] = json!(text.chars().count() > PAGE_TEXT_CHARS);
            value["text"] = json!(text.chars().take(PAGE_TEXT_CHARS).collect::<String>());
            Json(value).into_response()
        }
        Err(error) => respond::internal_error("read page", &error),
    }
}
