//! Email monitoring: connect the inbox you apply from, test it, and poll it.
//!
//! Credentials are write-only over this API: `status` says whether a password is set but never
//! returns it. A poll is idempotent (every processed message is remembered), never worsens a known
//! outcome, and leaves ambiguous mail alone.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use agent_hire_email::{self as email, MailboxConfig, Message, Outcome};
use agent_hire_llm::{LlmRequest, OutputSchema, Settings};
use agent_hire_store::{LeadStore, SettingsStore, python_json_dumps};
use axum::Json;
use axum::body::Bytes;
use axum::extract::{RawQuery, State};
use axum::response::{IntoResponse, Response};
use serde_json::{Map, Value, json};

use crate::app_state::AppState;
use crate::cart_routes;
use crate::log;
use crate::respond;
use crate::validation::{self, ModelReader, QueryReader};

const KEY_ENABLED: &str = "email_monitor_enabled";
const KEY_HOST: &str = "email_monitor_host";
const KEY_PORT: &str = "email_monitor_port";
const KEY_USERNAME: &str = "email_monitor_username";
const KEY_PASSWORD: &str = "email_monitor_password";
const KEY_MAILBOX: &str = "email_monitor_mailbox";
const KEY_SEEN_UIDS: &str = "email_monitor_seen_uids";
const KEY_MODE: &str = "email_monitor_mode";
const MODE_RULES: &str = "rules";
const MODE_RULES_AND_MODEL: &str = "rules_and_model";
/// Model calls per poll; a backlog is worked through over several polls.
const MAX_MODEL_CALLS_PER_POLL: usize = 25;
/// Processed message ids remembered (bounded so the setting cannot grow without limit).
const MAX_SEEN: usize = 2000;
const TERMINAL_STATUSES: [&str; 3] = ["accepted", "rejected", "discarded"];
const PRE_APPLY_STATUSES: [&str; 4] = ["discovered", "evaluating", "tailoring", "approved"];
const NOT_CONFIGURED: &str = "Email monitoring isn't configured yet.";

type SettingsMap = HashMap<String, String>;

fn settings_map(store: &SettingsStore) -> Result<SettingsMap, agent_hire_store::StoreError> {
    Ok(store.all()?.into_iter().collect())
}

fn get<'a>(settings: &'a SettingsMap, key: &str) -> &'a str {
    settings.get(key).map_or("", String::as_str)
}

/// The mailbox settings, or `None` until host, username and password are all set.
fn load_config(settings: &SettingsMap) -> Option<MailboxConfig> {
    let host = get(settings, KEY_HOST).trim();
    let username = get(settings, KEY_USERNAME).trim();
    let password = get(settings, KEY_PASSWORD);
    if host.is_empty() || username.is_empty() || password.is_empty() {
        return None;
    }
    let port = get(settings, KEY_PORT)
        .trim()
        .parse::<u16>()
        .unwrap_or(email::DEFAULT_PORT);
    let mailbox = get(settings, KEY_MAILBOX).trim();
    Some(MailboxConfig {
        host: host.to_owned(),
        username: username.to_owned(),
        password: password.to_owned(),
        port,
        mailbox: if mailbox.is_empty() {
            "INBOX".to_owned()
        } else {
            mailbox.to_owned()
        },
    })
}

fn load_seen(settings: &SettingsMap) -> Vec<String> {
    let raw = get(settings, KEY_SEEN_UIDS);
    let raw = if raw.is_empty() { "[]" } else { raw };
    match serde_json::from_str::<Value>(raw) {
        Ok(Value::Array(items)) => items
            .into_iter()
            .map(|item| match item {
                Value::String(text) => text,
                other => other.to_string(),
            })
            .collect(),
        _ => Vec::new(),
    }
}

fn mode(settings: &SettingsMap) -> &'static str {
    match get(settings, KEY_MODE) {
        MODE_RULES => MODE_RULES,
        _ => MODE_RULES_AND_MODEL,
    }
}

fn enabled(settings: &SettingsMap) -> bool {
    settings
        .get(KEY_ENABLED)
        .map_or("false", String::as_str)
        .to_lowercase()
        == "true"
}

