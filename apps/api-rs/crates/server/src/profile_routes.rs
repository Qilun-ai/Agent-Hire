//! Native profile CRUD routes and their request models.

use std::sync::Arc;

use agent_hire_store::{ProfileEntryKind, ProfileStore, StoreError};
use axum::Json;
use axum::extract::{Extension, Path, State};
use axum::http::StatusCode;
use axum::response::Response;
use serde_json::{Map, Value, json};

use crate::app_state::AppState;
use crate::respond;
use crate::validation::{ModelBody, ModelReader};

type EditResponse = Result<Json<Value>, Response>;

/// Returns the canonical profile, with point children for legacy rows.
pub async fn get_profile(
    State(state): State<Arc<AppState>>,
) -> Result<Json<Map<String, Value>>, Response> {
    let store = state.profile.clone();
    respond::blocking("read profile", move || store.get_profile())
        .await
        .map(Json)
}

/// Validates and replaces the candidate name and summary.
pub async fn update_candidate(
    State(state): State<Arc<AppState>>,
    ModelBody(body): ModelBody,
) -> EditResponse {
    let mut reader = ModelReader::new(&body);
    let name = reader.string("n", Some(160), false).unwrap_or_default();
    let summary = reader.string("s", Some(4000), false).unwrap_or_default();
    reader.forbid_extra(&["n", "s"]);
    reader.finish()?;
    require_text(&[&name, &summary], "Name or summary is required")?;
    edit(&state, move |store| store.update_candidate(&name, &summary)).await
}

/// Validates and updates only the identity fields supplied in the request.
pub async fn update_identity(
    State(state): State<Arc<AppState>>,
    ModelBody(body): ModelBody,
) -> EditResponse {
    const FIELDS: [(&str, usize); 7] = [
        ("email", 200),
        ("phone", 80),
        ("linkedin_url", 500),
        ("github_url", 500),
        ("website_url", 500),
        ("city", 200),
        ("country", 200),
    ];
    let mut reader = ModelReader::new(&body);
    let fields = FIELDS
        .iter()
        .filter_map(|&(key, max)| {
            reader
                .string(key, Some(max), false)
                .map(|value| (key, value))
        })
        .collect::<Vec<_>>();
    reader.forbid_extra(&FIELDS.map(|(key, _)| key));
    reader.finish()?;
    edit(&state, move |store| store.update_identity(&fields)).await
}

/// Adds a skill on POST, or replaces the path id on PUT.
pub async fn save_skill(
    State(state): State<Arc<AppState>>,
    path: Option<Path<String>>,
    ModelBody(body): ModelBody,
) -> EditResponse {
    let id = path.map(|Path(id)| validate_path_id(id)).transpose()?;
    let mut reader = ModelReader::new(&body);
    validate_body_id(&body, &mut reader);
    let name = reader.string("n", Some(160), false).unwrap_or_default();
    let category = reader
        .string("cat", Some(80), false)
        .unwrap_or_else(|| "general".to_owned());
    reader.forbid_extra(&["id", "n", "cat"]);
    reader.finish()?;
    require_text(&[&name], "Skill name is required")?;
    edit(&state, move |store| {
        store.save_skill(id.as_deref(), &name, &category)
    })
    .await
}

