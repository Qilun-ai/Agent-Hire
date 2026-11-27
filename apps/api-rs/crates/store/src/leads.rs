//! The applications pipeline (`leads` and the tables around it).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt;

use agent_hire_leadtext::resolve_lead_seniority;
use rusqlite::types::ValueRef;
use rusqlite::{Connection, OptionalExtension, Row, params};
use serde_json::{Map, Value, json};

use crate::clock::UtcTime;
use crate::database::{Database, StoreError};
use crate::lead_filters::{
    Enrichment, LeadFilters, LeadRecord, LeadSort, LeadView, Requirement, is_applied_status,
    normalize_location, similarity_key, text,
};
use crate::lead_identity::{canonical_identity, identity_bundle, normalize_source_url};
use crate::py_json;

/// The columns of the standard read model, in order.
pub(super) const COLUMNS: &str = "job_id, title, company, url, platform, status, score, reason, match_points, asset_path, \
     description, gaps, cover_letter_path, selected_projects, kind, budget, signal_score, signal_reason, \
     signal_tags, outreach_reply, outreach_dm, source_meta, feedback, feedback_note, followup_due_at, \
     last_contacted_at, outreach_email, proposal_draft, fit_bullets, followup_sequence, proof_snippet, \
     tech_stack, location, urgency, base_signal_score, learning_delta, learning_reason, created_at, \
     resume_version, base_score";

/// Every status a lead may have.
const STATUSES: [&str; 14] = [
    "discovered",
    "evaluating",
    "tailoring",
    "approved",
    "applied",
    "interviewing",
    "rejected",
    "accepted",
    "discarded",
    "matched",
    "bidding",
    "proposal_sent",
    "awarded",
    "completed",
];

/// Whether `status` is a lead status.
pub fn is_valid_status(status: &str) -> bool {
    STATUSES.contains(&status)
}

/// A lead operation that failed.
#[derive(Debug)]
pub enum LeadError {
    /// No lead has this id.
    NotFound(String),
    /// The request was invalid; the message says why.
    Invalid(String),
    /// The discovery session filter names a session that does not exist.
    UnknownSession,
    /// The database failed.
    Store(StoreError),
}

impl fmt::Display for LeadError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LeadError::NotFound(job_id) => write!(formatter, "lead '{job_id}' not found"),
            LeadError::Invalid(message) => formatter.write_str(message),
            LeadError::UnknownSession => formatter.write_str("unknown save session"),
            LeadError::Store(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for LeadError {}

impl From<StoreError> for LeadError {
    fn from(error: StoreError) -> LeadError {
        LeadError::Store(error)
    }
}

impl From<rusqlite::Error> for LeadError {
    fn from(error: rusqlite::Error) -> LeadError {
        LeadError::Store(StoreError::Sqlite(error))
    }
}

/// Reads and changes leads.
#[derive(Clone, Debug)]
pub struct LeadStore {
    pub(super) database: Database,
}

/// A SQLite value as JSON.
fn sql_json(value: ValueRef<'_>) -> Value {
    match value {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(number) => json!(number),
        ValueRef::Real(number) => json!(number),
        ValueRef::Text(bytes) => Value::String(String::from_utf8_lossy(bytes).into_owned()),
        ValueRef::Blob(bytes) => Value::String(String::from_utf8_lossy(bytes).into_owned()),
    }
}

/// Python truthiness of a JSON value.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|n| n != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

fn or_default(value: Value, default: Value) -> Value {
    if truthy(&value) { value } else { default }
}

/// A JSON list column: arrays as stored, comma lists for legacy text, `[]` otherwise.
fn list_column(value: &Value) -> Value {
    let Some(raw) = value.as_str() else {
        return if value.is_array() {
            value.clone()
        } else {
            json!([])
        };
    };
    if raw.trim().is_empty() {
        return json!([]);
    }
    match serde_json::from_str::<Value>(raw) {
        Ok(Value::Array(items)) => Value::Array(items),
        Ok(_) => json!([]),
        Err(_) => Value::Array(
            raw.split(',')
                .map(str::trim)
                .filter(|part| !part.is_empty())
                .map(|part| Value::String(part.to_owned()))
                .collect(),
        ),
    }
}

/// The text stored for a list column: `[]` for nothing, a string that already looks like JSON as
/// is, comma lists split, list items trimmed with blanks dropped.
fn list_text(value: Option<&Value>) -> String {
    let items: Vec<Value> = match value {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::String(text)) if text.starts_with('[') => return text.clone(),
        Some(Value::String(text)) => text
            .split(',')
            .map(str::trim)
            .filter(|part| !part.is_empty())
            .map(|part| json!(part))
            .collect(),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| match item {
                Value::String(text) => text.trim().to_owned(),
                other => other.to_string(),
            })
            .filter(|item| !item.is_empty())
            .map(Value::String)
            .collect(),
        Some(other) => vec![json!(other.to_string())],
    };
    py_json::dumps(&Value::Array(items), false)
}

