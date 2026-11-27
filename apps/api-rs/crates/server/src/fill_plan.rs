//! Resolves page fields into saved values, decision-model picks, or grounded answers.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Duration;

use agent_hire_jobindex::decision::{self, DecisionClient, Endpoint};
use agent_hire_llm::{LlmError, LlmRequest, Settings};
use agent_hire_store::{StoreError, truncate_chars};
use axum::Json;
use axum::extract::State;
use axum::response::Response;
use futures_util::StreamExt;
use rusqlite::OptionalExtension;
use serde_json::{Map, Value, json};

use crate::app_state::AppState;
use crate::fill_job_data as job_data;
use crate::fill_plan_fields::{self as fields, PageField};
use crate::fill_routes;
use crate::log;
use crate::respond;
use crate::validation::{ModelBody, ModelReader};

const WRITE: &str = "WRITE";
const SKIP: &str = "SKIP";
const MIN_PROBABILITY: f64 = 0.5;

const MAP_SYSTEM: &str = r#"You map job-application form fields to the candidate's saved values. For each field return the
key of the saved value that correctly answers it, "WRITE" if it needs a written answer that no single saved value
gives, or "SKIP" if the candidate should not fill it. For dropdown/radio fields also return the exact option text
that matches the chosen value (copy it from the options), or "" if none matches. Never guess a fact.
Return JSON: {"fields": [{"id": ..., "key": ..., "option": ...}]}"#;

const WRITE_SYSTEM: &str = r#"You write short answers for job-application questions, for the candidate described below.
Use ONLY the facts given (saved values, job data, résumé, notes). Never invent experience, numbers, employers or
skills. If the facts do not answer a question, return "" for it. Plain text, no markdown. Input boxes: one short
line. Text areas: at most 120 words. For dropdowns/radios answer with one option copied exactly; for checkbox
groups ([checkboxes]) list every option that applies, one per line, copied exactly.
Return JSON: {"answers": [{"id": ..., "text": ...}]}"#;

struct Inputs {
    settings: Settings,
    lead: Value,
    profile: Value,
    facts: Map<String, Value>,
    defaults: Map<String, Value>,
    notes: String,
    asker: Option<Asker>,
}

/// Builds the fill plan for a page of application fields.
pub async fn plan(
    State(state): State<Arc<AppState>>,
    ModelBody(body): ModelBody,
) -> Result<Json<Value>, Response> {
    let mut reader = ModelReader::new(&body);
    let job_id = reader.string("job_id", Some(200), true).unwrap_or_default();
    let fields = reader.model_list("fields", 250, PageField::read);
    reader.finish()?;
    let app = Arc::clone(&state);
    let inputs = respond::blocking("load form-fill inputs", move || load(&app, &job_id)).await?;
    let resume = job_data::resume_text(&inputs.profile);
    let record =
        job_data::get_or_build(&state.leads, &inputs.settings, &inputs.lead, &resume).await;
    let values = fields::catalog(&inputs.profile, inputs.facts, inputs.defaults, &record);
    let plan = Plan {
        lead: &inputs.lead,
        fields: &fields,
        values: &values,
        settings: &inputs.settings,
        notes: &inputs.notes,
        resume: &resume,
        record: &record,
    };
    Ok(Json(plan.build(inputs.asker).await))
}

fn load(state: &AppState, job_id: &str) -> Result<Inputs, StoreError> {
    let settings = Settings::new(state.settings.all()?);
    let lead = Value::Object(state.leads.by_id(job_id)?.unwrap_or_default());
    let profile = state.profile.get_profile()?;
    let facts = fill_routes::facts(&state.settings, &profile);
    let defaults = fill_routes::defaults(&state.settings);
    let notes = state
        .database
        .connect()
        .and_then(|conn| {
            Ok(conn
                .query_row(
                    "SELECT merged_text FROM misc_context WHERE id = 'singleton'",
                    [],
                    |row| row.get::<_, String>(0),
                )
                .optional()?
                .unwrap_or_default())
        })
        .unwrap_or_default();
    let client = state.job_index_runtime.decisions().clone();
    let endpoint = client.endpoints_from_settings(true).into_iter().next();
    let asker = endpoint.map(|endpoint| Asker { client, endpoint });
    Ok(Inputs {
        settings,
        lead,
        profile: Value::Object(profile),
        facts,
        defaults,
        notes,
        asker,
    })
}

