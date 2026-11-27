//! In-memory rolling-window rate limits (reset on restart).

use std::collections::VecDeque;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use axum::Json;
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde_json::json;

/// At most `max_calls` per `window`.
pub struct RateLimiter {
    max_calls: usize,
    window: Duration,
    calls: Mutex<VecDeque<Instant>>,
}

impl RateLimiter {
    pub const fn new(max_calls: usize, window: Duration) -> RateLimiter {
        RateLimiter {
            max_calls,
            window,
            calls: Mutex::new(VecDeque::new()),
        }
    }

    /// Counts a call; on refusal, the 429 to send (with `Retry-After`).
    pub fn check(&self) -> Result<(), Response> {
        let now = Instant::now();
        let Ok(mut calls) = self.calls.lock() else {
            return Ok(());
        };
        while calls
            .front()
            .is_some_and(|at| now.duration_since(*at) >= self.window)
        {
            calls.pop_front();
        }
        if calls.len() < self.max_calls {
            calls.push_back(now);
            return Ok(());
        }
        let oldest = calls.front().copied().unwrap_or(now);
        let wait = self
            .window
            .saturating_sub(now.duration_since(oldest))
            .as_secs_f64()
            .ceil()
            .max(1.0) as u64;
        let unit = if wait == 1 { "second" } else { "seconds" };
        let mut response = (
            StatusCode::TOO_MANY_REQUESTS,
            Json(json!({ "detail": format!("Too many requests. Please wait {wait} {unit}.") })),
        )
            .into_response();
        if let Ok(value) = HeaderValue::from_str(&wait.to_string()) {
            response.headers_mut().insert(header::RETRY_AFTER, value);
        }
        Err(response)
    }
}