/// Deletes a skill by id or name.
pub async fn delete_skill(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> EditResponse {
    let id = validate_path_id(id)?;
    edit(&state, move |store| {
        store.delete_skill(&id)?;
        Ok(json!({ "ok": true }))
    })
    .await
}

/// Adds an experience on POST, or replaces the path id on PUT.
pub async fn save_experience(
    State(state): State<Arc<AppState>>,
    path: Option<Path<String>>,
    ModelBody(body): ModelBody,
) -> EditResponse {
    let id = path.map(|Path(id)| validate_path_id(id)).transpose()?;
    let mut reader = ModelReader::new(&body);
    validate_body_id(&body, &mut reader);
    let role = reader.string("role", Some(180), false).unwrap_or_default();
    let company = reader.string("co", Some(180), false).unwrap_or_default();
    let period = reader
        .string("period", Some(120), false)
        .unwrap_or_default();
    let description = reader.string("d", Some(8000), false).unwrap_or_default();
    reader.forbid_extra(&["id", "role", "co", "period", "d"]);
    reader.finish()?;
    require_text(&[&role, &company], "Role or company is required")?;
    edit(&state, move |store| {
        store.save_experience(id.as_deref(), &role, &company, &period, &description)
    })
    .await
}

/// Deletes an experience by id or label.
pub async fn delete_experience(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> EditResponse {
    let id = validate_path_id(id)?;
    edit(&state, move |store| {
        store.delete_experience(&id)?;
        Ok(json!({ "ok": true }))
    })
    .await
}

/// Adds a project on POST, or replaces the path id on PUT.
pub async fn save_project(
    State(state): State<Arc<AppState>>,
    path: Option<Path<String>>,
    ModelBody(body): ModelBody,
) -> EditResponse {
    let id = path.map(|Path(id)| validate_path_id(id)).transpose()?;
    let mut reader = ModelReader::new(&body);
    validate_body_id(&body, &mut reader);
    let title = reader.string("title", Some(220), false).unwrap_or_default();
    let stack = reader
        .string("stack", Some(2000), false)
        .unwrap_or_default();
    let repo = reader.string("repo", Some(1000), false).unwrap_or_default();
    let impact = reader
        .string("impact", Some(8000), false)
        .unwrap_or_default();
    reader.forbid_extra(&["id", "title", "stack", "repo", "impact"]);
    reader.finish()?;
    require_text(&[&title], "Project title is required")?;
    edit(&state, move |store| {
        store.save_project(id.as_deref(), &title, &stack, &repo, &impact)
    })
    .await
}

/// Deletes a project by id or title.
pub async fn delete_project(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> EditResponse {
    let id = validate_path_id(id)?;
    edit(&state, move |store| {
        store.delete_project(&id)?;
        Ok(json!({ "ok": true }))
    })
    .await
}

/// Validates and adds an education, certification, or achievement entry.
pub async fn add_entry(
    State(state): State<Arc<AppState>>,
    Extension(kind): Extension<ProfileEntryKind>,
    ModelBody(body): ModelBody,
) -> EditResponse {
    let mut reader = ModelReader::new(&body);
    let title = reader.string("title", Some(500), false).unwrap_or_default();
    reader.forbid_extra(&["title"]);
    reader.finish()?;
    let required_message = match kind {
        ProfileEntryKind::Education => "Education title is required",
        ProfileEntryKind::Certification => "Certification title is required",
        ProfileEntryKind::Achievement => "Achievement title is required",
    };
    require_text(&[&title], required_message)?;
    edit(&state, move |store| store.add_entry(kind, &title)).await
}

/// Deletes the education, certification, or achievement entry captured by the path.
pub async fn delete_entry(
    State(state): State<Arc<AppState>>,
    Extension(kind): Extension<ProfileEntryKind>,
    entry: Option<Path<String>>,
) -> EditResponse {
    let entry = entry.map(|Path(entry)| entry).unwrap_or_default();
    edit(&state, move |store| {
        store.delete_entry(kind, &entry)?;
        Ok(json!({ "ok": true }))
    })
    .await
}

fn validate_path_id(id: String) -> Result<String, Response> {
    if id.contains('/') {
        Err(respond::detail(
            StatusCode::METHOD_NOT_ALLOWED,
            "Method Not Allowed",
        ))
    } else {
        Ok(id)
    }
}

fn validate_body_id(body: &Map<String, Value>, reader: &mut ModelReader<'_>) {
    // Older clients send this optional field. The route still determines the id.
    if !body.get("id").is_some_and(Value::is_null) {
        reader.string("id", Some(160), false);
    }
}

fn require_text(values: &[&str], detail: &str) -> Result<(), Response> {
    if values.iter().any(|text| {
        text.chars()
            .any(|c| !c.is_whitespace() && !matches!(c, '\u{1c}'..='\u{1f}'))
    }) {
        Ok(())
    } else {
        Err(respond::detail(StatusCode::UNPROCESSABLE_ENTITY, detail))
    }
}

async fn edit(
    state: &AppState,
    work: impl FnOnce(ProfileStore) -> Result<Value, StoreError> + Send + 'static,
) -> EditResponse {
    let store = state.profile.clone();
    respond::blocking("edit profile", move || work(store))
        .await
        .map(Json)
}
