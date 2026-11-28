//! Saving feedback and the resulting batch of learned lead scores.

use rusqlite::{OptionalExtension, TransactionBehavior, params};
use serde_json::{Map, Value};

use crate::database::StoreError;
use crate::leads::{self, LeadError, LeadStore};
use crate::{py_json, truncate_chars};

const TRAINING_FIELDS: &[&str] = &[
    "feedback",
    "platform",
    "company",
    "kind",
    "signal_tags",
    "tech_stack",
    "source_meta",
    "location",
    "urgency",
    "budget",
    "title",
    "description",
];

/// A consistent set of training examples and eligible leads for a ranking pass.
pub struct LearningInputs {
    /// The newest leads with feedback.
    pub examples: Vec<Value>,
    /// The newest leads still eligible to be ranked.
    pub leads: Vec<Value>,
}

impl LeadStore {
    /// Saves feedback and its activity line together, returning the full updated lead.
    pub fn save_feedback(
        &self,
        id: &str,
        feedback: &str,
        note: &str,
    ) -> Result<Option<Map<String, Value>>, LeadError> {
        let transition = match feedback {
            "trash" | "too_generic" | "not_ai" | "not_freelance" | "not_relevant" | "duplicate"
            | "low_quality" | "incorrect_category" => Some("discarded"),
            "already_contacted" | "good" | "relevant" => None,
            _ => return Err(LeadError::Invalid(format!("Invalid feedback: {feedback}"))),
        };

        let mut connection = self.database.connect()?;
        let transaction = connection.transaction().map_err(StoreError::from)?;
        let changed = transaction
            .execute(
                "UPDATE leads SET feedback = ?1, feedback_note = ?2,
                status = CASE WHEN ?3 IS NOT NULL THEN ?3
                    WHEN ?1 = 'already_contacted' AND kind = 'freelance' THEN 'proposal_sent'
                    WHEN ?1 = 'already_contacted' THEN 'applied'
                    ELSE status END
             WHERE job_id = ?4",
                params![feedback, note, transition, id],
            )
            .map_err(StoreError::from)?;
        if changed == 0 {
            return Ok(None);
        }
        transaction
            .execute(
                "INSERT INTO events(job_id, action) VALUES (?1, ?2)",
                params![id, format!("feedback={feedback}")],
            )
            .map_err(StoreError::from)?;
        let updated = leads::lead_by_id(&transaction, id)?;
        // Keep the local activity entry clients saw after writing search feedback.
        if matches!(feedback, "good" | "not_relevant") {
            transaction
                .execute(
                    "INSERT INTO events(job_id, action) VALUES (?1, ?2)",
                    params![id, format!("search_feedback={feedback}")],
                )
                .map_err(StoreError::from)?;
        }
        transaction.commit().map_err(StoreError::from)?;
        Ok(updated)
    }

    /// Reads the training set and eligible rows from the same SQLite snapshot.
    pub fn learning_inputs(&self) -> Result<LearningInputs, StoreError> {
        let mut connection = self.database.connect()?;
        let transaction = connection.transaction()?;
        let examples = {
            let mut statement = transaction.prepare(&format!(
                "SELECT {} FROM leads WHERE feedback != '' ORDER BY created_at DESC LIMIT 300",
                leads::COLUMNS,
            ))?;
            let rows = statement.query_map([], |row| {
                let mut lead = leads::read_model(row)?;
                lead.retain(|key, _| TRAINING_FIELDS.contains(&key.as_str()));
                for key in ["platform", "company", "title", "description"] {
                    if lead[key].is_null() {
                        lead[key] = Value::String(String::new());
                    }
                }
                Ok(Value::Object(lead))
            })?;
            rows.collect::<Result<_, _>>()?
        };
        let leads = {
            let mut statement = transaction.prepare(&format!(
                "SELECT {} FROM leads WHERE feedback = '' AND status != 'discarded'
                 ORDER BY created_at DESC LIMIT 500",
                leads::COLUMNS,
            ))?;
            let rows = statement.query_map([], |row| leads::read_model(row).map(Value::Object))?;
            rows.collect::<Result<_, _>>()?
        };
        transaction.commit()?;
        Ok(LearningInputs { examples, leads })
    }

    /// Writes one ranking pass in a transaction and returns the rows it actually changed.
    pub fn save_learning_scores(&self, changes: &[Value]) -> Result<Vec<Value>, StoreError> {
        if changes.is_empty() {
            return Ok(Vec::new());
        }
        let mut connection = self.database.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut persisted = Vec::new();
        for update in changes {
            let Some(id) = update["job_id"].as_str() else {
                continue;
            };
            let metadata: Option<Option<String>> = transaction.query_row(
                "SELECT source_meta FROM leads WHERE job_id = ?1 AND feedback = '' AND status != 'discarded'",
                [id],
                |row| row.get(0),
            ).optional()?;
            let Some(metadata) = metadata else { continue };
            let mut meta = leads::object_column(&Value::String(metadata.unwrap_or_default()));
            // Preserve hand-offs and other metadata saved while the embedder was running.
            if let Some(learning) = update["source_meta"].get("learning") {
                meta.insert("learning".to_owned(), learning.clone());
            } else {
                meta.shift_remove("learning");
            }
            let number = |key: &str| update[key].as_i64().unwrap_or_default();
            let reason = |key: &str| truncate_chars(update[key].as_str().unwrap_or_default(), 700);
            transaction.execute(
                "UPDATE leads SET signal_score = ?1, signal_reason = ?2, source_meta = ?3,
                    base_signal_score = ?4, learning_delta = ?5, learning_reason = ?6,
                    score = ?7, base_score = ?8 WHERE job_id = ?9",
                params![
                    number("signal_score"),
                    reason("signal_reason"),
                    py_json::dumps(&Value::Object(meta), false),
                    number("base_signal_score"),
                    number("learning_delta"),
                    reason("learning_reason"),
                    update["score"]
                        .as_i64()
                        .unwrap_or_else(|| number("base_score")),
                    number("base_score"),
                    id,
                ],
            )?;
            let row = transaction.query_row(
                &format!("SELECT {} FROM leads WHERE job_id = ?1", leads::COLUMNS),
                [id],
                leads::read_model,
            )?;
            persisted.push(Value::Object(row));
        }
        transaction.commit()?;
        Ok(persisted)
    }
}
