//! A small blocking HTTP client that speaks the backend's auth schemes.

use std::time::Duration;

use serde_json::Value;
use ureq::Agent;

use crate::backend::AGENT_SECRET;

const REQUEST_TIMEOUT: Duration = Duration::from_secs(600);
const MAX_BODY_BYTES: u64 = 64 * 1024 * 1024;

/// How a request authenticates.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Auth {
    /// `Authorization: Bearer <token from /bootstrap>`, as the web app, agent and extension send.
    Token,
    /// No credentials at all.
    Anonymous,
    /// A well-formed but wrong bearer token.
    WrongToken,
    /// The bearer token plus the agent's `X-Agent-Secret` header.
    AgentSecret,
}

/// A decoded response body.
#[derive(Clone, Debug, PartialEq)]
pub enum Body {
    /// Body was empty.
    Empty,
    /// `application/json` (or any body that parses as JSON).
    Json(Value),
    /// Any other UTF-8 body.
    Text(String),
    /// A binary body such as a PDF.
    Bytes(Vec<u8>),
}

/// One HTTP response.
#[derive(Clone, Debug, PartialEq)]
pub struct Response {
    /// HTTP status code.
    pub status: u16,
    /// The media type without parameters, for example `application/json`.
    pub content_type: String,
    /// The decoded body.
    pub body: Body,
}

impl Response {
    /// The JSON body.
    ///
    /// # Panics
    ///
    /// Panics if the body is not JSON; use it only where the contract says the route returns JSON.
    pub fn json(&self) -> &Value {
        match &self.body {
            Body::Json(value) => value,
            other => panic!(
                "expected a JSON body (status {}), got {other:?}",
                self.status
            ),
        }
    }
}

/// An HTTP client bound to one backend's base URL and session token.
pub struct Client {
    agent: Agent,
    base_url: String,
    token: String,
}

impl Client {
    /// Fetches the session token from `GET /bootstrap`, as the browser UI and agent do.
    ///
    /// # Panics
    ///
    /// Panics if `/bootstrap` does not answer with a token.
    pub fn bootstrap(base_url: &str) -> Client {
        let agent: Agent = Agent::config_builder()
            .http_status_as_error(false)
            .timeout_global(Some(REQUEST_TIMEOUT))
            .build()
            .into();
        let mut client = Client {
            agent,
            base_url: base_url.to_owned(),
            token: String::new(),
        };
        let response = client.request("GET", "/bootstrap", None, Auth::Anonymous);
        let token = response.json()["token"]
            .as_str()
            .expect("/bootstrap returns a token string");
        client.token = token.to_owned();
        client
    }

    /// The session token.
    pub fn token(&self) -> &str {
        &self.token
    }

    /// Sends a request with an optional JSON body.
    ///
    /// # Panics
    ///
    /// Panics on transport errors (connection refused, timeout); HTTP error statuses are returned.
    pub fn request(&self, method: &str, path: &str, json: Option<&Value>, auth: Auth) -> Response {
        let bytes = json.map(|v| serde_json::to_vec(v).expect("serialising a JSON body"));
        self.send(
            method,
            path,
            bytes.as_deref().map(|b| ("application/json", b)),
            auth,
        )
    }

    /// Sends a request with a raw body of the given content type (multipart uploads, form posts).
    ///
    /// # Panics
    ///
    /// Panics on transport errors.
    pub fn request_raw(
        &self,
        method: &str,
        path: &str,
        content_type: &str,
        body: &[u8],
        auth: Auth,
    ) -> Response {
        self.send(method, path, Some((content_type, body)), auth)
    }

    fn send(&self, method: &str, path: &str, body: Option<(&str, &[u8])>, auth: Auth) -> Response {
        let url = format!("{}{}", self.base_url, path);
        let mut builder = ureq::http::Request::builder().method(method).uri(&url);
        match auth {
            Auth::Token => {
                builder = builder.header("Authorization", format!("Bearer {}", self.token))
            }
            Auth::Anonymous => {}
            Auth::WrongToken => builder = builder.header("Authorization", "Bearer not-the-token"),
            Auth::AgentSecret => {
                builder = builder
                    .header("Authorization", format!("Bearer {}", self.token))
                    .header("X-Agent-Secret", AGENT_SECRET);
            }
        }
        let result = match body {
            Some((content_type, bytes)) => {
                let request = builder
                    .header("Content-Type", content_type)
                    .body(bytes.to_vec())
                    .expect("building the request");
                self.agent.run(request)
            }
            None => self
                .agent
                .run(builder.body(()).expect("building the request")),
        };
        let mut response = result.unwrap_or_else(|e| panic!("{method} {url} failed: {e}"));
        let status = response.status().as_u16();
        let content_type = response
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .map(|v| {
                v.split(';')
                    .next()
                    .unwrap_or("")
                    .trim()
                    .to_ascii_lowercase()
            })
            .unwrap_or_default();
        let bytes = response
            .body_mut()
            .with_config()
            .limit(MAX_BODY_BYTES)
            .read_to_vec()
            .unwrap_or_else(|e| panic!("reading the body of {method} {url}: {e}"));
        Response {
            status,
            content_type,
            body: decode(bytes),
        }
    }
}

fn decode(bytes: Vec<u8>) -> Body {
    if bytes.is_empty() {
        return Body::Empty;
    }
    if let Ok(value) = serde_json::from_slice::<Value>(&bytes) {
        return Body::Json(value);
    }
    match String::from_utf8(bytes) {
        Ok(text) => Body::Text(text),
        Err(e) => Body::Bytes(e.into_bytes()),
    }
}
