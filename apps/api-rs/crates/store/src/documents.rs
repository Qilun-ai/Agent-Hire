//! Uploaded document records in the shared application database.

use std::path::Path;

use rusqlite::{OptionalExtension, Row, params};
use serde_json::{Value, json};

use crate::database::{Database, StoreError};
use crate::profile::ProfileStore;
use crate::{profile_text, python_json_dumps};

/// A new document whose file has already been stored.
pub struct DocumentUpload<'a> {
    /// Random document identifier.
    pub id: &'a str,
    /// `resume` or `cover_letter`.
    pub kind: &'a str,
    /// Display topic.
    pub topic: &'a str,
    /// Tag selected at upload, if present.
    pub tag_id: Option<&'a str>,
    /// Full local file path.
    pub file_path: &'a str,
    /// Client-supplied filename.
    pub source_filename: &'a str,
    /// MIME type of the stored file.
    pub mime: &'a str,
    /// Plain-text preview of the uploaded document.
    pub excerpt: &'a str,
    /// Résumé provenance id, empty for cover letters.
    pub resume_id: &'a str,
    /// Parsed résumé when legacy extraction ran.
    pub parsed_profile: Option<&'a Value>,
}

/// The uploaded documents in one application database.
#[derive(Clone, Debug)]
pub struct DocumentStore {
    database: Database,
}

impl DocumentStore {
    /// Creates a repository for `database`.
    pub fn new(database: Database) -> DocumentStore {
        DocumentStore { database }
    }

    /// Lists matching documents, newest first, with their optional tag display names.
    pub fn list(&self, kind: Option<&str>, tag_id: Option<&str>) -> Result<Vec<Value>, StoreError> {
        let connection = self.database.connect()?;
        let mut statement = connection.prepare(
            "SELECT d.*, COALESCE(t.name, '') AS tag_name FROM documents d
             LEFT JOIN tags t ON t.id = d.tag_id
             WHERE (?1 IS NULL OR d.kind = ?1) AND (?2 IS NULL OR d.tag_id = ?2)
             ORDER BY d.created_at DESC, d.id DESC",
        )?;
        let kind = kind.filter(|kind| matches!(*kind, "resume" | "cover_letter"));
        let tag_id = tag_id.filter(|tag| !tag.is_empty());
        let rows = statement.query_map([kind, tag_id], |row| {
            let mut document = document(row)?;
            document["tag_name"] = json!(row.get::<_, String>("tag_name")?);
            Ok(document)
        })?;
        Ok(rows.collect::<Result<_, _>>()?)
    }

    /// Registers a saved file and reconciles its tagged résumé points.
    pub fn create(&self, upload: &DocumentUpload<'_>) -> Result<Value, StoreError> {
        let profile = match upload.tag_id {
            Some(_) if upload.kind == "resume" => {
                Some(ProfileStore::new(self.database.clone()).get_profile()?)
            }
            _ => None,
        };
        let mut connection = self.database.connect()?;
        let transaction = connection.transaction()?;
        let parsed = upload.parsed_profile.filter(|profile| profile.is_object());
        let parsed = parsed.map_or_else(
            || "{}".to_owned(),
            |profile| python_json_dumps(profile, false),
        );
        transaction.execute(
            "INSERT INTO documents(id, kind, topic, tag_id, file_path, source_filename, mime,
                                   excerpt, source, resume_id, parsed_profile_json)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'upload', ?9, ?10)",
            params![
                upload.id,
                upload.kind,
                profile_text::trim(upload.topic),
                upload.tag_id,
                upload.file_path,
                upload.source_filename,
                upload.mime,
                crate::truncate_chars(upload.excerpt, 8000),
                crate::truncate_chars(upload.resume_id, 160),
                parsed
            ],
        )?;
        let mut result = transaction.query_row(
            "SELECT * FROM documents WHERE id = ?1",
            [upload.id],
            document,
        )?;
        if let (Some(profile), Some(tag_id)) = (profile, upload.tag_id) {
            let mut inserted = 0;
            let mut candidates = 0;
            let excerpt = profile_text::collapse(upload.excerpt).to_lowercase();
            for (collection, kind) in [
                ("skills", "skill"),
                ("exp", "experience"),
                ("projects", "project"),
            ] {
                for row in profile[collection].as_array().into_iter().flatten() {
                    let points: &[Value] = if kind == "skill" {
                        std::slice::from_ref(row)
                    } else {
                        row["points"].as_array().map_or(&[], Vec::as_slice)
                    };
                    for point in points {
                        let Some(id) = point["id"].as_str() else {
                            continue;
                        };
                        let sourced = point["source_resume_ids"]
                            .as_array()
                            .is_some_and(|ids| ids.iter().any(|id| id == upload.resume_id));
                        let label = point[if kind == "skill" { "n" } else { "text" }]
                            .as_str()
                            .unwrap_or_default();
                        if sourced
                            || contains_label(
                                &excerpt,
                                &profile_text::collapse(label).to_lowercase(),
                            )
                        {
                            candidates += 1;
                            inserted += transaction.execute(
                                "INSERT OR IGNORE INTO point_tags(point_kind, point_id, tag_id) VALUES (?1, ?2, ?3)",
                                [kind, id, tag_id],
                            )?;
                        }
                    }
                }
            }
            result["tag_repair"] = json!({
                "status": if candidates == 0 { "no_match" } else { "ok" },
                "document_id": upload.id, "tag_id": tag_id,
                "candidates": candidates, "rows_inserted": inserted, "rows_existing": candidates - inserted,
            });
        }
        transaction.commit()?;
        Ok(result)
    }

    /// An exact task or the active task followed by the most recently started historical task.
    pub fn ingest_status(&self, task_id: Option<&str>) -> Result<Option<Value>, StoreError> {
        let connection = self.database.connect()?;
        let row = if let Some(id) = task_id.filter(|id| !id.is_empty()) {
            connection.query_row(
                "SELECT * FROM ingestion_tasks WHERE task_id = ?1",
                [id],
                task,
            )
        } else {
            connection.query_row(
                "SELECT * FROM ingestion_tasks
                 ORDER BY (status = 'processing') DESC, started_at DESC, task_id DESC LIMIT 1",
                [],
                task,
            )
        };
        Ok(row.optional()?)
    }
}

