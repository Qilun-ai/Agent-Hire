//! The canonical profile in SQLite, shared with the legacy backend during migration.

use rusqlite::{Connection, Transaction, TransactionBehavior};
use serde_json::{Map, Value, json};

use crate::database::{Database, StoreError};
use crate::profile_deletions::{Collection, Tombstones};
use crate::{profile_points, profile_text, python_json_dumps, settings};

pub(super) const SNAPSHOT: &str = "profile_snapshot_json";
const BACKUPS: [&str; 3] = [
    "profile_snapshot_json_bak1",
    "profile_snapshot_json_bak2",
    "profile_snapshot_json_bak3",
];
const DELETIONS: &str = "profile_deleted_items_json";

const IDENTITY_KEYS: [&str; 7] = [
    "email",
    "phone",
    "linkedin_url",
    "github_url",
    "website_url",
    "city",
    "country",
];

pub(super) fn clear_settings(connection: &Connection) -> Result<(), StoreError> {
    for key in [SNAPSHOT, DELETIONS]
        .into_iter()
        .chain(BACKUPS)
        .chain(IDENTITY_KEYS)
    {
        connection.execute("DELETE FROM settings WHERE key = ?1", [key])?;
    }
    Ok(())
}

/// A title-only entry in the profile.
#[derive(Clone, Copy, Debug)]
pub enum ProfileEntryKind {
    /// Education.
    Education,
    /// Certification.
    Certification,
    /// Achievement.
    Achievement,
}

impl ProfileEntryKind {
    fn collection(self) -> Collection {
        match self {
            ProfileEntryKind::Education => Collection::Education,
            ProfileEntryKind::Certification => Collection::Certifications,
            ProfileEntryKind::Achievement => Collection::Achievements,
        }
    }
}

/// Reads and edits the profile snapshot in the application database.
#[derive(Clone, Debug)]
pub struct ProfileStore {
    database: Database,
}

impl ProfileStore {
    /// The profile in `database`.
    pub fn new(database: Database) -> ProfileStore {
        ProfileStore { database }
    }

    /// Reads the canonical profile and derives missing point children without writing them back.
    pub fn get_profile(&self) -> Result<Map<String, Value>, StoreError> {
        let mut connection = self.database.connect()?;
        let transaction = connection.transaction()?;
        let snapshot = settings::read(&transaction, SNAPSHOT)?;
        let mut profile = read_snapshot(&transaction, snapshot.as_deref())?;
        read_tombstones(&transaction)?.filter(&mut profile);
        transaction.commit()?;
        profile_points::ensure(&mut profile);
        Ok(profile)
    }

    /// Replaces the candidate name and summary.
    pub fn update_candidate(&self, name: &str, summary: &str) -> Result<Value, StoreError> {
        let name = profile_text::trim(name);
        let summary = profile_text::summary(summary);
        self.change(|_, profile, _| {
            profile["n"] = json!(name);
            profile["s"] = json!(summary);
            Ok(json!({ "n": name, "s": summary }))
        })
    }

    /// Updates the supplied identity fields and returns the complete identity.
    pub fn update_identity(&self, fields: &[(&str, String)]) -> Result<Value, StoreError> {
        self.change(|transaction, profile, _| {
            for (key, value) in fields {
                if IDENTITY_KEYS.contains(key) {
                    let value = profile_text::trim(value);
                    settings::write(transaction, key, value)?;
                    profile["identity"][key] = json!(value);
                }
            }
            Ok(profile["identity"].clone())
        })
    }

    /// Adds a skill, or replaces the skill with the supplied id.
    pub fn save_skill(
        &self,
        id: Option<&str>,
        name: &str,
        category: &str,
    ) -> Result<Value, StoreError> {
        let name = profile_text::trim(name);
        let category = profile_text::trim(category);
        let category = if category.is_empty() {
            "general"
        } else {
            category
        };
        let id = id.map_or_else(|| profile_text::id(name), str::to_owned);
        self.change(|_, profile, tombstones| {
            tombstones.forget(Collection::Skills, &[&id, name]);
            let row = json!({ "id": id, "n": name, "cat": category });
            upsert(profile, Collection::Skills, row.clone());
            Ok(row)
        })
    }

