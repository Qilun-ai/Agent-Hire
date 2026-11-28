//! Synchronous-route generation and its persistence payload (§2.2, §3, §11).

use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use agent_hire_llm::Settings;
use regex::Regex;
use serde_json::{Value, json};
use thiserror::Error;

use crate::assets::AssetFiles;
use crate::fallback::{ContactLine, Draft};
use crate::selection::{ProfileScope, Selection};
use crate::{assets, contacts, drafting, fallback, fields, keywords, selection};

const FAILURE_DETAIL: &str = "Generation failed. See the activity log for details.";

static URL: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)(?:https?://|www\.)\S+").expect("valid lead URL pattern"));

/// Data and options resolved by the server for `POST /leads/{job_id}/generate`.
pub struct GenerateRequest<'a> {
    /// The ID from the route.
    pub job_id: &'a str,
    /// The generation job ID allocated by the caller.
    pub generation_job_id: &'a str,
    /// The lead loaded by ID, if present.
    pub lead: Option<&'a Value>,
    /// The master profile snapshot.
    pub profile: &'a Value,
    /// The current settings snapshot.
    pub settings: &'a Settings,
    /// The directory receiving the PDF files.
    pub output_dir: &'a Path,
    /// The stored résumé version for the lead.
    pub current_version: u64,
    /// The scoped draft inputs and optional preflight data.
    pub options: GenerateOptions<'a>,
}

/// Optional scope, selection, template, and contact inputs.
#[derive(Clone, Copy, Debug, Default)]
pub struct GenerateOptions<'a> {
    /// Template ID from the explicit route query, if supplied.
    pub template_id: &'a str,
    /// Resolved template, including an explicit empty template. Absent means resolution failed.
    pub template: Option<&'a str>,
    /// The tag-scoped rewrite base already resolved by the caller.
    pub cover_letter_base: &'a str,
    /// Profile point scope and saved conflicts.
    pub scope: ProfileScope<'a>,
    /// User selection, when present.
    pub selection: Option<&'a Selection>,
    /// Whether to show contact fields and location on the résumé.
    pub contact_line: ContactLine<'a>,
    /// Corpus detail already fetched by the server when hydration is eligible.
    pub corpus_detail: Option<&'a Value>,
    /// Exact failure from the app's generation-readiness gate.
    pub readiness_blocker: Option<&'a str>,
}

/// A route-level failure with the public status and the internal error detail.
#[derive(Debug, Error)]
#[non_exhaustive]
pub enum GenerationError {
    /// The requested lead is absent.
    #[error("Lead not found")]
    LeadNotFound,
    /// The lead failed the app's readiness gate.
    #[error("{0}")]
    Blocked(String),
    /// An explicitly requested résumé template failed to resolve.
    #[error("Resume template {0} could not be loaded")]
    Template(String),
    /// A selection exceeds a bound in §2.1.
    #[error("selection.{0} has too many items")]
    InvalidSelection(&'static str),
    /// The selection suggestion provider failed.
    #[error("AI selection failed: {0}")]
    Suggestion(String),
    /// A temporary provider or connection failure.
    #[error("{0}")]
    Transient(String),
    /// Generation failed permanently.
    #[error("{0}")]
    Permanent(String),
}

impl GenerationError {
    /// The HTTP status the server should use for this failure.
    #[must_use]
    pub fn status(&self) -> u16 {
        match self {
            Self::LeadNotFound => 404,
            Self::Blocked(_) | Self::Template(_) | Self::InvalidSelection(_) => 422,
            Self::Suggestion(_) => 502,
            Self::Transient(_) => 503,
            Self::Permanent(_) => 500,
        }
    }

    /// Seconds for the `Retry-After` header, when specified.
    #[must_use]
    pub fn retry_after(&self) -> Option<u32> {
        matches!(self, Self::Transient(_)).then_some(30)
    }

    /// Public body for the synchronous route.
    #[must_use]
    pub fn body(&self) -> Value {
        let detail = if matches!(self, Self::Transient(_) | Self::Permanent(_)) {
            FAILURE_DETAIL.to_owned()
        } else {
            self.to_string()
        };
        json!({"detail":detail})
    }

