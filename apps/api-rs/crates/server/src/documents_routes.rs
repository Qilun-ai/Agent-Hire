//! Document uploads, listing, and persisted ingestion status.

use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use agent_hire_llm::Settings;
use agent_hire_store::DocumentUpload;
use axum::Json;
use axum::extract::{FromRequest, Multipart, RawQuery, Request, State};
use axum::http::{StatusCode, header};
use axum::response::Response;
use serde_json::{Value, json};

use crate::app_state::AppState;
use crate::rate_limit::RateLimiter;
use crate::{log, respond, resume_ingest, validation};

const KIND_ERROR: &str = "kind must be 'resume' or 'cover_letter'";
const MAX_BYTES: usize = 10 * 1024 * 1024;
static UPLOADS: RateLimiter = RateLimiter::new(10, Duration::from_secs(60));

/// Lists documents in the library and their tags.
pub async fn list(
    State(state): State<Arc<AppState>>,
    RawQuery(query): RawQuery,
) -> Result<Json<Value>, Response> {
    let pairs = validation::query_pairs(query.as_deref());
    let kind = validation::query_value(&pairs, "kind").map(str::to_owned);
    if kind.as_deref().is_some_and(|kind| !valid_kind(kind)) {
        return Err(respond::detail(StatusCode::BAD_REQUEST, KIND_ERROR));
    }
    let tag_id = validation::query_value(&pairs, "tag_id").map(str::to_owned);
    let store = state.documents.clone();
    let documents = respond::blocking("list documents", move || {
        store.list(kind.as_deref(), tag_id.as_deref())
    })
    .await?;
    Ok(Json(json!({"documents": documents})))
}

/// Returns a named ingestion task, or the current/latest task when no id is supplied.
pub async fn ingest_status(
    State(state): State<Arc<AppState>>,
    RawQuery(query): RawQuery,
) -> Result<Json<Value>, Response> {
    let pairs = validation::query_pairs(query.as_deref());
    let task_id = validation::query_value(&pairs, "task_id")
        .filter(|id| !id.is_empty())
        .map(str::to_owned);
    let id = task_id.clone();
    let store = state.documents.clone();
    if let Some(task) = respond::blocking("read ingestion task", move || {
        store.ingest_status(id.as_deref())
    })
    .await?
    {
        return Ok(Json(task));
    }
    if let Some(id) = task_id {
        return Err(respond::detail(
            StatusCode::NOT_FOUND,
            &format!("Ingestion task '{id}' not found"),
        ));
    }
    Ok(Json(json!({
        "task_id": null, "status": "idle", "stage": "idle", "stage_number": 0,
        "stage_message": "No ingestion task active.", "filename": null, "tag_id": null,
        "elapsed_seconds": 0.0, "progress_percent": 0, "thoughts": [], "has_staged_duplicates": false,
        "reviewable_duplicates": [], "error": null, "started_at": null, "completed_at": null, "result": null,
    })))
}

#[derive(Default)]
struct Upload {
    kind: String,
    topic: String,
    tag_id: String,
    ingest: String,
    file: Option<UploadFile>,
    text_file: Option<String>,
}

struct UploadFile {
    filename: String,
    bytes: Vec<u8>,
    too_large: bool,
}

struct StoredUpload {
    path: PathBuf,
    filename: String,
    text: String,
    suffix: &'static str,
    mime: &'static str,
}

