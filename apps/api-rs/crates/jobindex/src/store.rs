//! SQLite storage for the job index (`job_index.sqlite3`).
//!
//! Freshness without invented dates: a job first seen after its company's first import is new,
//! so `first_seen_at` stands in for its posting time (`baseline = 0`); jobs in a company's very
//! first import (`baseline = 1`) only count as recent when the company published a date.

use std::collections::HashSet;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use agent_hire_store::python_json_dumps;
use rusqlite::types::ValueRef;
use rusqlite::{Connection, OptionalExtension, Row, params, params_from_iter};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::datetime::DateTime;
use crate::{geo, rules, skills};

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS jobs(
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    company_slug TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    apply_url TEXT,
    locations TEXT,
    location_text TEXT,
    country_codes TEXT NOT NULL DEFAULT '',
    workplace_type TEXT,
    employment_type TEXT,
    department TEXT,
    salary TEXT,
    description TEXT,
    date_posted TEXT,
    posted_source TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    baseline INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    skills TEXT NOT NULL DEFAULT '[]',
    rule_software INTEGER NOT NULL DEFAULT 0,
    rule_level TEXT,
    rules_version INTEGER NOT NULL DEFAULT 0,
    min_years INTEGER,
    content_hash TEXT,
    lead_id TEXT
);
CREATE INDEX IF NOT EXISTS jobs_first_seen ON jobs(first_seen_at);
CREATE INDEX IF NOT EXISTS jobs_posted ON jobs(date_posted);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company_slug, active);
CREATE INDEX IF NOT EXISTS jobs_software ON jobs(rule_software, active);

CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    title, skills, content='jobs', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
    INSERT INTO jobs_fts(rowid, title, skills) VALUES (new.rowid, new.title, new.skills);
END;
CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, title, skills) VALUES ('delete', old.rowid, old.title, old.skills);
END;
CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE OF title, skills ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, title, skills) VALUES ('delete', old.rowid, old.title, old.skills);
    INSERT INTO jobs_fts(rowid, title, skills) VALUES (new.rowid, new.title, new.skills);
END;

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS decisions(
    job_id TEXT NOT NULL,
    role_key TEXT NOT NULL,
    role_match INTEGER NOT NULL,
    role_p REAL,
    level TEXT,
    source TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY(job_id, role_key)
);

CREATE TABLE IF NOT EXISTS companies(
    slug TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    first_import_at TEXT NOT NULL,
    last_import_at TEXT NOT NULL,
    last_status TEXT,
    last_run_id TEXT,
    active_jobs INTEGER NOT NULL DEFAULT 0,
    last_duration_seconds REAL
);

CREATE TABLE IF NOT EXISTS runs(
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    companies TEXT NOT NULL,
    imported TEXT NOT NULL DEFAULT '[]',
    new_jobs INTEGER NOT NULL DEFAULT 0,
    seen_jobs INTEGER NOT NULL DEFAULT 0,
    pid INTEGER,
    output_dir TEXT,
    error TEXT
);
"#;

/// Bump when the rules or skills change what they derive (shared with the Python backend).
pub const RULES_VERSION: i64 = 5;
const GEO_VERSION: &str = "1";

/// Columns search reads; all of them live in the `jobs_card` covering index.
const CARD_COLUMNS: [&str; 18] = [
    "rule_software",
    "id",
    "company",
    "company_slug",
    "title",
    "location_text",
    "country_codes",
    "workplace_type",
    "skills",
    "rule_level",
    "min_years",
    "date_posted",
    "posted_source",
    "first_seen_at",
    "baseline",
    "url",
    "apply_url",
    "lead_id",
];

const UPSERT_COLUMNS: [&str; 20] = [
    "company",
    "title",
    "url",
    "apply_url",
    "locations",
    "location_text",
    "country_codes",
    "workplace_type",
    "employment_type",
    "department",
    "salary",
    "description",
    "date_posted",
    "posted_source",
    "skills",
    "rule_software",
    "rule_level",
    "min_years",
    "content_hash",
    "rules_version",
];

const NO_WAL: [&str; 9] = [
    "fuse", "ntfs", "cifs", "smb", "nfs", "9p", "vboxsf", "sshfs", "davfs",
];

static INITIALIZED: LazyLock<Mutex<HashSet<PathBuf>>> =
    LazyLock::new(|| Mutex::new(HashSet::new()));

/// `datetime.now(utc).isoformat(timespec="seconds")`.
pub fn now_iso() -> String {
    DateTime::now_utc().isoformat_seconds()
}

