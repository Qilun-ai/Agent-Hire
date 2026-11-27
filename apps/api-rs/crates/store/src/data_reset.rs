//! Resetting the standalone SQLite backend and its generated assets.

use std::ffi::OsString;
use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use rusqlite::{Connection, TransactionBehavior};
use serde_json::{Value, json};

use crate::database::Database;
use crate::profile;
use crate::settings;

const SIDECARS: [&str; 3] = ["", "-wal", "-shm"];

/// Clears the app data and reports each operation. A data-only reset preserves a database backup,
/// application settings outside the profile, and résumé templates.
pub fn reset(database: &Database, assets: &Path, clear_settings: bool) -> Value {
    let mut errors = Vec::new();
    let backup_removed = if clear_settings {
        remove_backups(database.path(), &mut errors)
    } else {
        0
    };
    let mut sqlite = Vec::new();
    let mut graph = Vec::new();
    match database.connect() {
        Ok(mut connection) => {
            match connection.transaction_with_behavior(TransactionBehavior::Immediate) {
                Ok(transaction) => {
                    // Copy the WAL beside the main file while no writer can change either.
                    if !clear_settings {
                        write_backups(database.path(), &mut errors);
                    }
                    match settings::read(&transaction, profile::SNAPSHOT) {
                        Ok(snapshot) => graph = graph_kinds(snapshot.as_deref()),
                        Err(error) => errors.push(format!("sqlite profile keys: {error}")),
                    }
                    sqlite = clear_tables(&transaction, clear_settings, &mut errors);
                    if let Err(error) = transaction.commit() {
                        sqlite.clear();
                        errors.push(format!("sqlite commit: {error}"));
                    }
                }
                Err(error) => errors.push(format!("sqlite begin: {error}")),
            }
        }
        Err(error) => errors.push(format!("sqlite list tables: {error}")),
    }
    let assets_removed = remove_assets(assets, &mut errors);
    let mut summary = json!({
        "sqlite_cleared": sqlite,
        "graph_cleared": graph,
        "vectors_dropped": [],
        "assets_removed": assets_removed,
        "settings_cleared": clear_settings,
        "errors": errors,
    });
    if clear_settings {
        summary["backup_removed"] = json!(backup_removed);
    }
    summary["scraper_untouched"] = json!(true);
    // The native backend has no legacy discovery configuration to reset.
    summary["search_stopped"] = json!(false);
    summary
}

fn graph_kinds(snapshot: Option<&str>) -> Vec<&'static str> {
    let snapshot: Value = snapshot
        .and_then(|snapshot| serde_json::from_str(snapshot).ok())
        .unwrap_or_default();
    let mut kinds = Vec::new();
    if ["n", "s"].iter().any(|key| {
        snapshot[key]
            .as_str()
            .is_some_and(|text| !text.trim().is_empty())
    }) {
        kinds.push("Candidate");
    }
    for (keys, kind) in [
        (&["skills"][..], "Skill"),
        (&["projects"][..], "Project"),
        (&["exp"][..], "Experience"),
        (&["certifications", "certs"][..], "Certification"),
        (&["education"][..], "Education"),
        (&["achievements", "awards"][..], "Achievement"),
    ] {
        if keys.iter().any(|key| {
            snapshot[key]
                .as_array()
                .is_some_and(|items| !items.is_empty())
        }) {
            kinds.push(kind);
        }
    }
    kinds
}

fn clear_tables(
    connection: &Connection,
    clear_settings: bool,
    errors: &mut Vec<String>,
) -> Vec<String> {
    let mut sqlite = Vec::new();
    if let Err(error) = connection.execute_batch("PRAGMA defer_foreign_keys = ON") {
        errors.push(format!("sqlite foreign keys: {error}"));
    }
    match tables(connection) {
        Ok(tables) => {
            for table in tables {
                if table.starts_with("sqlite_")
                    || table == "schema_migrations"
                    || (!clear_settings
                        && matches!(table.as_str(), "settings" | "resume_templates"))
                {
                    continue;
                }
                let quoted = table.replace('"', "\"\"");
                match connection.execute(&format!("DELETE FROM \"{quoted}\""), []) {
                    Ok(_) => sqlite.push(table),
                    Err(error) => errors.push(format!("sqlite {table}: {error}")),
                }
            }
        }
        Err(error) => errors.push(format!("sqlite list tables: {error}")),
    }
    if !clear_settings {
        match profile::clear_settings(connection) {
            Ok(()) => sqlite.push("settings:profile".to_owned()),
            Err(error) => errors.push(format!("sqlite profile keys: {error}")),
        }
    }
    sqlite
}