    /// Deletes a skill by id or name.
    pub fn delete_skill(&self, id: &str) -> Result<(), StoreError> {
        self.delete(Collection::Skills, id)
    }

    /// Adds an experience, or replaces the experience with the supplied id.
    pub fn save_experience(
        &self,
        id: Option<&str>,
        role: &str,
        company: &str,
        period: &str,
        description: &str,
    ) -> Result<Value, StoreError> {
        let [role, company, period, description] =
            [role, company, period, description].map(profile_text::trim);
        let adding = id.is_none();
        let label = format!("{role}{company}");
        let id = id.map_or_else(|| profile_text::id(&label), str::to_owned);
        self.change(|_, profile, tombstones| {
            tombstones.forget(
                Collection::Experience,
                &[
                    &id,
                    role,
                    company,
                    &label,
                    &format!("{role} at {company}"),
                    &format!("{role} - {company}"),
                ],
            );
            let row = json!({
                "id": id, "role": role, "co": company, "period": period, "d": description,
            });
            let mut stored = row.clone();
            // The legacy add path filled an empty description with its labeled text.
            if adding && description.is_empty() {
                stored["d"] = json!(label_text(&[
                    ("Experience", role),
                    ("Company", company),
                    ("Period", period),
                    ("Description", ""),
                ]));
            }
            upsert(profile, Collection::Experience, stored);
            Ok(row)
        })
    }

    /// Deletes an experience by id or label.
    pub fn delete_experience(&self, id: &str) -> Result<(), StoreError> {
        self.delete(Collection::Experience, id)
    }

    /// Adds a project, or replaces the project with the supplied id.
    pub fn save_project(
        &self,
        id: Option<&str>,
        title: &str,
        stack: &str,
        repo: &str,
        impact: &str,
    ) -> Result<Value, StoreError> {
        let [title, stack, repo, impact] = [title, stack, repo, impact].map(profile_text::trim);
        let adding = id.is_none();
        let id = id.map_or_else(|| profile_text::id(title), str::to_owned);
        // The mutation response exposes raw comma splits, but readers use the normalized stack.
        let raw_stack: Vec<&str> = if stack.is_empty() {
            Vec::new()
        } else {
            stack.split(',').collect()
        };
        let raw_stack = json!(raw_stack);
        let row =
            json!({ "id": id, "title": title, "stack": raw_stack, "repo": repo, "impact": impact });
        let stack_items = profile_text::stack(&raw_stack);
        self.change(|_, profile, tombstones| {
            tombstones.forget(Collection::Projects, &[&id, title]);
            for skill in &stack_items {
                let skill_id = profile_text::id(skill);
                tombstones.forget(Collection::Skills, &[&skill_id, skill]);
                let row = json!({ "id": skill_id, "n": skill, "cat": "project_stack" });
                upsert(profile, Collection::Skills, row);
            }
            let mut stored = row.clone();
            stored["stack"] = json!(stack_items);
            if adding && impact.is_empty() {
                stored["impact"] = json!(label_text(&[
                    ("Project", title),
                    ("Stack", stack),
                    ("Impact", ""),
                ]));
            }
            upsert(profile, Collection::Projects, stored);
            Ok(row)
        })
    }

    /// Deletes a project by id or title.
    pub fn delete_project(&self, id: &str) -> Result<(), StoreError> {
        self.delete(Collection::Projects, id)
    }

    /// Adds an education, certification, or achievement entry.
    pub fn add_entry(&self, kind: ProfileEntryKind, title: &str) -> Result<Value, StoreError> {
        let title = profile_text::trim(title);
        let id = profile_text::id(title);
        let kind = kind.collection();
        self.change(|_, profile, tombstones| {
            tombstones.forget(kind, &[&id, title]);
            let entries = collection_mut(profile, kind);
            if !entries.iter().any(|entry| {
                entry
                    .as_str()
                    .or_else(|| entry["title"].as_str())
                    .is_some_and(|existing| existing.to_lowercase() == title.to_lowercase())
            }) {
                entries.push(json!(title));
            }
            Ok(json!({ "id": id, "title": title }))
        })
    }

