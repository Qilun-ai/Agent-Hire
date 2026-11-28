//! Jupyter sessions, fixed kernel requests, and chunked contents uploads.

use std::fs::File;
use std::io::Read as _;
use std::net::{TcpStream, ToSocketAddrs as _};
use std::path::Path;
use std::time::{Duration, Instant};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use serde_json::{Value, json};
use tungstenite::client::IntoClientRequest as _;
use tungstenite::http::HeaderValue;
use tungstenite::protocol::WebSocketConfig;
use tungstenite::stream::MaybeTlsStream;
use tungstenite::{Message, WebSocket};
use ureq::http::{Method, Request, Response};
use ureq::{Agent, Body};
use url::Url;

use crate::kaggle::KaggleError;

const UPLOAD_CHUNK: usize = 8 << 20;
const MAX_MESSAGE: usize = 32 << 20;
const OPEN_TIMEOUT: Duration = Duration::from_secs(30);

pub(crate) struct Jupyter {
    origin: Url,
    auth: String,
    secrets: Vec<String>,
    http: Agent,
    session: Option<String>,
    kernel: Option<String>,
}

impl Jupyter {
    pub(crate) fn new(link: &str) -> Result<Self, KaggleError> {
        let mut origin = parse_link(link).ok_or_else(|| {
            KaggleError::new("Paste the full Kaggle notebook link, including ?token=…")
        })?;
        let mut query: Vec<(String, String)> = Vec::new();
        for (key, value) in origin.query_pairs() {
            if let Some((_, existing)) = query.iter_mut().find(|(name, _)| name == key.as_ref()) {
                *existing = value.into_owned();
            } else {
                query.push((key.into_owned(), value.into_owned()));
            }
        }
        let auth = url::form_urlencoded::Serializer::new(String::new())
            .extend_pairs(&query)
            .finish();
        let secrets = query
            .into_iter()
            .map(|(_, value)| value)
            .filter(|value| !value.is_empty())
            .collect();
        origin.set_path("");
        origin.set_fragment(None);
        origin.set_query(None);
        let http = Agent::new_with_config(
            Agent::config_builder()
                .http_status_as_error(false)
                .max_redirects(0)
                .build(),
        );
        Ok(Self {
            origin,
            auth,
            secrets,
            http,
            session: None,
            kernel: None,
        })
    }

    pub(crate) fn clean(&self, message: &str) -> String {
        let mut message = message.to_owned();
        for secret in &self.secrets {
            let encoded: String = url::form_urlencoded::byte_serialize(secret.as_bytes()).collect();
            message = message
                .replace(secret, "[REDACTED]")
                .replace(&encoded, "[REDACTED]");
        }
        message
    }

    pub(crate) fn validate(&self) -> Result<(), KaggleError> {
        self.request(Method::GET, "/api/status", None, 40)?;
        Ok(())
    }

    fn url(&self, path: &str) -> Url {
        let mut url = self.origin.clone();
        url.set_path(path);
        url.set_query(Some(&self.auth));
        url
    }

    fn request(
        &self,
        method: Method,
        path: &str,
        body: Option<&Value>,
        timeout: u64,
    ) -> Result<Response<Body>, KaggleError> {
        let mut request = Request::builder()
            .method(method)
            .uri(self.url(path).as_str());
        if body.is_some() {
            request = request.header("Content-Type", "application/json");
        }
        let request = request
            .body(body.map(Value::to_string).unwrap_or_default())
            .map_err(|_| KaggleError::new("Could not build the notebook request"))?;
        let request = self
            .http
            .configure_request(request)
            .timeout_global(Some(Duration::from_secs(timeout)))
            .build();
        let response = self.http.run(request).map_err(|error| {
            let detail: String = self.clean(&error.to_string()).chars().take(200).collect();
            KaggleError::new(format!("Could not reach the Kaggle notebook: {detail}"))
        })?;
        let status = response.status().as_u16();
        match status {
            401 | 403 => Err(KaggleError::new(
                "The Kaggle link was refused — it has probably expired. Paste a fresh link from the notebook.",
            )),
            300.. => Err(KaggleError::new(format!(
                "Kaggle notebook answered HTTP {status} on {path}. Is the session still running?"
            ))),
            _ => Ok(response),
        }
    }

