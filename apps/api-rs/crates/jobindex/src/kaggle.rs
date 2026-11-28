//! Kaggle notebook connection, deployment status, and remote inference.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::{Duration, Instant};

use agent_hire_store::{SettingsError, SettingsStore};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use serde_json::{Value, json};
use thiserror::Error;

use crate::ModelDirectories;
use crate::jupyter::{self, Jupyter};
use crate::{kaggle_code, kaggle_deploy};

/// Whether Kaggle-backed inference is enabled.
pub const KEY_ENABLED: &str = "kaggle_enabled";
/// The token-bearing notebook link.
pub const KEY_LINK: &str = "kaggle_connection_token";
/// The decision and classifier gateway credential.
pub const KEY_GATEWAY: &str = "kaggle_gateway_key";
/// The owned deployment in the notebook.
pub const KEY_DEPLOYMENT: &str = "kaggle_deployment_id";
/// The encoder-device preference.
pub const KEY_ENCODER_DEVICE: &str = "encoder_device";

const RECHECK_INTERVAL: Duration = Duration::from_secs(60);
const STATUS_TTL: Duration = Duration::from_secs(5);
const MODEL_BYTES: f64 = 9_527_503_392.0;
const DECISION_BATCH: usize = 32;

/// Maximum job texts per remote classifier request.
pub const ENCODE_BATCH: usize = 2048;
/// Minimum number of rows to send to Kaggle in automatic mode.
pub const KAGGLE_MIN_JOBS: usize = 1000;

/// A notebook or settings failure with the HTTP status expected by the route.
#[derive(Debug, Error)]
#[error("{message}")]
pub struct KaggleError {
    status: u16,
    message: String,
}

impl KaggleError {
    pub(crate) fn new(message: impl std::fmt::Display) -> Self {
        Self {
            status: 422,
            message: message.to_string(),
        }
    }

    /// HTTP status for the route's error response.
    #[must_use]
    pub fn status(&self) -> u16 {
        self.status
    }

    /// The FastAPI error body for this failure.
    #[must_use]
    pub fn body(&self) -> Value {
        json!({"detail": self.message})
    }
}

impl From<SettingsError> for KaggleError {
    fn from(error: SettingsError) -> Self {
        Self {
            status: 500,
            message: error.to_string(),
        }
    }
}

/// A notebook encoder verdict.
#[derive(Debug)]
pub struct RemotePrediction {
    /// The model's winning label.
    pub label: String,
    /// The probability returned by the notebook.
    pub probability: f64,
}

/// A cloneable handle to one notebook connection per app runtime.
#[derive(Clone)]
pub struct KaggleManager {
    inner: Arc<Inner>,
}

struct Inner {
    settings: SettingsStore,
    models: ModelDirectories,
    connection: Mutex<Connection>,
    cache: Mutex<Status>,
    rechecking: AtomicBool,
}

#[derive(Default)]
struct Connection {
    client: Option<Jupyter>,
    link: String,
}

struct Status {
    value: Value,
    checked_at: Option<Instant>,
    link: String,
}

impl Default for Status {
    fn default() -> Self {
        Self {
            value: disconnected(),
            checked_at: None,
            link: String::new(),
        }
    }
}

impl KaggleManager {
    /// Creates a manager using the app's settings and local encoder paths.
    #[must_use]
    pub fn new(settings: SettingsStore, models: ModelDirectories) -> Self {
        Self {
            inner: Arc::new(Inner {
                settings,
                models,
                connection: Mutex::default(),
                cache: Mutex::default(),
                rechecking: AtomicBool::new(false),
            }),
        }
    }

    /// Migrates masked notebook credentials before the runtime begins serving requests.
    pub fn migrate_legacy_settings(&self) -> Result<(), KaggleError> {
        migrate_legacy_settings(&self.inner.settings)
    }

    fn settings(&self) -> HashMap<String, String> {
        settings_snapshot(&self.inner.settings)
    }

