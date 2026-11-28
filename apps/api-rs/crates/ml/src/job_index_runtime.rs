//! Server-facing job index routes and blocking worker lifecycle.

use std::fs;
use std::io;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use agent_hire_jobindex::autoclassify::{self, Autoclassifier};
use agent_hire_jobindex::decision::{DecisionClient, Endpoint};
use agent_hire_jobindex::kaggle::{KaggleError, KaggleManager};
use agent_hire_jobindex::runs::{self, TickMode};
use agent_hire_jobindex::{self as jobindex, JobIndex, ModelDirectories, decision_store};
use agent_hire_store::SettingsStore;
use serde_json::{Value, json};

use crate::ModelKind;
use crate::encoder_worker::EncoderWorker;
use crate::worker_signal::WorkerSignal;

const PROGRESS_TTL: Duration = Duration::from_secs(10);
const BACKLOG_TTL: Duration = Duration::from_secs(30);

/// One app's job-index routes and background workers.
///
/// Route methods perform blocking database or network I/O. An async server can invoke them
/// through its blocking pool. Start one runtime for each served job index.
pub struct JobIndexRuntime {
    state: Arc<State>,
    lifecycle: Mutex<()>,
    workers: Mutex<Option<Workers>>,
}

struct State {
    index: JobIndex,
    settings: SettingsStore,
    scraper: PathBuf,
    models: ModelDirectories,
    kaggle: KaggleManager,
    decisions: DecisionClient,
    autoclassifier: Autoclassifier,
    level_loaded: AtomicBool,
    department_loaded: AtomicBool,
    cached: Mutex<CachedCounts>,
}

#[derive(Default)]
struct CachedCounts {
    backlogs: Option<CachedValue>,
    progress: Option<CachedValue>,
}

struct CachedValue {
    recorded: Instant,
    data: Value,
}

struct Workers {
    signal: Arc<WorkerSignal>,
    threads: Vec<JoinHandle<()>>,
}

impl JobIndexRuntime {
    /// Creates the route handlers without starting background threads or loading the models.
    #[must_use]
    pub fn new(index: JobIndex, settings: SettingsStore, scraper: PathBuf) -> Self {
        let models = ModelDirectories::from_jobs_dir(index.dir());
        Self::with_models(index, settings, scraper, models)
    }

    /// Creates the runtime with explicit model directories.
    #[must_use]
    pub fn with_models(
        index: JobIndex,
        settings: SettingsStore,
        scraper: PathBuf,
        models: ModelDirectories,
    ) -> Self {
        let kaggle = KaggleManager::new(settings.clone(), models.clone());
        let decisions = DecisionClient::new(settings.clone(), kaggle.clone());
        let autoclassifier = Autoclassifier::new(index.clone(), decisions.clone());
        Self {
            state: Arc::new(State {
                index,
                settings,
                scraper,
                models,
                kaggle,
                decisions,
                autoclassifier,
                level_loaded: AtomicBool::new(false),
                department_loaded: AtomicBool::new(false),
                cached: Mutex::default(),
            }),
            lifecycle: Mutex::new(()),
            workers: Mutex::new(None),
        }
    }

    /// Migrates legacy Kaggle settings. Servers can also call this when services are disabled.
    pub fn migrate_legacy_settings(&self) -> Result<(), KaggleError> {
        self.state.kaggle.migrate_legacy_settings()
    }