    fn open(&mut self) -> Result<(), KaggleError> {
        let name = format!("agent-hire-{}", random_id(6)?);
        let body =
            json!({"path": name, "name": name, "type":"console", "kernel":{"name":"python3"}});
        let session: Value = self
            .request(Method::POST, "/api/sessions", Some(&body), 40)?
            .body_mut()
            .read_json()
            .map_err(|_| KaggleError::new("Invalid notebook session response"))?;
        self.session = id_from(&session["id"]);
        self.kernel = id_from(&session["kernel"]["id"]);
        if self.session.is_none() || self.kernel.is_none() {
            return Err(KaggleError::new("Invalid notebook session response"));
        }
        Ok(())
    }

    fn connect_channels(
        &self,
        kernel: &str,
    ) -> Result<WebSocket<MaybeTlsStream<TcpStream>>, KaggleError> {
        let mut url = self.url(&format!("/api/kernels/{kernel}/channels"));
        url.set_scheme("wss")
            .map_err(|_| KaggleError::new("Invalid notebook URL"))?;
        let failure = || KaggleError::new("Could not open the notebook kernel");
        let host = url.host_str().ok_or_else(failure)?.trim_matches(['[', ']']);
        let port = url.port_or_known_default().ok_or_else(failure)?;
        let mut request = url.as_str().into_client_request().map_err(|_| failure())?;
        let origin = self.origin.origin().ascii_serialization();
        request.headers_mut().insert(
            "Origin",
            HeaderValue::from_str(&origin).map_err(|_| failure())?,
        );
        let cookies: Vec<String> = self
            .http
            .cookie_jar_lock()
            .iter()
            .map(|cookie| cookie.to_string())
            .collect();
        if !cookies.is_empty() {
            let cookie = HeaderValue::from_str(&cookies.join("; ")).map_err(|_| failure())?;
            request.headers_mut().insert("Cookie", cookie);
        }

        let start = Instant::now();
        let addresses = (host, port).to_socket_addrs().map_err(|_| failure())?;
        for address in addresses {
            let Some(remaining) = OPEN_TIMEOUT.checked_sub(start.elapsed()) else {
                break;
            };
            let Ok(socket) = TcpStream::connect_timeout(&address, remaining) else {
                continue;
            };
            let timeout = OPEN_TIMEOUT
                .saturating_sub(start.elapsed())
                .max(Duration::from_millis(1));
            socket
                .set_read_timeout(Some(timeout))
                .map_err(|_| failure())?;
            socket
                .set_write_timeout(Some(timeout))
                .map_err(|_| failure())?;
            let config = WebSocketConfig::default()
                .max_message_size(Some(MAX_MESSAGE))
                .max_frame_size(Some(MAX_MESSAGE));
            let (socket, _) =
                tungstenite::client_tls_with_config(request, socket, Some(config), None).map_err(
                    |error| {
                        let detail: String =
                            self.clean(&error.to_string()).chars().take(200).collect();
                        KaggleError::new(format!("Could not open the notebook kernel: {detail}"))
                    },
                )?;
            return Ok(socket);
        }
        Err(failure())
    }