/// A JSON object column: the object as stored, `{}` otherwise.
pub(super) fn object_column(value: &Value) -> Map<String, Value> {
    match value {
        Value::String(raw) => match serde_json::from_str::<Value>(raw) {
            Ok(Value::Object(map)) => map,
            _ => Map::new(),
        },
        Value::Object(map) => map.clone(),
        _ => Map::new(),
    }
}

/// The standard read model of one `leads` row selected with [`COLUMNS`].
pub(super) fn read_model(row: &Row<'_>) -> rusqlite::Result<Map<String, Value>> {
    let column = |index: usize| -> rusqlite::Result<Value> { Ok(sql_json(row.get_ref(index)?)) };
    let string =
        |index: usize| -> rusqlite::Result<Value> { Ok(or_default(column(index)?, json!(""))) };
    let number =
        |index: usize| -> rusqlite::Result<Value> { Ok(or_default(column(index)?, json!(0))) };
    let meta = object_column(&column(21)?);
    let asset_path = string(9)?;
    let mut lead = Map::new();
    lead.insert("job_id".to_owned(), column(0)?);
    lead.insert("title".to_owned(), column(1)?);
    lead.insert("company".to_owned(), column(2)?);
    lead.insert("url".to_owned(), column(3)?);
    lead.insert("platform".to_owned(), column(4)?);
    lead.insert("status".to_owned(), column(5)?);
    lead.insert("score".to_owned(), number(6)?);
    lead.insert("reason".to_owned(), string(7)?);
    lead.insert("match_points".to_owned(), list_column(&column(8)?));
    lead.insert("asset".to_owned(), asset_path.clone());
    lead.insert("description".to_owned(), string(10)?);
    lead.insert("gaps".to_owned(), list_column(&column(11)?));
    lead.insert("resume_asset".to_owned(), asset_path);
    lead.insert("cover_letter_asset".to_owned(), string(12)?);
    lead.insert("selected_projects".to_owned(), list_column(&column(13)?));
    lead.insert("kind".to_owned(), or_default(column(14)?, json!("job")));
    lead.insert("budget".to_owned(), string(15)?);
    lead.insert("signal_score".to_owned(), number(16)?);
    lead.insert("signal_reason".to_owned(), string(17)?);
    lead.insert("signal_tags".to_owned(), list_column(&column(18)?));
    lead.insert("outreach_reply".to_owned(), string(19)?);
    lead.insert("outreach_dm".to_owned(), string(20)?);
    let meta_value = |key: &str, default: Value| {
        or_default(meta.get(key).cloned().unwrap_or(Value::Null), default)
    };
    let quality_score = meta_value("lead_quality_score", json!(0));
    let quality_reason = meta_value("lead_quality_reason", json!(""));
    let coverage = meta_value("keyword_coverage", json!({}));
    let contacts = meta_value("contact_lookup", json!({}));
    lead.insert("source_meta".to_owned(), Value::Object(meta));
    lead.insert("lead_quality_score".to_owned(), quality_score);
    lead.insert("lead_quality_reason".to_owned(), quality_reason);
    lead.insert("keyword_coverage".to_owned(), coverage);
    lead.insert("contact_lookup".to_owned(), contacts);
    lead.insert("feedback".to_owned(), string(22)?);
    lead.insert("feedback_note".to_owned(), string(23)?);
    lead.insert("followup_due_at".to_owned(), string(24)?);
    lead.insert("last_contacted_at".to_owned(), string(25)?);
    lead.insert("outreach_email".to_owned(), string(26)?);
    lead.insert("proposal_draft".to_owned(), string(27)?);
    lead.insert("fit_bullets".to_owned(), list_column(&column(28)?));
    lead.insert("followup_sequence".to_owned(), list_column(&column(29)?));
    lead.insert("proof_snippet".to_owned(), string(30)?);
    lead.insert("tech_stack".to_owned(), list_column(&column(31)?));
    lead.insert("location".to_owned(), string(32)?);
    lead.insert("urgency".to_owned(), string(33)?);
    lead.insert("base_signal_score".to_owned(), number(34)?);
    lead.insert("learning_delta".to_owned(), number(35)?);
    lead.insert("learning_reason".to_owned(), string(36)?);
    lead.insert("created_at".to_owned(), string(37)?);
    lead.insert("resume_version".to_owned(), number(38)?);
    lead.insert("base_score".to_owned(), number(39)?);
    Ok(lead)
}

fn table_exists(connection: &Connection, name: &str) -> Result<bool, StoreError> {
    Ok(connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1)",
        [name],
        |row| row.get(0),
    )?)
}