fn status_of(settings: &SettingsMap) -> Value {
    let config = load_config(settings);
    let processed: HashSet<String> = load_seen(settings).into_iter().collect();
    json!({
        "configured": config.is_some(),
        "enabled": enabled(settings),
        "mailbox": config.map(|config| config.redacted()),
        "processed_count": processed.len(),
        "mode": mode(settings),
    })
}

async fn status_response(state: &AppState) -> Response {
    let store = state.settings.clone();
    match respond::blocking("email status", move || settings_map(&store)).await {
        Ok(settings) => Json(status_of(&settings)).into_response(),
        Err(response) => response,
    }
}

/// `GET /api/v1/email/status`.
pub async fn status(State(state): State<Arc<AppState>>) -> Response {
    status_response(&state).await
}

/// `POST /api/v1/email/connect`: saves the mailbox settings. An omitted password keeps the
/// stored one, so changing the port or folder does not mean retyping an app password.
pub async fn connect(State(state): State<Arc<AppState>>, body: Bytes) -> Response {
    let body = match validation::object_body(&body) {
        Ok(body) => body,
        Err(response) => return response,
    };
    let mut reader = ModelReader::new(&body);
    let host = reader.string("host", None, true);
    let username = reader.string("username", None, true);
    let password = reader.optional_string("password");
    let port = reader.integer("port", None, None).unwrap_or(993);
    let mailbox = reader
        .string("mailbox", None, false)
        .unwrap_or_else(|| "INBOX".to_owned());
    let enabled = reader.boolean("enabled").unwrap_or(true);
    let mode = reader.optional_one_of("mode", &[MODE_RULES, MODE_RULES_AND_MODEL]);
    if let Err(response) = reader.finish() {
        return response;
    }
    let mailbox = mailbox.trim();
    let mut values = vec![
        (
            KEY_HOST.to_owned(),
            host.unwrap_or_default().trim().to_owned(),
        ),
        (
            KEY_USERNAME.to_owned(),
            username.unwrap_or_default().trim().to_owned(),
        ),
        (KEY_PORT.to_owned(), port.to_string()),
        (
            KEY_MAILBOX.to_owned(),
            if mailbox.is_empty() {
                "INBOX".to_owned()
            } else {
                mailbox.to_owned()
            },
        ),
        (
            KEY_ENABLED.to_owned(),
            if enabled { "true" } else { "false" }.to_owned(),
        ),
    ];
    if let Some(mode) = mode {
        values.push((KEY_MODE.to_owned(), mode));
    }
    if let Some(password) = password.filter(|password| !password.is_empty()) {
        values.push((KEY_PASSWORD.to_owned(), password));
    }
    let store = state.settings.clone();
    if let Err(response) =
        respond::blocking("save email settings", move || store.save(&values)).await
    {
        return response;
    }
    status_response(&state).await
}

/// `POST /api/v1/email/disconnect`: turns monitoring off and clears the stored password.
pub async fn disconnect(State(state): State<Arc<AppState>>) -> Response {
    let store = state.settings.clone();
    let values = vec![
        (KEY_ENABLED.to_owned(), "false".to_owned()),
        (KEY_PASSWORD.to_owned(), String::new()),
    ];
    if let Err(response) =
        respond::blocking("save email settings", move || store.save(&values)).await
    {
        return response;
    }
    status_response(&state).await
}

/// `POST /api/v1/email/check`: tests the connection without polling.
pub async fn check(State(state): State<Arc<AppState>>) -> Response {
    let store = state.settings.clone();
    let result = tokio::task::spawn_blocking(move || {
        let settings = settings_map(&store).map_err(|error| error.to_string())?;
        let Some(config) = load_config(&settings) else {
            return Ok(json!({ "ok": false, "error": NOT_CONFIGURED }));
        };
        Ok::<_, String>(match email::check(&config) {
            Ok(value) => value,
            Err(error) => json!({ "ok": false, "error": error.0 }),
        })
    })
    .await;
    match result {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(error)) => respond::internal_error("email check", &error),
        Err(error) => respond::internal_error("email check", &error),
    }
}