    fn cache(&self) -> MutexGuard<'_, Status> {
        self.inner
            .cache
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
    }

    fn connection(&self) -> MutexGuard<'_, Connection> {
        self.inner
            .connection
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
    }

    /// Returns the public status with the current saved preferences.
    #[must_use]
    pub fn public_status(&self) -> Value {
        public_status(self.cache().value.clone(), &self.settings())
    }

    /// Refreshes notebook state. Pass `true` to bypass the five-second cache.
    #[must_use]
    pub fn refresh(&self, force: bool) -> Value {
        let settings = self.settings();
        let link = setting(&settings, KEY_LINK);
        if link.is_empty() {
            *self.cache() = Status::default();
            return public_status(disconnected(), &settings);
        }
        if !force && let Some(recent) = self.recent(link) {
            return public_status(recent, &settings);
        }
        let mut connection = self.connection();
        let settings = self.settings();
        let link = setting(&settings, KEY_LINK);
        if link.is_empty() {
            *self.cache() = Status::default();
            return public_status(disconnected(), &settings);
        }
        if !force && let Some(recent) = self.recent(link) {
            return public_status(recent, &settings);
        }
        let status = match connection
            .client(link)
            .and_then(|client| read_status(client, &settings, 45))
        {
            Ok(status) => status,
            Err(error) => {
                connection.client = None;
                json!({"connected":false,"phase":"unreachable","error":error.to_string()})
            }
        };
        self.record(status, link, &settings)
    }

    fn recent(&self, link: &str) -> Option<Value> {
        let cache = self.cache();
        (cache.link == link && cache.checked_at.is_some_and(|at| at.elapsed() < STATUS_TTL))
            .then(|| cache.value.clone())
    }

    fn record(&self, value: Value, link: &str, settings: &HashMap<String, String>) -> Value {
        let result = public_status(value.clone(), settings);
        *self.cache() = Status {
            value,
            checked_at: Some(Instant::now()),
            link: link.to_owned(),
        };
        result
    }

    /// Saves a tested notebook link and starts or reuses the owned deployments.
    pub fn connect(&self, link: &str) -> Result<Value, KaggleError> {
        let mut connection = self.connection();
        let settings = self.settings();
        let link = if link.trim().is_empty() {
            setting(&settings, KEY_LINK)
        } else {
            link.trim()
        };
        if !is_jupyter_link(link) {
            return Err(KaggleError::new(
                "That doesn't look like a Kaggle notebook link. In the notebook, copy the full URL that contains ?token=…",
            ));
        }
        let client = Jupyter::new(link)?;
        client.validate()?;
        *connection = Connection {
            client: Some(client),
            link: link.to_owned(),
        };
        self.save(&[(KEY_LINK, link), (KEY_ENABLED, "true")])?;
        let settings = self.settings();
        let client = connection.client(link)?;
        let state = client.last_json(kaggle_code::STATE_CODE, 60, &Value::Null)?;
        let deployment = setting(&settings, KEY_DEPLOYMENT);
        let key = setting(&settings, KEY_GATEWAY);
        if owns(&state, deployment) && is_running(&state) && !key.is_empty() {
            let classifiers = &state["classifiers"];
            if !owns(classifiers, deployment) || !is_running(classifiers) {
                kaggle_deploy::classifiers(client, &self.inner.models, key, deployment)?;
            }
            let status = read_status(client, &settings, 45)?;
            return Ok(self.record(status, link, &settings));
        }

        let mut bytes = [0u8; 32];
        getrandom::fill(&mut bytes).map_err(KaggleError::new)?;
        let key = URL_SAFE_NO_PAD.encode(bytes);
        let deployment = jupyter::random_id(16)?;
        kaggle_deploy::decision(client, &key, &deployment)?;
        self.save(&[(KEY_GATEWAY, &key), (KEY_DEPLOYMENT, &deployment)])?;
        kaggle_deploy::classifiers(client, &self.inner.models, &key, &deployment)?;
        let status =
            json!({"connected":true,"phase":"starting","deployment_id":deployment,"progress":0.0});
        Ok(self.record(status, link, &self.settings()))
    }

    /// Changes whether Kaggle inference may be used, keeping the connection settings.
    pub fn toggle(&self, enabled: bool) -> Result<Value, KaggleError> {
        self.save(&[(KEY_ENABLED, if enabled { "true" } else { "false" })])?;
        Ok(self.public_status())
    }

    /// Sets the preference to `auto`, `kaggle`, or `local`.
    pub fn set_encoder_device(&self, device: &str) -> Result<Value, KaggleError> {
        let device = device.to_lowercase();
        if !matches!(device.as_str(), "auto" | "kaggle" | "local") {
            return Err(KaggleError {
                status: 400,
                message: "device must be auto, kaggle or local".to_owned(),
            });
        }
        self.save(&[(KEY_ENCODER_DEVICE, &device)])?;
        Ok(self.public_status())
    }

    /// Stops the notebook services and clears the deployment credentials.
    pub fn stop(&self) -> Result<Value, KaggleError> {
        let mut connection = self.connection();
        let settings = self.settings();
        let link = setting(&settings, KEY_LINK);
        let result = connection
            .client(link)
            .and_then(|client| {
                let mut result = client.last_json(kaggle_code::STOP_CODE, 60, &Value::Null)?;
                let stopped =
                    client.last_json(kaggle_code::CLASSIFIER_STOP_CODE, 60, &Value::Null)?;
                if let Some(result) = result.as_object_mut() {
                    result.insert("classifiers_stopped".to_owned(), stopped["stopped"].clone());
                }
                Ok(result)
            })
            .unwrap_or_else(|error| json!({"stopped":false,"error":error.to_string()}));
        self.save(&[(KEY_DEPLOYMENT, ""), (KEY_GATEWAY, "")])?;
        self.record(
            json!({"connected":!connection.link.is_empty(),"phase":"stopped"}),
            link,
            &settings,
        );
        Ok(result)
    }

    /// Clears the notebook link, gateway credential, and deployment ID.
    pub fn forget(&self) -> Result<Value, KaggleError> {
        let mut connection = self.connection();
        for key in [KEY_LINK, KEY_GATEWAY, KEY_DEPLOYMENT] {
            self.inner
                .settings
                .delete(key)
                .map_err(SettingsError::from)?;
        }
        *connection = Connection::default();
        *self.cache() = Status::default();
        Ok(self.public_status())
    }

    /// Reports decision-model readiness, checking unready notebooks in the background.
    #[must_use]
    pub fn ready(&self) -> bool {
        self.service_ready(false)
    }

    /// Reports remote encoder readiness, checking unready notebooks in the background.
    #[must_use]
    pub fn classifiers_ready(&self) -> bool {
        self.service_ready(true)
    }

    fn service_ready(&self, classifiers: bool) -> bool {
        let settings = self.settings();
        let link = setting(&settings, KEY_LINK);
        if !enabled(&settings) || link.is_empty() {
            return false;
        }
        let ready = |status: &Value| {
            let service = if classifiers {
                &status["classifiers"]
            } else {
                status
            };
            service["phase"] == "ready"
        };
        let cache = self.cache();
        let was_ready = cache.link == link && ready(&cache.value);
        let due = cache.link != link
            || cache
                .checked_at
                .is_none_or(|at| at.elapsed() >= RECHECK_INTERVAL);
        drop(cache);
        if was_ready {
            return ready(&self.refresh(false));
        }
        if due && !self.inner.rechecking.swap(true, Ordering::AcqRel) {
            let manager = self.clone();
            let result = thread::Builder::new()
                .name("kaggle-recheck".to_owned())
                .spawn(move || {
                    let _ = manager.refresh(true);
                    manager.inner.rechecking.store(false, Ordering::Release);
                });
            if result.is_err() {
                self.inner.rechecking.store(false, Ordering::Release);
            }
        }
        false
    }

    /// The saved encoder preference with invalid values normalized to `auto`.
    #[must_use]
    pub fn encoder_device(&self) -> &'static str {
        encoder_device_setting(&self.settings())
    }

    /// Reports whether the notebook is connected to the ready classifier service.
    #[must_use]
    pub fn classifiers_cached_ready(&self) -> bool {
        let settings = self.settings();
        let cache = self.cache();
        enabled(&settings)
            && cache.link == setting(&settings, KEY_LINK)
            && cache.value["classifiers"]["phase"] == "ready"
    }

    /// Sends System One request bodies through the notebook's decision gateway.
    pub fn classify_bodies(&self, bodies: &[Value]) -> Result<Vec<Value>, KaggleError> {
        let mut connection = self.connection();
        let settings = self.settings();
        let key = setting(&settings, KEY_GATEWAY);
        if key.is_empty() {
            return Err(KaggleError::new("The Kaggle model is not deployed yet"));
        }
        let client = connection.client(setting(&settings, KEY_LINK))?;
        let mut results = Vec::with_capacity(bodies.len());
        for batch in bodies.chunks(DECISION_BATCH) {
            let metadata = json!({"bodies":batch,"key":key});
            let response = client.last_json(kaggle_code::CLASSIFY_CODE, 600, &metadata)?;
            let Value::Array(responses) = response else {
                return Err(KaggleError::new("Invalid Kaggle decision response"));
            };
            results.extend(responses);
        }
        Ok(results)
    }

    /// Encodes prepared texts, preserving input order across notebook batches.
    pub fn encode(
        &self,
        model: &str,
        texts: &[String],
    ) -> Result<Vec<RemotePrediction>, KaggleError> {
        let mut connection = self.connection();
        let settings = self.settings();
        let key = setting(&settings, KEY_GATEWAY);
        if key.is_empty() {
            return Err(KaggleError::new(
                "The Kaggle classifiers are not deployed yet",
            ));
        }
        let client = connection.client(setting(&settings, KEY_LINK))?;
        let mut results = Vec::with_capacity(texts.len());
        for batch in texts.chunks(ENCODE_BATCH) {
            let metadata = json!({"model":model,"texts":batch,"key":key});
            let value = client.last_json(kaggle_code::ENCODE_CODE, 900, &metadata)?;
            let rows = value["results"]
                .as_array()
                .filter(|rows| rows.len() == batch.len())
                .ok_or_else(|| {
                    let error = value["error"].as_str().unwrap_or("bad answer");
                    let error: String = client
                        .clean(error)
                        .replace(key, "[REDACTED]")
                        .chars()
                        .take(200)
                        .collect();
                    KaggleError::new(format!("classifier service: {error}"))
                })?;
            for row in rows {
                let label = row[0]
                    .as_str()
                    .ok_or_else(|| KaggleError::new("Invalid classifier label"))?;
                let probability = row[1]
                    .as_f64()
                    .or_else(|| row[1].as_str()?.parse().ok())
                    .filter(|p| p.is_finite())
                    .ok_or_else(|| KaggleError::new("Invalid classifier probability"))?;
                results.push(RemotePrediction {
                    label: label.to_owned(),
                    probability,
                });
            }
        }
        Ok(results)
    }

    fn save(&self, values: &[(&str, &str)]) -> Result<(), KaggleError> {
        let pairs: Vec<(String, String)> = values
            .iter()
            .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
            .collect();
        self.inner.settings.save(&pairs)?;
        Ok(())
    }
}