/// Adds `source_meta.seniority_level`, `source_meta.is_beginner` and a top-level
/// `seniority_level` to a job lead, as the list and detail routes show it.
pub fn annotate_seniority(lead: &mut Map<String, Value>) {
    let band = resolve_lead_seniority(lead);
    let mut meta = lead
        .get("source_meta")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    meta.insert("seniority_level".to_owned(), json!(band.as_str()));
    meta.insert("is_beginner".to_owned(), json!(band.is_beginner()));
    lead.insert("source_meta".to_owned(), Value::Object(meta));
    lead.insert("seniority_level".to_owned(), json!(band.as_str()));
}

/// Whether a lead is a job lead (`kind` empty or `job`).
fn is_job(lead: &Map<String, Value>) -> bool {
    matches!(lead.get("kind"), None | Some(Value::Null)) || matches!(text(lead, "kind"), "" | "job")
}

impl LeadStore {
    /// The leads in `database`.
    pub fn new(database: Database) -> LeadStore {
        LeadStore { database }
    }

    /// Every lead, newest first, with enrichment attached.
    pub fn all(&self) -> Result<Vec<Map<String, Value>>, StoreError> {
        let connection = self.database.connect()?;
        let records = load_records(&connection)?;
        Ok(records.into_iter().map(|record| record.lead).collect())
    }

    /// One lead with its 20 newest events, or `None`.
    pub fn by_id(&self, job_id: &str) -> Result<Option<Map<String, Value>>, StoreError> {
        let connection = self.database.connect()?;
        lead_by_id(&connection, job_id)
    }

    /// Changes a lead's status and logs `status_changed=<status>`.
    pub fn update_status(&self, job_id: &str, status: &str) -> Result<(), LeadError> {
        if !is_valid_status(status) {
            return Err(LeadError::Invalid(format!("Invalid status: {status}")));
        }
        let mut connection = self.database.connect()?;
        let transaction = connection.transaction()?;
        let changed = transaction.execute(
            "UPDATE leads SET status = ?1 WHERE job_id = ?2",
            params![status, job_id],
        )?;
        if changed == 0 {
            return Err(LeadError::NotFound(job_id.to_owned()));
        }
        transaction.execute(
            "INSERT INTO events(job_id, action) VALUES (?1, ?2)",
            params![job_id, format!("status_changed={status}")],
        )?;
        transaction.commit()?;
        Ok(())
    }

    /// Inserts a new lead (never overwrites an existing id); returns whether a row was added.
    pub fn save(&self, lead: &Map<String, Value>) -> Result<bool, StoreError> {
        let text = |key: &str| -> String {
            match lead.get(key) {
                Some(Value::String(value)) => value.clone(),
                Some(value) if truthy(value) => value.to_string(),
                _ => String::new(),
            }
        };
        let integer = |key: &str| -> i64 {
            match lead.get(key) {
                Some(Value::Number(number)) => number
                    .as_i64()
                    .unwrap_or_else(|| number.as_f64().unwrap_or(0.0) as i64),
                Some(Value::String(value)) => value.trim().parse().unwrap_or(0),
                Some(Value::Bool(true)) => 1,
                _ => 0,
            }
        };
        let kind = text("kind");
        let signal_score = integer("signal_score");
        let base_signal = match integer("base_signal_score") {
            0 => signal_score,
            other => other,
        };
        let meta = lead
            .get("source_meta")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let connection = self.database.connect()?;
        let inserted = connection.execute(
            "INSERT OR IGNORE INTO leads(job_id, title, company, url, platform, description, kind, budget, signal_score, \
             signal_reason, signal_tags, outreach_reply, outreach_dm, outreach_email, proposal_draft, fit_bullets, \
             followup_sequence, proof_snippet, tech_stack, location, urgency, base_signal_score, learning_delta, \
             learning_reason, source_meta) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22,?23,?24,?25)",
            params![
                text("job_id"),
                text("title"),
                text("company"),
                text("url"),
                text("platform"),
                text("description"),
                if kind.is_empty() { "job".to_owned() } else { kind },
                text("budget"),
                signal_score,
                text("signal_reason").chars().take(700).collect::<String>(),
                list_text(lead.get("signal_tags")),
                text("outreach_reply"),
                text("outreach_dm"),
                text("outreach_email"),
                text("proposal_draft"),
                list_text(lead.get("fit_bullets")),
                list_text(lead.get("followup_sequence")),
                text("proof_snippet"),
                list_text(lead.get("tech_stack")),
                text("location"),
                text("urgency"),
                base_signal,
                integer("learning_delta"),
                text("learning_reason").chars().take(700).collect::<String>(),
                py_json::dumps(&Value::Object(meta), false),
            ],
        )?;
        Ok(inserted > 0)
    }

