//! Job-specific data cached on the lead for the application filler.

use std::sync::LazyLock;

use agent_hire_llm::{LlmError, LlmRequest, OutputSchema, Settings};
use agent_hire_store::{LeadStore, UtcTime, python_json_dumps, truncate_chars};
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::log;

const META_KEY: &str = "fill_job_data";
const MIN_DESCRIPTION: usize = 200;

pub(super) const JOB_FIELDS: &[(&str, &str)] = &[
    (
        "years_relevant_experience",
        "the candidate's years of experience relevant to THIS job's main skills (a number)",
    ),
    (
        "key_skills",
        "the candidate's skills that match this job, comma separated",
    ),
    (
        "project_highlights",
        "two to four short points about the candidate's projects or work that are most relevant to this job",
    ),
    (
        "experience_summary",
        "a 2-3 sentence summary of the candidate's experience, aimed at this job",
    ),
    (
        "why_interested",
        "why the candidate is interested in this specific job and company",
    ),
];

/// One decision about the source of job-specific answers, reused until the text is enriched.
pub async fn get_or_build(
    store: &LeadStore,
    settings: &Settings,
    lead: &Value,
    resume: &str,
) -> Value {
    let id = text(lead, "job_id");
    if id.is_empty() {
        return json!({"source":"resume","confidence":0.0,"data":{},"reason":"unknown job"});
    }
    let length = description_chars(text(lead, "description"));
    let cached = &lead["source_meta"][META_KEY];
    let stale = cached["description_chars"]
        .as_u64()
        .is_some_and(|old| old < MIN_DESCRIPTION as u64)
        && length >= MIN_DESCRIPTION;
    if cached.is_object() && !text(cached, "decided_at").is_empty() && !stale {
        return cached.clone();
    }
    let record = match build(store, settings, lead, resume, length).await {
        Ok(record) => record,
        Err(error) => {
            log::info(
                "fill",
                &format!(
                    "job data file unavailable for {id}: {}",
                    model_error_name(&error)
                ),
            );
            return json!({"source":"resume","confidence":0.0,"data":{}});
        }
    };
    let store = store.clone();
    let id = id.to_owned();
    let cached = record.clone();
    let _ =
        tokio::task::spawn_blocking(move || store.set_source_meta_field(&id, META_KEY, &cached))
            .await;
    record
}

