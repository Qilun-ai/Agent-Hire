//! Manual classification of the indexed software backlog.

use std::sync::{Mutex, TryLockError};

use serde_json::{Map, Value, json};

use crate::decision::{DecisionClient, DecisionJob};
use crate::decision_store::{self, QueuedVerdict};
use crate::store::JobIndex;

/// The decision-model role attached to software backlog verdicts.
pub const ROLE_KEY: &str = "family:software";
/// The default number of jobs in a manual batch.
pub const BATCH: i64 = 96;
/// The decision model remains reserved for form filling during background ticks.
pub const FORM_FILLING_ONLY: bool = true;

/// A backlog classifier. Only one manual batch can run on a handle at a time.
pub struct Autoclassifier {
    index: JobIndex,
    decisions: DecisionClient,
    busy: Mutex<()>,
}

impl Autoclassifier {
    /// Creates a backlog classifier for an index and its configured decision client.
    #[must_use]
    pub fn new(index: JobIndex, decisions: DecisionClient) -> Self {
        Self {
            index,
            decisions,
            busy: Mutex::new(()),
        }
    }

    /// Returns the requested batch result or the Python skip reason.
    #[must_use]
    pub fn classify_batch(&self, limit: i64) -> Value {
        let _busy = match self.busy.try_lock() {
            Ok(guard) => guard,
            Err(TryLockError::WouldBlock) => return json!({"skipped":"already_running"}),
            Err(TryLockError::Poisoned(error)) => error.into_inner(),
        };
        self.run_batch(limit).unwrap_or_else(|error| {
            eprintln!("job index background classification failed: {error}");
            json!({"skipped":"error","error":"OperationalError"})
        })
    }

    fn run_batch(&self, limit: i64) -> rusqlite::Result<Value> {
        let endpoints = self.decisions.endpoints_from_settings(true);
        let Some(first) = endpoints.first() else {
            return Ok(json!({"skipped":"no_decision_model"}));
        };
        let jobs = decision_store::pending_jobs(&self.index, ROLE_KEY, limit)?;
        if jobs.is_empty() {
            return Ok(json!({"classified":0,"endpoint":first.name()}));
        }
        let input: Vec<DecisionJob<'_>> = jobs
            .iter()
            .map(|job| DecisionJob {
                id: &job.id,
                title: &job.title,
                company: &job.company,
                description: &job.description,
            })
            .collect();
        for endpoint in &endpoints {
            let answers =
                self.decisions
                    .classify(endpoint, &input, Some("software"), "software engineer", 4);
            let verdicts: Vec<QueuedVerdict<'_>> = jobs
                .iter()
                .filter_map(|job| {
                    let verdict = answers.get(&job.id)?;
                    Some(QueuedVerdict {
                        job,
                        role_match: verdict["role_match"].as_bool().unwrap_or_default(),
                        probability: verdict["role_p"].as_f64(),
                        label: verdict["level"].as_str(),
                    })
                })
                .collect();
            if !verdicts.is_empty() {
                let classified = decision_store::save_verdicts(
                    &self.index,
                    ROLE_KEY,
                    endpoint.name(),
                    &verdicts,
                )?;
                return Ok(json!({"classified":classified,"endpoint":endpoint.name()}));
            }
        }
        Ok(json!({"classified":0,"skipped":"no_answers"}))
    }

    /// Returns `POST /jobindex/classify` including updated backlog counts.
    pub fn classify_route(&self, limit: i64) -> rusqlite::Result<Value> {
        let mut result = self.classify_batch(limit);
        result["backlog"] = backlog_counts(&self.index)?;
        Ok(result)
    }

    /// Runs the decision-model scheduler tick. It is inert in form-filling-only mode.
    pub fn tick(&self) {
        if !FORM_FILLING_ONLY {
            let _ = self.classify_batch(BATCH);
        }
    }
}

/// Counts active software postings and groups cached verdicts by source.
pub fn backlog_counts(index: &JobIndex) -> rusqlite::Result<Value> {
    let connection = index.connect()?;
    let (total, done): (i64, i64) = connection.query_row(
        "SELECT COUNT(*),COUNT(d.job_id) FROM jobs j
         LEFT JOIN decisions d ON d.job_id=j.id AND d.role_key=?1
         WHERE j.active=1 AND j.rule_software=1",
        [ROLE_KEY],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )?;
    let mut statement = connection
        .prepare("SELECT source, COUNT(*) FROM decisions WHERE role_key=?1 GROUP BY source")?;
    let rows = statement.query_map([ROLE_KEY], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    let mut sources = Map::new();
    for row in rows {
        let (source, count) = row?;
        sources.insert(source, json!(count));
    }
    Ok(json!({"software_jobs":total,"classified":done,"remaining":total-done,"by_source":sources}))
}