    /// Appends an activity line for a lead (the action is cut to 200 characters).
    pub fn record_event(&self, job_id: &str, action: &str) -> Result<(), StoreError> {
        let connection = self.database.connect()?;
        let action: String = action.chars().take(200).collect();
        connection.execute(
            "INSERT INTO events(job_id, action) VALUES (?1, ?2)",
            params![job_id, action],
        )?;
        Ok(())
    }

    /// Sets the follow-up date (and the first-contact date if unset); returns the updated lead,
    /// or `None` if there is no such lead.
    pub fn set_followup(
        &self,
        job_id: &str,
        due: &str,
        now: &str,
    ) -> Result<Option<Map<String, Value>>, StoreError> {
        let mut connection = self.database.connect()?;
        let transaction = connection.transaction()?;
        let exists: Option<i64> = transaction
            .query_row("SELECT 1 FROM leads WHERE job_id = ?1", [job_id], |row| {
                row.get(0)
            })
            .optional()?;
        if exists.is_none() {
            return Ok(None);
        }
        transaction.execute(
            "UPDATE leads SET followup_due_at = ?1, \
             last_contacted_at = CASE WHEN COALESCE(last_contacted_at, '') = '' THEN ?2 ELSE last_contacted_at END \
             WHERE job_id = ?3",
            params![due, now, job_id],
        )?;
        transaction.execute(
            "INSERT INTO events(job_id, action) VALUES (?1, ?2)",
            params![job_id, format!("followup_due={due}")],
        )?;
        transaction.commit()?;
        lead_by_id(&connection, job_id)
    }

    /// Stores the extension hand-off for a lead in `source_meta.prepared_fill_context`.
    pub fn save_prepared_fill_context(
        &self,
        job_id: &str,
        context: &Value,
    ) -> Result<(), LeadError> {
        self.set_source_meta_field(job_id, "prepared_fill_context", context)
    }

    /// Sets one top-level `source_meta` key.
    pub fn set_source_meta_field(
        &self,
        job_id: &str,
        key: &str,
        value: &Value,
    ) -> Result<(), LeadError> {
        let mut connection = self.database.connect()?;
        let transaction = connection.transaction()?;
        let raw: Option<Option<String>> = transaction
            .query_row(
                "SELECT source_meta FROM leads WHERE job_id = ?1",
                [job_id],
                |row| row.get(0),
            )
            .optional()?;
        let Some(raw) = raw else {
            return Err(LeadError::NotFound(job_id.to_owned()));
        };
        let mut meta = object_column(&Value::String(raw.unwrap_or_default()));
        meta.insert(key.to_owned(), value.clone());
        transaction.execute(
            "UPDATE leads SET source_meta = ?1 WHERE job_id = ?2",
            params![py_json::dumps(&Value::Object(meta), false), job_id],
        )?;
        transaction.commit()?;
        Ok(())
    }