async fn build(
    store: &LeadStore,
    settings: &Settings,
    lead: &Value,
    resume: &str,
    length: usize,
) -> Result<Value, LlmError> {
    let mut record = json!({
        "source":"resume","confidence":0.0,"data":{},"from_job":"",
        "decided_at":UtcTime::now().to_iso_micros(),
    });
    if resume.trim().is_empty() {
        record["reason"] = json!("no résumé in the profile");
        return Ok(record);
    }
    record["description_chars"] = json!(length);
    // Thin corpus postings used a retired scraper API for hydration.
    if length < MIN_DESCRIPTION {
        record["source"] = json!("defaults");
        record["reason"] = json!("the posting has no description to tailor answers to");
        return Ok(record);
    }
    let store = store.clone();
    let id = text(lead, "job_id").to_owned();
    let existing = tokio::task::spawn_blocking(move || existing_files(&store, &id))
        .await
        .unwrap_or_default();
    let listing = if existing.is_empty() {
        "(none)".to_owned()
    } else {
        existing
            .iter()
            .map(|file| {
                format!(
                    "- {}: {} at {}",
                    text(file, "job_id"),
                    text(file, "title"),
                    text(file, "company")
                )
            })
            .collect::<Vec<_>>()
            .join("\n")
    };
    const DECIDE: &str = "Choose job-specific application data based on the posting and résumé. Pick generate when real résumé evidence supports tailored answers, reuse when a listed file fits this exact posting, defaults when general answers suffice, or resume when only résumé facts are needed. Give the relevant reuse job ID if any, confidence from 0 to 1, and a short reason.";
    let description = job_text(lead, 4000);
    let prompt = format!(
        "JOB:\n{description}\n\nRÉSUMÉ:\n{}\n\nEXISTING DATA FILES:\n{listing}",
        truncate_chars(resume, 6000)
    );
    let decision = structured(
        settings,
        DECIDE,
        &prompt,
        "job_data_source",
        object_schema(json!({
            "action":{"type":"string"}, "reuse_job_id":{"type":"string"},
            "confidence":{"type":"number"}, "reason":{"type":"string"},
        })),
    )
    .await?;
    let action = text(&decision, "action").trim().to_lowercase();
    let confidence = decision["confidence"]
        .as_f64()
        .or_else(|| {
            decision["confidence"]
                .as_str()
                .and_then(|value| value.parse().ok())
        })
        .unwrap_or_default()
        .clamp(0.0, 1.0);
    record["confidence"] = json!((confidence * 1000.0).round_ties_even() / 1000.0);
    record["decided"] = json!(action);
    record["reason"] = json!(truncate_chars(text(&decision, "reason"), 300));
    match action.as_str() {
        "generate" if confidence >= 0.8 => {
            const GENERATE: &str = "Write job-specific application fields using only facts stated in the résumé. Derive relevant years from stated dates, select skills that actually appear, and pick two to four factual project or experience highlights. Keep unsupported fields empty. Briefly summarize relevant experience and explain interest using only the supplied job and résumé.";
            let prompt = format!(
                "JOB:\n{description}\n\nRÉSUMÉ:\n{}",
                truncate_chars(resume, 8000)
            );
            let value = structured(
                settings,
                GENERATE,
                &prompt,
                "job_data_answers",
                object_schema(json!({
                    "years_relevant_experience":{"type":"string"},
                    "key_skills":{"type":"array","items":{"type":"string"}},
                    "project_highlights":{"type":"array","items":{"type":"string"}},
                    "experience_summary":{"type":"string"},
                    "why_interested":{"type":"string"},
                })),
            )
            .await?;
            record["source"] = json!("generate");
            record["data"] = grounded(&value, resume);
        }
        "reuse" if confidence >= 0.8 => {
            if let Some(file) = existing
                .iter()
                .find(|file| file["job_id"] == decision["reuse_job_id"])
            {
                record["source"] = json!("reuse");
                record["data"] = file["data"].clone();
                record["from_job"] = file["job_id"].clone();
            } else {
                record["source"] = json!("defaults");
            }
        }
        "defaults" | "generate" | "reuse" => record["source"] = json!("defaults"),
        _ => {}
    }
    Ok(record)
}

fn existing_files(store: &LeadStore, exclude: &str) -> Vec<Value> {
    let mut files: Vec<_> = store.all().unwrap_or_default().into_iter().filter_map(|lead| {
        let record = &lead["source_meta"][META_KEY];
        if lead.get("job_id").and_then(Value::as_str) == Some(exclude)
            || record["source"] != "generate"
            || record["data"].as_object().is_none_or(Map::is_empty)
        {
            return None;
        }
        Some(json!({
            "job_id":lead["job_id"],"title":lead["title"],"company":lead["company"],"data":record["data"],
        }))
    }).collect();
    if files.len() > 20 {
        files.drain(..files.len() - 20);
    }
    files
}

fn grounded(value: &Value, resume: &str) -> Value {
    static YEARS: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^\d{1,2}(\.\d)?\+?$").expect("valid years pattern"));
    let lower = resume.to_lowercase();
    let skills = items(&value["key_skills"])
        .iter()
        .filter_map(Value::as_str)
        .map(str::trim)
        .filter(|skill| !skill.is_empty() && lower.contains(&skill.to_lowercase()))
        .collect::<Vec<_>>()
        .join(", ");
    let years = text(value, "years_relevant_experience").trim();
    let years = if YEARS.is_match(years) { years } else { "" };
    let highlights = items(&value["project_highlights"])
        .iter()
        .take(4)
        .filter_map(Value::as_str)
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(|line| format!("- {line}"))
        .collect::<Vec<_>>()
        .join("\n");
    json!({
        "years_relevant_experience":years,"key_skills":skills,"project_highlights":highlights,
        "experience_summary":text(value,"experience_summary").trim(),"why_interested":text(value,"why_interested").trim(),
    })
}

