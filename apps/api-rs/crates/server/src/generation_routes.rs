//! Drafting and selection routes. The generation library renders documents; the server owns state.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, PoisonError, Weak};
use std::time::Duration;

use agent_hire_generation::{
    self as generation, ContactLine, GenerateOptions, GenerateRequest, GenerationError,
    ProfileScope, Selection, SuggestRequest,
};
use agent_hire_llm::Settings;
use agent_hire_store::{GenerationJobUpdate, GenerationScope, StoreError};
use axum::Json;
use axum::extract::{Path, RawQuery, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};
use tokio::sync::{Mutex as AsyncMutex, OwnedMutexGuard};

use crate::app_state::AppState;
use crate::rate_limit::RateLimiter;
use crate::validation::{self, ModelReader, OptionalModelBody, QueryReader};
use crate::{generation_readiness, log, python_text, respond};

static GENERATION: RateLimiter = RateLimiter::new(5, Duration::from_secs(60));

/// Serializes same-lead requests so they each obtain a new résumé version.
#[derive(Default)]
pub struct GenerationLocks(Mutex<HashMap<String, Weak<AsyncMutex<()>>>>);

impl GenerationLocks {
    async fn acquire(&self, id: &str) -> OwnedMutexGuard<()> {
        let lock = {
            let mut locks = self.0.lock().unwrap_or_else(PoisonError::into_inner);
            locks.retain(|_, lock| lock.strong_count() != 0);
            if let Some(lock) = locks.get(id).and_then(Weak::upgrade) {
                lock
            } else {
                let lock = Arc::new(AsyncMutex::new(()));
                locks.insert(id.to_owned(), Arc::downgrade(&lock));
                lock
            }
        };
        lock.lock_owned().await
    }
}

struct Input {
    template_id: String,
    tag_id: String,
    selection: Option<Selection>,
    contacts: Option<Vec<String>>,
    show_location: Option<bool>,
}

/// Drafts and persists a synchronous résumé and cover-letter package.
pub async fn generate(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
    RawQuery(query): RawQuery,
    OptionalModelBody(body): OptionalModelBody,
) -> Result<Json<Value>, Response> {
    let pairs = validation::query_pairs(query.as_deref());
    let mut query = QueryReader::default();
    let show_location = validation::query_value(&pairs, "show_location")
        .map(|_| query.boolean(&pairs, "show_location", false));
    query.finish()?;
    let contacts: Vec<String> = pairs
        .iter()
        .filter(|(key, _)| key == "contact")
        .map(|(_, value)| value.clone())
        .collect();
    let mut reader = ModelReader::new(&body);
    let selection = reader.optional_model("selection", read_selection);
    reader.forbid_extra(&["selection"]);
    reader.finish()?;
    GENERATION.check()?;
    let input = Input {
        template_id: validation::query_value(&pairs, "template_id")
            .unwrap_or_default()
            .to_owned(),
        tag_id: validation::query_value(&pairs, "tag_id")
            .unwrap_or_default()
            .to_owned(),
        selection,
        contacts: (!contacts.is_empty()).then_some(contacts),
        show_location,
    };
    // Once accepted, finish the version write even if the HTTP client disconnects.
    tokio::spawn(generate_package(state, id, input))
        .await
        .map_err(|error| respond::internal_error("generate application", &error))?
        .map(Json)
}

fn read_selection(reader: &mut ModelReader<'_>) -> Selection {
    let selection = Selection {
        version: reader.integer("version", None, None).unwrap_or(1),
        sections: reader.string_list("sections", Some(16)).unwrap_or_default(),
        experience_order: reader
            .string_list("experience_order", Some(64))
            .unwrap_or_default(),
        experience_on: reader
            .string_list("experience_on", Some(64))
            .unwrap_or_default(),
        projects_order: reader
            .string_list("projects_order", Some(64))
            .unwrap_or_default(),
        projects_on: reader
            .string_list("projects_on", Some(64))
            .unwrap_or_default(),
        points_on: reader
            .string_list("points_on", Some(512))
            .unwrap_or_default(),
        skills_on: reader
            .string_list("skills_on", Some(256))
            .unwrap_or_default(),
    };
    reader.forbid_extra(&[
        "version",
        "sections",
        "experience_order",
        "experience_on",
        "projects_order",
        "projects_on",
        "points_on",
        "skills_on",
    ]);
    selection
}

