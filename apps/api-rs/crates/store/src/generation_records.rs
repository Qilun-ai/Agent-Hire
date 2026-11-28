//! SQLite inputs and persistence for generation routes.

use rusqlite::{OptionalExtension, TransactionBehavior, params};
use serde_json::{Value, json};

use crate::clock::UtcTime;
use crate::database::{Database, StoreError};
use crate::leads::{self, LeadError, LeadStore};
use crate::py_json;

/// IDs tagged for other résumé variants, and saved wording conflicts.
#[derive(Default)]
pub struct GenerationScope {
    /// Skills excluded by the selected tag.
    pub skills: Vec<String>,
    /// Projects or project points excluded by the selected tag.
    pub projects: Vec<String>,
    /// Experiences or experience points excluded by the selected tag.
    pub experiences: Vec<String>,
    /// Saved wording conflicts with their members.
    pub conflicts: Vec<Value>,
}

/// A generation job status transition.
pub enum GenerationJobUpdate<'a> {
    /// Drafting has started.
    Running,
    /// The enriched lead returned by generation.
    Succeeded(&'a Value),
    /// A failure message for the activity history.
    Failed(&'a str),
}

impl Database {
    /// Loads an explicit résumé template or the default when the ID is empty.
    pub fn resume_template(&self, id: &str) -> Result<Option<String>, StoreError> {
        let connection = self.connect()?;
        let value = if id.is_empty() {
            connection.query_row(
                "SELECT content FROM resume_templates WHERE is_default = 1 ORDER BY created_at DESC, id LIMIT 1",
                [],
                |row| row.get(0),
            ).optional()?
        } else {
            connection
                .query_row(
                    "SELECT content FROM resume_templates WHERE id = ?1",
                    [id],
                    |row| row.get(0),
                )
                .optional()?
        };
        Ok(value)
    }

    /// Reads tag exclusions and conflict groups for generation without changing the profile.
    pub fn generation_scope(&self, tag: &str) -> Result<GenerationScope, StoreError> {
        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        let mut scope = GenerationScope::default();
        if !tag.is_empty() {
            let mut statement = transaction.prepare(
                "SELECT point_kind, point_id FROM point_tags GROUP BY point_kind, point_id
                 HAVING COUNT(CASE WHEN tag_id = ?1 THEN 1 END) = 0
                 ORDER BY point_kind, point_id",
            )?;
            let rows = statement.query_map([tag], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?;
            for row in rows {
                let (kind, id) = row?;
                match kind.as_str() {
                    "skill" => scope.skills.push(id),
                    "project" => scope.projects.push(id),
                    "experience" => scope.experiences.push(id),
                    _ => {}
                }
            }
        }
        {
            let mut groups = transaction.prepare(
                "SELECT id, status, picked_kind, picked_id FROM conflict_groups
                 WHERE status IN ('open', 'picked') ORDER BY created_at, id",
            )?;
            let mut members = transaction.prepare(
                "SELECT point_kind, point_id FROM conflict_members
                 WHERE group_id = ?1 ORDER BY created_at, point_kind, point_id",
            )?;
            let rows = groups.query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, Option<String>>(3)?,
                ))
            })?;
            for row in rows {
                let (id, status, picked_kind, picked_id) = row?;
                let points = members
                    .query_map([&id], |row| {
                        Ok(json!({
                            "point_kind":row.get::<_,String>(0)?,"point_id":row.get::<_,String>(1)?,
                        }))
                    })?
                    .collect::<Result<Vec<_>, _>>()?;
                scope.conflicts.push(json!({
                    "id":id,"status":status,"picked_kind":picked_kind,"picked_id":picked_id,"members":points,
                }));
            }
        }
        transaction.commit()?;
        Ok(scope)
    }

    /// Creates a queued job to track a synchronous package request.
    pub fn create_generation_job(&self, id: &str, lead_id: &str) -> Result<(), StoreError> {
        let connection = self.connect()?;
        connection.execute_batch(
            "CREATE TABLE IF NOT EXISTS generation_jobs (
                job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                progress INTEGER DEFAULT 0, input_json TEXT DEFAULT '{}', result_json TEXT DEFAULT '{}',
                error TEXT DEFAULT '', created_at TEXT DEFAULT '', started_at TEXT DEFAULT '', finished_at TEXT DEFAULT ''
            )",
        )?;
        let input = py_json::dumps(&json!({"job_id":lead_id}), false);
        connection.execute(
            "INSERT INTO generation_jobs(job_id,kind,status,input_json,created_at)
             VALUES (?1,'generate_package','queued',?2,?3)",
            params![id, input, UtcTime::now().to_iso_micros()],
        )?;
        Ok(())
    }

    /// Updates the job's status, timestamps and result together.
    pub fn update_generation_job(
        &self,
        id: &str,
        update: GenerationJobUpdate<'_>,
    ) -> Result<(), StoreError> {
        let connection = self.connect()?;
        let (status, progress, result, error) = match update {
            GenerationJobUpdate::Running => ("running", Some(10), None, None),
            GenerationJobUpdate::Succeeded(lead) => (
                "succeeded",
                Some(100),
                Some(py_json::dumps(&json!({"lead":lead}), false)),
                None,
            ),
            GenerationJobUpdate::Failed(error) => ("failed", None, None, Some(error)),
        };
        connection.execute(
            "UPDATE generation_jobs SET status = ?1, progress = COALESCE(?2, progress),
                result_json = COALESCE(?3, result_json), error = COALESCE(?4, error),
                started_at = CASE WHEN started_at = '' AND ?1 = 'running' THEN ?5 ELSE started_at END,
                finished_at = CASE WHEN finished_at = '' AND ?1 IN ('succeeded','failed') THEN ?5 ELSE finished_at END
             WHERE job_id = ?6",
            params![status, progress, result, error, UtcTime::now().to_iso_micros(), id],
        )?;
        Ok(())
    }
}