/// The posting time a job is judged by: the company's date, else first-seen for non-baseline jobs.
pub fn posted_expr(alias: &str) -> String {
    let a = if alias.is_empty() {
        String::new()
    } else {
        format!("{alias}.")
    };
    format!("COALESCE({a}date_posted, CASE WHEN {a}baseline=0 THEN {a}first_seen_at END)")
}

/// The index folder: `AGENT_HIRE_JOBS_DIR`, else `jobs/` next to the app database.
pub fn jobs_dir(app_database: &Path) -> PathBuf {
    let path = match env::var("AGENT_HIRE_JOBS_DIR")
        .ok()
        .map(|dir| dir.trim().to_owned())
        .filter(|dir| !dir.is_empty())
    {
        Some(dir) => expand_home(&dir),
        None => app_database
            .parent()
            .map_or_else(|| PathBuf::from("jobs"), |dir| dir.join("jobs")),
    };
    let _ = fs::create_dir_all(&path);
    path
}

fn expand_home(path: &str) -> PathBuf {
    match path.strip_prefix("~/") {
        Some(rest) => env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_default()
            .join(rest),
        None => PathBuf::from(path),
    }
}

/// Whether WAL is safe here: local disks yes; FUSE and network mounts no.
fn wal_ok(target: &Path) -> bool {
    match env::var("AGENT_HIRE_SQLITE_WAL").as_deref().map(str::trim) {
        Ok("0") => return false,
        Ok("1") => return true,
        _ => {}
    }
    let Ok(mounts) = fs::read_to_string("/proc/mounts") else {
        return true;
    };
    let path = fs::canonicalize(target.parent().unwrap_or(target))
        .unwrap_or_else(|_| target.to_path_buf());
    let path = path.join(target.file_name().unwrap_or_default());
    let path = path.to_string_lossy();
    let mut best = String::new();
    let mut fstype = String::new();
    for line in mounts.lines() {
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() > 2 {
            let mount = parts[1];
            let inside =
                path == mount || path.starts_with(&format!("{}/", mount.trim_end_matches('/')));
            if inside && mount.len() > best.len() {
                best = mount.to_owned();
                fstype = parts[2].to_owned();
            }
        }
    }
    !NO_WAL.iter().any(|prefix| fstype.starts_with(prefix))
}

/// The job index database.
#[derive(Clone, Debug)]
pub struct JobIndex {
    path: PathBuf,
}

impl JobIndex {
    /// The index in `dir` (`job_index.sqlite3`).
    pub fn new(dir: &Path) -> JobIndex {
        JobIndex {
            path: dir.join("job_index.sqlite3"),
        }
    }

    /// The folder the index lives in.
    pub fn dir(&self) -> &Path {
        self.path.parent().unwrap_or(Path::new("."))
    }

    /// A connection, creating or upgrading the schema on first use.
    pub fn connect(&self) -> rusqlite::Result<Connection> {
        let connection = Connection::open(&self.path)?;
        connection.busy_timeout(Duration::from_secs(30))?;
        let first = INITIALIZED
            .lock()
            .map(|done| !done.contains(&self.path))
            .unwrap_or(true);
        if first {
            let mode = if wal_ok(&self.path) { "WAL" } else { "DELETE" };
            connection.query_row(&format!("PRAGMA journal_mode={mode}"), [], |_| Ok(()))?;
        }
        connection.execute_batch("PRAGMA synchronous=NORMAL")?;
        if first {
            self.initialize(&connection)?;
            if let Ok(mut done) = INITIALIZED.lock() {
                done.insert(self.path.clone());
            }
        }
        Ok(connection)
    }

    fn initialize(&self, connection: &Connection) -> rusqlite::Result<()> {
        connection.execute_batch(SCHEMA)?;
        let columns = table_columns(connection, "jobs")?;
        if !columns.contains("rules_version") {
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN rules_version INTEGER NOT NULL DEFAULT 0",
                [],
            )?;
        }
        if !table_columns(connection, "companies")?.contains("last_duration_seconds") {
            connection.execute(
                "ALTER TABLE companies ADD COLUMN last_duration_seconds REAL",
                [],
            )?;
        }
        if !columns.contains("country_codes") {
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN country_codes TEXT NOT NULL DEFAULT ''",
                [],
            )?;
        }
        backfill_countries(connection)?;
        let card = format!("active, {}, {}", posted_expr(""), CARD_COLUMNS.join(", "));
        let stored: Option<String> = connection
            .query_row("SELECT value FROM meta WHERE key='jobs_card'", [], |row| {
                row.get(0)
            })
            .optional()?;
        if stored.as_deref() != Some(card.as_str()) {
            if stored.is_some() {
                connection.execute("DROP INDEX IF EXISTS jobs_card", [])?;
            }
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('jobs_card', ?1)",
                [&card],
            )?;
        }
        connection.execute(
            &format!("CREATE INDEX IF NOT EXISTS jobs_card ON jobs({card})"),
            [],
        )?;
        Ok(())
    }
}

