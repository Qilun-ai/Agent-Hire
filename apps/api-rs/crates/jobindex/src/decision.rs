//! System One questions, answer parsing, and configured decision-model transports.

use std::env;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Mutex, PoisonError};
use std::thread;
use std::time::Duration;

use agent_hire_store::SettingsStore;
use serde_json::{Map, Value, json};
use thiserror::Error;
use ureq::Agent;

use crate::kaggle::{self, KaggleManager};
use crate::rules::LEVELS;

/// The number of description characters sent to the decision model.
pub const STATE_CHARS: usize = 3500;
const PROBE_STATE: &str = "Distributed systems engineer building backend services. Requires 3 years of professional experience and Python proficiency.";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(120);

/// A configured System One endpoint.
#[derive(Clone)]
pub struct Endpoint {
    name: &'static str,
    url: String,
    key: String,
    model: String,
}

impl Endpoint {
    /// The identifier stored as a decision's source and shown by the API.
    #[must_use]
    pub fn name(&self) -> &'static str {
        self.name
    }

    /// The full System One URL for a configured HTTP endpoint.
    #[must_use]
    pub fn systemone_url(&self) -> String {
        let base = self.url.trim_end_matches('/');
        if base.ends_with("/v1/systemone") {
            base.to_owned()
        } else if base.ends_with("/v1") {
            format!("{base}/systemone")
        } else {
            format!("{base}/v1/systemone")
        }
    }

    /// The health URL on a configured HTTP endpoint.
    #[must_use]
    pub fn health_url(&self) -> String {
        let base = self.systemone_url();
        let root = base.strip_suffix("/v1/systemone").unwrap_or(&base);
        format!("{root}/health")
    }

    fn is_kaggle(&self) -> bool {
        self.name == "kaggle"
    }
}

/// A posting passed to the decision model.
#[derive(Clone, Copy, Debug)]
pub struct DecisionJob<'a> {
    /// ID used as the key of the classification response.
    pub id: &'a str,
    /// Posting title.
    pub title: &'a str,
    /// Employer name.
    pub company: &'a str,
    /// Posting description.
    pub description: &'a str,
}

/// A System One request failure.
#[derive(Debug, Error)]
#[non_exhaustive]
pub enum DecisionError {
    /// The server is rate-limiting requests.
    #[error("rate_limited")]
    RateLimited,
    /// The server returned an error HTTP status.
    #[error("http_{0}")]
    Http(u16),
    /// The answer object was missing or malformed.
    #[error("invalid_response")]
    InvalidResponse,
    /// The body was not JSON.
    #[error("JSONDecodeError")]
    InvalidJson,
    /// A numeric choice score could not be parsed.
    #[error("ValueError")]
    InvalidChoice,
    /// The transport could not deliver the request.
    #[error("{0}")]
    Transport(&'static str),
}

/// Client for configured decision endpoints, including the optional notebook gateway.
#[derive(Clone)]
pub struct DecisionClient {
    settings: SettingsStore,
    kaggle: KaggleManager,
    http: Agent,
}

impl DecisionClient {
    /// Creates a client that shares the app's Kaggle manager.
    #[must_use]
    pub fn new(settings: SettingsStore, kaggle: KaggleManager) -> Self {
        let http = Agent::new_with_config(
            Agent::config_builder()
                .max_redirects(0)
                .http_status_as_error(false)
                .build(),
        );
        Self {
            settings,
            kaggle,
            http,
        }
    }

    /// Returns Kaggle first when requested and ready, followed by the configured HTTP API.
    #[must_use]
    pub fn endpoints_from_settings(&self, check_kaggle: bool) -> Vec<Endpoint> {
        endpoints_from_settings(&self.settings, check_kaggle.then_some(&self.kaggle))
    }

    /// Sends typed System One questions. On success, returns the `answers` object.
    pub fn ask(
        &self,
        endpoint: &Endpoint,
        state: &str,
        questions: &Value,
        timeout: Duration,
    ) -> Result<Value, DecisionError> {
        let body = request_body(endpoint, state, questions);
        if endpoint.is_kaggle() {
            let responses = self
                .kaggle
                .classify_bodies(&[body])
                .map_err(|_| DecisionError::Transport("KaggleError"))?;
            return responses
                .into_iter()
                .next()
                .map_or(Err(DecisionError::InvalidResponse), answers);
        }
        let url = endpoint.systemone_url();
        let mut request = self
            .http
            .post(&url)
            .header("ngrok-skip-browser-warning", "1");
        if !endpoint.key.is_empty() {
            request = request.header("Authorization", &format!("Bearer {}", endpoint.key));
        }
        let mut response = request
            .config()
            .timeout_global(Some(timeout))
            .build()
            .send_json(&body)
            .map_err(transport_error)?;
        let status = response.status().as_u16();
        match status {
            429 => Err(DecisionError::RateLimited),
            400.. => Err(DecisionError::Http(status)),
            _ => {
                let data: Value = response.body_mut().read_json().map_err(transport_error)?;
                answers(data)
            }
        }
    }

