//! The activity log (`events(id, job_id, action, ts)`).

use rusqlite::params;

use crate::database::{Database, StoreError};

/// The job id recorded for events that belong to no job.
pub const SYSTEM_JOB_ID: &str = "__system__";

/// One logged event.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Event {
    /// The job it is about, or [`SYSTEM_JOB_ID`].
    pub job_id: Option<String>,
    /// What happened, in words.
    pub action: Option<String>,
    /// When, as SQLite `datetime('now')` text (UTC, `YYYY-MM-DD HH:MM:SS`).
    pub ts: Option<String>,
}

/// Appends to and reads the activity log.
#[derive(Clone, Debug)]
pub struct EventLog {
    database: Database,
}

impl EventLog {
    /// The log in `database`.
    pub fn new(database: Database) -> EventLog {
        EventLog { database }
    }

    /// Logs `action` for `job_id` (an empty id becomes [`SYSTEM_JOB_ID`]). The id is cut to 160
    /// characters and the action to 1000.
    pub fn record(&self, job_id: &str, action: &str) -> Result<(), StoreError> {
        let job_id = if job_id.is_empty() {
            SYSTEM_JOB_ID
        } else {
            job_id
        };
        let connection = self.database.connect()?;
        connection.execute(
            "INSERT INTO events(job_id, action) VALUES (?1, ?2)",
            params![truncate_chars(job_id, 160), truncate_chars(action, 1000)],
        )?;
        Ok(())
    }

    /// The most recent event timestamp for each job, in the order of the given ids.
    pub fn latest_timestamps(&self, job_ids: &[&str]) -> Result<Vec<Option<String>>, StoreError> {
        if job_ids.is_empty() {
            return Ok(Vec::new());
        }
        let connection = self.database.connect()?;
        let mut statement = connection.prepare("SELECT max(ts) FROM events WHERE job_id = ?1")?;
        let mut timestamps = Vec::with_capacity(job_ids.len());
        for id in job_ids {
            timestamps.push(statement.query_row([id], |row| row.get(0))?);
        }
        Ok(timestamps)
    }

    /// The newest events first, optionally for one job. Events logged in the same second come
    /// newest-inserted first.
    pub fn list(&self, limit: i64, job_id: Option<&str>) -> Result<Vec<Event>, StoreError> {
        let connection = self.database.connect()?;
        let job_id = job_id.filter(|id| !id.is_empty());
        let mut statement = connection.prepare(
            "SELECT job_id, action, ts FROM events WHERE (?1 IS NULL OR job_id = ?1) \
             ORDER BY ts DESC, id DESC LIMIT ?2",
        )?;
        let rows = statement.query_map(params![job_id, limit], |row| {
            Ok(Event {
                job_id: row.get(0)?,
                action: row.get(1)?,
                ts: row.get(2)?,
            })
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }
}

/// The first `max` characters of `text`.
pub fn truncate_chars(text: &str, max: usize) -> &str {
    match text.char_indices().nth(max) {
        Some((end, _)) => &text[..end],
        None => text,
    }
}