    /// Deletes an education, certification, or achievement entry by id or title.
    pub fn delete_entry(&self, kind: ProfileEntryKind, entry: &str) -> Result<(), StoreError> {
        self.delete(kind.collection(), entry)
    }

    fn delete(&self, kind: Collection, value: &str) -> Result<(), StoreError> {
        self.change(|_, profile, tombstones| {
            tombstones.delete(kind, value, collection_mut(profile, kind));
            Ok(())
        })
    }

    pub(super) fn change<T>(
        &self,
        edit: impl FnOnce(
            &Connection,
            &mut Map<String, Value>,
            &mut Tombstones,
        ) -> Result<T, StoreError>,
    ) -> Result<T, StoreError> {
        let mut connection = self.database.connect()?;
        // Lock before loading the snapshot so concurrent edits cannot overwrite each other's work.
        let mut transaction =
            connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let previous = settings::read(&transaction, SNAPSHOT)?;
        let mut profile = read_snapshot(&transaction, previous.as_deref())?;
        let mut tombstones = read_tombstones(&transaction)?;
        tombstones.filter(&mut profile);
        let result = edit(&transaction, &mut profile, &mut tombstones)?;
        tombstones.filter(&mut profile);
        if tombstones.changed() {
            settings::write(
                &transaction,
                DELETIONS,
                &python_json_dumps(&tombstones.to_json(), false),
            )?;
        }
        if let Some(previous) = previous.filter(|text| !text.is_empty()) {
            // Backup failures must not prevent saving the current snapshot.
            let _ = backup(&mut transaction, &previous);
        }
        settings::write(
            &transaction,
            SNAPSHOT,
            &python_json_dumps(&Value::Object(profile), false),
        )?;
        transaction.commit()?;
        Ok(result)
    }
}

fn read_snapshot(
    connection: &Connection,
    snapshot: Option<&str>,
) -> Result<Map<String, Value>, StoreError> {
    let input = snapshot
        .and_then(|text| serde_json::from_str::<Value>(text).ok())
        .unwrap_or_default();
    let mut identity = Map::new();
    for key in IDENTITY_KEYS {
        let nested = profile_text::truthy_string(&input["identity"][key]);
        let top_level = profile_text::truthy_string(&input[key]);
        let value = if !nested.is_empty() {
            nested.into_owned()
        } else if !top_level.is_empty() {
            top_level.into_owned()
        } else {
            settings::read(connection, key)?.unwrap_or_default()
        };
        identity.insert(key.to_owned(), json!(value));
    }

    let mut input = match input {
        Value::Object(object) => object,
        _ => Map::new(),
    };
    let mut profile = Map::new();
    let name = input.remove("n").unwrap_or_default();
    let summary = input.remove("s").unwrap_or_default();
    profile.insert("n".to_owned(), json!(profile_text::truthy_string(&name)));
    profile.insert(
        "s".to_owned(),
        json!(profile_text::summary(&profile_text::truthy_string(
            &summary
        ))),
    );
    for key in [
        "skills",
        "projects",
        "exp",
        "certifications",
        "education",
        "achievements",
    ] {
        let mut rows = take_collection(&mut input, key);
        if rows.is_empty() {
            let alias = match key {
                "certifications" => Some("certs"),
                "achievements" => Some("awards"),
                _ => None,
            };
            if let Some(alias) = alias {
                rows = take_collection(&mut input, alias);
            }
        }
        if key == "projects" {
            for project in rows.iter_mut().filter_map(Value::as_object_mut) {
                let stack = project.get("stack").unwrap_or(&Value::Null);
                let stack = profile_text::stack(stack);
                project.insert("stack".to_owned(), json!(stack));
            }
        }
        profile.insert(key.to_owned(), Value::Array(rows));
    }
    profile.insert("identity".to_owned(), Value::Object(identity));
    Ok(profile)
}

fn take_collection(input: &mut Map<String, Value>, key: &str) -> Vec<Value> {
    match input.remove(key) {
        Some(Value::Array(rows)) => rows,
        _ => Vec::new(),
    }
}