fn table_columns(connection: &Connection, table: &str) -> rusqlite::Result<HashSet<String>> {
    let mut statement = connection.prepare(&format!("PRAGMA table_info({table})"))?;
    let names = statement.query_map([], |row| row.get::<_, String>(1))?;
    names.collect()
}

fn backfill_countries(connection: &Connection) -> rusqlite::Result<()> {
    let done: Option<String> = connection
        .query_row(
            "SELECT value FROM meta WHERE key='geo_version'",
            [],
            |row| row.get(0),
        )
        .optional()?;
    if done.as_deref() == Some(GEO_VERSION) {
        return Ok(());
    }
    let rows: Vec<(i64, Option<String>, Option<String>)> = {
        let mut statement =
            connection.prepare("SELECT rowid, location_text, locations FROM jobs")?;
        let mapped = statement.query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?;
        mapped.collect::<rusqlite::Result<_>>()?
    };
    for (rowid, text, locations) in rows {
        let codes = geo::codes_column(text.as_deref().unwrap_or(""), locations.as_deref());
        connection.execute(
            "UPDATE jobs SET country_codes=?1 WHERE rowid=?2",
            params![codes, rowid],
        )?;
    }
    connection.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('geo_version', ?1)",
        [GEO_VERSION],
    )?;
    Ok(())
}

/// A SQLite value as JSON.
pub fn sql_json(value: ValueRef<'_>) -> Value {
    match value {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(number) => json!(number),
        ValueRef::Real(number) => json!(number),
        ValueRef::Text(bytes) | ValueRef::Blob(bytes) => {
            Value::String(String::from_utf8_lossy(bytes).into_owned())
        }
    }
}

/// A row as a JSON object with its columns in order.
pub fn row_object(row: &Row<'_>) -> rusqlite::Result<Map<String, Value>> {
    let statement = row.as_ref();
    let mut out = Map::new();
    for index in 0..statement.column_count() {
        let name = statement.column_name(index)?.to_owned();
        out.insert(name, sql_json(row.get_ref(index)?));
    }
    Ok(out)
}

/// `json.dumps(..., sort_keys=True, default=str)`.
fn sorted_dumps(value: &Value) -> String {
    fn sort(value: &Value) -> Value {
        match value {
            Value::Object(map) => {
                let mut keys: Vec<&String> = map.keys().collect();
                keys.sort();
                Value::Object(
                    keys.into_iter()
                        .map(|key| (key.clone(), sort(&map[key])))
                        .collect(),
                )
            }
            Value::Array(items) => Value::Array(items.iter().map(sort).collect()),
            other => other.clone(),
        }
    }
    python_json_dumps(&sort(value), true)
}

fn hash_record(record: &Map<String, Value>) -> String {
    let keys = [
        "title",
        "description_text",
        "locations",
        "apply_url",
        "url",
        "date_posted",
    ];
    let values = Value::Array(
        keys.iter()
            .map(|key| record.get(*key).cloned().unwrap_or(Value::Null))
            .collect(),
    );
    hex(&Sha256::digest(sorted_dumps(&values).as_bytes()))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Python's `str()` of a value for a text column (lists joined, objects as JSON).
fn as_text(value: Option<&Value>) -> Option<String> {
    match value? {
        Value::Null => None,
        Value::String(text) => Some(text.clone()),
        Value::Array(items) => Some(
            items
                .iter()
                .filter(|item| {
                    !matches!(item, Value::Null | Value::Bool(false)) && item.as_str() != Some("")
                })
                .map(|item| match item {
                    Value::String(text) => text.clone(),
                    other => other.to_string(),
                })
                .collect::<Vec<_>>()
                .join(", "),
        ),
        Value::Object(_) => Some(python_json_dumps(value?, true)),
        Value::Bool(flag) => Some(if *flag { "True" } else { "False" }.to_owned()),
        Value::Number(number) => Some(number.to_string()),
    }
}

const LOCATION_KEYS: [&str; 11] = [
    "text",
    "name",
    "label",
    "address",
    "city",
    "cityState",
    "state",
    "region",
    "office",
    "country",
    "location",
];

fn location_item_text(item: &Value) -> String {
    match item {
        Value::String(text) => text.trim().to_owned(),
        Value::Object(map) => {
            let mut parts: Vec<String> = Vec::new();
            for key in LOCATION_KEYS {
                if let Some(text) = map
                    .get(key)
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|text| !text.is_empty())
                    && !parts.iter().any(|part| part == text)
                {
                    parts.push(text.to_owned());
                }
                if matches!(key, "text" | "name" | "label" | "address") && !parts.is_empty() {
                    break; // one full display string is enough
                }
            }
            parts.join(", ")
        }
        Value::Array(items) => items
            .iter()
            .map(location_item_text)
            .filter(|text| !text.is_empty())
            .collect::<Vec<_>>()
            .join(", "),
        _ => String::new(),
    }
}

