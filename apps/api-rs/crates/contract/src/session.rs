//! One test group: an isolated backend plus the transcript of the requests made to it.

use serde_json::{Value, json};

use crate::backend::{Backend, Target, repo_root, workspace_root};
use crate::client::{Auth, Body, Client, Response};
use crate::golden::{self, Redactions};

/// Longest text body kept verbatim in a golden file; longer ones are summarised.
const MAX_TEXT_IN_GOLDEN: usize = 4000;

/// A fresh backend for one test group and the ordered transcript of what it answered.
///
/// Steps are recorded in order; [`Session::finish`] compares the whole transcript with the
/// group's golden file. Dropping a session without finishing it stops the backend without checking.
pub struct Session {
    group: String,
    target: Target,
    backend: Backend,
    client: Client,
    transcript: Vec<Value>,
    redactions: Vec<(String, String)>,
}

impl Session {
    /// Starts an isolated backend for `group` (named after its golden file) and bootstraps a token.
    ///
    /// # Panics
    ///
    /// Panics if the backend cannot be started.
    pub fn start(group: &str) -> Session {
        Self::start_with_environment(group, &[])
    }

    /// Starts a group with the old résumé parser enabled and an otherwise clean environment.
    pub fn start_with_legacy_parser(group: &str) -> Session {
        Self::start_with_environment(group, &[("AGENT_HIRE_LEGACY_PARSER", "1")])
    }

    fn start_with_environment(group: &str, extra: &[(&str, &str)]) -> Session {
        let target = Target::from_env();
        let data_root = workspace_root().join("target/contract-runs").join(group);
        let backend = Backend::start_with_environment(&target, &data_root, extra);
        let client = Client::bootstrap(backend.base_url());
        Session {
            group: group.to_owned(),
            target,
            backend,
            client,
            transcript: Vec::new(),
            redactions: Vec::new(),
        }
    }

    /// The client, for requests that should not be part of the transcript (setup, polling).
    pub fn client(&self) -> &Client {
        &self.client
    }

    /// Sends an authenticated request with an optional JSON body and records it as a step.
    pub fn step(&mut self, label: &str, method: &str, path: &str, body: Option<Value>) -> Response {
        self.step_as(label, method, path, body, Auth::Token)
    }

    /// Like [`Session::step`] with an explicit auth scheme.
    pub fn step_as(
        &mut self,
        label: &str,
        method: &str,
        path: &str,
        body: Option<Value>,
        auth: Auth,
    ) -> Response {
        let response = self.client.request(method, path, body.as_ref(), auth);
        self.record(label, method, path, body, auth, &response);
        response
    }

    /// Sends a raw (non-JSON) body, such as a multipart upload, and records it as a step.
    /// `description` stands in for the body in the golden file.
    pub fn step_raw(
        &mut self,
        label: &str,
        method: &str,
        path: &str,
        content_type: &str,
        body: &[u8],
        description: &str,
    ) -> Response {
        let response = self
            .client
            .request_raw(method, path, content_type, body, Auth::Token);
        self.record(
            label,
            method,
            path,
            Some(json!(description)),
            Auth::Token,
            &response,
        );
        response
    }