fn description_chars(description: &str) -> usize {
    static URL: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)https?://\S+|www\.\S+").expect("valid posting URL pattern")
    });
    URL.replace_all(description, "").trim().chars().count()
}

/// The résumé evidence available to the application writer.
pub fn resume_text(profile: &Value) -> String {
    let mut lines = Vec::new();
    for (field, label) in [("n", "Name"), ("s", "Summary")] {
        let value = text(profile, field);
        if !value.is_empty() {
            lines.push(format!("{label}: {value}"));
        }
    }
    let skills = skills(profile);
    if !skills.is_empty() {
        lines.push(format!("Skills: {skills}"));
    }
    for exp in experiences(profile).iter().filter(|exp| exp.is_object()) {
        lines.push(format!(
            "Experience: {} at {} ({}): {}",
            first(exp, &["role", "title"]),
            first(exp, &["co", "company"]),
            first(exp, &["period", "dates"]),
            first(exp, &["d", "description"])
        ));
    }
    for project in items(&profile["projects"])
        .iter()
        .filter(|project| project.is_object())
    {
        let stack = if let Some(stack) = project["stack"].as_array() {
            stack
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        } else {
            text(project, "stack").to_owned()
        };
        lines.push(format!(
            "Project: {} [{stack}]: {}",
            text(project, "title"),
            first(project, &["impact", "description"])
        ));
    }
    for entry in items(&profile["education"]) {
        let title = first(entry, &["text", "title"]);
        let line = entry.as_str().map(str::to_owned).unwrap_or_else(|| {
            if title.is_empty() {
                python_json_dumps(entry, false)
            } else {
                title.to_owned()
            }
        });
        lines.push(format!("Education: {line}"));
    }
    lines.join("\n")
}

pub(super) fn job_text(lead: &Value, max_description: usize) -> String {
    format!(
        "Title: {}\nCompany: {}\n\n{}",
        text(lead, "title"),
        text(lead, "company"),
        truncate_chars(text(lead, "description"), max_description)
    )
}

pub(super) fn object_schema(properties: Value) -> Value {
    let required = properties
        .as_object()
        .expect("schema properties are an object")
        .keys()
        .cloned()
        .collect::<Vec<_>>();
    json!({"type":"object","properties":properties,"required":required,"additionalProperties":false})
}

pub(super) async fn structured(
    settings: &Settings,
    system: &str,
    prompt: &str,
    name: &str,
    schema: Value,
) -> Result<Value, LlmError> {
    let request = LlmRequest {
        system,
        user: prompt,
        ..LlmRequest::default()
    };
    let schema = OutputSchema {
        name: name.to_owned(),
        schema,
    };
    let answer = agent_hire_llm::call_structured(settings, &request, &schema).await?;
    if answer.is_object() {
        Ok(answer)
    } else {
        Err(LlmError::Parse("expected structured object".to_owned()))
    }
}

pub(super) fn model_error_name(_error: &LlmError) -> &'static str {
    // Python's structured client wraps provider and validation failures in this exception.
    "InstructorRetryException"
}

pub(super) fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or_default()
}

pub(super) fn first<'a>(value: &'a Value, keys: &[&str]) -> &'a str {
    keys.iter()
        .map(|key| text(value, key))
        .find(|text| !text.is_empty())
        .unwrap_or_default()
}

pub(super) fn items(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}

pub(super) fn experiences(profile: &Value) -> &[Value] {
    let exp = items(&profile["exp"]);
    if exp.is_empty() {
        items(&profile["experience"])
    } else {
        exp
    }
}

pub(super) fn skills(profile: &Value) -> String {
    items(&profile["skills"])
        .iter()
        .map(|skill| skill.as_str().unwrap_or_else(|| text(skill, "n")))
        .filter(|name| !name.is_empty())
        .collect::<Vec<_>>()
        .join(", ")
}

pub(super) fn values_text(values: &Map<String, Value>) -> String {
    values
        .iter()
        .map(|(key, saved)| {
            format!(
                "- {key} ({}): {}",
                text(saved, "description"),
                truncate_chars(text(saved, "value"), 300)
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}