/// A display string for a job's locations (sites publish strings or objects).
pub fn locations_text(locations: &Value) -> String {
    let items: Vec<Value> = match locations {
        Value::String(_) | Value::Object(_) => vec![locations.clone()],
        Value::Array(items) => items.clone(),
        _ => return String::new(),
    };
    let mut seen: Vec<String> = Vec::new();
    for item in &items {
        let text = location_item_text(item);
        if !text.is_empty() && !seen.contains(&text) {
            seen.push(text);
        }
    }
    seen.join("; ")
}

/// The stored row for one scraped record.
pub fn row_from_record(record: &Map<String, Value>, slug: &str) -> Map<String, Value> {
    let field = |key: &str| -> String {
        match record.get(key) {
            Some(Value::String(text)) => text.trim().to_owned(),
            Some(Value::Null) | None => String::new(),
            Some(other) => other.to_string(),
        }
    };
    let title = field("title");
    let description = field("description_text");
    let derived = RuleFields::new(&title, &description);
    let fetched = record.get("fetched_at").and_then(Value::as_str);
    let (posted, posted_source) = rules::parse_posted(
        record.get("date_posted").unwrap_or(&Value::Null),
        record.get("date_posted_raw").unwrap_or(&Value::Null),
        fetched,
    );
    let locations = match record.get("locations") {
        Some(Value::String(text)) => json!([text]),
        Some(value) if truthy(value) => value.clone(),
        _ => json!([]),
    };
    let locations_json = python_json_dumps(&locations, true);
    let location_text = locations_text(&locations);
    let identity = record
        .get("record_identity")
        .filter(|v| truthy(v))
        .or_else(|| record.get("record_id"));
    let id = match identity {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Null) | None => "None".to_owned(),
        Some(other) => other.to_string(),
    };
    let company = record
        .get("company")
        .filter(|v| truthy(v))
        .and_then(Value::as_str)
        .unwrap_or(slug)
        .to_owned();
    let department = as_text(
        record
            .get("department")
            .filter(|v| truthy(v))
            .or_else(|| record.get("team")),
    );
    let mut row = Map::new();
    row.insert("id".to_owned(), json!(id));
    row.insert("company".to_owned(), json!(company));
    row.insert("company_slug".to_owned(), json!(slug));
    row.insert("title".to_owned(), json!(title));
    row.insert(
        "url".to_owned(),
        record.get("url").cloned().unwrap_or(Value::Null),
    );
    row.insert(
        "apply_url".to_owned(),
        record.get("apply_url").cloned().unwrap_or(Value::Null),
    );
    row.insert(
        "country_codes".to_owned(),
        json!(geo::codes_column(&location_text, Some(&locations_json))),
    );
    row.insert("locations".to_owned(), json!(locations_json));
    row.insert("location_text".to_owned(), json!(location_text));
    row.insert(
        "workplace_type".to_owned(),
        json!(as_text(record.get("workplace_type"))),
    );
    row.insert(
        "employment_type".to_owned(),
        json!(as_text(record.get("employment_type"))),
    );
    row.insert("department".to_owned(), json!(department));
    row.insert("salary".to_owned(), json!(as_text(record.get("salary"))));
    row.insert("description".to_owned(), json!(description));
    row.insert("date_posted".to_owned(), json!(posted));
    row.insert("posted_source".to_owned(), json!(posted_source));
    row.insert("skills".to_owned(), json!(derived.skills));
    row.insert("rule_software".to_owned(), json!(derived.software));
    row.insert("rule_level".to_owned(), json!(derived.level));
    row.insert("min_years".to_owned(), json!(derived.years));
    row.insert("content_hash".to_owned(), json!(hash_record(record)));
    row.insert("rules_version".to_owned(), json!(RULES_VERSION));
    row
}

struct RuleFields {
    skills: String,
    software: i64,
    level: Option<String>,
    years: Option<i64>,
}