struct Plan<'a> {
    lead: &'a Value,
    fields: &'a [PageField],
    values: &'a Map<String, Value>,
    settings: &'a Settings,
    notes: &'a str,
    resume: &'a str,
    record: &'a Value,
}

struct Pick {
    id: String,
    key: String,
    option: String,
    source: &'static str,
}

impl Plan<'_> {
    async fn build(&self, mut asker: Option<Asker>) -> Value {
        let by_id: HashMap<&str, &PageField> = self
            .fields
            .iter()
            .map(|field| (field.id.as_str(), field))
            .collect();
        let mut skipped = Map::new();
        let mut picks = Vec::<Pick>::new();
        for field in self.fields {
            if let Some(reason) = field.reason_to_skip() {
                skipped.insert(field.id.clone(), json!(reason));
                continue;
            }
            if let Some(key) = fields::rule_key(field, self.values) {
                pick(&mut picks, &field.id, key, "", "rules");
            }
        }
        // Consent and human checks remain blocked even if a page repeats an ID.
        picks.retain(|pick| !skipped.contains_key(&pick.id));
        let blocked: HashSet<String> = skipped.keys().cloned().collect();

        let pending = self
            .fields
            .iter()
            .filter(|field| {
                !blocked.contains(&field.id) && !picks.iter().any(|pick| pick.id == field.id)
            })
            .collect::<Vec<_>>();
        let mut decided_by = "rules";
        let mut unsure = Vec::new();
        if !pending.is_empty() {
            decided_by = "main_model";
            if let Some(decision) = &asker {
                let questions = key_question(self.values);
                let batch = pending
                    .iter()
                    .map(|field| (field_state(field, self.lead), questions.clone()))
                    .collect();
                if let Ok(answers) = decision.ask(batch).await {
                    decided_by = "decision_model";
                    for (index, field) in pending.iter().enumerate() {
                        let label = answers
                            .get(index)
                            .and_then(Option::as_ref)
                            .and_then(|row| confident_choice(&row["fact"]));
                        if let Some(label) = label.filter(|label| self.valid_key(label)) {
                            pick(&mut picks, &field.id, label, "", "decision_model");
                        } else {
                            unsure.push(*field);
                        }
                    }
                } else {
                    asker = None;
                    unsure = pending;
                }
            } else {
                unsure = pending;
            }
        }
        if !unsure.is_empty() {
            if decided_by == "decision_model" {
                decided_by = "decision_model+main_model";
            }
            if let Err(error) = self.map_fields(&unsure, &mut picks).await {
                let reason = format!(
                    "no model available ({})",
                    job_data::model_error_name(&error)
                );
                for field in unsure {
                    skipped.entry(&field.id).or_insert_with(|| json!(reason));
                }
            }
        }

        let mut fills = Vec::new();
        let mut to_write = Vec::new();
        let mut choice_questions = Vec::new();
        for pick in &picks {
            let field = by_id[pick.id.as_str()];
            match pick.key.as_str() {
                SKIP => {
                    skipped.insert(pick.id.clone(), json!("not for the candidate to fill"));
                    continue;
                }
                WRITE => {
                    to_write.push(field);
                    continue;
                }
                _ => {}
            }
            let saved = &self.values[&pick.key];
            let value = job_data::text(saved, "value");
            let from = job_data::text(saved, "source");
            if field.options.is_empty() {
                fills.push(fill(&pick.id, value, &pick.key, pick.source, from));
            } else if field.kind == "checkboxes" {
                let matched = fields::match_many(value, &field.options);
                if matched.is_empty() {
                    skipped.insert(pick.id.clone(), no_option(value));
                } else {
                    fills.push(fill(&pick.id, &matched, &pick.key, pick.source, from));
                }
            } else if let Some(option) = exact_or_matching(&pick.option, value, &field.options) {
                fills.push(fill(&pick.id, option, &pick.key, pick.source, from));
            } else if asker.is_some() {
                choice_questions.push((field, pick, value));
            } else {
                skipped.insert(pick.id.clone(), no_option(value));
            }
        }

        if let Some(asker) = asker.filter(|_| !choice_questions.is_empty()) {
            let batch = choice_questions
                .iter()
                .map(|(field, _, value)| {
                    (
                        field_state(field, self.lead),
                        option_question(value, &field.options),
                    )
                })
                .collect::<Vec<_>>();
            let answers = asker.ask(batch).await.unwrap_or_default();
            for (index, (field, pick, value)) in choice_questions.into_iter().enumerate() {
                let label = answers
                    .get(index)
                    .and_then(Option::as_ref)
                    .and_then(|row| confident_choice(&row["option"]))
                    .filter(|label| field.options.iter().any(|option| option == label));
                if let Some(label) = label {
                    fills.push(fill(
                        &field.id,
                        label,
                        &pick.key,
                        "decision_model",
                        job_data::text(&self.values[&pick.key], "source"),
                    ));
                } else {
                    skipped.insert(field.id.clone(), no_option(value));
                }
            }
        }

        if !to_write.is_empty() {
            let answers = match self.write_answers(&to_write).await {
                Ok(value) => value,
                Err(error) => {
                    let reason = format!(
                        "could not write an answer ({})",
                        job_data::model_error_name(&error)
                    );
                    for field in &to_write {
                        skipped.insert(field.id.clone(), json!(reason));
                    }
                    Value::Null
                }
            };
            let answers = written_text(&answers);
            for field in to_write {
                let answer = answers.get(field.id.as_str()).copied().unwrap_or_default();
                let value = if field.options.is_empty() {
                    answer.to_owned()
                } else if field.kind == "checkboxes" {
                    fields::match_many(answer, &field.options)
                } else {
                    exact_or_matching(answer, answer, &field.options)
                        .unwrap_or_default()
                        .to_owned()
                };
                if value.is_empty() {
                    skipped
                        .entry(&field.id)
                        .or_insert_with(|| json!("nothing in your data answers it"));
                } else {
                    fills.push(fill(&field.id, &value, WRITE, "main_model", "written"));
                }
            }
        }

        if self.fields.iter().any(PageField::asks_for_phone_code) {
            let code = self
                .values
                .get("phone_country_code")
                .map_or("", |saved| job_data::text(saved, "value"));
            for fill in &mut fills {
                let field = by_id[job_data::text(fill, "id")];
                if field.kind == "tel" || (fill["key"] == "phone" && field.options.is_empty()) {
                    fill["value"] =
                        json!(fields::without_code(job_data::text(fill, "value"), code));
                }
            }
        }
        for fill in &mut fills {
            if by_id[job_data::text(fill, "id")].kind == "url" {
                fill["value"] = json!(fields::with_scheme(job_data::text(fill, "value")));
            }
        }
        let filled: HashSet<&str> = fills
            .iter()
            .map(|item| job_data::text(item, "id"))
            .collect();
        for field in self.fields {
            if !filled.contains(field.id.as_str()) {
                skipped
                    .entry(&field.id)
                    .or_insert_with(|| json!("no matching value"));
            }
        }
        let skipped = skipped
            .into_iter()
            .filter(|(id, _)| !filled.contains(id.as_str()))
            .map(|(id, reason)| json!({"id":id,"reason":reason}))
            .collect::<Vec<_>>();
        json!({
            "fills":fills,"skipped":skipped,"decided_by":decided_by,
            "job_data":{"source":self.record["source"],"confidence":self.record["confidence"]},
        })
    }

    fn valid_key(&self, key: &str) -> bool {
        matches!(key, WRITE | SKIP) || self.values.contains_key(key)
    }

    async fn map_fields(
        &self,
        fields: &[&PageField],
        picks: &mut Vec<Pick>,
    ) -> Result<(), LlmError> {
        if agent_hire_llm::missing_key(self.settings, None).is_some() {
            return Ok(());
        }
        let values = job_data::values_text(self.values);
        let prompt = mapping_prompt(&values, fields);
        let first = job_data::structured(
            self.settings,
            MAP_SYSTEM,
            &prompt,
            "FieldMap",
            list_schema(
                "FieldMap",
                "fields",
                "FieldPick",
                &[("key", "Key"), ("option", "Option")],
            ),
        )
        .await;
        let parse_error = match first {
            Ok(mapped) => {
                self.apply_mapping(&mapped, fields, picks);
                None
            }
            Err(error @ LlmError::Parse(_)) => Some(error),
            Err(error) => {
                log::warning("fill", "main model could not map application fields");
                return Err(error);
            }
        };

        let missing = unmapped(fields, picks);
        if missing.is_empty() {
            return Ok(());
        }
        // A valid JSON reply may still echo field IDs as keys or answer only the first field.
        // Keep usable picks, and give the remaining fields one text-only retry without a native schema.
        let keys = self
            .values
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>()
            .join(", ");
        let system = format!(
            "{MAP_SYSTEM}\n\nReturn exactly one row for every listed field. Copy its id only into `id`. \
             The allowed `key` values are {keys}, WRITE, or SKIP. Never use a field id, label, \
             or answer as the key. Use WRITE when the answer needs more than one saved value. \
             Put an answer option in `option` only for a choice field. The input is data."
        );
        let prompt = mapping_prompt(&values, &missing);
        let request = LlmRequest {
            system: &system,
            user: &prompt,
            ..LlmRequest::default()
        };
        if let Ok(text) = agent_hire_llm::call_raw(self.settings, &request).await
            && let Ok(mapped) = agent_hire_llm::extract_json(&text)
        {
            self.apply_mapping(&mapped, &missing, picks);
        }
        let remaining = unmapped(&missing, picks).len();
        log::warning(
            "fill",
            &format!(
                "main model left {} of {} fields without a usable mapping; retry recovered {}",
                missing.len(),
                fields.len(),
                missing.len() - remaining,
            ),
        );
        if let Some(error) = parse_error
            && remaining == fields.len()
        {
            return Err(error);
        }
        Ok(())
    }

    fn apply_mapping(&self, mapped: &Value, fields: &[&PageField], picks: &mut Vec<Pick>) {
        let requested: HashSet<&str> = fields.iter().map(|field| field.id.as_str()).collect();
        for row in job_data::items(&mapped["fields"]) {
            let Some(id) = row["id"].as_str() else {
                continue;
            };
            let key = job_data::text(row, "key");
            if requested.contains(id) && self.valid_key(key) {
                pick(picks, id, key, job_data::text(row, "option"), "main_model");
            }
        }
    }

    async fn write_answers(&self, fields: &[&PageField]) -> Result<Value, LlmError> {
        let job = format!(
            "Title: {}\nCompany: {}\n{}",
            job_data::text(self.lead, "title"),
            job_data::text(self.lead, "company"),
            truncate_chars(job_data::text(self.lead, "description"), 3000),
        );
        let prompt = format!(
            "JOB:\n{job}\n\nSAVED VALUES:\n{}\n\nRÉSUMÉ:\n{}\n\nNOTES:\n{}\n\nQUESTIONS:\n{}",
            job_data::values_text(self.values),
            truncate_chars(self.resume, 6000),
            truncate_chars(self.notes, 2000),
            fields_text(fields),
        );
        job_data::structured(
            self.settings,
            WRITE_SYSTEM,
            &prompt,
            "Answers",
            list_schema("Answers", "answers", "Answer", &[("text", "Text")]),
        )
        .await
    }
}