async fn generate_package(
    state: Arc<AppState>,
    id: String,
    input: Input,
) -> Result<Value, Response> {
    let _lock = state.generation_locks.acquire(&id).await;
    let _profile = state.profile_writes.read().await;
    let job =
        new_job_id().map_err(|error| respond::internal_error("allocate generation job", &error))?;
    let database = state.database.clone();
    let store = state.leads.clone();
    let job_id = job.clone();
    let lead_id = id.clone();
    let lead = respond::blocking("allocate generation job", move || {
        database.create_generation_job(&job_id, &lead_id)?;
        store.by_id(&lead_id).map(|lead| lead.map(Value::Object))
    })
    .await?;
    let Some(lead) = lead else {
        agent_event(&state, "gen_error", format!("Lead {id} not found")).await;
        return Err(generation_error(&GenerationError::LeadNotFound));
    };
    // The retired corpus backend is left out of readiness and drafting.
    if let Some(blocker) = generation_readiness::blocker(&lead) {
        update_job(&state, &job, GenerationJobUpdate::Failed(blocker)).await;
        agent_event(&state, "gen_error", blocker.to_owned()).await;
        return Err(respond::detail(StatusCode::UNPROCESSABLE_ENTITY, blocker));
    }
    let template = load_template(&state, &input.template_id).await?;
    let resources = resources(&state, &input.tag_id, true).await?;
    let title = display(&lead["title"]);
    let company = display(&lead["company"]);
    agent_event(
        &state,
        "gen_start",
        format!("Generating for {title} @ {company}"),
    )
    .await;
    update_job(&state, &job, GenerationJobUpdate::Running).await;
    update_status(&state, &id, "tailoring", &lead).await;

    let output = state.paths.assets_dir();
    let result = generation::generate(GenerateRequest {
        job_id: &id,
        generation_job_id: &job,
        lead: Some(&lead),
        profile: &resources.profile,
        settings: &resources.settings,
        output_dir: &output,
        current_version: lead["resume_version"].as_u64().unwrap_or_default(),
        options: GenerateOptions {
            template: Some(&template),
            template_id: &input.template_id,
            cover_letter_base: &resources.cover_letter_base,
            scope: resources.scope(&input.tag_id),
            selection: input.selection.as_ref(),
            contact_line: ContactLine {
                allowed: input.contacts.as_deref(),
                show_location: input.show_location,
            },
            ..GenerateOptions::default()
        },
    })
    .await;
    let generated = match result {
        Ok(generated) => generated,
        Err(error) => return Err(fail(&state, &id, &job, &lead, error).await),
    };
    let enriched = generated.enriched_lead(&[]);
    let package = generated.package.clone();
    let version = generated.files.version;
    let store = state.leads.clone();
    let lead_id = id.clone();
    let persisted = tokio::task::spawn_blocking(move || {
        store.save_generated_assets(&lead_id, version, &enriched)?;
        let mut errors = Vec::new();
        if let Err(error) = store.save_generated_outreach(&lead_id, &package) {
            errors.push(format!("outreach fields: {error}"));
        }
        if let Err(error) = store.save_generated_contacts(&lead_id, &enriched["contact_lookup"]) {
            errors.push(format!("contact lookup: {error}"));
        }
        Ok::<_, agent_hire_store::LeadError>(errors)
    })
    .await;
    let errors = match persisted {
        Ok(Ok(errors)) => errors,
        other => {
            let detail = match other {
                Ok(Err(error)) => error.to_string(),
                Err(error) => error.to_string(),
                Ok(Ok(_)) => unreachable!("persistence succeeded"),
            };
            for file in [&generated.files.resume, &generated.files.cover_letter] {
                let _ = tokio::fs::remove_file(file).await;
            }
            return Err(fail(
                &state,
                &id,
                &job,
                &lead,
                GenerationError::Permanent(format!(
                    "generated assets could not be saved: {detail}"
                )),
            )
            .await);
        }
    };
    for error in &errors {
        log::warning("generation", error);
    }
    let enriched = generated.enriched_lead(&errors);
    state
        .hub
        .broadcast(json!({"type":"LEAD_UPDATED","data":enriched}))
        .await;
    agent_event(
        &state,
        "gen_done",
        format!("Resume and cover letter ready: {title}"),
    )
    .await;
    update_job(&state, &job, GenerationJobUpdate::Succeeded(&enriched)).await;
    Ok(generated.response(&errors))
}