fn tables(connection: &Connection) -> rusqlite::Result<Vec<String>> {
    let mut statement =
        connection.prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid")?;
    statement.query_map([], |row| row.get(0))?.collect()
}

fn append(path: &Path, suffix: &str) -> PathBuf {
    let mut name = OsString::from(path);
    name.push(suffix);
    PathBuf::from(name)
}

fn write_backups(database: &Path, errors: &mut Vec<String>) {
    for suffix in SIDECARS {
        let source = append(database, suffix);
        let target = append(database, &format!(".pre-reset.bak{suffix}"));
        let result = if source.is_file() {
            fs::copy(&source, &target).and_then(|_| {
                let times = fs::FileTimes::new().set_modified(fs::metadata(source)?.modified()?);
                fs::File::options()
                    .write(true)
                    .open(&target)?
                    .set_times(times)
            })
        } else {
            remove_if_present(&target).map(|_| ())
        };
        if let Err(error) = result {
            errors.push(format!("pre-reset backup: {error}"));
        }
    }
}

fn remove_backups(database: &Path, errors: &mut Vec<String>) -> usize {
    let mut removed = 0;
    for suffix in SIDECARS {
        let path = append(database, &format!(".pre-reset.bak{suffix}"));
        match remove_if_present(&path) {
            Ok(existed) => removed += usize::from(existed),
            Err(error) => errors.push(format!("pre-reset backup removal: {error}")),
        }
    }
    removed
}

fn remove_if_present(path: &Path) -> std::io::Result<bool> {
    match fs::remove_file(path) {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

fn remove_assets(assets: &Path, errors: &mut Vec<String>) -> usize {
    let entries = match fs::read_dir(assets) {
        Ok(entries) => entries,
        Err(error) if error.kind() == ErrorKind::NotFound => return 0,
        Err(error) => {
            errors.push(format!("asset {}: {error}", assets.display()));
            return 0;
        }
    };
    let mut removed = 0;
    for entry in entries {
        match entry {
            Ok(entry) if entry.path().is_file() => match remove_if_present(&entry.path()) {
                Ok(existed) => removed += usize::from(existed),
                Err(error) => errors.push(format!(
                    "asset {}: {error}",
                    entry.file_name().to_string_lossy()
                )),
            },
            Ok(_) => {}
            Err(error) => errors.push(format!("asset {}: {error}", assets.display())),
        }
    }
    removed
}

#[cfg(test)]
mod tests {
    use std::fs;

    use serde_json::json;

    use super::reset;
    use crate::{Database, ProfileStore, SettingsStore};

    #[test]
    fn data_only_backup_retains_the_original_profile() {
        let root = std::env::temp_dir().join(format!("agent-hire-reset-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let assets = root.join("assets");
        fs::create_dir_all(assets.join("subdirectory")).expect("create the asset directories");
        fs::write(assets.join("generated.pdf"), b"asset").expect("write a generated asset");
        let database = Database::open(&root.join("crm.db")).expect("open test database");
        let settings = SettingsStore::new(database.clone());
        settings
            .save(&[
                ("email".to_owned(), "user@example.net".to_owned()),
                ("custom_setting".to_owned(), "keep".to_owned()),
            ])
            .expect("seed settings");
        let store = ProfileStore::new(database.clone());
        store
            .save_skill(None, "Go", "language")
            .expect("seed profile");

        let first = reset(&database, &assets, false);
        assert_eq!(first["errors"], json!([]));
        assert_eq!(first["graph_cleared"], json!(["Skill"]));
        assert_eq!(first["assets_removed"], 1);
        assert!(assets.join("subdirectory").is_dir());
        assert_eq!(
            settings.get("custom_setting", "").expect("read settings"),
            "keep"
        );
        assert_eq!(settings.get("email", "").expect("read identity"), "");
        let backup = Database::open(&root.join("crm.db.pre-reset.bak")).expect("open the backup");
        let old = ProfileStore::new(backup)
            .get_profile()
            .expect("read backed up profile");
        assert_eq!(old["skills"][0]["n"], "Go");

        let second = reset(&database, &assets, true);
        assert_eq!(second["errors"], json!([]));
        assert_eq!(
            settings.get("custom_setting", "").expect("read settings"),
            ""
        );
        assert!(!root.join("crm.db.pre-reset.bak").exists());
        fs::remove_dir_all(root).expect("remove test database");
    }
}