fn read_tombstones(connection: &Connection) -> Result<Tombstones, StoreError> {
    let input = settings::read(connection, DELETIONS)?
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default();
    Ok(Tombstones::from_json(input))
}

fn backup(transaction: &mut Transaction<'_>, current: &str) -> Result<(), StoreError> {
    let savepoint = transaction.savepoint()?;
    for pair in BACKUPS.windows(2).rev() {
        if let Some(value) = settings::read(&savepoint, pair[0])?.filter(|text| !text.is_empty()) {
            settings::write(&savepoint, pair[1], &value)?;
        }
    }
    settings::write(&savepoint, BACKUPS[0], current)?;
    savepoint.commit()?;
    Ok(())
}

fn collection_mut(profile: &mut Map<String, Value>, kind: Collection) -> &mut Vec<Value> {
    profile
        .get_mut(kind.key())
        .and_then(Value::as_array_mut)
        .expect("canonical profile lists are initialized")
}

fn upsert(profile: &mut Map<String, Value>, kind: Collection, row: Value) {
    let rows = collection_mut(profile, kind);
    if let Some(existing) = rows.iter_mut().find(|existing| existing["id"] == row["id"]) {
        *existing = row;
    } else {
        rows.push(row);
    }
}

fn label_text(fields: &[(&str, &str)]) -> String {
    fields
        .iter()
        .map(|(label, value)| {
            if value.is_empty() {
                format!("{label}:")
            } else {
                format!("{label}: {value}")
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[cfg(test)]
mod tests {
    use std::fs;

    use serde_json::json;

    use super::{BACKUPS, DELETIONS, ProfileEntryKind, ProfileStore, SNAPSHOT};
    use crate::database::Database;
    use crate::settings::SettingsStore;

    #[test]
    fn snapshot_backups_and_derived_points_survive_reopening() {
        let directory =
            std::env::temp_dir().join(format!("agent-hire-profile-{}", std::process::id()));
        let _ = fs::remove_dir_all(&directory);
        let database = Database::open(&directory.join("crm.db")).expect("opens the test database");
        let settings = SettingsStore::new(database.clone());
        let store = ProfileStore::new(database);
        let mut snapshots = Vec::new();
        for name in ["Zoë", "Renée", "André", "Cécile"] {
            store
                .update_candidate(name, "Backend engineer.")
                .expect("sets candidate");
            snapshots.push(settings.get(SNAPSHOT, "").expect("reads snapshot"));
        }
        assert!(snapshots[0].starts_with(r#"{"n": "Zoë", "s": "Backend engineer", "skills": []"#));
        for (key, expected) in BACKUPS.iter().zip(snapshots[..3].iter().rev()) {
            assert_eq!(&settings.get(key, "").expect("reads backup"), expected);
        }

        store
            .save_experience(
                None,
                "Senior Engineer",
                "Ledgerline",
                "",
                "Payments platform on Kafka.",
            )
            .expect("adds experience");
        let snapshot = settings.get(SNAPSHOT, "").expect("reads snapshot");
        let database = Database::open(&directory.join("crm.db")).expect("reopens database");
        let store = ProfileStore::new(database);
        let profile = store.get_profile().expect("reads profile");
        assert_eq!(profile["exp"][0]["points"][0]["id"], "c2f76a3f9745");
        assert_eq!(
            settings.get(SNAPSHOT, "").expect("reads snapshot"),
            snapshot
        );
        assert!(!snapshot.contains("\"points\""));

        store
            .add_entry(ProfileEntryKind::Education, " École / Paris ")
            .expect("adds entry");
        store
            .delete_entry(ProfileEntryKind::Education, "%C3%A9cole%20/%20PARIS")
            .expect("deletes entry");
        assert_eq!(
            store.get_profile().expect("reads profile")["education"],
            json!([])
        );
        assert!(
            settings
                .get(DELETIONS, "")
                .expect("reads tombstones")
                .contains("école / paris")
        );
        fs::remove_dir_all(directory).expect("removes test database");
    }
}
