//! IMAP access to the inbox the user applies from.
//!
//! IMAP with an app-specific password works on Gmail, Outlook, Fastmail and self-hosted mail,
//! and needs no registered OAuth client (which a local app cannot keep secret). The mailbox is
//! always opened read-only and messages are fetched with `BODY.PEEK[]`, so nothing is marked read.

use std::fmt;
use std::sync::LazyLock;

use agent_hire_store::UtcTime;
use mail_parser::MessageParser;
use regex::Regex;
use serde_json::{Value, json};

/// The usual implicit-TLS IMAP port.
pub const DEFAULT_PORT: u16 = 993;
const MAX_MESSAGES: usize = 200;
const MAX_BODY_CHARS: usize = 20_000;

/// Where and how to log in.
#[derive(Clone, PartialEq, Eq)]
pub struct MailboxConfig {
    /// The IMAP server.
    pub host: String,
    /// The login name.
    pub username: String,
    /// The (app-specific) password.
    pub password: String,
    /// The port; implicit TLS.
    pub port: u16,
    /// The folder to read, usually `INBOX`.
    pub mailbox: String,
}

impl fmt::Debug for MailboxConfig {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.redacted().to_string())
    }
}

impl MailboxConfig {
    /// The config without the password, safe to return over the API.
    pub fn redacted(&self) -> Value {
        json!({
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "mailbox": self.mailbox,
            "use_ssl": true,
            "password_set": !self.password.is_empty(),
        })
    }
}

/// One message as the rules see it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Message {
    /// The message's sequence number in the mailbox, as text.
    pub uid: String,
    /// The decoded `From` header.
    pub from_address: String,
    /// The decoded subject.
    pub subject: String,
    /// The plain-text body (HTML is de-tagged), cut to 20 000 characters.
    pub body: String,
}

/// A connection or login failure, with a message meant for the user.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EmailError(pub String);

impl fmt::Display for EmailError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for EmailError {}

type Session = imap::Session<imap::Connection>;

fn connect(config: &MailboxConfig) -> Result<Session, EmailError> {
    let client = imap::ClientBuilder::new(config.host.as_str(), config.port)
        .connect()
        .map_err(|error| {
            EmailError(format!(
                "Could not reach {}:{} — {error}",
                config.host, config.port
            ))
        })?;
    client.login(&config.username, &config.password).map_err(|_| {
        EmailError(
            "Login rejected. Most providers require an app-specific password for IMAP rather than your normal \
             account password."
                .to_owned(),
        )
    })
}

fn open_mailbox(session: &mut Session, mailbox: &str) -> Result<u32, EmailError> {
    session
        .examine(mailbox)
        .map(|status| status.exists)
        .map_err(|_| {
            EmailError(format!(
                "Mailbox {} could not be opened.",
                python_repr(mailbox)
            ))
        })
}

/// Logs in and opens the mailbox read-only; reports how many messages it holds.
pub fn check(config: &MailboxConfig) -> Result<Value, EmailError> {
    let mut session = connect(config)?;
    let count = open_mailbox(&mut session, &config.mailbox);
    let _ = session.logout();
    Ok(json!({ "ok": true, "mailbox": config.mailbox, "messages": count? }))
}

/// Messages from the last `since_days` days (at most 200), newest first.
pub fn recent(config: &MailboxConfig, since_days: i64) -> Result<Vec<Message>, EmailError> {
    let since = UtcTime::now().plus_seconds(-since_days.max(1) * 86_400);
    const MONTHS: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let month = MONTHS[usize::try_from(since.month.saturating_sub(1))
        .unwrap_or(0)
        .min(11)];
    let since = format!("{:02}-{month}-{:04}", since.day, since.year);
    let mut session = connect(config)?;
    let result = fetch_since(&mut session, &config.mailbox, &since);
    let _ = session.logout();
    result
}

