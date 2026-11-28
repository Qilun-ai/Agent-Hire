//! Live events for `/ws` clients from routes served in Rust.

use serde_json::Value;
use tokio::sync::broadcast;

use agent_hire_store::EventLog;

use crate::log;

/// How many undelivered messages a slow client may fall behind before it skips some.
const BACKLOG: usize = 256;

/// Fans events out to every connected `/ws` client.
#[derive(Clone)]
pub struct Hub {
    sender: broadcast::Sender<String>,
    activity: EventLog,
}

impl Hub {
    pub fn new(activity: EventLog) -> Hub {
        Hub {
            sender: broadcast::channel(BACKLOG).0,
            activity,
        }
    }

    /// A receiver for one client.
    pub fn subscribe(&self) -> broadcast::Receiver<String> {
        self.sender.subscribe()
    }

    /// Sends `message` to every client. Agent and ingest messages are also written to the
    /// activity log as `"<event>: <detail>"` under their job (or task) id.
    pub async fn broadcast(&self, message: Value) {
        let kind = message["type"].as_str().unwrap_or("");
        if kind == "agent" || kind.starts_with("ingest_") {
            let first = |keys: &[&str]| -> String {
                keys.iter()
                    .find_map(|key| {
                        let value = &message[*key];
                        let text = match value {
                            Value::String(text) => text.trim().to_owned(),
                            Value::Null => String::new(),
                            other => other.to_string(),
                        };
                        (!text.is_empty()).then_some(text)
                    })
                    .unwrap_or_default()
            };
            let id = first(&["job_id", "task_id"]);
            let event = {
                let name = first(&["event", "type"]);
                if name.is_empty() {
                    "agent".to_owned()
                } else {
                    name
                }
            };
            let detail = first(&["msg", "message", "thought", "error"]);
            let text = if detail.is_empty() {
                event
            } else {
                format!("{event}: {detail}")
            };
            let log_book = self.activity.clone();
            let recorded = tokio::task::spawn_blocking(move || log_book.record(&id, &text)).await;
            if !matches!(recorded, Ok(Ok(()))) {
                log::warning("ws", "websocket_event_record_failed");
            }
        }
        // Nobody listening is fine.
        let _ = self.sender.send(message.to_string());
    }
}