    /// Asks a notebook a batch of typed questions, preserving input order.
    pub fn ask_notebook_batch(
        &self,
        endpoint: &Endpoint,
        batch: &[(String, Value)],
    ) -> Result<Vec<Option<Value>>, DecisionError> {
        let bodies = batch
            .iter()
            .map(|(state, questions)| request_body(endpoint, state, questions))
            .collect::<Vec<_>>();
        self.kaggle
            .classify_bodies(&bodies)
            .map(|results| {
                results
                    .into_iter()
                    .map(|result| answers(result).ok())
                    .collect()
            })
            .map_err(|_| DecisionError::Transport("KaggleError"))
    }

    /// Classifies postings for a role, keyed by job ID. Failed requests are absent.
    #[must_use]
    pub fn classify(
        &self,
        endpoint: &Endpoint,
        jobs: &[DecisionJob<'_>],
        family: Option<&str>,
        role_text: &str,
        concurrency: usize,
    ) -> Value {
        let questions = questions_for(family, role_text);
        if endpoint.is_kaggle() {
            return self.classify_kaggle(endpoint, jobs, &questions);
        }
        let order = AtomicUsize::new(0);
        let failures = AtomicUsize::new(0);
        let results = Mutex::new(vec![None; jobs.len()]);
        thread::scope(|scope| {
            let worker = || {
                while failures.load(Ordering::Relaxed) < 3 {
                    let index = order.fetch_add(1, Ordering::Relaxed);
                    let Some(job) = jobs.get(index) else { break };
                    let state = job_state(job.title, job.company, job.description);
                    match self.ask(endpoint, &state, &questions, REQUEST_TIMEOUT) {
                        Ok(answers) => {
                            let verdict = verdict(&answers);
                            results.lock().unwrap_or_else(PoisonError::into_inner)[index] = verdict;
                        }
                        Err(_) => {
                            failures.fetch_add(1, Ordering::Relaxed);
                        }
                    }
                }
            };
            for _ in 0..concurrency.max(1).min(jobs.len()) {
                scope.spawn(worker);
            }
        });
        let results = results.into_inner().unwrap_or_else(PoisonError::into_inner);
        let verdicts: Map<String, Value> = jobs
            .iter()
            .zip(results)
            .filter_map(|(job, verdict)| verdict.map(|verdict| (job.id.to_owned(), verdict)))
            .collect();
        Value::Object(verdicts)
    }

    fn classify_kaggle(
        &self,
        endpoint: &Endpoint,
        jobs: &[DecisionJob<'_>],
        questions: &Value,
    ) -> Value {
        let bodies: Vec<Value> = jobs
            .iter()
            .map(|job| {
                let state = job_state(job.title, job.company, job.description);
                request_body(endpoint, &state, questions)
            })
            .collect();
        let Ok(responses) = self.kaggle.classify_bodies(&bodies) else {
            return json!({});
        };
        let results: Map<String, Value> = jobs
            .iter()
            .zip(responses)
            .filter_map(|(job, response)| {
                verdict(&response["answers"]).map(|verdict| (job.id.to_owned(), verdict))
            })
            .collect();
        Value::Object(results)
    }

    /// Probes one configured endpoint and returns its reachability verdict.
    #[must_use]
    pub fn probe(&self, endpoint: &Endpoint) -> Value {
        let questions = questions_for(Some("software"), "software engineer");
        if endpoint.is_kaggle() {
            let job = DecisionJob {
                id: "probe",
                title: "Backend engineer",
                company: "Control",
                description: PROBE_STATE,
            };
            let answers = self.classify_kaggle(endpoint, &[job], &questions);
            let verdict = &answers["probe"];
            let mut result = json!({
                "ok":!verdict.is_null(), "endpoint":"kaggle",
                "role_p":verdict["role_p"], "level":verdict["level"],
            });
            if verdict.is_null() {
                result["error"] = json!("no answer from the Kaggle model");
            }
            return result;
        }
        let result = self
            .ask(endpoint, PROBE_STATE, &questions, Duration::from_secs(60))
            .and_then(|answers| {
                let probability = probability(&answers["role"]);
                Ok(json!({
                    "ok":probability.is_some(), "endpoint":endpoint.name,
                    "role_p":probability, "level":choice(&answers["experience"])?,
                }))
            });
        result.unwrap_or_else(
            |error| json!({"ok":false,"endpoint":endpoint.name,"error":error.to_string()}),
        )
    }

    /// Returns the `POST /jobindex/decision/probe` response.
    #[must_use]
    pub fn probe_route(&self) -> Value {
        let endpoints = self.endpoints_from_settings(true);
        if endpoints.is_empty() {
            return json!({"configured":false,"fallback":"rules","results":[]});
        }
        let results = thread::scope(|scope| {
            let calls: Vec<_> = endpoints
                .iter()
                .map(|endpoint| scope.spawn(move || self.probe(endpoint)))
                .collect();
            calls
                .into_iter()
                .zip(&endpoints)
                .map(|(call, endpoint)| {
                    call.join().unwrap_or_else(
                        |_| json!({"ok":false,"endpoint":endpoint.name,"error":"RuntimeError"}),
                    )
                })
                .collect::<Vec<Value>>()
        });
        json!({"configured":true,"results":results})
    }
}

/// Reads the configured endpoints. A manager enables the optional Kaggle readiness check.
#[must_use]
pub fn endpoints_from_settings(
    settings: &SettingsStore,
    kaggle: Option<&KaggleManager>,
) -> Vec<Endpoint> {
    let settings = kaggle::settings_snapshot(settings);
    let mut endpoints = Vec::new();
    if kaggle::enabled(&settings) && kaggle.is_some_and(KaggleManager::ready) {
        endpoints.push(Endpoint {
            name: "kaggle",
            url: String::new(),
            key: String::new(),
            model: "nimble-q8".to_owned(),
        });
    }
    let decision_setting = |key: &str, variable: &str| {
        setting_or_environment(kaggle::setting(&settings, key), env::var(variable).ok())
    };
    let url = decision_setting("decision_api_url", DECISION_URL_VARIABLE);
    if !url.is_empty() {
        endpoints.push(Endpoint {
            name: "decision_api",
            url,
            key: decision_setting("decision_api_key", DECISION_KEY_VARIABLE),
            model: decision_setting("decision_api_model", DECISION_MODEL_VARIABLE),
        });
    }
    endpoints
}

/// Environment fallback for `decision_api_url`, used when the app's setting is empty.
pub const DECISION_URL_VARIABLE: &str = "AGENT_HIRE_DECISION_API_URL";
/// Environment fallback for `decision_api_key`.
pub const DECISION_KEY_VARIABLE: &str = "AGENT_HIRE_DECISION_API_KEY";
/// Environment fallback for `decision_api_model`.
pub const DECISION_MODEL_VARIABLE: &str = "AGENT_HIRE_DECISION_API_MODEL";

/// The trimmed setting, else the trimmed environment value, else `""`.
fn setting_or_environment(setting: &str, environment: Option<String>) -> String {
    let setting = setting.trim();
    if setting.is_empty() {
        environment.map_or_else(String::new, |value| value.trim().to_owned())
    } else {
        setting.to_owned()
    }
}

/// Formats a job as System One state, cutting the trimmed description by Unicode character.
#[must_use]
pub fn job_state(title: &str, company: &str, description: &str) -> String {
    let description =
        description.trim_matches(|c: char| c.is_whitespace() || matches!(c, '\x1c'..='\x1f'));
    let limit = description
        .char_indices()
        .nth(STATE_CHARS)
        .map_or(description.len(), |(end, _)| end);
    format!(
        "Title: {title}\nCompany: {company}\n\n{}",
        &description[..limit]
    )
}

/// Builds typed role-match and experience questions.
#[must_use]
pub fn questions_for(family: Option<&str>, role_text: &str) -> Value {
    let role = if family == Some("software") {
        json!({
            "type":"noul", "instructions":"Does this role belong to software development and engineering?",
            "criteria":{
                "true":"Software development, software QA, DevOps/SRE, cloud/platform, data/ML/AI engineering, or software architecture.",
                "false":"Sales, recruiting, HR, marketing, finance, healthcare, non-software engineering, general IT support, or purely functional/business consulting.",
            },
        })
    } else {
        json!({
            "type":"noul", "instructions":format!("Is this job a '{role_text}' role or a very close equivalent?"),
            "criteria":{
                "true": format!("The job's main function is {role_text} work."),
                "false": format!("The job's main function is something other than {role_text}."),
            },
        })
    };
    json!({
        "role":role,
        "experience":{
            "type":"choice","instructions":"Classify the required minimum professional experience for this job.",
            "criteria":{
                "intern":"Internship or apprenticeship, still studying",
                "fresher":"No experience required: fresher or new graduate",
                "junior":"1-2 years","mid":"3-4 years","senior":"5-8 years","lead":"8+ years, staff/principal/lead",
            },
        },
    })
}

fn request_body(endpoint: &Endpoint, state: &str, questions: &Value) -> Value {
    let mut body = json!({"state":state,"questions":questions});
    if !endpoint.model.is_empty() {
        body["model"] = json!(endpoint.model);
    }
    body
}

fn answers(mut value: Value) -> Result<Value, DecisionError> {
    let answers = value.get_mut("answers").map_or(Value::Null, Value::take);
    if answers.is_object() {
        Ok(answers)
    } else {
        Err(DecisionError::InvalidResponse)
    }
}

fn transport_error(error: ureq::Error) -> DecisionError {
    match error {
        ureq::Error::Json(_) => DecisionError::InvalidJson,
        ureq::Error::Timeout(_) => DecisionError::Transport("ReadTimeout"),
        ureq::Error::BadUri(_) | ureq::Error::Http(_) => {
            DecisionError::Transport("UnsupportedProtocol")
        }
        ureq::Error::Protocol(_) => DecisionError::Transport("RemoteProtocolError"),
        _ => DecisionError::Transport("ConnectError"),
    }
}

fn probability(answer: &Value) -> Option<f64> {
    ["noul", "probability", "bool", "p"]
        .into_iter()
        .find_map(|key| {
            answer[key]
                .as_f64()
                .or_else(|| answer[key].as_bool().map(|flag| f64::from(u8::from(flag))))
        })
}

/// Resolves a System One choice from a direct label or a scored distribution.
pub fn choice(answer: &Value) -> Result<Option<&str>, DecisionError> {
    for key in ["choice", "probabilities", "distribution", "scores"] {
        let value = &answer[key];
        if let Some(label) = value.as_str() {
            return Ok(Some(label));
        }
        if let Some(scores) = value.as_object().filter(|scores| !scores.is_empty()) {
            let mut best: Option<(&str, f64)> = None;
            for (label, value) in scores {
                let score = match value {
                    Value::Null => 0.0,
                    Value::Bool(flag) => f64::from(u8::from(*flag)),
                    Value::String(text) if text.is_empty() => 0.0,
                    Value::String(text) => text
                        .trim()
                        .parse()
                        .map_err(|_| DecisionError::InvalidChoice)?,
                    _ => value.as_f64().ok_or(DecisionError::InvalidChoice)?,
                };
                if best.is_none_or(|(_, previous)| score > previous) {
                    best = Some((label, score));
                }
            }
            return Ok(best.map(|(label, _)| label));
        }
    }
    Ok(["answer", "label", "value"]
        .into_iter()
        .find_map(|key| answer[key].as_str()))
}

fn verdict(answers: &Value) -> Option<Value> {
    let probability = probability(&answers["role"])?;
    let level = choice(&answers["experience"])
        .ok()?
        .filter(|level| LEVELS.contains(level));
    Some(json!({
        "role_match":probability >= 0.5, "role_p":round_probability(probability), "level":level,
    }))
}

/// Rounds probabilities to the four decimal places used in stored verdicts.
#[must_use]
pub fn round_probability(probability: f64) -> f64 {
    // Formatting rounds the input float directly, including values just to either side of a tie.
    format!("{probability:.4}").parse().unwrap_or(probability)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{DecisionError, answers, choice, job_state, setting_or_environment, verdict};

    #[test]
    fn decision_settings_win_over_the_environment() {
        let from_env = Some(" https://env.example/v1 ".to_owned());
        assert_eq!(
            setting_or_environment(" ", from_env.clone()),
            "https://env.example/v1"
        );
        assert_eq!(
            setting_or_environment("https://app.example", from_env),
            "https://app.example"
        );
        assert_eq!(setting_or_environment("", None), "");
    }

    #[test]
    fn rejects_non_object_answer_bodies() {
        for response in [
            json!([{"answers":{}}]),
            json!(7),
            json!({"answers":["mid"]}),
        ] {
            assert!(matches!(
                answers(response),
                Err(DecisionError::InvalidResponse)
            ));
        }
    }

    #[test]
    fn parses_ordered_scores_and_boolean_probabilities() {
        assert_eq!(
            choice(&json!({"scores":{"senior":"0.80","junior":0.8}})).expect("valid scores"),
            Some("senior")
        );
        assert_eq!(
            verdict(&json!({"role":{"bool":true},"experience":{"distribution":{"invented":0.9}}})),
            Some(json!({"role_match":true,"role_p":1.0,"level":null}))
        );
        assert_eq!(
            verdict(&json!({"role":{"p":0.49999},"experience":{"choice":"mid"}})),
            Some(json!({"role_match":false,"role_p":0.5,"level":"mid"}))
        );
        assert!(verdict(&json!({"role":{"p":"0.9"}})).is_none());
    }

    #[test]
    fn cuts_the_trimmed_state_by_characters() {
        let description = format!("   {}!  ", "👩".repeat(super::STATE_CHARS));
        assert_eq!(
            job_state("Engineer", "Example", &description),
            format!(
                "Title: Engineer\nCompany: Example\n\n{}",
                "👩".repeat(super::STATE_CHARS)
            )
        );
    }
}
