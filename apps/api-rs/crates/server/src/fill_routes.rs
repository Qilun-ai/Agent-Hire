//! The user's application data files (true facts and default answers) used to fill forms.
//!
//! Both are JSON in the settings store. Facts are things only the user can state truthfully;
//! defaults are reusable answers for jobs without a job-specific data file.

use std::sync::Arc;

use agent_hire_store::{SettingsStore, python_json_dumps};
use axum::Json;
use axum::body::Bytes;
use axum::extract::State;
use axum::response::{IntoResponse, Response};
use serde_json::{Map, Value, json};

use crate::app_state::AppState;
use crate::profile_source;
use crate::respond;
use crate::validation;

const FACTS_KEY: &str = "application_facts";
const DEFAULTS_KEY: &str = "application_defaults";
const PREFER_NOT: &str = "I don't wish to answer";
const YES_NO: &[&str] = &["Yes", "No"];

/// One saved value: its key, label, what the decision model reads, its group, default and the
/// answers the UI suggests.
pub struct Field {
    pub key: &'static str,
    pub label: &'static str,
    pub description: &'static str,
    pub group: &'static str,
    pub default: &'static str,
    pub options: &'static [&'static str],
}

const fn field(
    key: &'static str,
    label: &'static str,
    description: &'static str,
    group: &'static str,
) -> Field {
    Field {
        key,
        label,
        description,
        group,
        default: "",
        options: &[],
    }
}