fn fetch_since(
    session: &mut Session,
    mailbox: &str,
    since: &str,
) -> Result<Vec<Message>, EmailError> {
    open_mailbox(session, mailbox)?;
    let found = session
        .search(format!("SINCE {since}"))
        .map_err(|_| EmailError("Mailbox search failed.".to_owned()))?;
    let mut numbers: Vec<u32> = found.into_iter().collect();
    numbers.sort_unstable();
    let start = numbers.len().saturating_sub(MAX_MESSAGES);
    let mut out = Vec::new();
    for number in numbers[start..].iter().rev() {
        // BODY.PEEK leaves \Seen untouched; a plain BODY[] would mark the user's mail read.
        let Ok(fetches) = session.fetch(number.to_string(), "(BODY.PEEK[])") else {
            continue;
        };
        let Some(raw) = fetches
            .iter()
            .next()
            .and_then(|fetch| fetch.body().map(<[u8]>::to_vec))
        else {
            continue;
        };
        let Some(parsed) = MessageParser::default().parse(&raw) else {
            continue;
        };
        let from_address = parsed
            .header_raw("From")
            .map(|raw| decode_header_text(raw.trim()))
            .unwrap_or_default();
        out.push(Message {
            uid: number.to_string(),
            from_address,
            subject: parsed.subject().unwrap_or("").to_owned(),
            body: body_text(&parsed),
        });
    }
    Ok(out)
}

/// A header with RFC 2047 encoded words decoded.
fn decode_header_text(raw: &str) -> String {
    let wrapped = format!("Subject: {raw}\r\n\r\n");
    MessageParser::default()
        .parse(wrapped.as_bytes())
        .and_then(|message| message.subject().map(str::to_owned))
        .unwrap_or_else(|| raw.to_owned())
}

static SCRIPT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?is)<\s*script[^>]*>.*?</\s*script\s*>").expect("valid pattern")
});
static STYLE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?is)<\s*style[^>]*>.*?</\s*style\s*>").expect("valid pattern"));
static TAG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<[^>]+>").expect("valid pattern"));
static NBSP: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"&nbsp;?").expect("valid pattern"));
static SPACES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[ \t]{2,}").expect("valid pattern"));

fn strip_html(html: &str) -> String {
    let text = SCRIPT.replace_all(html, " ");
    let text = STYLE.replace_all(&text, " ");
    let text = TAG.replace_all(&text, " ");
    let text = NBSP.replace_all(&text, " ");
    SPACES.replace_all(&text, " ").trim().to_owned()
}

/// The plain-text body, falling back to de-tagged HTML (many recruiting systems send HTML only).
fn body_text(message: &mail_parser::Message<'_>) -> String {
    let html_parts: Vec<String> = message
        .html_body
        .iter()
        .filter_map(|part| message.part(*part))
        .filter(|part| part.is_text_html())
        .filter_map(|part| part.text_contents().map(str::to_owned))
        .collect();
    let text_parts: Vec<String> = message
        .text_body
        .iter()
        .filter_map(|part| message.part(*part))
        .filter(|part| part.is_text() && !part.is_text_html())
        .filter_map(|part| part.text_contents().map(str::to_owned))
        .collect();
    let chosen = if !text_parts.is_empty() {
        text_parts.join("\n")
    } else {
        strip_html(&html_parts.join("\n"))
    };
    chosen.chars().take(MAX_BODY_CHARS).collect()
}

/// Python's `repr` of a string, as the original error messages quote mailbox names.
fn python_repr(text: &str) -> String {
    if text.contains('\'') && !text.contains('"') {
        format!("\"{text}\"")
    } else {
        format!("'{}'", text.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn html_and_headers() {
        assert_eq!(
            strip_html("<style>x{}</style><p>Hi&nbsp;there</p>  <script>bad()</script>"),
            "Hi there"
        );
        assert_eq!(
            decode_header_text("=?utf-8?B?SGVsbG8g8J+Riw==?="),
            "Hello 👋"
        );
        assert_eq!(python_repr("INBOX"), "'INBOX'");
    }

    #[test]
    fn parses_a_multipart_message() {
        let raw = b"From: =?utf-8?Q?Acme_Careers?= <jobs@acme.io>\r\nSubject: Your application\r\nContent-Type: multipart/alternative; boundary=b\r\n\r\n--b\r\nContent-Type: text/html\r\n\r\n<p>We regret to inform you</p>\r\n--b--\r\n";
        let parsed = MessageParser::default().parse(&raw[..]).expect("parses");
        assert_eq!(body_text(&parsed), "We regret to inform you");
    }
}