impl RuleFields {
    fn new(title: &str, description: &str) -> Self {
        let found = skills::extract_skills(&format!("{title}\n{description}"));
        let (level, years) = rules::rule_level(title, description);
        let software = i64::from(rules::rule_is_software(
            title,
            &found,
            skills::software_skills(),
        ));
        let skills = python_json_dumps(&json!(found), true);
        Self {
            skills,
            software,
            level,
            years,
        }
    }
}

struct StaleRules {
    rowid: i64,
    title: String,
    description: Option<String>,
    locations: Option<String>,
    content_hash: Option<String>,
}

/// Recomputes a bounded batch of stale rules with the same derivation used by scrape imports.
pub fn recompute_rules(connection: &mut Connection, limit: i64) -> rusqlite::Result<usize> {
    let rows: Vec<StaleRules> = {
        let mut statement = connection.prepare(
            "SELECT rowid,title,description,locations,content_hash FROM jobs WHERE rules_version < ?1 LIMIT ?2"
        )?;
        statement
            .query_map(params![RULES_VERSION, limit], |row| {
                Ok(StaleRules {
                    rowid: row.get(0)?,
                    title: row.get(1)?,
                    description: row.get(2)?,
                    locations: row.get(3)?,
                    content_hash: row.get(4)?,
                })
            })?
            .collect::<rusqlite::Result<_>>()?
    };
    if rows.is_empty() {
        return Ok(0);
    }
    let updates = rows
        .into_iter()
        .map(|row| {
            let rules = RuleFields::new(&row.title, row.description.as_deref().unwrap_or_default());
            let location = row
                .locations
                .as_deref()
                .and_then(|value| serde_json::from_str(value).ok())
                .map_or_else(String::new, |value| locations_text(&value));
            let countries = geo::codes_column(&location, row.locations.as_deref());
            (row, rules, location, countries)
        })
        .collect::<Vec<_>>();
    let transaction = connection.transaction()?;
    let mut count = 0;
    {
        let mut statement = transaction.prepare_cached(
            "UPDATE jobs SET skills=?1,rule_software=?2,rule_level=?3,min_years=?4,location_text=?5,
             country_codes=?6,rules_version=?7 WHERE rowid=?8 AND rules_version < ?7
             AND content_hash IS ?9 AND title=?10 AND description IS ?11 AND locations IS ?12"
        )?;
        for (row, rules, location, countries) in updates {
            count += statement.execute(params![
                rules.skills,
                rules.software,
                rules.level,
                rules.years,
                location,
                countries,
                RULES_VERSION,
                row.rowid,
                row.content_hash,
                row.title,
                row.description,
                row.locations,
            ])?;
        }
    }
    transaction.commit()?;
    Ok(count)
}

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

fn sql_value(value: &Value) -> rusqlite::types::Value {
    match value {
        Value::Null => rusqlite::types::Value::Null,
        Value::Bool(flag) => rusqlite::types::Value::Integer(i64::from(*flag)),
        Value::Number(number) => match number.as_i64() {
            Some(n) => rusqlite::types::Value::Integer(n),
            None => rusqlite::types::Value::Real(number.as_f64().unwrap_or(0.0)),
        },
        Value::String(text) => rusqlite::types::Value::Text(text.clone()),
        other => rusqlite::types::Value::Text(other.to_string()),
    }
}

/// Counts from importing one company.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ImportCounts {
    /// Records seen.
    pub seen: usize,
    /// Genuinely new jobs (not counted on a company's first import).
    pub new: usize,
    /// Jobs whose content changed.
    pub updated: usize,
    /// Jobs closed because a complete scrape no longer listed them.
    pub closed: usize,
}