    /// Records the PDF's text, normalizing whitespace to avoid depending on PDF encoding.
    ///
    /// # Panics
    ///
    /// Panics if the response is not a PDF or Poppler cannot extract its text.
    pub fn step_pdf_text(&mut self, label: &str, path: &str) {
        use std::io::Write;
        use std::process::{Command, Stdio};

        let mut response = self.client.request("GET", path, None, Auth::Token);
        assert_eq!(response.status, 200, "PDF response: {:?}", response.body);
        assert_eq!(response.content_type, "application/pdf");
        let Body::Bytes(bytes) = response.body else {
            panic!("expected PDF bytes")
        };
        let mut child = Command::new("pdftotext")
            .args(["-layout", "-nopgbrk", "-", "-"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .expect("start pdftotext");
        let mut stdin = child.stdin.take().expect("PDF stdin is piped");
        let writer = std::thread::spawn(move || stdin.write_all(&bytes));
        let output = child.wait_with_output().expect("wait for pdftotext");
        writer
            .join()
            .expect("PDF writer ran")
            .expect("write PDF to pdftotext");
        assert!(
            output.status.success(),
            "pdftotext failed: {}",
            output.status
        );
        let text = String::from_utf8(output.stdout).expect("UTF-8 PDF text");
        let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
        assert!(!text.is_empty(), "expected extractable PDF text");
        response.body = Body::Json(json!({"text":text}));
        self.record(label, "GET", path, None, Auth::Token, &response);
    }

    /// Replaces `value` with `placeholder` everywhere in the transcript, including steps already
    /// recorded. Use it for values the backend generates at random (document ids, file names);
    /// deterministic ids such as `custom-<hash>` must stay in the golden file.
    pub fn redact(&mut self, value: &str, placeholder: &str) {
        if !value.is_empty() {
            self.redactions
                .push((value.to_owned(), placeholder.to_owned()));
        }
    }

    /// Replaces a backend-specific diagnostic in the most recent step, using a JSON pointer
    /// relative to its response body.
    ///
    /// # Panics
    ///
    /// Panics when the step or the field does not exist.
    pub fn mask_response_field(&mut self, pointer: &str, placeholder: &str) {
        let step = self.transcript.last_mut().expect("a step was recorded");
        let value = step["body"]
            .pointer_mut(pointer)
            .expect("response field exists");
        *value = json!(placeholder);
    }

    /// Compares the transcript with the golden file (or records it with `CONTRACT_RECORD=1`).
    ///
    /// # Panics
    ///
    /// Panics if the transcript differs from the golden file.
    pub fn finish(self) {
        let mut transcript = Value::Array(self.transcript);
        if !self.redactions.is_empty() {
            let redactions = Redactions {
                literals: self.redactions,
            };
            transcript = golden::normalise_with(&transcript, &redactions);
        }
        golden::check(
            &self.group,
            &transcript,
            matches!(self.target, Target::Python { .. }),
        );
    }

    fn record(
        &mut self,
        label: &str,
        method: &str,
        path: &str,
        body: Option<Value>,
        auth: Auth,
        response: &Response,
    ) {
        let redactions = Redactions {
            literals: vec![
                (self.client.token().to_owned(), "<token>".to_owned()),
                (
                    self.backend.data_root().display().to_string(),
                    "<data>".to_owned(),
                ),
                (repo_root().display().to_string(), "<repo>".to_owned()),
            ],
        };
        let recorded_body = match &response.body {
            Body::Empty => Value::Null,
            Body::Json(value) => sort_event_lists(golden::normalise_with(value, &redactions)),
            Body::Text(text) if text.len() <= MAX_TEXT_IN_GOLDEN => {
                golden::normalise_with(&Value::String(text.clone()), &redactions)
            }
            Body::Text(text) => {
                json!({ "text_bytes_over": MAX_TEXT_IN_GOLDEN, "starts": &text[..text.floor_char_boundary(80)] })
            }
            Body::Bytes(bytes) => {
                json!({ "binary_prefix": String::from_utf8_lossy(&bytes[..bytes.len().min(4)]) })
            }
        };
        let mut request = json!({ "method": method, "path": path });
        if let Some(body) = body {
            request["body"] = body;
        }
        if auth != Auth::Token {
            request["auth"] = json!(format!("{auth:?}"));
        }
        self.transcript.push(json!({
            "step": label,
            "request": request,
            "status": response.status,
            "content_type": response.content_type,
            "body": recorded_body,
        }));
    }
}

/// Sorts every event list (an array of objects that all have `action` and `ts`) by content.
///
/// The backend stores event times with one-second resolution and orders by time alone, so events
/// written in the same second have no defined order; the contract only fixes which events exist.
fn sort_event_lists(value: Value) -> Value {
    match value {
        Value::Array(items) => {
            let mut items: Vec<Value> = items.into_iter().map(sort_event_lists).collect();
            let is_event = |v: &Value| v.get("action").is_some() && v.get("ts").is_some();
            if !items.is_empty() && items.iter().all(is_event) {
                items.sort_by_cached_key(|v| v.to_string());
            }
            Value::Array(items)
        }
        Value::Object(map) => Value::Object(
            map.into_iter()
                .map(|(k, v)| (k, sort_event_lists(v)))
                .collect(),
        ),
        other => other,
    }
}