impl Connection {
    fn client(&mut self, link: &str) -> Result<&mut Jupyter, KaggleError> {
        if link.is_empty() {
            return Err(KaggleError::new(
                "No Kaggle link yet — paste one in the Kaggle box at the top right.",
            ));
        }
        if self.client.is_none() || self.link != link {
            self.client = Some(Jupyter::new(link)?);
            self.link = link.to_owned();
        }
        self.client
            .as_mut()
            .ok_or_else(|| KaggleError::new("Missing Kaggle client"))
    }
}

/// Checks whether the URL is a token-bearing HTTPS notebook link.
#[must_use]
pub fn is_jupyter_link(link: &str) -> bool {
    jupyter::parse_link(link).is_some()
}

/// Moves a legacy notebook link into the masked setting and clears the old fields.
pub fn migrate_legacy_settings(settings: &SettingsStore) -> Result<(), KaggleError> {
    let snapshot: HashMap<String, String> = settings
        .all()
        .map_err(SettingsError::from)?
        .into_iter()
        .collect();
    let link = setting(&snapshot, "kaggle_server_url").trim();
    if link.is_empty() && setting(&snapshot, "kaggle_server_key").is_empty() {
        return Ok(());
    }
    let mut pairs = vec![
        ("kaggle_server_url".to_owned(), String::new()),
        ("kaggle_server_key".to_owned(), String::new()),
        ("kaggle_server_model".to_owned(), String::new()),
    ];
    if is_jupyter_link(link) && setting(&snapshot, KEY_LINK).is_empty() {
        pairs.push((KEY_LINK.to_owned(), link.to_owned()));
        pairs.push((KEY_ENABLED.to_owned(), "true".to_owned()));
    }
    settings.save(&pairs)?;
    Ok(())
}

