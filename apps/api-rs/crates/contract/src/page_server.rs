//! A tiny local web server with fixed pages, for routes that fetch URLs.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::thread;

/// One page: status, content type and body.
#[derive(Clone, Debug)]
pub struct Page {
    /// The path it is served at, for example `/jobs/1`.
    pub path: String,
    /// The HTTP status.
    pub status: u16,
    /// The `Content-Type`.
    pub content_type: String,
    /// The body.
    pub body: String,
}

/// A server on `127.0.0.1` that answers the given pages (404 for anything else) until the
/// test process exits.
pub struct PageServer {
    base_url: String,
}

impl PageServer {
    /// Starts serving `pages`.
    ///
    /// # Panics
    ///
    /// Panics if no local port can be bound.
    pub fn start(pages: Vec<Page>) -> PageServer {
        let listener = TcpListener::bind("127.0.0.1:0").expect("binding a local port");
        let port = listener.local_addr().expect("a bound address").port();
        thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let pages = pages.clone();
                thread::spawn(move || {
                    let mut reader = BufReader::new(match stream.try_clone() {
                        Ok(clone) => clone,
                        Err(_) => return,
                    });
                    let mut request_line = String::new();
                    if reader.read_line(&mut request_line).is_err() {
                        return;
                    }
                    loop {
                        let mut line = String::new();
                        if reader.read_line(&mut line).is_err() || line == "\r\n" || line.is_empty()
                        {
                            break;
                        }
                    }
                    let path = request_line
                        .split_whitespace()
                        .nth(1)
                        .unwrap_or("/")
                        .to_owned();
                    let page = pages.iter().find(|page| page.path == path);
                    let (status, content_type, body) = match page {
                        Some(page) => (page.status, page.content_type.as_str(), page.body.as_str()),
                        None => (404, "text/html", "<html><body>Not found</body></html>"),
                    };
                    let reason = if status < 400 { "OK" } else { "Error" };
                    let response = format!(
                        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = stream.write_all(response.as_bytes());
                });
            }
        });
        PageServer {
            base_url: format!("http://127.0.0.1:{port}"),
        }
    }

    /// For example `http://127.0.0.1:41234`.
    pub fn base_url(&self) -> &str {
        &self.base_url
    }
}