fn pick(picks: &mut Vec<Pick>, id: &str, key: &str, option: &str, source: &'static str) {
    let pick = Pick {
        id: id.to_owned(),
        key: key.to_owned(),
        option: option.to_owned(),
        source,
    };
    if let Some(existing) = picks.iter_mut().find(|old| old.id == id) {
        *existing = pick;
    } else {
        picks.push(pick);
    }
}

fn fill(id: &str, value: &str, key: &str, source: &str, from: &str) -> Value {
    json!({"id":id,"value":value,"key":key,"source":source,"from":from})
}

fn exact_or_matching<'a>(option: &str, value: &str, options: &'a [String]) -> Option<&'a str> {
    options
        .iter()
        .find(|candidate| *candidate == option)
        .map(String::as_str)
        .or_else(|| fields::match_option(value, options))
        .filter(|choice| !choice.is_empty())
}

fn no_option(value: &str) -> Value {
    json!(format!("no option matches “{}”", truncate_chars(value, 60)))
}

fn list_schema(name: &str, key: &str, row: &str, optional: &[(&str, &str)]) -> Value {
    let mut properties = Map::new();
    properties.insert("id".to_owned(), json!({"title":"Id","type":"string"}));
    for &(property, title) in optional {
        properties.insert(
            property.to_owned(),
            json!({"default":"","title":title,"type":"string"}),
        );
    }
    // Match the original planner's named schema and defaults for Responses providers.
    let title = if key == "fields" { "Fields" } else { "Answers" };
    json!({
        "$defs": {row: {"properties":properties,"required":["id"],"title":row,"type":"object"}},
        "properties": {key: {"default":[],"items":{"$ref":format!("#/$defs/{row}")},"title":title,"type":"array"}},
        "title": name,
        "type": "object",
    })
}