const fn with(mut base: Field, default: &'static str, options: &'static [&'static str]) -> Field {
    base.default = default;
    base.options = options;
    base
}

pub const FACT_FIELDS: &[Field] = &[
    field(
        "full_name",
        "Full name",
        "the candidate's full legal name",
        "Identity",
    ),
    field(
        "first_name",
        "First name",
        "the candidate's first (given) name",
        "Identity",
    ),
    field(
        "last_name",
        "Last name",
        "the candidate's last name (surname, family name)",
        "Identity",
    ),
    field(
        "preferred_name",
        "Preferred name",
        "the name the candidate prefers to be called",
        "Identity",
    ),
    with(
        field(
            "pronouns",
            "Pronouns",
            "the candidate's pronouns",
            "Identity",
        ),
        PREFER_NOT,
        &[],
    ),
    field("email", "Email", "the candidate's email address", "Contact"),
    field(
        "phone",
        "Phone",
        "the candidate's phone number (with country code)",
        "Contact",
    ),
    field(
        "phone_country_code",
        "Phone country code",
        "the dialling country code of the candidate's phone, like +91",
        "Contact",
    ),
    field(
        "address_line1",
        "Street address",
        "street address line of the candidate's home address",
        "Address",
    ),
    field("city", "City", "the city the candidate lives in", "Address"),
    field(
        "state",
        "State / province",
        "the state, province or region the candidate lives in",
        "Address",
    ),
    field(
        "postal_code",
        "Postal code",
        "the postal code / ZIP code of the candidate's address",
        "Address",
    ),
    field(
        "country",
        "Country",
        "the country the candidate lives in",
        "Address",
    ),
    field(
        "linkedin_url",
        "LinkedIn",
        "the candidate's LinkedIn profile URL",
        "Links",
    ),
    field(
        "github_url",
        "GitHub",
        "the candidate's GitHub profile URL",
        "Links",
    ),
    field(
        "portfolio_url",
        "Website / portfolio",
        "the candidate's personal website or portfolio URL",
        "Links",
    ),
    field(
        "citizenship",
        "Citizenship",
        "the country (or countries) the candidate is a citizen of",
        "Eligibility",
    ),
    field(
        "country_of_residence",
        "Country of residence",
        "the country where the candidate legally resides",
        "Eligibility",
    ),
    with(
        field(
            "work_authorized",
            "Authorised to work?",
            "whether the candidate is legally authorised to work in the job's country (yes/no)",
            "Eligibility",
        ),
        "",
        YES_NO,
    ),
    with(
        field(
            "needs_sponsorship",
            "Needs visa sponsorship?",
            "whether the candidate now or in future requires visa sponsorship to work (yes/no)",
            "Eligibility",
        ),
        "",
        YES_NO,
    ),
    field(
        "visa_status",
        "Visa / permit status",
        "the candidate's current visa or work-permit status",
        "Eligibility",
    ),
    with(
        field(
            "over_18",
            "18 or older?",
            "whether the candidate is at least 18 years old (yes/no)",
            "Eligibility",
        ),
        "Yes",
        YES_NO,
    ),
    with(
        field(
            "willing_to_relocate",
            "Willing to relocate?",
            "whether the candidate is willing to relocate for the job (yes/no)",
            "Availability",
        ),
        "",
        YES_NO,
    ),
    field(
        "notice_period",
        "Notice period",
        "how much notice the candidate must give a current employer, or how soon they can join",
        "Availability",
    ),
    field(
        "earliest_start_date",
        "Earliest start date",
        "the earliest date the candidate can start",
        "Availability",
    ),
    field(
        "salary_expectation",
        "Salary expectation",
        "the candidate's expected salary or compensation",
        "Availability",
    ),
    field(
        "current_ctc",
        "Current salary",
        "the candidate's current salary / CTC",
        "Availability",
    ),
    with(
        field(
            "relative_at_company",
            "Relatives at the company?",
            "whether the candidate has a relative or close relation working at the hiring company (yes/no, and who)",
            "Relationships",
        ),
        "No",
        YES_NO,
    ),
    with(
        field(
            "previously_employed_here",
            "Worked here before?",
            "whether the candidate has previously worked for the hiring company (yes/no)",
            "Relationships",
        ),
        "No",
        YES_NO,
    ),
    field(
        "referral",
        "Referred by",
        "the name of the employee who referred the candidate, if any",
        "Relationships",
    ),
    with(
        field(
            "how_heard",
            "How did you hear about us?",
            "how the candidate heard about the job (source)",
            "Relationships",
        ),
        "Company careers website",
        &[],
    ),
    with(
        field(
            "veteran_status",
            "Veteran status",
            "the candidate's protected-veteran or military-veteran status",
            "Military & EEO",
        ),
        PREFER_NOT,
        &[],
    ),
    with(
        field(
            "military_service",
            "Military service",
            "whether the candidate has served in the military, and details",
            "Military & EEO",
        ),
        "No",
        &[],
    ),
    with(
        field(
            "gender",
            "Gender",
            "the candidate's gender (voluntary self-identification)",
            "Military & EEO",
        ),
        PREFER_NOT,
        &[],
    ),
    with(
        field(
            "race_ethnicity",
            "Race / ethnicity",
            "the candidate's race or ethnicity (voluntary self-identification)",
            "Military & EEO",
        ),
        PREFER_NOT,
        &[],
    ),
    with(
        field(
            "disability_status",
            "Disability",
            "whether the candidate has a disability (voluntary self-identification)",
            "Military & EEO",
        ),
        PREFER_NOT,
        &[],
    ),
    with(
        field(
            "sexual_orientation",
            "Sexual orientation",
            "the candidate's sexual orientation (voluntary self-identification)",
            "Military & EEO",
        ),
        PREFER_NOT,
        &[],
    ),
    field(
        "languages",
        "Languages",
        "languages the candidate speaks and how well",
        "Other",
    ),
    field(
        "highest_education",
        "Highest education",
        "the candidate's highest completed degree and field",
        "Other",
    ),
];

pub const DEFAULT_FIELDS: &[Field] = &[
    field(
        "total_years_experience",
        "Total years of experience",
        "the candidate's total years of professional experience (a number)",
        "Default answers",
    ),
    field(
        "current_title",
        "Current / latest job title",
        "the candidate's current or most recent job title",
        "Default answers",
    ),
    field(
        "current_company",
        "Current / latest employer",
        "the candidate's current or most recent employer",
        "Default answers",
    ),
    field(
        "summary",
        "Short summary",
        "a short professional summary of the candidate (2-3 sentences)",
        "Default answers",
    ),
    field(
        "why_interested",
        "Why this role (default)",
        "a general answer to why the candidate is interested in this kind of role",
        "Default answers",
    ),
];

fn load(settings: &SettingsStore, key: &str) -> Map<String, Value> {
    let raw = settings.get(key, "").unwrap_or_default();
    let raw = if raw.is_empty() { "{}".to_owned() } else { raw };
    let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&raw) else {
        return Map::new();
    };
    map.into_iter()
        .filter(|(_, value)| !value.is_null())
        .map(|(key, value)| {
            let text = match value {
                Value::String(text) => text,
                other => crate::python_text::str_of(&other),
            };
            (key, Value::String(text))
        })
        .collect()
}

/// What the résumé already knows, to prefill facts the user has not saved.
fn seed_from_profile(profile: &Map<String, Value>) -> Map<String, Value> {
    let empty = Map::new();
    let identity = profile
        .get("identity")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let pick = |keys: &[&str]| -> String {
        for key in keys {
            for value in [identity.get(*key), profile.get(*key)]
                .into_iter()
                .flatten()
            {
                let text = match value {
                    Value::String(text) => text.trim().to_owned(),
                    Value::Null | Value::Bool(false) => String::new(),
                    other => other.to_string(),
                };
                if !text.is_empty() {
                    return text;
                }
            }
        }
        String::new()
    };
    let name = pick(&["name", "n"]);
    let (first, last) = name.split_once(' ').unwrap_or((name.as_str(), ""));
    let last_word = last.rsplit(' ').next().unwrap_or("");
    let mut out = Map::new();
    for (key, value) in [
        ("full_name", name.clone()),
        ("first_name", first.to_owned()),
        ("last_name", last_word.to_owned()),
        ("email", pick(&["email"])),
        ("phone", pick(&["phone"])),
        ("city", pick(&["city"])),
        ("linkedin_url", pick(&["linkedin_url"])),
        ("github_url", pick(&["github_url"])),
        ("portfolio_url", pick(&["website_url"])),
        ("citizenship", pick(&["citizenship"])),
    ] {
        if !value.is_empty() {
            out.insert(key.to_owned(), Value::String(value));
        }
    }
    out
}