    /// Builds the failure lead to persist and broadcast for a runtime failure (§3.7).
    #[must_use]
    pub fn failed_lead(&self, lead: &Value) -> Option<Value> {
        let mut lead = Value::Object(lead.as_object()?.clone());
        let status = match self {
            Self::Transient(_) => "tailoring",
            Self::Permanent(_) => "discovered",
            _ => return None,
        };
        let mut meta = Value::Object(lead["source_meta"].as_object().cloned().unwrap_or_default());
        meta["generation_error"] = json!(self.to_string());
        if let Some(seconds) = self.retry_after() {
            meta["retry_after"] = json!(seconds);
        } else if let Some(meta) = meta.as_object_mut() {
            meta.remove("retry_after");
        }
        lead["source_meta"] = meta;
        lead["status"] = json!(status);
        Some(lead)
    }
}

/// PDF paths and metadata to persist before sending the ready response.
#[derive(Debug)]
pub struct Generated {
    /// Rendered version and paths for the generated asset write.
    pub files: AssetFiles,
    /// The package dictionary from §11.1.
    pub package: Value,
    /// Contact lookup or null when disabled.
    pub contact_lookup: Value,
    /// Whether an eligible corpus description replaced the lead's description.
    pub description_hydrated: bool,
    /// Stale selection IDs discarded by preparation.
    pub stale_selection_ids: usize,
    lead: Value,
    job_id: String,
    generation_job_id: String,
}

impl Generated {
    /// The enriched lead used for persistence, broadcasts, and the job's result (§3.5–3.6).
    #[must_use]
    pub fn enriched_lead(&self, persistence_errors: &[String]) -> Value {
        let mut lead = self.lead.clone();
        lead["asset"] = self.package["resume"].clone();
        lead["resume_asset"] = self.package["resume"].clone();
        lead["cover_letter_asset"] = self.package["cover_letter"].clone();
        lead["selected_projects"] = self.package["selected_projects"].clone();
        lead["keyword_coverage"] = self.package["keyword_coverage"].clone();
        for (field, source) in [
            ("outreach_reply", "founder_message"),
            ("outreach_dm", "linkedin_note"),
            ("outreach_email", "cold_email"),
        ] {
            let value = self
                .package
                .get(source)
                .cloned()
                .unwrap_or_else(|| json!(fields::string(&lead, &[field])));
            lead[field] = value;
        }
        let contact = if self.contact_lookup.is_object() {
            self.contact_lookup.clone()
        } else {
            json!({})
        };
        lead["status"] = json!("approved");
        let mut meta = Value::Object(lead["source_meta"].as_object().cloned().unwrap_or_default());
        meta["contact_lookup"] = contact.clone();
        if !persistence_errors.is_empty() {
            meta["generation_persistence_errors"] = json!(persistence_errors);
        }
        lead["source_meta"] = meta;
        lead["contact_lookup"] = contact;
        lead
    }