impl LeadStore {
    /// Saves both PDFs, the résumé version and keyword coverage before approving the lead.
    pub fn save_generated_assets(
        &self,
        id: &str,
        version: u64,
        lead: &Value,
    ) -> Result<(), LeadError> {
        let mut connection = self.database.connect()?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(StoreError::from)?;
        let meta: Option<Option<String>> = transaction
            .query_row(
                "SELECT source_meta FROM leads WHERE job_id = ?1",
                [id],
                |row| row.get(0),
            )
            .optional()
            .map_err(StoreError::from)?;
        let Some(meta) = meta else {
            return Err(LeadError::NotFound(id.to_owned()));
        };
        let mut meta = leads::object_column(&Value::String(meta.unwrap_or_default()));
        let coverage = &lead["keyword_coverage"];
        if coverage
            .as_object()
            .is_some_and(|object| !object.is_empty())
        {
            meta.insert("keyword_coverage".to_owned(), coverage.clone());
        }
        let resume = lead["resume_asset"].as_str().unwrap_or_default();
        let letter = lead["cover_letter_asset"].as_str().unwrap_or_default();
        let projects = py_json::dumps(&lead["selected_projects"], false);
        transaction
            .execute(
                "UPDATE leads SET status='approved', asset_path=?1, cover_letter_path=?2,
                selected_projects=?3, source_meta=?4, resume_version=?5 WHERE job_id=?6",
                params![
                    resume,
                    letter,
                    projects,
                    py_json::dumps(&Value::Object(meta), false),
                    version,
                    id
                ],
            )
            .map_err(StoreError::from)?;
        transaction
            .execute(
                "INSERT INTO events(job_id,action) VALUES (?1,?2)",
                params![id, format!("assets=resume:{resume} cover:{letter}")],
            )
            .map_err(StoreError::from)?;
        transaction.commit().map_err(StoreError::from)?;
        Ok(())
    }

    /// Saves only the nonempty outreach fields in a generated package.
    pub fn save_generated_outreach(&self, id: &str, package: &Value) -> Result<(), StoreError> {
        let get = |key: &str| package[key].as_str().filter(|field| !field.is_empty());
        let values = [
            get("founder_message"),
            get("linkedin_note"),
            get("cold_email"),
        ];
        if values.iter().any(Option::is_some) {
            self.database.connect()?.execute(
                "UPDATE leads SET outreach_reply=COALESCE(?1,outreach_reply),
                    outreach_dm=COALESCE(?2,outreach_dm), outreach_email=COALESCE(?3,outreach_email)
                 WHERE job_id=?4",
                params![values[0], values[1], values[2], id],
            )?;
        }
        Ok(())
    }

    /// Persists the contact lookup and its status for the lead activity timeline.
    pub fn save_generated_contacts(&self, id: &str, lookup: &Value) -> Result<(), LeadError> {
        let mut connection = self.database.connect()?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(StoreError::from)?;
        let meta: Option<Option<String>> = transaction
            .query_row(
                "SELECT source_meta FROM leads WHERE job_id = ?1",
                [id],
                |row| row.get(0),
            )
            .optional()
            .map_err(StoreError::from)?;
        let Some(meta) = meta else {
            return Err(LeadError::NotFound(id.to_owned()));
        };
        let mut meta = leads::object_column(&Value::String(meta.unwrap_or_default()));
        let lookup = if lookup.as_object().is_none_or(serde_json::Map::is_empty) {
            json!({"status":"empty","contacts":[]})
        } else {
            lookup.clone()
        };
        let status = lookup["status"].as_str().unwrap_or("unknown").to_owned();
        meta.insert("contact_lookup".to_owned(), lookup);
        transaction
            .execute(
                "UPDATE leads SET source_meta=?1 WHERE job_id=?2",
                params![py_json::dumps(&Value::Object(meta), false), id],
            )
            .map_err(StoreError::from)?;
        transaction
            .execute(
                "INSERT INTO events(job_id,action) VALUES (?1,?2)",
                params![id, format!("contact_lookup={status}")],
            )
            .map_err(StoreError::from)?;
        transaction.commit().map_err(StoreError::from)?;
        Ok(())
    }
}