/// The result of one monitoring pass.
#[derive(Debug, Default)]
struct PollResult {
    scanned: usize,
    matched: usize,
    updated: usize,
    skipped_seen: usize,
    model_calls: usize,
    outcomes: Vec<Value>,
    error: Option<String>,
    enabled: bool,
}

impl PollResult {
    fn to_json(&self) -> Value {
        json!({
            "scanned": self.scanned,
            "matched": self.matched,
            "updated": self.updated,
            "skipped_seen": self.skipped_seen,
            "model_calls": self.model_calls,
            "outcomes": self.outcomes,
            "error": self.error,
            "enabled": self.enabled,
        })
    }
}

/// `POST /api/v1/email/poll?since_days=14&force=false`: one monitoring pass now. `force`
/// re-processes mail already seen, so a classification fix applies to older mail too.
pub async fn poll(State(state): State<Arc<AppState>>, RawQuery(query): RawQuery) -> Response {
    let pairs = validation::query_pairs(query.as_deref());
    let mut reader = QueryReader::default();
    let since_days = reader.integer(&pairs, "since_days").unwrap_or(14);
    let force = reader.boolean(&pairs, "force", false);
    if let Err(response) = reader.finish() {
        return response;
    }
    let result = run_poll(&state, since_days, force).await;
    if result.updated > 0 {
        state
            .hub
            .broadcast(json!({
                "type": "agent",
                "event": "email_outcomes",
                "msg": format!("Email monitoring updated {} application(s)", result.updated),
            }))
            .await;
    }
    Json(result.to_json()).into_response()
}

async fn run_poll(state: &AppState, since_days: i64, force: bool) -> PollResult {
    let store = state.settings.clone();
    let Ok(Ok(settings)) = tokio::task::spawn_blocking(move || settings_map(&store)).await else {
        return PollResult {
            error: Some("could not read settings".to_owned()),
            enabled: true,
            ..PollResult::default()
        };
    };
    if !force && !enabled(&settings) {
        return PollResult {
            enabled: false,
            ..PollResult::default()
        };
    }
    let Some(config) = load_config(&settings) else {
        return PollResult {
            error: Some(NOT_CONFIGURED.to_owned()),
            enabled: true,
            ..PollResult::default()
        };
    };
    let fetched = tokio::task::spawn_blocking(move || email::recent(&config, since_days)).await;
    let messages = match fetched {
        Ok(Ok(messages)) => messages,
        Ok(Err(error)) => {
            log::warning("email", &format!("email poll failed: {error}"));
            return PollResult {
                error: Some(error.0),
                enabled: true,
                ..PollResult::default()
            };
        }
        Err(error) => {
            return PollResult {
                error: Some(error.to_string()),
                enabled: true,
                ..PollResult::default()
            };
        }
    };
    let (leads_store, settings_store) = (state.leads.clone(), state.settings.clone());
    let leads = tokio::task::spawn_blocking(move || applied_leads(&leads_store, &settings_store))
        .await
        .unwrap_or_default();
    let mut seen = load_seen(&settings);
    let use_model = mode(&settings) == MODE_RULES_AND_MODEL;
    let llm_settings = Settings::new(settings.clone());
    let mut result = PollResult {
        scanned: messages.len(),
        enabled: true,
        ..PollResult::default()
    };
    for message in &messages {
        if !force && seen.contains(&message.uid) {
            result.skipped_seen += 1;
            continue;
        }
        if !seen.contains(&message.uid) {
            seen.push(message.uid.clone());
        }
        let mut matched = email::match_lead(
            &message.from_address,
            &message.subject,
            &message.body,
            &leads,
        );
        let mut verdict = matched
            .as_ref()
            .map(|_| email::classify_outcome(&message.subject, &message.body));
        let unsure = verdict
            .as_ref()
            .is_none_or(|verdict| verdict.outcome == Outcome::Unknown);
        if use_model
            && unsure
            && result.model_calls < MAX_MODEL_CALLS_PER_POLL
            && email::is_candidate(&message.from_address, &message.subject, &leads)
        {
            result.model_calls += 1;
            if let Some((decided_match, decided_verdict)) =
                ask_model(&llm_settings, message, &leads).await
            {
                matched = Some(decided_match);
                verdict = Some(decided_verdict);
            }
        }
        let (Some(matched), Some(verdict)) = (matched, verdict) else {
            continue;
        };
        result.matched += 1;
        if verdict.outcome == Outcome::Unknown {
            continue;
        }
        let changed = apply_outcome(state, &matched, verdict.outcome, message).await;
        result.outcomes.push(json!({
            "job_id": matched.job_id,
            "company": matched.company,
            "outcome": verdict.outcome.as_str(),
            "confidence": verdict.confidence,
            "match_reason": matched.reason,
            "subject": message.subject.chars().take(160).collect::<String>(),
            "status_changed": changed,
        }));
        if changed {
            result.updated += 1;
        }
    }
    let start = seen.len().saturating_sub(MAX_SEEN);
    let stored = python_json_dumps(&json!(seen[start..]), true);
    let store = state.settings.clone();
    let _ = tokio::task::spawn_blocking(move || store.save(&[(KEY_SEEN_UIDS.to_owned(), stored)]))
        .await;
    result
}