fn document(row: &Row<'_>) -> rusqlite::Result<Value> {
    let file_path: String = row.get("file_path")?;
    let exists = !file_path.is_empty() && Path::new(&file_path).is_file();
    Ok(json!({
        "id": row.get::<_, String>("id")?,
        "kind": row.get::<_, String>("kind")?,
        "topic": row.get::<_, String>("topic")?,
        "tag_id": row.get::<_, Option<String>>("tag_id")?,
        "file_path": file_path,
        "source_filename": row.get::<_, Option<String>>("source_filename")?,
        "mime": row.get::<_, Option<String>>("mime")?,
        "excerpt": row.get::<_, Option<String>>("excerpt")?,
        "source": row.get::<_, Option<String>>("source")?,
        "created_at": row.get::<_, Option<String>>("created_at")?,
        "exists": exists,
    }))
}

fn task(row: &Row<'_>) -> rusqlite::Result<Value> {
    let status: String = row.get("status")?;
    let started: String = row.get("started_at")?;
    let completed: Option<String> = row.get("completed_at")?;
    let thoughts = json_column(row, "thoughts_json", json!([]))?;
    let duplicates = json_column(row, "duplicates_json", json!([]))?;
    let has_duplicates =
        status == "review_required" || duplicates.as_array().is_some_and(|items| !items.is_empty());
    Ok(json!({
        "task_id": row.get::<_, String>("task_id")?, "status": status,
        "filename": row.get::<_, String>("filename")?, "file_path": row.get::<_, String>("file_path")?,
        "tag_id": row.get::<_, Option<String>>("tag_id")?, "topic": row.get::<_, String>("topic")?,
        "stage": row.get::<_, String>("stage")?, "stage_number": row.get::<_, i64>("stage_number")?,
        "stage_message": row.get::<_, String>("stage_message")?,
        "progress_percent": row.get::<_, i64>("progress_percent")?,
        "elapsed_seconds": elapsed(&started, completed.as_deref()), "thoughts": thoughts,
        "has_staged_duplicates": has_duplicates, "duplicates": duplicates, "reviewable_duplicates": duplicates,
        "result": json_column(row, "result_json", json!({}))?,
        "error": row.get::<_, Option<String>>("error")?, "started_at": started, "completed_at": completed,
    }))
}

fn json_column(row: &Row<'_>, key: &str, fallback: Value) -> rusqlite::Result<Value> {
    let raw: Option<String> = row.get(key)?;
    Ok(raw
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .filter(|value| {
            (fallback.is_array() && value.is_array()) || (fallback.is_object() && value.is_object())
        })
        .unwrap_or(fallback))
}

fn elapsed(started: &str, completed: Option<&str>) -> f64 {
    let Some(start) = timestamp(started) else {
        return 0.0;
    };
    let end = match completed {
        Some(completed) => timestamp(completed),
        None => timestamp(&crate::UtcTime::now().to_iso_micros()),
    };
    end.map_or(0.0, |end| {
        ((end - start) * 10.0).round_ties_even().max(0.0) / 10.0
    })
}

fn timestamp(text: &str) -> Option<f64> {
    let seconds = crate::clock::parse_sqlite_datetime(text)?;
    let fraction: String = text
        .get(19..)
        .and_then(|rest| rest.strip_prefix('.'))
        .unwrap_or_default()
        .chars()
        .take_while(char::is_ascii_digit)
        .collect();
    Some(seconds as f64 + format!("0.{fraction}").parse::<f64>().unwrap_or_default())
}

fn contains_label(text: &str, label: &str) -> bool {
    if label.is_empty() {
        return false;
    }
    text.match_indices(label).any(|(offset, _)| {
        let word = |c: char| c.is_alphanumeric() || matches!(c, '+' | '#' | '.' | '-');
        !text[..offset].ends_with(word) && !text[offset + label.len()..].starts_with(word)
    })
}