/// Upserts one company's scraped records. A complete scrape closes jobs it did not see; a
/// partial one never closes anything.
pub fn import_company(
    connection: &Connection,
    slug: &str,
    company: &str,
    records: impl Iterator<Item = Map<String, Value>>,
    run_id: &str,
    complete: bool,
    duration_seconds: Option<f64>,
) -> rusqlite::Result<ImportCounts> {
    let stamp = now_iso();
    let known: Option<i64> = connection
        .query_row("SELECT 1 FROM companies WHERE slug=?1", [slug], |row| {
            row.get(0)
        })
        .optional()?;
    let baseline = known.is_none();
    let mut counts = ImportCounts::default();
    let mut seen_ids: Vec<String> = Vec::new();
    let placeholders: Vec<String> = (0..UPSERT_COLUMNS.len())
        .map(|i| format!("?{}", i + 6))
        .collect();
    let insert = format!(
        "INSERT INTO jobs(id, company_slug, first_seen_at, last_seen_at, baseline, active, {}) VALUES(?1,?2,?3,?4,?5,1,{})",
        UPSERT_COLUMNS.join(", "),
        placeholders.join(",")
    );
    let assignments: Vec<String> = UPSERT_COLUMNS
        .iter()
        .enumerate()
        .map(|(i, c)| format!("{c}=?{}", i + 1))
        .collect();
    let update = format!(
        "UPDATE jobs SET {}, last_seen_at=?{}, active=1 WHERE id=?{}",
        assignments.join(", "),
        UPSERT_COLUMNS.len() + 1,
        UPSERT_COLUMNS.len() + 2
    );
    for record in records {
        if !record.get("title").is_some_and(truthy) {
            continue;
        }
        let row = row_from_record(&record, slug);
        let id = row["id"].as_str().unwrap_or("").to_owned();
        if id.is_empty() || id == "None" {
            continue;
        }
        seen_ids.push(id.clone());
        let existing: Option<Option<String>> = connection
            .query_row("SELECT content_hash FROM jobs WHERE id=?1", [&id], |row| {
                row.get(0)
            })
            .optional()?;
        let values: Vec<rusqlite::types::Value> = UPSERT_COLUMNS
            .iter()
            .map(|column| sql_value(&row[*column]))
            .collect();
        match existing {
            None => {
                let fetched = record
                    .get("fetched_at")
                    .and_then(Value::as_str)
                    .filter(|text| !text.is_empty());
                let first_seen = if baseline {
                    fetched.unwrap_or(&stamp).to_owned()
                } else {
                    stamp.clone()
                };
                let mut all: Vec<rusqlite::types::Value> = vec![
                    id.clone().into(),
                    slug.to_owned().into(),
                    first_seen.into(),
                    stamp.clone().into(),
                    i64::from(baseline).into(),
                ];
                all.extend(values);
                connection.execute(&insert, params_from_iter(all))?;
                if !baseline {
                    counts.new += 1;
                }
            }
            Some(hash) if hash.as_deref() != row["content_hash"].as_str() => {
                let mut all = values;
                all.push(stamp.clone().into());
                all.push(id.clone().into());
                connection.execute(&update, params_from_iter(all))?;
                connection.execute("DELETE FROM decisions WHERE job_id=?1", [&id])?;
                counts.updated += 1;
            }
            Some(_) => {
                connection.execute(
                    "UPDATE jobs SET last_seen_at=?1, active=1 WHERE id=?2",
                    params![stamp, id],
                )?;
            }
        }
    }
    if complete && !seen_ids.is_empty() {
        connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _seen(id TEXT PRIMARY KEY)",
            [],
        )?;
        connection.execute("DELETE FROM _seen", [])?;
        for id in &seen_ids {
            connection.execute("INSERT OR IGNORE INTO _seen(id) VALUES(?1)", [id])?;
        }
        counts.closed = connection.execute(
            "UPDATE jobs SET active=0 WHERE company_slug=?1 AND active=1 AND id NOT IN (SELECT id FROM _seen)",
            [slug],
        )?;
    }
    counts.seen = seen_ids.len();
    let active: i64 = connection.query_row(
        "SELECT COUNT(*) FROM jobs WHERE company_slug=?1 AND active=1",
        [slug],
        |row| row.get(0),
    )?;
    connection.execute(
        "INSERT INTO companies(slug, company, first_import_at, last_import_at, last_status, last_run_id, active_jobs, \
         last_duration_seconds) VALUES(?1,?2,?3,?4,?5,?6,?7,?8) ON CONFLICT(slug) DO UPDATE SET company=excluded.company, \
         last_import_at=excluded.last_import_at, last_status=excluded.last_status, last_run_id=excluded.last_run_id, \
         active_jobs=excluded.active_jobs, \
         last_duration_seconds=COALESCE(excluded.last_duration_seconds, companies.last_duration_seconds)",
        params![slug, company, stamp, stamp, if complete { "complete" } else { "partial" }, run_id, active, duration_seconds],
    )?;
    Ok(counts)
}