/// Suggests résumé evidence without creating or modifying a generation job.
pub async fn suggest_selection(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
    RawQuery(query): RawQuery,
) -> Result<Json<Value>, Response> {
    GENERATION.check()?;
    let pairs = validation::query_pairs(query.as_deref());
    let tag = validation::query_value(&pairs, "tag_id").unwrap_or_default();
    let store = state.leads.clone();
    let lead_id = id.clone();
    let lead = respond::blocking("read lead for suggestions", move || store.by_id(&lead_id))
        .await?
        .map(Value::Object)
        .ok_or_else(|| generation_error(&GenerationError::LeadNotFound))?;
    // As in generation, readiness uses the stored posting after corpus retirement.
    if let Some(blocker) = generation_readiness::blocker(&lead) {
        return Err(respond::detail(StatusCode::UNPROCESSABLE_ENTITY, blocker));
    }
    let resources = resources(&state, tag, false).await?;
    let scope = resources.scope(tag);
    let result = generation::suggest_selection(SuggestRequest {
        job_id: &id,
        lead: Some(&lead),
        profile: &resources.profile,
        settings: &resources.settings,
        scope,
        corpus_detail: None,
        readiness_blocker: None,
    })
    .await
    .map_err(|error| generation_error(&error))?;
    let selected = &result["selection"];
    let mut rewrites = Vec::new();
    if let Some(drafts) = result["rewrites"]
        .as_array()
        .filter(|items| !items.is_empty())
    {
        let prepared = generation::prepare_profile(&resources.profile, scope, None);
        let parents = suggestion_parents(&prepared.profile);
        for draft in drafts.iter().take(6) {
            let Some(point) = draft["point_id"].as_str() else {
                continue;
            };
            if let Some((kind, parent)) = parents.get(point) {
                rewrites.push(json!({
                    "point_id":point,"parent_kind":kind,"parent_id":parent,
                    "suggested_text":draft["text"],"reason":draft["reason"].as_str().unwrap_or_default(),
                }));
            }
        }
    }
    Ok(Json(json!({
        "status":"ready","job_id":id,
        "skills_on":selected["skills_on"],"experience_on":selected["experience_on"],
        "projects_on":selected["projects_on"],"points_on":selected["points_on"],"suggestions":rewrites,
    })))
}

fn suggestion_parents(profile: &Value) -> HashMap<&str, (&str, &str)> {
    let mut map = HashMap::new();
    for (field, kind) in [("exp", "experience"), ("projects", "projects")] {
        for parent in profile[field].as_array().into_iter().flatten() {
            let Some(id) = parent["id"].as_str() else {
                continue;
            };
            for point in parent["points"].as_array().into_iter().flatten() {
                if let Some(point_id) = point["id"].as_str() {
                    map.insert(point_id, (kind, id));
                }
            }
        }
    }
    map
}

struct Resources {
    profile: Value,
    settings: Settings,
    scope: GenerationScope,
    cover_letter_base: String,
}

impl Resources {
    fn scope<'a>(&'a self, tag: &'a str) -> ProfileScope<'a> {
        ProfileScope {
            tag_id: tag,
            excluded_skills: &self.scope.skills,
            excluded_projects: &self.scope.projects,
            excluded_experiences: &self.scope.experiences,
            conflicts: &self.scope.conflicts,
        }
    }
}

async fn resources(
    state: &AppState,
    tag: &str,
    rewrite_letter: bool,
) -> Result<Resources, Response> {
    let store = state.profile.clone();
    let database = state.database.clone();
    let settings = state.settings.clone();
    let documents = state.documents.clone();
    let tag = tag.to_owned();
    respond::blocking("generation inputs", move || {
        let profile = Value::Object(store.get_profile()?);
        let settings = Settings::new(settings.all()?);
        let scope = database.generation_scope(&tag).unwrap_or_else(|error| {
            log::warning(
                "generation",
                &format!("load tag scope and conflicts: {error}"),
            );
            GenerationScope::default()
        });
        let cover_letter_base = if rewrite_letter && !tag.is_empty() {
            let latest = documents
                .list(Some("cover_letter"), Some(&tag))?
                .into_iter()
                .next();
            generation::resolve_cover_letter_base(&tag, latest.as_ref())
        } else {
            String::new()
        };
        Ok::<_, StoreError>(Resources {
            profile,
            settings,
            scope,
            cover_letter_base,
        })
    })
    .await
}

