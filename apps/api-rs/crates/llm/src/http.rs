//! Shared HTTP pieces for talking to providers.

use std::sync::LazyLock;
use std::time::Duration;

use reqwest::header::{HeaderMap, HeaderValue};

/// The User-Agent several providers need: some reject requests that do not look like a browser.
pub const BROWSER_USER_AGENT: &str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";

/// The User-Agent sent to OpenCode.
pub const OPENCODE_USER_AGENT: &str = concat!("Agent-Hire/", "1.4.0");

static OPENCODE_SESSION: LazyLock<String> = LazyLock::new(|| {
    let mut bytes = [0_u8; 16];
    // A failed random source only makes the id less unique; it is not a secret.
    let _ = getrandom::fill(&mut bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    format!("agent-hire-{hex}")
});

/// The id OpenCode uses to route this process's requests, made once per process.
pub fn opencode_session_id() -> &'static str {
    &OPENCODE_SESSION
}

/// A client with the given total timeout, no redirects to other hosts' surprise and no proxy
/// from the environment.
pub fn http_client(timeout: Duration) -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(timeout)
        .connect_timeout(Duration::from_secs(10).min(timeout))
        .build()
        .unwrap_or_default()
}

/// OpenCode's headers; with `anthropic`, also the Anthropic protocol headers.
pub fn opencode_headers(key: &str, anthropic: bool) -> HeaderMap {
    let mut headers = HeaderMap::new();
    insert(&mut headers, "authorization", &format!("Bearer {key}"));
    insert(&mut headers, "content-type", "application/json");
    insert(&mut headers, "user-agent", OPENCODE_USER_AGENT);
    insert(&mut headers, "x-opencode-session", opencode_session_id());
    if anthropic {
        insert(&mut headers, "x-api-key", key);
        insert(&mut headers, "anthropic-version", "2023-06-01");
    }
    headers
}

/// Adds a header, skipping values that are not valid header text.
pub fn insert(headers: &mut HeaderMap, name: &'static str, value: &str) {
    if let Ok(value) = HeaderValue::from_str(value) {
        headers.insert(name, value);
    }
}
