//! Starts and stops the job-index workers off the async executor.

use std::sync::Arc;

use tokio::task;

use crate::app_state::AppState;
use crate::log;

/// Starts the job-index workers before serving requests.
pub async fn start(state: Arc<AppState>) {
    match task::spawn_blocking(move || state.job_index_runtime.start()).await {
        Ok(Ok(())) => log::info("jobindex", "native workers started"),
        Ok(Err(error)) => {
            log::warning("jobindex", &format!("cannot start native workers: {error}"))
        }
        Err(error) => log::warning(
            "jobindex",
            &format!("job-index startup task failed: {error}"),
        ),
    }
}

/// Joins the runtime threads during shutdown.
pub async fn stop(state: Arc<AppState>) {
    state.request_shutdown();
    if let Err(error) = task::spawn_blocking(move || state.job_index_runtime.stop()).await {
        log::warning(
            "jobindex",
            &format!("job-index shutdown task failed: {error}"),
        );
    }
}