/// A job added by hand; returns its reference, `custom-<20 hex>`.
pub fn add_custom_job(
    connection: &Connection,
    title: &str,
    company: &str,
    description: &str,
    url: &str,
    location: &str,
    workplace_type: &str,
) -> rusqlite::Result<String> {
    let head: String = description.chars().take(200).collect();
    let digest = hex(&Sha256::digest(
        format!("{company}|{title}|{url}|{head}").as_bytes(),
    ));
    let ident = format!("custom-{}", &digest[..20]);
    let optional = |text: &str| {
        if text.is_empty() {
            Value::Null
        } else {
            json!(text)
        }
    };
    let mut record = Map::new();
    record.insert("record_identity".to_owned(), json!(ident));
    record.insert("title".to_owned(), json!(title));
    record.insert("company".to_owned(), json!(company));
    record.insert("description_text".to_owned(), json!(description));
    record.insert("url".to_owned(), optional(url));
    record.insert("apply_url".to_owned(), optional(url));
    record.insert(
        "locations".to_owned(),
        if location.is_empty() {
            json!([])
        } else {
            json!([location])
        },
    );
    record.insert("workplace_type".to_owned(), optional(workplace_type));
    record.insert("date_posted".to_owned(), Value::Null);
    record.insert("fetched_at".to_owned(), json!(now_iso()));
    let row = row_from_record(&record, "custom");
    let stamp = now_iso();
    let placeholders: Vec<String> = (0..UPSERT_COLUMNS.len())
        .map(|i| format!("?{}", i + 4))
        .collect();
    let updates: Vec<String> = UPSERT_COLUMNS
        .iter()
        .map(|c| format!("{c}=excluded.{c}"))
        .collect();
    let sql = format!(
        "INSERT INTO jobs(id, company_slug, first_seen_at, last_seen_at, baseline, active, {}) VALUES(?1,'custom',?2,?3,0,1,{}) \
         ON CONFLICT(id) DO UPDATE SET {}, last_seen_at=excluded.last_seen_at, active=1",
        UPSERT_COLUMNS.join(", "),
        placeholders.join(","),
        updates.join(", ")
    );
    let mut values: Vec<rusqlite::types::Value> =
        vec![ident.clone().into(), stamp.clone().into(), stamp.into()];
    values.extend(UPSERT_COLUMNS.iter().map(|column| sql_value(&row[*column])));
    connection.execute(&sql, params_from_iter(values))?;
    Ok(ident)
}

/// The pipeline (lead) id for an index job. Index ids are usually hashes, but some sources use
/// `company:uuid` or even path-like ids; lead ids end up in URLs and file names, so anything other
/// than 1-128 letters, digits, `_` or `-` is replaced by `job-<first 20 hex of its SHA-256>`.
pub fn lead_id_for(index_id: &str) -> String {
    let safe = (1..=128).contains(&index_id.len())
        && index_id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
    if safe {
        index_id.to_owned()
    } else {
        let digest = hex(&Sha256::digest(index_id.as_bytes()));
        format!("job-{}", &digest[..20])
    }
}

/// Deletes jobs (and their decisions); returns how many jobs went.
pub fn delete_jobs(connection: &Connection, refs: &[String]) -> rusqlite::Result<usize> {
    let mut removed = 0;
    for chunk in refs.chunks(500) {
        let marks = vec!["?"; chunk.len()].join(",");
        connection.execute(
            &format!("DELETE FROM decisions WHERE job_id IN ({marks})"),
            params_from_iter(chunk),
        )?;
        removed += connection.execute(
            &format!("DELETE FROM jobs WHERE id IN ({marks})"),
            params_from_iter(chunk),
        )?;
    }
    if removed > 0 {
        forget_stats();
    }
    Ok(removed)
}

/// Empties the index; returns how many jobs there were.
pub fn reset_index(connection: &Connection) -> rusqlite::Result<i64> {
    let jobs: i64 = connection.query_row("SELECT COUNT(*) FROM jobs", [], |row| row.get(0))?;
    connection.execute_batch(
        "DELETE FROM decisions; DELETE FROM jobs; INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild'); \
         DELETE FROM companies; DELETE FROM runs WHERE status != 'running';",
    )?;
    forget_stats();
    Ok(jobs)
}

/// One job with every column, or `None`.
pub fn get_job(
    connection: &Connection,
    job_id: &str,
) -> rusqlite::Result<Option<Map<String, Value>>> {
    connection
        .query_row("SELECT * FROM jobs WHERE id=?1", [job_id], row_object)
        .optional()
}

/// Cached decisions of one kind for the given jobs.
pub fn decisions_for(
    connection: &Connection,
    role_key: &str,
    job_ids: &[String],
) -> rusqlite::Result<Map<String, Value>> {
    let mut out = Map::new();
    for chunk in job_ids.chunks(900) {
        let marks = vec!["?"; chunk.len()].join(",");
        let mut statement = connection.prepare(&format!(
            "SELECT * FROM decisions WHERE role_key=? AND job_id IN ({marks})"
        ))?;
        let mut values: Vec<&str> = vec![role_key];
        values.extend(chunk.iter().map(String::as_str));
        let rows = statement.query_map(params_from_iter(values), row_object)?;
        for row in rows {
            let row = row?;
            if let Some(id) = row.get("job_id").and_then(Value::as_str).map(str::to_owned) {
                out.insert(id, Value::Object(row));
            }
        }
    }
    Ok(out)
}