    pub(crate) fn last_json(
        &mut self,
        code: &str,
        timeout: u64,
        metadata: &Value,
    ) -> Result<Value, KaggleError> {
        if self.kernel.is_none() {
            self.close();
            self.open()?;
        }
        let kernel = self
            .kernel
            .as_deref()
            .ok_or_else(|| KaggleError::new("Missing notebook kernel"))?;
        let deadline = Instant::now() + Duration::from_secs(timeout);
        let mut channels = self.connect_channels(kernel)?;
        let id = random_id(16)?;
        let message = json!({
            "header":{"msg_id":id,"username":"agent_hire","session":random_id(16)?,
                      "msg_type":"execute_request","version":"5.3"},
            "parent_header":{}, "metadata":metadata, "channel":"shell",
            "content":{"code":code,"silent":false,"store_history":false,"user_expressions":{},
                       "allow_stdin":false,"stop_on_error":true},
        });
        let interrupted = || KaggleError::new("Notebook connection interrupted");
        channels
            .send(Message::text(message.to_string()))
            .map_err(|_| interrupted())?;
        let mut output = String::new();
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(KaggleError::new("The notebook took too long to answer"));
            }
            let socket = match channels.get_mut() {
                MaybeTlsStream::Plain(socket) => socket,
                MaybeTlsStream::Rustls(stream) => &mut stream.sock,
                _ => return Err(interrupted()),
            };
            socket
                .set_read_timeout(Some(remaining))
                .map_err(|_| interrupted())?;
            let message = match channels.read() {
                Ok(message) => message,
                Err(tungstenite::Error::Io(error))
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                    ) =>
                {
                    return Err(KaggleError::new("The notebook took too long to answer"));
                }
                Err(_) => return Err(interrupted()),
            };
            let Message::Text(text) = message else {
                continue;
            };
            let reply: Value = serde_json::from_str(&text)
                .map_err(|_| KaggleError::new("Invalid notebook kernel response"))?;
            if reply["parent_header"]["msg_id"].as_str() != Some(id.as_str()) {
                continue;
            }
            let content = &reply["content"];
            match reply["header"]["msg_type"].as_str() {
                Some("stream") => {
                    let text = content["text"].as_str().unwrap_or_default();
                    if output.len().saturating_add(text.len()) > MAX_MESSAGE {
                        return Err(KaggleError::new("Notebook output exceeded the size limit"));
                    }
                    output.push_str(text);
                }
                Some("error") => {
                    let name = content["ename"].as_str().unwrap_or("Error");
                    let detail = content["evalue"].as_str().unwrap_or_default();
                    let detail: String = self
                        .clean(&format!("{name}: {detail}"))
                        .chars()
                        .take(500)
                        .collect();
                    return Err(KaggleError::new(detail));
                }
                Some("status") if content["execution_state"] == "idle" => break,
                _ => (),
            }
        }
        let _ = channels.close(None);
        let last = output
            .trim()
            .lines()
            .last()
            .ok_or_else(|| KaggleError::new("The notebook returned no output"))?;
        serde_json::from_str(last).map_err(|_| KaggleError::new("Invalid notebook JSON response"))
    }

    pub(crate) fn upload(&self, local: &Path, remote: &str) -> Result<(), KaggleError> {
        let mut file = File::open(local).map_err(KaggleError::new)?;
        let length = file.metadata().map_err(KaggleError::new)?.len();
        if let Some((parent, _)) = remote.rsplit_once('/') {
            let mut directory = String::from("/api/contents");
            for segment in parent.split('/') {
                directory.push('/');
                directory.push_str(segment);
                let _ = self.request(
                    Method::PUT,
                    &directory,
                    Some(&json!({"type":"directory"})),
                    40,
                );
            }
        }
        let chunks = length.div_ceil(UPLOAD_CHUNK as u64).max(1);
        let mut buffer = vec![0; UPLOAD_CHUNK];
        for number in 1..=chunks {
            let offset = (number - 1) * UPLOAD_CHUNK as u64;
            let size = (length - offset).min(UPLOAD_CHUNK as u64) as usize;
            file.read_exact(&mut buffer[..size])
                .map_err(KaggleError::new)?;
            let mut body =
                json!({"type":"file","format":"base64","content":STANDARD.encode(&buffer[..size])});
            if chunks > 1 {
                body["chunk"] = if number == chunks {
                    json!(-1)
                } else {
                    json!(number)
                };
            }
            self.request(
                Method::PUT,
                &format!("/api/contents/{remote}"),
                Some(&body),
                300,
            )?;
        }
        Ok(())
    }

    fn close(&mut self) {
        self.kernel = None;
        if let Some(session) = self.session.take() {
            let _ = self.request(
                Method::DELETE,
                &format!("/api/sessions/{session}"),
                None,
                15,
            );
        }
    }
}

impl Drop for Jupyter {
    fn drop(&mut self) {
        self.close();
    }
}

pub(crate) fn parse_link(link: &str) -> Option<Url> {
    let url = Url::parse(link.trim()).ok()?;
    (url.scheme() == "https"
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.query_pairs().any(|(key, _)| key == "token"))
    .then_some(url)
}

fn id_from(value: &Value) -> Option<String> {
    let id = value.as_str()?;
    (!id.is_empty()
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-'))
    .then(|| id.to_owned())
}

pub(crate) fn random_id(bytes: usize) -> Result<String, KaggleError> {
    use std::fmt::Write as _;
    let mut random = vec![0u8; bytes];
    getrandom::fill(&mut random).map_err(KaggleError::new)?;
    let mut id = String::with_capacity(bytes * 2);
    for byte in random {
        write!(&mut id, "{byte:02x}").expect("writing to a String cannot fail");
    }
    Ok(id)
}