async fn load_template(state: &AppState, id: &str) -> Result<String, Response> {
    let database = state.database.clone();
    let settings = state.settings.clone();
    let id = id.to_owned();
    let requested = id.clone();
    let result = tokio::task::spawn_blocking(move || {
        let template = database.resume_template(&id);
        if id.is_empty() && !matches!(template, Ok(Some(_))) {
            settings.get("resume_template", "").map(Some)
        } else {
            template
        }
    })
    .await;
    match result {
        Ok(Ok(Some(template))) => Ok(template),
        _ if !requested.is_empty() => Err(generation_error(&GenerationError::Template(
            python_text::string_repr(&requested),
        ))),
        Ok(Err(error)) => Err(respond::internal_error("load résumé template", &error)),
        Err(error) => Err(respond::internal_error("load résumé template", &error)),
        Ok(Ok(None)) => Ok(String::new()),
    }
}

fn new_job_id() -> Result<String, getrandom::Error> {
    let mut bytes = [0; 6];
    getrandom::fill(&mut bytes)?;
    let id = bytes
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(format!("generate_package-{id}"))
}

async fn update_job(state: &AppState, id: &str, update: GenerationJobUpdate<'_>) {
    let database = state.database.clone();
    let id = id.to_owned();
    let result = match update {
        GenerationJobUpdate::Running => {
            tokio::task::spawn_blocking(move || {
                database.update_generation_job(&id, GenerationJobUpdate::Running)
            })
            .await
        }
        GenerationJobUpdate::Succeeded(lead) => {
            let lead = lead.clone();
            tokio::task::spawn_blocking(move || {
                database.update_generation_job(&id, GenerationJobUpdate::Succeeded(&lead))
            })
            .await
        }
        GenerationJobUpdate::Failed(error) => {
            let error = error.to_owned();
            tokio::task::spawn_blocking(move || {
                database.update_generation_job(&id, GenerationJobUpdate::Failed(&error))
            })
            .await
        }
    };
    match result {
        Ok(Ok(())) => {}
        Ok(Err(error)) => log::warning("generation", &format!("save job status: {error}")),
        Err(error) => log::warning("generation", &format!("save job status task: {error}")),
    }
}

async fn update_status(state: &AppState, id: &str, status: &'static str, lead: &Value) {
    let store = state.leads.clone();
    let id = id.to_owned();
    match tokio::task::spawn_blocking(move || store.update_status(&id, status)).await {
        Ok(Ok(())) => {
            let mut lead = lead.clone();
            lead["status"] = json!(status);
            state
                .hub
                .broadcast(json!({"type":"LEAD_UPDATED","data":lead}))
                .await;
        }
        Ok(Err(error)) => log::warning("generation", &format!("update lead status: {error}")),
        Err(error) => log::warning("generation", &format!("update lead status task: {error}")),
    }
}

async fn fail(
    state: &AppState,
    id: &str,
    job: &str,
    lead: &Value,
    error: GenerationError,
) -> Response {
    let message = error.to_string();
    update_job(state, job, GenerationJobUpdate::Failed(&message)).await;
    if let Some(failed) = error.failed_lead(lead) {
        let status = if error.retry_after().is_some() {
            "tailoring"
        } else {
            "discovered"
        };
        update_status(state, id, status, &failed).await;
    }
    agent_event(
        state,
        "gen_error",
        format!(
            "Generation failed for {}: {message}",
            display(&lead["title"])
        ),
    )
    .await;
    generation_error(&error)
}

async fn agent_event(state: &AppState, event: &str, message: String) {
    state
        .hub
        .broadcast(json!({"type":"agent","event":event,"msg":message}))
        .await;
}

fn generation_error(error: &GenerationError) -> Response {
    let status = StatusCode::from_u16(error.status()).expect("generation HTTP status is valid");
    let mut response = (status, Json(error.body())).into_response();
    if let Some(seconds) = error.retry_after() {
        response
            .headers_mut()
            .insert(header::RETRY_AFTER, HeaderValue::from(seconds));
    }
    response
}

fn display(value: &Value) -> &str {
    value
        .as_str()
        .filter(|text| !text.is_empty())
        .unwrap_or("?")
}