static STATS_CACHE: LazyLock<Mutex<Option<(PathBuf, Instant, Value)>>> =
    LazyLock::new(|| Mutex::new(None));

/// Index counts, reused for 30 s (they count every job).
pub fn stats_cached(index: &JobIndex, connection: &Connection) -> rusqlite::Result<Value> {
    if let Ok(cache) = STATS_CACHE.lock()
        && let Some((path, at, value)) = cache.as_ref()
        && *path == index.path
        && at.elapsed() < Duration::from_secs(30)
    {
        return Ok(value.clone());
    }
    let value = stats(connection)?;
    if let Ok(mut cache) = STATS_CACHE.lock() {
        *cache = Some((index.path.clone(), Instant::now(), value.clone()));
    }
    Ok(value)
}

/// Drops the cached index counts so the next status read sees deletions at once.
fn forget_stats() {
    if let Ok(mut cache) = STATS_CACHE.lock() {
        *cache = None;
    }
}

/// Index counts.
pub fn stats(connection: &Connection) -> rusqlite::Result<Value> {
    let (total, active): (i64, Option<i64>) =
        connection.query_row("SELECT COUNT(*), SUM(active) FROM jobs", [], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })?;
    let cutoff = DateTime::now_utc()
        .minus_seconds(24 * 3600)
        .isoformat_seconds();
    let recent: i64 = connection.query_row(
        &format!(
            "SELECT COUNT(*) FROM jobs WHERE active=1 AND {} >= ?1",
            posted_expr("")
        ),
        [&cutoff],
        |row| row.get(0),
    )?;
    let dated: i64 = connection.query_row(
        "SELECT COUNT(*) FROM jobs WHERE active=1 AND date_posted IS NOT NULL",
        [],
        |row| row.get(0),
    )?;
    let (companies, last_import): (i64, Option<String>) = connection.query_row(
        "SELECT COUNT(*), MAX(last_import_at) FROM companies",
        [],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )?;
    let software: i64 = connection.query_row(
        "SELECT COUNT(*) FROM jobs WHERE active=1 AND rule_software=1",
        [],
        |row| row.get(0),
    )?;
    Ok(json!({
        "jobs_total": total,
        "jobs_active": active.unwrap_or(0),
        "jobs_software": software,
        "jobs_last_24h": recent,
        "jobs_with_published_date": dated,
        "companies_indexed": companies,
        "last_import_at": last_import,
    }))
}

#[cfg(test)]
mod tests {
    #[test]
    fn lead_ids_are_safe_for_urls_and_file_names() {
        assert_eq!(super::lead_id_for("00ab-cd_9"), "00ab-cd_9");
        let derived = super::lead_id_for("atlan:20fe5046-b287-40f6-990f-842ad75526e5");
        assert!(
            derived.starts_with("job-") && derived.len() == 24,
            "got {derived}"
        );
        assert_eq!(
            derived,
            super::lead_id_for("atlan:20fe5046-b287-40f6-990f-842ad75526e5")
        );
        assert_ne!(
            derived,
            super::lead_id_for("fractal:/job/AWS-Data-Engineer_SR-15059")
        );
        assert!(super::lead_id_for("").starts_with("job-"));
    }

    use super::*;

    #[test]
    fn custom_jobs_have_stable_references() {
        let dir = std::env::temp_dir().join(format!("agent-hire-jobindex-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("temp dir");
        let index = JobIndex::new(&dir);
        let connection = index.connect().expect("connects");
        let id = add_custom_job(
            &connection,
            "Senior Backend Engineer, Fleet Platform",
            "Nimbus Robotics",
            "Build event-driven services in Go with Kafka and PostgreSQL. 5+ years of backend experience. Own on-call and SLOs.",
            "http://127.0.0.1:8099/jobs/nr-1042/",
            "Hyderabad, Telangana, India",
            "hybrid",
        )
        .expect("adds");
        // The same reference the Python backend gives this job (contract golden).
        assert_eq!(id, "custom-33424b74785e25ccf34a");
        let job = get_job(&connection, &id).expect("reads").expect("exists");
        assert_eq!(job["rule_level"], json!("senior"));
        assert_eq!(job["country_codes"], json!(",IN,"));
        assert_eq!(stats(&connection).expect("stats")["jobs_active"], json!(1));
        let _ = fs::remove_dir_all(&dir);
    }
}
