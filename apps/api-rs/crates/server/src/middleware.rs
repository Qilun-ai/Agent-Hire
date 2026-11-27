//! Request-wide middleware: request ids, the trusted-host check and CORS.

use std::sync::LazyLock;

use axum::body::Body;
use axum::extract::Request;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use regex::Regex;

/// Origins that get CORS headers: the desktop shell and any local web origin.
static ALLOWED_ORIGIN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"^(tauri://localhost|https?://(localhost|127\.0\.0\.1|tauri\.localhost|\[::1\])(?::\d+)?)$",
    )
    .expect("the origin pattern is valid")
});

const PREFLIGHT_METHODS: &str = "DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT";
const PREFLIGHT_MAX_AGE: &str = "600";

/// Copies a non-empty `x-request-id` request header onto the response.
pub async fn request_id(request: Request, next: Next) -> Response {
    let id = request
        .headers()
        .get("x-request-id")
        .filter(|value| !value.is_empty())
        .cloned();
    let mut response = next.run(request).await;
    if let Some(id) = id {
        response.headers_mut().insert("x-request-id", id);
    }
    response
}

/// Answers `400 Invalid host header` unless the `Host` is a loopback name.
pub async fn trusted_host(request: Request, next: Next) -> Response {
    let host = request
        .headers()
        .get(header::HOST)
        .and_then(|value| value.to_str().ok())
        .or_else(|| request.uri().host());
    if !host.is_some_and(is_loopback_host) {
        return (StatusCode::BAD_REQUEST, "Invalid host header").into_response();
    }
    next.run(request).await
}

fn is_loopback_host(host: &str) -> bool {
    let name = if let Some(rest) = host.strip_prefix('[') {
        // `[::1]` or `[::1]:8000`.
        match rest.split_once(']') {
            Some((address, _)) => return address == "::1",
            None => return false,
        }
    } else if host == "::1" {
        host
    } else {
        host.rsplit_once(':').map_or(host, |(name, _)| name)
    };
    matches!(name, "localhost" | "127.0.0.1" | "::1")
}

/// CORS for local origins: answers preflights and marks responses readable.
pub async fn cors(request: Request, next: Next) -> Response {
    let origin = request.headers().get(header::ORIGIN).cloned();
    let Some(origin) = origin else {
        return next.run(request).await;
    };
    let allowed = origin
        .to_str()
        .is_ok_and(|value| ALLOWED_ORIGIN.is_match(value));
    let is_preflight = request.method() == Method::OPTIONS
        && request
            .headers()
            .contains_key(header::ACCESS_CONTROL_REQUEST_METHOD);
    if is_preflight {
        return preflight(request.headers(), &origin, allowed);
    }
    let mut response = next.run(request).await;
    if allowed {
        let headers = response.headers_mut();
        headers.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, origin);
        headers.append(header::VARY, HeaderValue::from_static("Origin"));
    }
    response
}

fn preflight(request_headers: &HeaderMap, origin: &HeaderValue, allowed: bool) -> Response {
    if !allowed {
        return (StatusCode::BAD_REQUEST, "Disallowed CORS origin").into_response();
    }
    let mut response = Response::new(Body::from("OK"));
    let headers = response.headers_mut();
    headers.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, origin.clone());
    headers.insert(
        header::ACCESS_CONTROL_ALLOW_METHODS,
        HeaderValue::from_static(PREFLIGHT_METHODS),
    );
    headers.insert(
        header::ACCESS_CONTROL_MAX_AGE,
        HeaderValue::from_static(PREFLIGHT_MAX_AGE),
    );
    if let Some(requested) = request_headers.get(header::ACCESS_CONTROL_REQUEST_HEADERS) {
        headers.insert(header::ACCESS_CONTROL_ALLOW_HEADERS, requested.clone());
    }
    headers.insert(header::VARY, HeaderValue::from_static("Origin"));
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn loopback_hosts() {
        for host in [
            "localhost",
            "localhost:8000",
            "127.0.0.1:1",
            "[::1]",
            "[::1]:80",
            "::1",
        ] {
            assert!(is_loopback_host(host), "{host} is loopback");
        }
        for host in ["example.com", "127.0.0.2", "localhost.evil:80", "[::2]", ""] {
            assert!(!is_loopback_host(host), "{host} is not loopback");
        }
    }

    #[test]
    fn origins() {
        for origin in [
            "tauri://localhost",
            "http://localhost:5173",
            "https://127.0.0.1",
            "http://[::1]:3000",
        ] {
            assert!(ALLOWED_ORIGIN.is_match(origin), "{origin} is allowed");
        }
        for origin in [
            "http://example.com",
            "http://localhost.evil",
            "chrome-extension://abc",
        ] {
            assert!(!ALLOWED_ORIGIN.is_match(origin), "{origin} is refused");
        }
    }
}