    /// Starts the two inference workers and the 30-second scrape ticker once.
    ///
    /// Reconciles exited scrapes before ticking. Existing saved jobs can also wake inference
    /// through `notify`. Call this on a blocking thread.
    pub fn start(&self) -> io::Result<()> {
        let _lifecycle = self
            .lifecycle
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        if self
            .workers
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .is_some()
        {
            return Ok(());
        }
        fs::create_dir_all(self.state.index.dir())?;
        if let Err(error) = self.migrate_legacy_settings() {
            eprintln!("could not migrate legacy Kaggle settings: {error}");
        }
        let signal = Arc::new(WorkerSignal::default());
        if let Err(error) = runs::interrupt_orphans(&self.state.index, || {
            self.state.invalidate_counts();
            signal.notify();
        }) {
            eprintln!("could not reconcile interrupted scrape runs: {error}");
        }
        let mut handles = Workers {
            signal,
            threads: Vec::with_capacity(3),
        };
        for kind in [ModelKind::Level, ModelKind::Department] {
            let state = Arc::clone(&self.state);
            let signal = Arc::clone(&handles.signal);
            let name = match kind {
                ModelKind::Level => "jobindex-level",
                ModelKind::Department => "jobindex-department",
            };
            let handle = thread::Builder::new().name(name.to_owned()).spawn(move || {
                state
                    .encoder(kind)
                    .run(&signal, || state.invalidate_counts());
            });
            handles.push(handle)?;
        }
        let state = Arc::clone(&self.state);
        let signal = Arc::clone(&handles.signal);
        let tick = thread::Builder::new()
            .name("jobindex-scrape".to_owned())
            .spawn(move || {
                while signal.wait(Duration::from_secs(runs::SCRAPE_TICK_SECONDS), None) {
                    if let Err(error) = state.tick(TickMode::Periodic, &signal) {
                        eprintln!("job index tick failed: {error}");
                    }
                }
            });
        handles.push(tick)?;
        *self.workers.lock().unwrap_or_else(PoisonError::into_inner) = Some(handles);
        Ok(())
    }

    /// Wakes both encoder workers after externally added or updated jobs.
    pub fn notify(&self) {
        self.state.invalidate_counts();
        let workers = self.workers.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(workers) = workers.as_ref() {
            workers.signal.notify();
        }
    }

    /// Stops the ticker and joins the workers after in-flight I/O or inference returns.
    pub fn stop(&self) {
        let _lifecycle = self
            .lifecycle
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let workers = self
            .workers
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        drop(workers);
    }

    /// Returns the local level runtime device, initially `not loaded`.
    #[must_use]
    pub fn device(&self) -> &'static str {
        self.state.device()
    }

    /// Returns the uncached `level_model` entry used in the status response.
    pub fn level_backlog_counts(&self) -> rusqlite::Result<Value> {
        self.state.encoder_backlog(ModelKind::Level)
    }

    /// Returns the uncached `department_model` entry used in the status response.
    pub fn department_backlog_counts(&self) -> rusqlite::Result<Value> {
        self.state.encoder_backlog(ModelKind::Department)
    }

    /// Returns the `GET /jobindex/status` response.
    pub fn status(&self) -> rusqlite::Result<Value> {
        let connection = self.state.index.connect()?;
        let index = jobindex::stats_cached(&self.state.index, &connection)?;
        let stored = self.state.settings.get("scrape_refresh_hours", "12").ok();
        let scrape = runs::run_status(
            &self.state.index,
            None,
            runs::refresh_hours(stored.as_deref()),
        )?;
        let endpoints: Vec<&str> = self
            .state
            .decisions
            .endpoints_from_settings(false)
            .iter()
            .map(Endpoint::name)
            .collect();
        let backlogs = self.state.backlogs()?;
        let mut level = backlogs["level_model"].clone();
        level["device"] = json!(self.device());
        Ok(json!({
            "index":index, "scrape":scrape, "decision_endpoints":endpoints,
            "classification":backlogs["classification"], "level_model":level,
            "department_model":backlogs["department_model"],
            "companies_available":runs::verified_slugs(&self.state.scraper).len(),
            "jobs_dir":self.state.index.dir(),
        }))
    }

    /// Returns the `GET /jobindex/encoders/progress` response.
    pub fn progress(&self) -> rusqlite::Result<Value> {
        let counts = self
            .state
            .cached
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let recent = fresh(counts.progress.as_ref(), PROGRESS_TTL);
        drop(counts);
        let device = if self.state.kaggle.classifiers_cached_ready() {
            "kaggle"
        } else {
            self.device()
        };
        if let Some(mut recent) = recent {
            recent["device"] = json!(device);
            return Ok(recent);
        }
        let progress = decision_store::encoder_progress(
            &self.state.index.connect()?,
            jobindex::model_available(&self.state.models.level),
            jobindex::model_available(&self.state.models.department),
            device,
        )?;
        self.state
            .cached
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .progress = Some(CachedValue {
            recorded: Instant::now(),
            data: progress.clone(),
        });
        Ok(progress)
    }

    /// Returns the `POST /jobindex/classify` response for a validated batch limit of 1–400.
    pub fn classify(&self, limit: i64) -> rusqlite::Result<Value> {
        let result = self.state.autoclassifier.classify_route(limit);
        self.state.invalidate_counts();
        result
    }

    /// Returns the `POST /jobindex/decision/probe` response.
    #[must_use]
    pub fn probe(&self) -> Value {
        self.state.decisions.probe_route()
    }

    /// The shared System One decision client for form filling.
    #[must_use]
    pub fn decisions(&self) -> &DecisionClient {
        &self.state.decisions
    }

    /// The shared manager for the Kaggle routes.
    #[must_use]
    pub fn kaggle(&self) -> &KaggleManager {
        &self.state.kaggle
    }

    /// The manual software backlog classifier, including counts and its inert scheduler tick.
    #[must_use]
    pub fn autoclassifier(&self) -> &Autoclassifier {
        &self.state.autoclassifier
    }
}

