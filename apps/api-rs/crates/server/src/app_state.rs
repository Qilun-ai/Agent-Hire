//! State shared by every request handler.

use std::path::PathBuf;
use std::time::Instant;

use agent_hire_jobindex::JobIndex;
use agent_hire_ml::{Embedder, JobIndexRuntime};
use agent_hire_store::{
    AppPaths, Database, DocumentStore, EventLog, LeadStore, ProfileStore, SettingsStore,
};
use tokio::sync::{Semaphore, watch};

use crate::feedback_relearn::FeedbackRelearn;
use crate::generation_routes::GenerationLocks;
use crate::hub::Hub;
use crate::token::Token;

/// Shared, read-mostly server state.
pub struct AppState {
    pub token: Token,
    pub started: Instant,
    pub paths: AppPaths,
    pub database: Database,
    pub documents: DocumentStore,
    pub profile_writes: tokio::sync::RwLock<()>,
    pub settings: SettingsStore,
    pub events: EventLog,
    pub leads: LeadStore,
    pub embedder: Embedder,
    pub feedback_relearn: FeedbackRelearn,
    pub generation_locks: GenerationLocks,
    pub profile: ProfileStore,
    pub jobs: JobIndex,
    pub job_index_runtime: JobIndexRuntime,
    pub repo_root: Option<PathBuf>,
    pub hub: Hub,
    pub websocket_slots: Semaphore,
    shutdown: watch::Sender<bool>,
}

impl AppState {
    pub fn new(
        token: Token,
        repo_root: Option<PathBuf>,
        paths: AppPaths,
        database: Database,
    ) -> AppState {
        let settings = SettingsStore::new(database.clone());
        let jobs = JobIndex::new(&agent_hire_jobindex::jobs_dir(database.path()));
        let job_index_runtime = JobIndexRuntime::new(
            jobs.clone(),
            settings.clone(),
            agent_hire_jobindex::scraper_dir(repo_root.as_deref()),
        );
        let embedder = Embedder::new(&paths.data_dir);
        AppState {
            token,
            started: Instant::now(),
            paths,
            database: database.clone(),
            documents: DocumentStore::new(database.clone()),
            profile_writes: tokio::sync::RwLock::new(()),
            settings,
            hub: Hub::new(EventLog::new(database.clone())),
            websocket_slots: Semaphore::new(50),
            events: EventLog::new(database.clone()),
            leads: LeadStore::new(database.clone()),
            embedder,
            feedback_relearn: FeedbackRelearn::default(),
            generation_locks: GenerationLocks::default(),
            profile: ProfileStore::new(database.clone()),
            jobs,
            job_index_runtime,
            repo_root,
            shutdown: watch::channel(false).0,
        }
    }

    /// Invalidates native counts and wakes the encoders after a route writes the index.
    pub fn notify_job_index(&self) {
        self.job_index_runtime.notify();
    }

    /// Asks the server to shut down gracefully.
    pub fn request_shutdown(&self) {
        self.shutdown.send_replace(true);
    }

    pub fn shutdown_requested(&self) -> watch::Receiver<bool> {
        self.shutdown.subscribe()
    }
}