    /// Deletes a lead and its events, keeping tombstones so it is not re-imported.
    pub fn delete(&self, job_id: &str) -> Result<(), LeadError> {
        let mut connection = self.database.connect()?;
        let transaction =
            connection.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let row: Option<(Option<String>, Option<String>)> = transaction
            .query_row(
                "SELECT url, source_meta FROM leads WHERE job_id = ?1",
                [job_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let Some((url, meta)) = row else {
            return Err(LeadError::NotFound(job_id.to_owned()));
        };
        if table_exists(&transaction, "lead_tombstones")? {
            let identities =
                deletion_identities(&transaction, job_id, url.as_deref(), meta.as_deref())?;
            let bundle = Value::Array(
                identities
                    .iter()
                    .map(|(kind, identity)| json!([kind, identity]))
                    .collect(),
            );
            let bundle_text = py_json::dumps(&bundle, true);
            let deleted_at = UtcTime::now().to_iso_micros();
            for (kind, identity) in &identities {
                transaction.execute(
                    "INSERT INTO lead_tombstones(kind, identity, deleted_at, identities_json) VALUES (?1, ?2, ?3, ?4) \
                     ON CONFLICT(kind, identity) DO UPDATE SET deleted_at = excluded.deleted_at, \
                     identities_json = excluded.identities_json",
                    params![kind, identity, deleted_at, bundle_text],
                )?;
            }
        }
        transaction.execute("DELETE FROM events WHERE job_id = ?1", [job_id])?;
        transaction.execute("DELETE FROM leads WHERE job_id = ?1", [job_id])?;
        transaction.commit()?;
        Ok(())
    }

    /// One page of job leads.
    pub fn page(
        &self,
        filters: &LeadFilters,
        view: LeadView,
        sort: LeadSort,
        page: usize,
        limit: usize,
        collapse_similar: bool,
    ) -> Result<Value, LeadError> {
        let connection = self.database.connect()?;
        let records = self.filtered(&connection, filters)?;
        let mut leads: Vec<Map<String, Value>> = records
            .into_iter()
            .map(|record| record.lead)
            .filter(|lead| LeadFilters::admits_view(view, lead))
            .collect();
        leads.sort_by(|a, b| sort.compare(a, b));
        let postings_total = leads.len();
        if collapse_similar {
            leads = collapse(leads, sort);
        }
        let total = leads.len();
        let pages = total.div_ceil(limit);
        let start = (page - 1).saturating_mul(limit);
        let items: Vec<Value> = leads
            .into_iter()
            .skip(start)
            .take(limit)
            .map(Value::Object)
            .collect();
        let mut out = json!({
            "items": items,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": pages,
            "has_more": page < pages,
        });
        if collapse_similar {
            out["postings_total"] = json!(postings_total);
        }
        Ok(out)
    }

    /// Every job lead passing the filters (no view), newest first, for the legacy list.
    pub fn filtered_leads(
        &self,
        filters: &LeadFilters,
    ) -> Result<Vec<Map<String, Value>>, LeadError> {
        let connection = self.database.connect()?;
        Ok(self
            .filtered(&connection, filters)?
            .into_iter()
            .map(|record| record.lead)
            .collect())
    }

    fn filtered(
        &self,
        connection: &Connection,
        filters: &LeadFilters,
    ) -> Result<Vec<LeadRecord>, LeadError> {
        let members = match &filters.session_id {
            Some(session) => Some(session_members(connection, session)?),
            None => None,
        };
        let now = UtcTime::now().unix_seconds();
        Ok(load_records(connection)?
            .into_iter()
            .filter(|record| is_job(&record.lead))
            .filter(|record| filters.admits(record, members.as_ref(), now))
            .collect())
    }

    /// Counts per bucket and the facet lists for the filters.
    pub fn summary(&self, filters: &LeadFilters) -> Result<Value, LeadError> {
        let connection = self.database.connect()?;
        let records = self.filtered(&connection, filters)?;
        let interviewed = interviewed_jobs(&connection)?;
        let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
        for bucket in [
            "hot",
            "discovered",
            "evaluated",
            "evaluating",
            "tailoring",
            "approved",
            "ready",
            "applied",
            "discarded",
            "interviewing",
            "accepted",
            "rejected",
            "accepted_after_interview",
            "rejected_after_interview",
        ] {
            counts.insert(bucket, 0);
        }
        let mut platforms: HashMap<String, usize> = HashMap::new();
        let mut locations: HashMap<String, (String, usize)> = HashMap::new();
        let mut companies: HashMap<String, (String, usize)> = HashMap::new();
        let mut skills: HashMap<String, HashSet<String>> = HashMap::new();
        let mut sponsorship: Map<String, Value> = Map::new();
        for record in &records {
            let lead = &record.lead;
            let status = text(lead, "status");
            let mut bump = |bucket: &str| {
                if let Some(count) = counts.get_mut(bucket) {
                    *count += 1;
                }
            };
            if LeadFilters::admits_view(LeadView::Hot, lead) {
                bump("hot");
            }
            if LeadFilters::admits_view(LeadView::Evaluated, lead) {
                bump("evaluated");
            }
            if matches!(status, "tailoring" | "approved") {
                bump("ready");
            }
            if is_applied_status(status) {
                bump("applied");
            }
            if matches!(
                status,
                "discovered"
                    | "evaluating"
                    | "tailoring"
                    | "approved"
                    | "discarded"
                    | "interviewing"
                    | "accepted"
                    | "rejected"
            ) {
                bump(status);
            }
            let job_id = text(lead, "job_id");
            if interviewed.contains(job_id) {
                if status == "accepted" {
                    bump("accepted_after_interview");
                } else if status == "rejected" {
                    bump("rejected_after_interview");
                }
            }
            let platform = text(lead, "platform");
            if !platform.is_empty() {
                *platforms.entry(platform.to_owned()).or_default() += 1;
            }
            let raw_location = text(lead, "location");
            let place = normalize_location(raw_location);
            if !place.is_empty() {
                let entry = locations
                    .entry(place)
                    .or_insert_with(|| (raw_location.to_owned(), 0));
                if raw_location < entry.0.as_str() {
                    entry.0 = raw_location.to_owned();
                }
                entry.1 += 1;
            }
            let company = text(lead, "company").trim();
            if !company.is_empty() {
                let entry = companies
                    .entry(company.to_lowercase())
                    .or_insert_with(|| (company.to_owned(), 0));
                if company < entry.0.as_str() {
                    entry.0 = company.to_owned();
                }
                entry.1 += 1;
            }
            for item in &record.requirements {
                let counted = matches!(item.requirement.as_str(), "required" | "preferred")
                    && !matches!(
                        item.item_type.as_str(),
                        "certification" | "licence" | "language"
                    );
                if counted {
                    skills
                        .entry(item.name.clone())
                        .or_default()
                        .insert(job_id.to_owned());
                }
            }
            let stated = record
                .enrichment
                .as_ref()
                .and_then(|e| e.sponsorship.clone())
                .unwrap_or_else(|| "not_stated".to_owned());
            let count = sponsorship.entry(stated).or_insert(json!(0));
            *count = json!(count.as_u64().unwrap_or(0) + 1);
        }
        let facet = |mut rows: Vec<(String, usize)>, limit: usize| -> Value {
            rows.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
            Value::Array(
                rows.into_iter()
                    .take(limit)
                    .map(|(value, count)| json!({"value": value, "count": count}))
                    .collect(),
            )
        };
        let skill_rows: Vec<(String, usize)> = skills
            .into_iter()
            .map(|(name, jobs)| (name, jobs.len()))
            .collect();
        Ok(json!({
            "total": records.len(),
            "counts": counts,
            "platforms": facet(platforms.into_iter().collect(), 20),
            "locations": facet(locations.into_values().collect(), 20),
            "skills": facet(skill_rows, 150),
            "companies": facet(companies.into_values().collect(), 100),
            "sponsorship": sponsorship,
        }))
    }

    /// Marks obvious junk (HN commentary, rows without a title or URL) as discarded.
    pub fn cleanup(&self, limit: i64, dry_run: bool) -> Result<Value, StoreError> {
        let limit = limit.clamp(1, 5000);
        let mut connection = self.database.connect()?;
        let candidates: Vec<(String, Map<String, Value>)> = {
            let mut statement = connection.prepare(
                "SELECT job_id, title, company, platform, url, description, reason, signal_reason FROM leads \
                 WHERE COALESCE(status, '') NOT IN ('approved','applied','interviewing','rejected','accepted','discarded','completed') \
                 ORDER BY created_at DESC LIMIT ?1",
            )?;
            let rows = statement.query_map([limit], |row| {
                let mut fields = Map::new();
                for (index, key) in [
                    "job_id",
                    "title",
                    "company",
                    "platform",
                    "url",
                    "description",
                    "reason",
                    "signal_reason",
                ]
                .iter()
                .enumerate()
                {
                    fields.insert((*key).to_owned(), sql_json(row.get_ref(index)?));
                }
                Ok((text(&fields, "job_id").to_owned(), fields))
            })?;
            rows.collect::<rusqlite::Result<_>>()?
        };
        let scanned = candidates.len();
        let transaction = connection.transaction()?;
        let mut items = Vec::new();
        for (job_id, fields) in candidates {
            let reasons = crate::lead_cleanup::reasons(&fields);
            if reasons.is_empty() {
                continue;
            }
            if !dry_run {
                let note: String = format!("DB cleanup: {}", reasons.join("; "))
                    .chars()
                    .take(1000)
                    .collect();
                transaction.execute(
                    "UPDATE leads SET status = 'discarded', feedback = 'incorrect_category', feedback_note = ?1 WHERE job_id = ?2",
                    params![note, job_id],
                )?;
                transaction.execute(
                    "INSERT INTO events(job_id, action) VALUES (?1, ?2)",
                    params![job_id, note],
                )?;
            }
            items.push(json!({
                "job_id": job_id,
                "title": fields.get("title").cloned().unwrap_or(Value::Null),
                "company": fields.get("company").cloned().unwrap_or(Value::Null),
                "platform": fields.get("platform").cloned().unwrap_or(Value::Null),
                "reasons": reasons,
            }));
        }
        transaction.commit()?;
        let discarded = if dry_run { 0 } else { items.len() };
        Ok(json!({
            "scanned": scanned,
            "discarded": discarded,
            "candidates": items.len(),
            "dry_run": dry_run,
            "items": items,
        }))
    }
}

/// The page representative per group of near-identical postings, with the siblings listed.
fn collapse(leads: Vec<Map<String, Value>>, sort: LeadSort) -> Vec<Map<String, Value>> {
    let mut groups: Vec<(String, Vec<Map<String, Value>>)> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::new();
    for lead in leads {
        let key = similarity_key(&lead);
        match index.get(&key) {
            Some(position) => groups[*position].1.push(lead),
            None => {
                index.insert(key.clone(), groups.len());
                groups.push((key, vec![lead]));
            }
        }
    }
    let engaged = |lead: &Map<String, Value>| !matches!(text(lead, "status"), "discovered" | "");
    let mut representatives: Vec<Map<String, Value>> = Vec::new();
    for (_, mut members) in groups {
        members.sort_by(|a, b| {
            engaged(b)
                .cmp(&engaged(a))
                .then_with(|| sort.compare(a, b))
                .then_with(|| text(a, "job_id").cmp(text(b, "job_id")))
        });
        let mut members = members.into_iter();
        let Some(mut head) = members.next() else {
            continue;
        };
        let siblings: Vec<Value> = members
            .map(|lead| json!({"job_id": lead.get("job_id"), "url": lead.get("url"), "status": lead.get("status")}))
            .collect();
        head.insert("similar_count".to_owned(), json!(siblings.len()));
        head.insert("similar_postings".to_owned(), Value::Array(siblings));
        representatives.push(head);
    }
    representatives.sort_by(|a, b| sort.compare(a, b));
    representatives
}

pub(super) fn lead_by_id(
    connection: &Connection,
    job_id: &str,
) -> Result<Option<Map<String, Value>>, StoreError> {
    let lead = connection
        .query_row(
            &format!("SELECT {COLUMNS} FROM leads WHERE job_id = ?1"),
            [job_id],
            read_model,
        )
        .optional()?;
    let Some(mut lead) = lead else {
        return Ok(None);
    };
    let mut statement = connection.prepare(
        "SELECT action, ts FROM events WHERE job_id = ?1 ORDER BY ts DESC, id DESC LIMIT 20",
    )?;
    let events = statement.query_map([job_id], |row| {
        Ok(json!({"action": sql_json(row.get_ref(0)?), "ts": sql_json(row.get_ref(1)?)}))
    })?;
    let events: Vec<Value> = events.collect::<rusqlite::Result<_>>()?;
    lead.insert("events".to_owned(), Value::Array(events));
    Ok(Some(lead))
}

/// Every lead, newest first, with enrichment and requirements.
fn load_records(connection: &Connection) -> Result<Vec<LeadRecord>, StoreError> {
    let mut statement = connection.prepare(&format!(
        "SELECT {COLUMNS} FROM leads ORDER BY created_at DESC"
    ))?;
    let leads: Vec<Map<String, Value>> = statement
        .query_map([], read_model)?
        .collect::<rusqlite::Result<_>>()?;
    let mut enrichments = HashMap::new();
    if table_exists(connection, "lead_enrichment")? {
        let mut statement = connection.prepare(
            "SELECT job_id, experience_level, experience_status, experience_reason, min_required_years, \
             max_required_years, preferred_min_years, role_status, primary_function_id, result_json, \
             requirements_status, sponsorship, employment_type FROM lead_enrichment",
        )?;
        let rows = statement.query_map([], |row| {
            let mut fields = Vec::new();
            for index in 0..13 {
                fields.push(sql_json(row.get_ref(index)?));
            }
            Ok(fields)
        })?;
        for row in rows {
            let row = row?;
            if let Some(job_id) = row[0].as_str() {
                enrichments.insert(job_id.to_owned(), row);
            }
        }
    }
    let mut memberships: HashMap<String, (Vec<String>, Vec<String>)> = HashMap::new();
    if table_exists(connection, "lead_role_memberships")? {
        let mut statement = connection.prepare(
            "SELECT job_id, node_id, membership FROM lead_role_memberships ORDER BY node_id",
        )?;
        let rows = statement.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        })?;
        for row in rows {
            let (job_id, node, membership) = row?;
            let entry = memberships.entry(job_id).or_default();
            match membership.as_deref() {
                Some("member") => entry.0.push(node),
                Some("unresolved") => entry.1.push(node),
                _ => {}
            }
        }
    }
    let mut requirements: HashMap<String, Vec<Requirement>> = HashMap::new();
    if table_exists(connection, "lead_requirements")? {
        let mut statement = connection
            .prepare("SELECT job_id, name, requirement, item_type FROM lead_requirements")?;
        let rows = statement.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                Requirement {
                    name: row.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    requirement: row.get::<_, Option<String>>(2)?.unwrap_or_default(),
                    item_type: row.get::<_, Option<String>>(3)?.unwrap_or_default(),
                },
            ))
        })?;
        for row in rows {
            let (job_id, requirement) = row?;
            requirements.entry(job_id).or_default().push(requirement);
        }
    }
    let mut out = Vec::with_capacity(leads.len());
    for mut lead in leads {
        let job_id = text(&lead, "job_id").to_owned();
        let enrichment = enrichments.get(&job_id).map(|row| {
            let (members, unresolved) = memberships.get(&job_id).cloned().unwrap_or_default();
            for (index, key) in [
                (1, "experience_level"),
                (2, "experience_status"),
                (3, "experience_reason"),
                (4, "min_required_years"),
                (5, "max_required_years"),
                (6, "preferred_min_years"),
                (7, "role_status"),
                (8, "primary_function_id"),
            ] {
                lead.insert(key.to_owned(), row[index].clone());
            }
            let result = object_column(&row[9]);
            let requirements_value = match result.get("requirements") {
                Some(value) if truthy(value) => value.clone(),
                _ => json!({"extraction_status": row[10].clone(), "items": []}),
            };
            lead.insert("enrichment".to_owned(), Value::Object(result));
            lead.insert("requirements".to_owned(), requirements_value);
            lead.insert("role_memberships".to_owned(), json!(members.clone()));
            let as_text = |index: usize| row[index].as_str().map(str::to_owned);
            Enrichment {
                experience_level: as_text(1),
                experience_status: as_text(2),
                role_status: as_text(7),
                requirements_status: as_text(10),
                sponsorship: as_text(11),
                employment_type: as_text(12),
                member_roles: members,
                unresolved_roles: unresolved,
            }
        });
        out.push(LeadRecord {
            requirements: requirements.remove(&job_id).unwrap_or_default(),
            lead,
            enrichment,
        });
    }
    Ok(out)
}