impl Drop for JobIndexRuntime {
    fn drop(&mut self) {
        self.stop();
    }
}

impl Workers {
    fn push(&mut self, thread: io::Result<JoinHandle<()>>) -> io::Result<()> {
        self.threads.push(thread?);
        Ok(())
    }
}

impl Drop for Workers {
    fn drop(&mut self) {
        self.signal.stop();
        for handle in self.threads.drain(..) {
            if handle.join().is_err() {
                eprintln!("a job index worker panicked");
            }
        }
    }
}

impl State {
    fn device(&self) -> &'static str {
        if self.level_loaded.load(Ordering::Acquire) {
            "cpu"
        } else {
            "not loaded"
        }
    }

    fn invalidate_counts(&self) {
        *self.cached.lock().unwrap_or_else(PoisonError::into_inner) = CachedCounts::default();
    }

    fn encoder(&self, kind: ModelKind) -> EncoderWorker<'_> {
        let (path, loaded) = match kind {
            ModelKind::Level => (&self.models.level, &self.level_loaded),
            ModelKind::Department => (&self.models.department, &self.department_loaded),
        };
        EncoderWorker {
            index: &self.index,
            kind,
            path,
            kaggle: &self.kaggle,
            loaded,
        }
    }

    fn tick(&self, mode: TickMode, signal: &WorkerSignal) -> rusqlite::Result<()> {
        runs::tick(&self.index, &self.settings, &self.scraper, mode, || {
            self.invalidate_counts();
            signal.notify();
        })
    }

    fn backlogs(&self) -> rusqlite::Result<Value> {
        let cache = self.cached.lock().unwrap_or_else(PoisonError::into_inner);
        let recent = fresh(cache.backlogs.as_ref(), BACKLOG_TTL);
        drop(cache);
        if let Some(backlogs) = recent {
            return Ok(backlogs);
        }
        let backlogs = json!({
            "classification":autoclassify::backlog_counts(&self.index)?,
            "level_model":self.encoder_backlog(ModelKind::Level)?,
            "department_model":self.encoder_backlog(ModelKind::Department)?,
        });
        self.cached
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .backlogs = Some(CachedValue {
            recorded: Instant::now(),
            data: backlogs.clone(),
        });
        Ok(backlogs)
    }

    fn encoder_backlog(&self, kind: ModelKind) -> rusqlite::Result<Value> {
        let (path, role) = match kind {
            ModelKind::Level => (&self.models.level, jobindex::LEVEL_ROLE_KEY),
            ModelKind::Department => (&self.models.department, jobindex::DEPARTMENT_ROLE_KEY),
        };
        let (active, judged) = decision_store::encoder_counts(&self.index.connect()?, role)?;
        let mut value = json!({
            "model":jobindex::model_available(path),"active_jobs":active,
            "judged":judged,"remaining":active-judged,
        });
        if kind == ModelKind::Level {
            value["device"] = json!(self.device());
        }
        Ok(value)
    }
}

fn fresh(cached: Option<&CachedValue>, ttl: Duration) -> Option<Value> {
    cached
        .filter(|entry| entry.recorded.elapsed() < ttl)
        .map(|entry| entry.data.clone())
}