/// Saved facts, else what the profile knows, else the field default.
pub fn facts(settings: &SettingsStore, profile: &Map<String, Value>) -> Map<String, Value> {
    let saved = load(settings, FACTS_KEY);
    let seeded = seed_from_profile(profile);
    FACT_FIELDS
        .iter()
        .map(|field| {
            let value = match saved.get(field.key) {
                Some(value) => value.clone(),
                None => seeded
                    .get(field.key)
                    .cloned()
                    .unwrap_or_else(|| Value::String(field.default.to_owned())),
            };
            (field.key.to_owned(), value)
        })
        .collect()
}

/// Saved default answers, else the field default.
pub fn defaults(settings: &SettingsStore) -> Map<String, Value> {
    let saved = load(settings, DEFAULTS_KEY);
    DEFAULT_FIELDS
        .iter()
        .map(|field| {
            (
                field.key.to_owned(),
                saved
                    .get(field.key)
                    .cloned()
                    .unwrap_or_else(|| json!(field.default)),
            )
        })
        .collect()
}

fn schema() -> Value {
    let describe = |field: &Field| json!({"key": field.key, "label": field.label, "group": field.group, "options": field.options, "default": field.default});
    json!({
        "facts": FACT_FIELDS.iter().map(describe).collect::<Vec<_>>(),
        "defaults": DEFAULT_FIELDS.iter().map(describe).collect::<Vec<_>>(),
    })
}

/// `GET /api/v1/fill/facts`: the field list, facts and default answers.
pub async fn get_facts(State(state): State<Arc<AppState>>) -> Response {
    let profile = profile_source::profile(&state).await;
    let store = state.settings.clone();
    match tokio::task::spawn_blocking(move || (facts(&store, &profile), defaults(&store))).await {
        Ok((facts, defaults)) => {
            Json(json!({ "schema": schema(), "facts": facts, "defaults": defaults }))
                .into_response()
        }
        Err(error) => respond::internal_error("fill facts", &error),
    }
}

/// A `dict[str, str] | None` body field, or the Pydantic errors for it.
fn string_map(
    body: &Map<String, Value>,
    name: &str,
    errors: &mut validation::Errors,
) -> Option<Map<String, Value>> {
    let value = body.get(name)?;
    if value.is_null() {
        return None;
    }
    let Some(map) = value.as_object() else {
        errors.push(
            "dict_type",
            &[json!("body"), json!(name)],
            "Input should be a valid dictionary",
            value,
            None,
        );
        return None;
    };
    let mut ok = true;
    for (key, item) in map {
        if !item.is_string() {
            ok = false;
            errors.push(
                "string_type",
                &[json!("body"), json!(name), json!(key)],
                "Input should be a valid string",
                item,
                None,
            );
        }
    }
    ok.then(|| map.clone())
}

/// `PUT /api/v1/fill/facts` with `{"facts": {...}, "defaults": {...}}` (either may be omitted).
pub async fn put_facts(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut errors = validation::Errors::default();
    let facts = string_map(&body, "facts", &mut errors);
    let defaults = string_map(&body, "defaults", &mut errors);
    if let Some(response) = errors.into_response() {
        return response;
    }
    let mut payload = Vec::new();
    let collect = |fields: &[Field], given: &Map<String, Value>| -> Value {
        let mut out = Map::new();
        for field in fields {
            let text = given
                .get(field.key)
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_owned();
            out.insert(field.key.to_owned(), Value::String(text));
        }
        Value::Object(out)
    };
    if let Some(facts) = facts {
        payload.push((
            FACTS_KEY.to_owned(),
            python_json_dumps(&collect(FACT_FIELDS, &facts), false),
        ));
    }
    if let Some(defaults) = defaults {
        payload.push((
            DEFAULTS_KEY.to_owned(),
            python_json_dumps(&collect(DEFAULT_FIELDS, &defaults), false),
        ));
    }
    if !payload.is_empty() {
        let store = state.settings.clone();
        if let Err(response) =
            respond::blocking("save fill facts", move || store.save(&payload)).await
        {
            return response;
        }
    }
    Json(json!({ "ok": true })).into_response()
}