fn mapping_prompt(values: &str, fields: &[&PageField]) -> String {
    format!(
        "SAVED VALUES:\n{values}\n\nFIELDS:\n{}",
        fields_text(fields)
    )
}

fn unmapped<'a>(fields: &[&'a PageField], picks: &[Pick]) -> Vec<&'a PageField> {
    fields
        .iter()
        .copied()
        .filter(|field| !picks.iter().any(|pick| pick.id == field.id))
        .collect()
}

fn written_text(answers: &Value) -> HashMap<&str, &str> {
    job_data::items(&answers["answers"])
        .iter()
        .filter_map(|row| Some((row["id"].as_str()?, job_data::text(row, "text").trim())))
        .collect()
}

fn fields_text(fields: &[&PageField]) -> String {
    fields
        .iter()
        .map(|field| {
            let options = if field.options.is_empty() {
                String::new()
            } else {
                format!(
                    " | options: {}",
                    field
                        .options
                        .iter()
                        .take(30)
                        .cloned()
                        .collect::<Vec<_>>()
                        .join(" / ")
                )
            };
            format!(
                "- id={} [{}] {}{options}",
                field.id,
                field.kind(),
                truncate_chars(field.question(), 300)
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

struct Asker {
    client: DecisionClient,
    endpoint: Endpoint,
}

impl Asker {
    async fn ask(&self, batch: Vec<(String, Value)>) -> Result<Vec<Option<Value>>, ()> {
        if self.endpoint.name() == "kaggle" {
            let client = self.client.clone();
            let endpoint = self.endpoint.clone();
            return tokio::task::spawn_blocking(move || {
                client.ask_notebook_batch(&endpoint, &batch)
            })
            .await
            .map_err(|_| ())?
            .map_err(|_| ());
        }
        let answers = futures_util::stream::iter(batch)
            .map(|(state, questions)| {
                let client = self.client.clone();
                let endpoint = self.endpoint.clone();
                async move {
                    tokio::task::spawn_blocking(move || {
                        client.ask(&endpoint, &state, &questions, Duration::from_secs(60))
                    })
                    .await
                    .ok()
                    .and_then(Result::ok)
                }
            })
            .buffered(4)
            .collect::<Vec<_>>()
            .await;
        Ok(answers)
    }
}

fn field_state(field: &PageField, lead: &Value) -> String {
    let mut state = format!(
        "Job application form for: {} at {}\nForm field question: {}\nField type: {}",
        job_data::first(lead, &["title"]),
        job_data::first(lead, &["company"]),
        field.question(),
        field.kind(),
    );
    if !field.options.is_empty() {
        let options = &field.options[..field.options.len().min(30)];
        state.push_str(&format!("\nOptions: {}", options.join(" | ")));
    }
    if !field.context.is_empty() {
        state.push_str(&format!(
            "\nNearby text: {}",
            truncate_chars(&field.context, 300)
        ));
    }
    state
}

fn key_question(values: &Map<String, Value>) -> Value {
    let mut criteria: Map<String, Value> = values
        .iter()
        .map(|(key, value)| (key.clone(), value["description"].clone()))
        .collect();
    criteria.insert(WRITE.to_owned(), json!("none of the saved values answers it: the field asks for a written answer (motivation, a project, a description, an essay) or a fact not listed"));
    criteria.insert(SKIP.to_owned(), json!("the candidate should not fill it (captcha, marketing opt-in, optional unrelated field, internal use)"));
    json!({"fact":{"type":"choice","instructions":"Which saved candidate value correctly answers this form field?","criteria":criteria}})
}

fn option_question(value: &str, options: &[String]) -> Value {
    let criteria: Map<String, Value> = options
        .iter()
        .take(30)
        .map(|option| (option.clone(), json!(option)))
        .collect();
    json!({"option":{"type":"choice","instructions":format!("The candidate's answer is: {value}. Which option says the same?"),"criteria":criteria}})
}

fn confident_choice(answer: &Value) -> Option<&str> {
    let label = decision::choice(answer).ok().flatten()?;
    let mut probability = None;
    for key in ["probabilities", "distribution", "scores"] {
        if let Some(value) = answer[key].get(label) {
            probability = value
                .as_f64()
                .or_else(|| value.as_str().and_then(|value| value.trim().parse().ok()))
                .or_else(|| value.as_bool().map(|value| f64::from(u8::from(value))));
        }
    }
    let probability = probability.or_else(|| {
        answer["p"].as_f64().or_else(|| {
            answer["p"]
                .as_bool()
                .map(|value| f64::from(u8::from(value)))
        })
    });
    probability
        .is_none_or(|p| p >= MIN_PROBABILITY)
        .then_some(label)
}

#[cfg(test)]
mod tests {
    use agent_hire_llm::Settings;
    use serde_json::{Map, Value, json};

    use super::{Plan, exact_or_matching, list_schema, unmapped, written_text};
    use crate::fill_plan_fields::PageField;
    use crate::validation::ModelReader;

    // OpenCode reply text. The free-text answers in the echoed options were shortened.
    const HY4_ECHOED_IDS: &str = r#"{
        "fields": [
            {"id":"f5","key":"f5","option":"Hyderabad, India"},
            {"id":"f9","key":"f9","option":"Ledgerline Fintech"},
            {"id":"f10","key":"f10","option":"5-7"},
            {"id":"f11","key":"f11","option":"30 days"},
            {"id":"f12","key":"f12","option":"INR 42 LPA"},
            {"id":"f13","key":"f13","option":"SKIP"},
            {"id":"f14","key":"f14","option":"[redacted]"},
            {"id":"f15","key":"f15","option":"[redacted]"},
            {"id":"f16","key":"f16","option":"Company website"},
            {"id":"f18","key":"f18","option":"Yes"},
            {"id":"f19","key":"f19","option":"No"},
            {"id":"f20","key":"f20","option":"Prefer not to say"}
        ]
    }"#;
    const DEEPSEEK_MISSING_ROWS: [&str; 2] = [
        r#"{"fields":[{"id":"f5","key":"current_location","option":""}]}"#,
        r#"{"fields":[{"id":"f5","key":"Current location (city, country)","option":"Current location (city, country)="}]}"#,
    ];

    fn field(id: &str) -> PageField {
        let mut body = Map::new();
        body.insert("id".to_owned(), json!(id));
        PageField::read(&mut ModelReader::new(&body))
    }

    #[test]
    fn recorded_unusable_maps_leave_the_requested_fields_pending() {
        let fields = [
            "f5", "f9", "f10", "f11", "f12", "f13", "f14", "f15", "f16", "f18", "f19", "f20",
        ]
        .map(field);
        let requested: Vec<&PageField> = fields.iter().collect();
        let values = Map::from_iter([
            ("city".to_owned(), json!({"value":"Hyderabad"})),
            (
                "current_company".to_owned(),
                json!({"value":"Ledgerline Fintech"}),
            ),
        ]);
        let plan = Plan {
            lead: &Value::Null,
            fields: &fields,
            values: &values,
            settings: &Settings::default(),
            notes: "",
            resume: "",
            record: &Value::Null,
        };
        for raw in [
            HY4_ECHOED_IDS,
            DEEPSEEK_MISSING_ROWS[0],
            DEEPSEEK_MISSING_ROWS[1],
        ] {
            let reply = agent_hire_llm::extract_json(raw).expect("recorded valid JSON reply");
            let mut picks = Vec::new();
            plan.apply_mapping(&reply, &requested, &mut picks);
            assert!(picks.is_empty());
            assert_eq!(unmapped(&requested, &picks).len(), requested.len());
        }
    }

    #[test]
    fn recorded_partial_map_keeps_valid_picks_and_only_retries_the_missing_id() {
        let fields = ["f5", "f9", "f18"].map(field);
        let requested: Vec<&PageField> = fields.iter().collect();
        let values = Map::from_iter([
            (
                "current_company".to_owned(),
                json!({"value":"Ledgerline Fintech"}),
            ),
            ("work_authorized".to_owned(), json!({"value":"Yes"})),
        ]);
        let plan = Plan {
            lead: &Value::Null,
            fields: &fields,
            values: &values,
            settings: &Settings::default(),
            notes: "",
            resume: "",
            record: &Value::Null,
        };
        let first = agent_hire_llm::extract_json(
            r#"{"fields":[
            {"id":"f5","key":"current_location_placeholder","option":"WRITE"},
            {"id":"f9","key":"current_company","option":"Ledgerline Fintech"},
            {"id":"f18","key":"work_authorized","option":"Yes"}
        ]}"#,
        )
        .expect("recorded partial mapping");
        let mut picks = Vec::new();
        plan.apply_mapping(&first, &requested, &mut picks);
        let missing = unmapped(&requested, &picks);
        assert_eq!(missing.len(), 1);
        assert_eq!(missing[0].id, "f5");

        let retry = agent_hire_llm::extract_json(
            r#"{"fields":[
            {"id":"f5","key":"WRITE"},
            {"id":"f9","key":"SKIP"}
        ]}"#,
        )
        .expect("retry with an omitted optional value and an unrequested ID");
        plan.apply_mapping(&retry, &missing, &mut picks);
        assert!(unmapped(&requested, &picks).is_empty());
        assert_eq!(picks[0].key, "current_company");
        assert_eq!(picks[1].option, "Yes");
        assert_eq!(picks[2].key, "WRITE");
        assert_eq!(picks[2].option, "");
    }

    #[test]
    fn recorded_answers_keep_written_text_and_match_an_exact_choice() {
        let raw = r#"{"answers":[
            {"id":"f5","text":"Hyderabad, India"},
            {"id":"f13","text":"Yes"},
            {"id":"f15","text":"[redacted]"}
        ]}"#;
        let reply = agent_hire_llm::extract_json(raw).expect("recorded writer reply");
        let answers = written_text(&reply);
        assert_eq!(answers["f5"], "Hyderabad, India");
        let options = [
            "Yes".to_owned(),
            "No".to_owned(),
            "Open to discuss".to_owned(),
        ];
        assert_eq!(
            exact_or_matching(answers["f13"], answers["f13"], &options),
            Some("Yes")
        );
        let optional = json!({"answers":[{"id":"f5"}]});
        assert_eq!(written_text(&optional)["f5"], "");
    }

    #[test]
    fn mapping_schema_has_the_archived_defaults() {
        let schema = list_schema(
            "FieldMap",
            "fields",
            "FieldPick",
            &[("key", "Key"), ("option", "Option")],
        );
        assert_eq!(
            schema,
            json!({
                "$defs": {"FieldPick": {
                    "properties": {
                        "id":{"title":"Id","type":"string"},
                        "key":{"default":"","title":"Key","type":"string"},
                        "option":{"default":"","title":"Option","type":"string"}
                    },
                    "required":["id"],"title":"FieldPick","type":"object"
                }},
                "properties":{"fields":{"default":[],"items":{"$ref":"#/$defs/FieldPick"},"title":"Fields","type":"array"}},
                "title":"FieldMap","type":"object"
            })
        );
    }
}