fn session_members(connection: &Connection, session: &str) -> Result<HashSet<String>, LeadError> {
    let mut known = false;
    let mut members = HashSet::new();
    if table_exists(connection, "discovery_save_sessions")? {
        let found: Option<i64> = connection
            .query_row(
                "SELECT 1 FROM discovery_save_sessions WHERE session_id = ?1",
                [session],
                |row| row.get(0),
            )
            .optional()?;
        if found.is_some() {
            known = true;
            let mut statement = connection.prepare(
                "SELECT local_job_id FROM discovery_save_memberships WHERE session_id = ?1",
            )?;
            for id in statement.query_map([session], |row| row.get::<_, String>(0))? {
                members.insert(id?);
            }
        }
    }
    if table_exists(connection, "scraper_import_sessions")? {
        let found: Option<i64> = connection
            .query_row(
                "SELECT 1 FROM scraper_import_sessions WHERE session_id = ?1",
                [session],
                |row| row.get(0),
            )
            .optional()?;
        if found.is_some() {
            known = true;
            let mut statement = connection.prepare(
                "SELECT local_job_id FROM scraper_import_memberships WHERE session_id = ?1 \
                 AND outcome IN ('inserted', 'updated', 'unchanged')",
            )?;
            for id in statement.query_map([session], |row| row.get::<_, String>(0))? {
                members.insert(id?);
            }
        }
    }
    if !known {
        return Err(LeadError::UnknownSession);
    }
    Ok(members)
}