async fn ask_model(
    settings: &Settings,
    message: &Message,
    leads: &[Map<String, Value>],
) -> Option<(email::LeadMatch, email::OutcomeVerdict)> {
    let companies = email::company_list(leads);
    if companies.is_empty() {
        return None;
    }
    let user = email::decision_prompt(
        &message.from_address,
        &message.subject,
        &message.body,
        &companies,
    );
    let request = LlmRequest {
        system: email::DECISION_SYSTEM,
        user: &user,
        step: Some("evaluator"),
        media: None,
    };
    let schema = OutputSchema {
        name: "EmailDecision".to_owned(),
        schema: email::decision_schema(),
    };
    // A model outage leaves the email to the rules; it never fails the poll.
    let decision = agent_hire_llm::call_structured(settings, &request, &schema)
        .await
        .ok()?;
    email::verdict_from_decision(&decision, &companies)
}

/// Leads worth matching mail against: ones the user engaged with, or queued in the cart.
fn applied_leads(leads: &LeadStore, settings: &SettingsStore) -> Vec<Map<String, Value>> {
    let Ok(all) = leads.all() else {
        return Vec::new();
    };
    let queued: HashSet<String> = cart_routes::read_cart(settings).into_iter().collect();
    all.into_iter()
        .filter(|lead| {
            let status = lead.get("status").and_then(Value::as_str).unwrap_or("");
            let job_id = lead.get("job_id").and_then(Value::as_str).unwrap_or("");
            matches!(
                status,
                "applied" | "interviewing" | "approved" | "tailoring"
            ) || queued.contains(job_id)
        })
        .collect()
}

/// Records the event and moves the status when the outcome warrants it; returns whether the
/// status changed.
async fn apply_outcome(
    state: &AppState,
    matched: &email::LeadMatch,
    outcome: Outcome,
    message: &Message,
) -> bool {
    let subject: String = message.subject.chars().take(80).collect();
    let action = format!(
        "email:{} ({}) {subject}",
        outcome.as_str(),
        matched.confidence
    );
    let store = state.leads.clone();
    let job_id = matched.job_id.clone();
    let outcome_status = email::outcome_status(outcome);
    tokio::task::spawn_blocking(move || {
        if let Err(error) = store.record_event(&job_id, &action) {
            log::warning(
                "email",
                &format!("could not record email event for {job_id}: {error}"),
            );
        }
        let current = store
            .by_id(&job_id)
            .ok()
            .flatten()
            .and_then(|lead| {
                lead.get("status")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            })
            .unwrap_or_default();
        let mut target = outcome_status;
        if outcome == Outcome::Acknowledged && PRE_APPLY_STATUSES.contains(&current.as_str()) {
            // "Thanks for applying" proves the application went in; it never moves anything back.
            target = Some("applied");
        }
        let Some(target) = target else { return false };
        if TERMINAL_STATUSES.contains(&current.as_str())
            || (current == "interviewing" && outcome == Outcome::Interview)
        {
            return false;
        }
        match store.update_status(&job_id, target) {
            Ok(()) => true,
            Err(error) => {
                log::warning(
                    "email",
                    &format!("could not set {target} on {job_id}: {error}"),
                );
                false
            }
        }
    })
    .await
    .unwrap_or(false)
}