/// Saves a multipart file and invokes the legacy parser only when explicitly enabled.
pub async fn upload(
    State(state): State<Arc<AppState>>,
    request: Request,
) -> Result<Json<Value>, Response> {
    let mut upload = read_upload(&state, request).await?;
    let file = validate_upload(&mut upload)?;
    UPLOADS.check()?;
    if !valid_kind(&upload.kind) {
        return Err(respond::detail(StatusCode::BAD_REQUEST, KIND_ERROR));
    }
    let Some((suffix, mime)) = file_type(&file.filename) else {
        let suffix = suffix(&file.filename);
        let suffix = if suffix.is_empty() { "(none)" } else { &suffix };
        return Err(respond::detail(
            StatusCode::BAD_REQUEST,
            &format!("Unsupported file type {suffix}; allowed: .doc, .docx, .md, .pdf, .txt"),
        ));
    };
    if file.too_large {
        return Err(respond::detail(
            StatusCode::PAYLOAD_TOO_LARGE,
            "File too large (max 10 MB)",
        ));
    }

    let _profile = state.profile_writes.read().await;
    let directory = state.paths.data_dir.join("documents").join(&upload.kind);
    let stored = respond::blocking("write upload", move || {
        save_file(&directory, file, suffix, mime)
    })
    .await?;
    let resume_id = if upload.kind == "resume" {
        format!("resume_{}", uuid_hex())
    } else {
        String::new()
    };
    let ingest =
        upload.kind == "resume" && upload.ingest == "true" && resume_ingest::legacy_enabled();
    let (parsed, ingest_error) = if ingest {
        match ingest_resume(&state, &stored, &resume_id, &upload.tag_id).await {
            Ok(parsed) => (Some(parsed), None),
            Err(error) => {
                log::warning(
                    "documents",
                    &format!("resume ingest after upload failed: {error}"),
                );
                let text: String = error
                    .lines()
                    .next()
                    .unwrap_or_default()
                    .chars()
                    .take(300)
                    .collect();
                (None, Some(text))
            }
        }
    } else {
        (None, None)
    };

    let store = state.documents.clone();
    let path = stored.path.clone();
    let result = tokio::task::spawn_blocking(move || {
        let filename = stored.filename;
        let topic = if upload.topic.is_empty() {
            file_stem(&filename)
        } else {
            &upload.topic
        };
        let document = DocumentUpload {
            id: &uuid_hex()[..12],
            kind: &upload.kind,
            topic,
            tag_id: (!upload.tag_id.is_empty()).then_some(upload.tag_id.as_str()),
            file_path: &stored.path.to_string_lossy(),
            source_filename: &filename,
            mime: stored.mime,
            excerpt: &stored.text,
            resume_id: &resume_id,
            parsed_profile: parsed.as_ref(),
        };
        store.create(&document)
    })
    .await;
    match result {
        Ok(Ok(mut document)) => {
            if let Some(error) = ingest_error.filter(|error| !error.is_empty()) {
                document["ingest_error"] = json!(error);
            }
            Ok(Json(document))
        }
        result => {
            let _ = tokio::fs::remove_file(path).await;
            let error = match result {
                Ok(Err(error)) => error.to_string(),
                Err(error) => error.to_string(),
                Ok(Ok(_)) => unreachable!("success was handled"),
            };
            Err(respond::detail(
                StatusCode::INTERNAL_SERVER_ERROR,
                &format!("could not register document: {error}"),
            ))
        }
    }
}

async fn ingest_resume(
    state: &AppState,
    stored: &StoredUpload,
    resume_id: &str,
    tag_id: &str,
) -> Result<Value, String> {
    let settings = state.settings.clone();
    let profile = state.profile.clone();
    let (settings, current) = tokio::task::spawn_blocking(move || {
        Ok::<_, agent_hire_store::StoreError>((
            Settings::new(settings.all()?),
            profile.get_profile()?,
        ))
    })
    .await
    .map_err(|error| error.to_string())?
    .map_err(|error| error.to_string())?;
    let path = (stored.suffix == ".pdf").then_some(stored.path.as_path());
    let parsed = resume_ingest::extract(&settings, &stored.text, path, &current, resume_id).await?;
    let profile = state.profile.clone();
    let resume_id = resume_id.to_owned();
    let tag_id = tag_id.to_owned();
    tokio::task::spawn_blocking(move || profile.import_resume(parsed, &resume_id, &tag_id))
        .await
        .map_err(|error| error.to_string())?
        .map_err(|error| error.to_string())
}