fn interviewed_jobs(connection: &Connection) -> Result<HashSet<String>, StoreError> {
    let mut statement = connection.prepare(
        "SELECT DISTINCT job_id FROM events WHERE action = 'status_changed=interviewing'",
    )?;
    let rows = statement.query_map([], |row| row.get::<_, Option<String>>(0))?;
    let mut out = HashSet::new();
    for row in rows {
        if let Some(job_id) = row? {
            out.insert(job_id);
        }
    }
    Ok(out)
}

/// Every identity of a lead that is about to be deleted.
fn deletion_identities(
    connection: &Connection,
    job_id: &str,
    url: Option<&str>,
    meta: Option<&str>,
) -> Result<Vec<(String, String)>, StoreError> {
    let mut identities: Vec<(String, String)> = Vec::new();
    let mut push = |kind: &str, value: String| {
        if !value.is_empty() && !identities.iter().any(|(k, v)| k == kind && *v == value) {
            identities.push((kind.to_owned(), value));
        }
    };
    push("canonical", canonical_identity(job_id));
    push("url", normalize_source_url(url.unwrap_or("")));
    if table_exists(connection, "lead_identity_aliases")? {
        let mut statement = connection
            .prepare("SELECT kind, identity FROM lead_identity_aliases WHERE job_id = ?1")?;
        let rows = statement.query_map([job_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        for row in rows {
            let (kind, identity) = row?;
            push(&kind, identity);
        }
    }
    let meta = object_column(&Value::String(meta.unwrap_or("").to_owned()));
    for key in ["corpus_job_id", "scraper_job_id"] {
        if let Some(value) = meta.get(key).and_then(Value::as_str) {
            push("canonical", canonical_identity(value));
        }
    }
    if table_exists(connection, "lead_job_data")? {
        let row: Option<(Option<String>, Option<String>)> = connection
            .query_row(
                "SELECT scraper_job_id, data_json FROM lead_job_data WHERE job_id = ?1",
                [job_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        if let Some((scraper_id, data)) = row {
            push(
                "canonical",
                canonical_identity(&scraper_id.unwrap_or_default()),
            );
            if let Ok(Value::Object(job)) = serde_json::from_str::<Value>(&data.unwrap_or_default())
            {
                for (kind, identity) in identity_bundle(&job) {
                    push(&kind, identity);
                }
            }
        }
    }
    Ok(identities)
}
