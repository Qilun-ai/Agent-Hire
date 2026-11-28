//! Shared wake-up and shutdown signals for the background index threads.

use std::sync::{Condvar, Mutex, PoisonError};
use std::time::Duration;

#[derive(Default)]
pub(crate) struct WorkerSignal {
    state: Mutex<State>,
    changed: Condvar,
}

#[derive(Default)]
struct State {
    stopped: bool,
    revision: u64,
}

impl WorkerSignal {
    pub(crate) fn notify(&self) {
        let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        state.revision = state.revision.wrapping_add(1);
        self.changed.notify_all();
    }

    pub(crate) fn stop(&self) {
        self.state
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .stopped = true;
        self.changed.notify_all();
    }

    pub(crate) fn stopped(&self) -> bool {
        self.state
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .stopped
    }

    pub(crate) fn wait(&self, interval: Duration, revision: Option<&mut u64>) -> bool {
        let state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        let (state, _) = self
            .changed
            .wait_timeout_while(state, interval, |state| {
                !state.stopped
                    && revision
                        .as_deref()
                        .is_none_or(|&seen| state.revision == seen)
            })
            .unwrap_or_else(PoisonError::into_inner);
        if let Some(revision) = revision {
            *revision = state.revision;
        }
        !state.stopped
    }
}