pub(crate) fn settings_snapshot(settings: &SettingsStore) -> HashMap<String, String> {
    settings.all().unwrap_or_default().into_iter().collect()
}

pub(crate) fn setting<'a>(settings: &'a HashMap<String, String>, key: &str) -> &'a str {
    settings.get(key).map_or("", String::as_str)
}

pub(crate) fn enabled(settings: &HashMap<String, String>) -> bool {
    matches!(
        setting(settings, KEY_ENABLED).to_lowercase().as_str(),
        "true" | "1" | "yes"
    )
}

fn encoder_device_setting(settings: &HashMap<String, String>) -> &'static str {
    match setting(settings, KEY_ENCODER_DEVICE)
        .to_lowercase()
        .as_str()
    {
        "kaggle" => "kaggle",
        "local" => "local",
        _ => "auto",
    }
}

fn public_status(mut value: Value, settings: &HashMap<String, String>) -> Value {
    value["enabled"] = json!(enabled(settings));
    value["ready"] = json!(value["phase"] == "ready");
    value["has_link"] = json!(!setting(settings, KEY_LINK).is_empty());
    value["encoder_device"] = json!(encoder_device_setting(settings));
    value
}

fn disconnected() -> Value {
    json!({"connected":false,"phase":null})
}

fn owns(service: &Value, deployment: &str) -> bool {
    !deployment.is_empty() && service["deployment_id"].as_str() == Some(deployment)
}

