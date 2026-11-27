//! Opening the app database and creating its schema.

use std::fmt;
use std::fs;
use std::fs::OpenOptions;
use std::path::{Path, PathBuf};
use std::time::Duration;

use rusqlite::Connection;

/// The schema of a fresh install, as the Python backend's migrations leave it.
const FRESH_SCHEMA: &str = include_str!("../schema/crm.sql");

/// The migrations that `FRESH_SCHEMA` corresponds to, recorded in `schema_migrations`.
const APPLIED_MIGRATIONS: [&str; 18] = [
    "001_initial.sql",
    "002_add_signal_columns.sql",
    "003_add_feedback_columns.sql",
    "004_resume_templates.sql",
    "005_documents_tags.sql",
    "006_point_tags.sql",
    "007_doc_selections.sql",
    "008_conflict_groups.sql",
    "009_misc_context.sql",
    "010_ingestion_tasks.sql",
    "011_staged_duplicates.sql",
    "012_migrate_opencode_custom_settings.sql",
    "013_resume_parse_provenance.sql",
    "014_discovery_persistence.sql",
    "015_scraper_api_import.sql",
    "016_user_feedback_and_saved_searches.sql",
    "017_feedback_search_indexes.sql",
    "018_job_attributes.sql",
];

/// A storage failure.
#[derive(Debug)]
pub enum StoreError {
    /// A database error.
    Sqlite(rusqlite::Error),
    /// A file-system error (creating the directory or the migration lock).
    Io(std::io::Error),
}

impl fmt::Display for StoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StoreError::Sqlite(error) => write!(formatter, "{error}"),
            StoreError::Io(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<rusqlite::Error> for StoreError {
    fn from(error: rusqlite::Error) -> StoreError {
        StoreError::Sqlite(error)
    }
}

impl From<std::io::Error> for StoreError {
    fn from(error: std::io::Error) -> StoreError {
        StoreError::Io(error)
    }
}

/// The app database at a fixed path. Each operation opens its own short-lived connection.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Database {
    path: PathBuf,
}

impl Database {
    /// Opens (creating if needed) the database at `path` and makes sure it has a schema.
    ///
    /// A database with no `schema_migrations` table gets the full fresh-install schema. A
    /// database that is already migrated is left as it is.
    pub fn open(path: &Path) -> Result<Database, StoreError> {
        if let Some(parent) = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            fs::create_dir_all(parent)?;
        }
        let database = Database {
            path: path.to_path_buf(),
        };
        database.ensure_schema()?;
        Ok(database)
    }

    /// The database file.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// A connection with the app's settings: foreign keys on, WAL journal, `NORMAL` sync and a
    /// 5 s busy timeout.
    pub fn connect(&self) -> Result<Connection, StoreError> {
        let connection = Connection::open(&self.path)?;
        connection.busy_timeout(Duration::from_millis(5000))?;
        connection.execute_batch(
            "PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL;",
        )?;
        Ok(connection)
    }

    fn ensure_schema(&self) -> Result<(), StoreError> {
        // The same lock file the Python backend takes while migrating, so two processes never
        // create the schema at once.
        let lock_path = format!("{}.migration.lock", self.path.display());
        let lock = OpenOptions::new()
            .create(true)
            .append(true)
            .open(lock_path)?;
        lock.lock()?;
        let result = self.create_schema_if_missing();
        let _ = lock.unlock();
        result
    }

    fn create_schema_if_missing(&self) -> Result<(), StoreError> {
        let mut connection = self.connect()?;
        let has_migrations: bool = connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations')",
            [],
            |row| row.get(0),
        )?;
        if has_migrations {
            return Ok(());
        }
        let transaction = connection.transaction()?;
        transaction.execute_batch(FRESH_SCHEMA)?;
        for name in APPLIED_MIGRATIONS {
            transaction.execute("INSERT INTO schema_migrations(name) VALUES (?1)", [name])?;
        }
        transaction.commit()?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_database_gets_the_schema_once() {
        let dir = std::env::temp_dir().join(format!("agent-hire-store-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let database = Database::open(&dir.join("crm.db")).expect("opens");
        // Opening again must not try to create the tables a second time.
        let database = Database::open(database.path()).expect("reopens");
        let connection = database.connect().expect("connects");
        let migrations: i64 = connection
            .query_row("SELECT COUNT(*) FROM schema_migrations", [], |row| {
                row.get(0)
            })
            .expect("counts");
        assert_eq!(migrations, 18);
        let leads_columns: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM pragma_table_info('leads')",
                [],
                |row| row.get(0),
            )
            .expect("counts");
        assert!(
            leads_columns >= 40,
            "leads has the migrated columns, got {leads_columns}"
        );
        let _ = fs::remove_dir_all(&dir);
    }
}