    /// Builds the success body after the critical asset-package write succeeds (§2.2).
    #[must_use]
    pub fn response(&self, persistence_errors: &[String]) -> Value {
        let mut lead = self.enriched_lead(persistence_errors);
        lead["generation_job_id"] = json!(self.generation_job_id);
        json!({"status":"ready","job_id":self.job_id,"lead":lead,"generation_job_id":self.generation_job_id})
    }
}

/// Generates the two PDFs and post-draft metadata for the synchronous generate route.
///
/// The server owns repository reads and writes, activity events, status updates, and job allocation.
pub async fn generate(request: GenerateRequest<'_>) -> Result<Generated, GenerationError> {
    let options = request.options;
    let (lead, description_hydrated) = ready_lead(
        request.lead,
        options.corpus_detail,
        options.readiness_blocker,
    )?;
    let template = match options.template {
        Some(template) => template,
        None if !options.template_id.is_empty() => {
            return Err(GenerationError::Template(repr(options.template_id)));
        }
        None => request.settings.get("resume_template"),
    };
    if let Some(field) = options.selection.and_then(Selection::invalid_field) {
        return Err(GenerationError::InvalidSelection(field));
    }
    let prepared = selection::prepare_profile(request.profile, options.scope, options.selection);
    let profile = &prepared.profile;
    let terms = keywords::candidates(&lead);
    let deterministic = || {
        fallback::build(
            &lead,
            profile,
            options.selection,
            options.contact_line,
            &terms,
        )
    };
    let fallback = deterministic();
    let configured = drafting::configured(request.settings);
    if configured && let Some(provider) = drafting::missing_key(request.settings) {
        return Err(GenerationError::Permanent(format!(
            "Missing API key for {provider}"
        )));
    }
    let reply = if configured {
        match drafting::draft(drafting::DraftInput {
            profile,
            lead: &lead,
            settings: request.settings,
            terms: &terms,
            binding: prepared.verbatim.then_some(fallback.resume.as_str()),
            template,
            cover_letter_base: if options.scope.tag_id.is_empty() {
                ""
            } else {
                options.cover_letter_base
            },
            contact: options.contact_line,
        })
        .await
        {
            Ok(response) => response,
            Err(error) if drafting::transient(&error) => {
                return Err(GenerationError::Transient(error.to_string()));
            }
            Err(error) => {
                eprintln!("application drafting failed, using the deterministic package: {error}");
                Value::Null
            }
        }
    } else {
        Value::Null
    };
    let mut draft = drafting::normalize(&reply, fallback, &lead, prepared.verbatim);
    let id = fields::text(fields::first(&lead, &["job_id", "id"])).into_owned();
    let version = request
        .current_version
        .checked_add(1)
        .ok_or_else(|| GenerationError::Permanent("résumé version overflow".to_owned()))?;
    let (files, reply) =
        match write_async(request.output_dir, &id, version, &draft, prepared.verbatim).await {
            Ok(files) => (files, reply),
            Err(error) => {
                eprintln!("PDF render failed, retrying with the deterministic package: {error}");
                draft = deterministic();
                let files =
                    write_async(request.output_dir, &id, version, &draft, prepared.verbatim)
                        .await
                        .map_err(|error| {
                            GenerationError::Permanent(format!("PDF render failed: {error}"))
                        })?;
                (files, Value::Null)
            }
        };
    let verified = keywords::verify(&terms, &reply);
    let coverage = keywords::coverage(
        profile,
        &verified.terms,
        &draft.resume,
        Some(verified.verification),
    );
    let package = json!({
        "resume":files.resume,"cover_letter":files.cover_letter,"selected_projects":draft.projects,
        "founder_message":draft.founder,"linkedin_note":draft.linkedin,"cold_email":draft.email,
        "keyword_coverage":coverage,"render_ratio":files.render_ratio,
    });
    let contact_lookup = if contacts::disabled(request.settings) {
        Value::Null
    } else {
        contacts::lookup_contacts(&lead, profile, request.settings).await
    };
    Ok(Generated {
        files,
        package,
        contact_lookup,
        description_hydrated,
        stale_selection_ids: prepared.stale_ids,
        lead,
        job_id: request.job_id.to_owned(),
        generation_job_id: request.generation_job_id.to_owned(),
    })
}

async fn write_async(
    output: &Path,
    id: &str,
    version: u64,
    draft: &Draft,
    verbatim: bool,
) -> std::io::Result<AssetFiles> {
    let output = PathBuf::from(output);
    let id = id.to_owned();
    let draft = draft.clone();
    tokio::task::spawn_blocking(move || assets::write(&output, &id, version, &draft, verbatim))
        .await
        .map_err(std::io::Error::other)?
}

/// Returns the corpus ID when a lead meets the thin-description hydration gate (§3.1).
#[must_use]
pub fn corpus_hydration_id(lead: &Value) -> Option<&Value> {
    let meta = lead.get("source_meta")?.as_object()?;
    let id = meta.get("corpus_job_id").filter(|id| fields::truthy(id))?;
    if meta.get("description_hydrated").is_some_and(fields::truthy) {
        return None;
    }
    (url_free_length(fields::string(lead, &["description"])) < 200).then_some(id)
}

/// Returns the lead and whether a longer corpus description was applied (§3.1).
#[must_use]
pub fn hydrate_description(lead: &Value, detail: Option<&Value>) -> (Value, bool) {
    let mut lead = lead.clone();
    if corpus_hydration_id(&lead).is_none() {
        return (lead, false);
    }
    let Some(detail) = detail else {
        return (lead, false);
    };
    let description = fields::string(detail, &["description_md", "description"]).trim();
    if url_free_length(description) <= url_free_length(fields::string(&lead, &["description"])) {
        return (lead, false);
    }
    lead["description"] = json!(description);
    lead["source_meta"]["description_hydrated"] = json!(true);
    (lead, true)
}

fn url_free_length(text: &str) -> usize {
    URL.replace_all(text, "").trim().chars().count()
}

pub(crate) fn ready_lead(
    lead: Option<&Value>,
    detail: Option<&Value>,
    blocker: Option<&str>,
) -> Result<(Value, bool), GenerationError> {
    let lead = lead
        .filter(|lead| lead.is_object())
        .ok_or(GenerationError::LeadNotFound)?;
    let hydrated = hydrate_description(lead, detail);
    if let Some(blocker) = blocker.filter(|blocker| !blocker.is_empty()) {
        return Err(GenerationError::Blocked(blocker.to_owned()));
    }
    Ok(hydrated)
}

fn repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut output = String::from(quote);
    for character in text.chars() {
        match character {
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            value if value == quote => {
                output.push('\\');
                output.push(value);
            }
            value => output.push(value),
        }
    }
    output.push(quote);
    output
}