async fn read_upload(state: &Arc<AppState>, request: Request) -> Result<Upload, Response> {
    let content_type = request
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default();
    if content_type.starts_with("application/x-www-form-urlencoded") {
        let bytes = axum::body::to_bytes(request.into_body(), MAX_BYTES)
            .await
            .map_err(|_| malformed("There was an error parsing the body"))?;
        let mut upload = Upload::default();
        for (key, value) in url::form_urlencoded::parse(&bytes) {
            set_field(&mut upload, &key, value.into_owned());
        }
        return Ok(upload);
    }
    if !content_type.starts_with("multipart/form-data") {
        return Ok(Upload::default());
    }
    let mut multipart = Multipart::from_request(request, state)
        .await
        .map_err(|_| malformed("Missing boundary in multipart."))?;
    let mut upload = Upload::default();
    while let Some(mut field) = multipart
        .next_field()
        .await
        .map_err(|_| malformed("There was an error parsing the body"))?
    {
        let name = field.name().unwrap_or_default().to_owned();
        if name == "file"
            && let Some(filename) = field.file_name().map(str::to_owned)
        {
            let mut file = UploadFile {
                filename,
                bytes: Vec::new(),
                too_large: false,
            };
            while let Some(chunk) = field
                .chunk()
                .await
                .map_err(|_| malformed("There was an error parsing the body"))?
            {
                if file.bytes.len() + chunk.len() > MAX_BYTES {
                    file.too_large = true;
                }
                if !file.too_large {
                    file.bytes.extend_from_slice(&chunk);
                }
            }
            upload.text_file = None;
            upload.file = Some(file);
        } else if matches!(
            name.as_str(),
            "kind" | "file" | "tag_id" | "topic" | "ingest"
        ) {
            let value = field
                .text()
                .await
                .map_err(|_| malformed("There was an error parsing the body"))?;
            set_field(&mut upload, &name, value);
        }
    }
    Ok(upload)
}

fn set_field(upload: &mut Upload, name: &str, value: String) {
    match name {
        "kind" => upload.kind = value,
        "topic" => upload.topic = value,
        "tag_id" => upload.tag_id = value,
        "ingest" => upload.ingest = value,
        "file" => {
            upload.file = None;
            upload.text_file = Some(value);
        }
        _ => {}
    }
}

fn validate_upload(upload: &mut Upload) -> Result<UploadFile, Response> {
    let mut errors = validation::Errors::default();
    if upload.kind.is_empty() {
        errors.missing(&[json!("body"), json!("kind")], &Value::Null);
    }
    if upload.file.is_none() {
        let loc = [json!("body"), json!("file")];
        match upload.text_file.as_deref().filter(|file| !file.is_empty()) {
            Some(file) => errors.push(
                "value_error",
                &loc,
                "Value error, Expected UploadFile, received: <class 'str'>",
                &json!(file),
                Some(json!({"error": {}})),
            ),
            None => errors.missing(&loc, &Value::Null),
        }
    }
    match errors.into_response() {
        Some(response) => Err(response),
        None => Ok(upload.file.take().expect("file was validated")),
    }
}

fn valid_kind(kind: &str) -> bool {
    matches!(kind, "resume" | "cover_letter")
}

fn suffix(filename: &str) -> String {
    Path::new(filename)
        .extension()
        .and_then(|extension| extension.to_str())
        .filter(|extension| !extension.is_empty())
        .map(|extension| format!(".{}", extension.to_lowercase()))
        .unwrap_or_default()
}

fn file_type(filename: &str) -> Option<(&'static str, &'static str)> {
    Some(match suffix(filename).as_str() {
        ".pdf" => (".pdf", "application/pdf"),
        ".doc" => (".doc", "application/msword"),
        ".docx" => (
            ".docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ".txt" => (".txt", "text/plain"),
        ".md" => (".md", "text/markdown"),
        _ => return None,
    })
}

fn file_stem(filename: &str) -> &str {
    Path::new(filename)
        .file_stem()
        .and_then(|stem| stem.to_str())
        .unwrap_or_default()
}

fn uuid_hex() -> String {
    let mut bytes = [0; 16];
    getrandom::fill(&mut bytes).expect("OS randomness is available");
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    format!("{:032x}", u128::from_be_bytes(bytes))
}

fn save_file(
    directory: &Path,
    file: UploadFile,
    suffix: &'static str,
    mime: &'static str,
) -> std::io::Result<StoredUpload> {
    fs::create_dir_all(directory)?;
    let path = loop {
        let path = directory.join(format!("{}{suffix}", &uuid_hex()[..12]));
        match OpenOptions::new().create_new(true).write(true).open(&path) {
            Ok(mut handle) => {
                if let Err(error) = handle.write_all(&file.bytes) {
                    drop(handle);
                    let _ = fs::remove_file(path);
                    return Err(error);
                }
                break path;
            }
            Err(error) if error.kind() == ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error),
        }
    };
    let text = resume_ingest::document_text(suffix, &file.bytes);
    Ok(StoredUpload {
        path,
        filename: file.filename,
        text,
        suffix,
        mime,
    })
}

fn malformed(detail: &str) -> Response {
    respond::detail(StatusCode::BAD_REQUEST, detail)
}