fn is_running(service: &Value) -> bool {
    service["phase"]
        .as_str()
        .is_some_and(|phase| !matches!(phase, "failed" | "stopped"))
}

fn read_status(
    client: &mut Jupyter,
    settings: &HashMap<String, String>,
    timeout: u64,
) -> Result<Value, KaggleError> {
    let state = client.last_json(kaggle_code::STATE_CODE, timeout, &Value::Null)?;
    let deployment = setting(settings, KEY_DEPLOYMENT);
    let key = setting(settings, KEY_GATEWAY);
    let sanitize = |value: &Value| {
        value.as_str().map(|text| {
            let text = client.clean(text);
            if key.is_empty() {
                text
            } else {
                text.replace(key, "[REDACTED]")
            }
        })
    };
    let log_tail = |value: &Value| -> Option<String> {
        let value = sanitize(value)?;
        let end = value
            .char_indices()
            .rev()
            .nth(299)
            .map_or(0, |(start, _)| start);
        Some(value[end..].to_owned())
    };
    let ours = owns(&state, deployment);
    let phase = if ours {
        state["phase"].as_str()
    } else {
        Some("not_deployed")
    };
    let error = if ours {
        sanitize(&state["error"])
    } else {
        None
    };
    let log = if phase == Some("failed") {
        Some(log_tail(&state["log"]).unwrap_or_default())
    } else {
        None
    };
    let progress = (state["downloaded"].as_f64().unwrap_or_default() / MODEL_BYTES).min(1.0);
    let cls = &state["classifiers"];
    let classifiers = if owns(cls, deployment) {
        json!({"phase":cls["phase"],"gpus":cls["gpus"],"jobs_per_s":cls["jobs_per_s"],
               "error":sanitize(&cls["error"]), "log_tail":log_tail(&cls["log"]).filter(|log| !log.is_empty())})
    } else {
        json!({"phase":null})
    };
    Ok(json!({
        "connected":true, "phase":phase, "error":error, "replicas":state["replicas"],
        "gpus":state["gpus"],"progress":(progress * 1_000.0).round_ties_even() / 1_000.0,
        "log_tail":log, "classifiers":classifiers,
    }))
}
