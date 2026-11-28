//! A coalescing background pass for lead feedback.

use std::sync::{Arc, Mutex, PoisonError};

use agent_hire_llm::Settings;
use agent_hire_ranking::{RankingService, ScoringContext};
use agent_hire_store::StoreError;
use serde_json::{Value, json};

use crate::app_state::AppState;
use crate::log;

/// An idle worker, or a running worker with a flag for pending feedback.
#[derive(Default)]
pub struct FeedbackRelearn {
    pending: Mutex<Option<bool>>,
}

/// Schedules a pass without waiting for inference or SQLite writes.
pub fn schedule(state: Arc<AppState>) {
    {
        let mut pending = state
            .feedback_relearn
            .pending
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        if pending.replace(true).is_some() {
            return;
        }
        *pending = Some(false);
    }
    tokio::spawn(async move {
        loop {
            let app = Arc::clone(&state);
            match tokio::task::spawn_blocking(move || recompute(&app)).await {
                Ok(Ok(changed)) => {
                    let count = changed.len();
                    for lead in changed {
                        state
                            .hub
                            .broadcast(json!({"type": "LEAD_UPDATED", "data": lead}))
                            .await;
                    }
                    if count != 0 {
                        state
                            .hub
                            .broadcast(json!({
                                "type": "agent", "event": "feedback_relearn",
                                "msg": format!("Re-ranked {count} lead(s) from your feedback"),
                            }))
                            .await;
                    }
                }
                Ok(Err(error)) => {
                    log::warning("leads", &format!("feedback rerank failed: {error}"))
                }
                Err(error) => {
                    log::warning("leads", &format!("feedback rerank task failed: {error}"))
                }
            }

            let mut pending = state
                .feedback_relearn
                .pending
                .lock()
                .unwrap_or_else(PoisonError::into_inner);
            if *pending != Some(true) {
                *pending = None;
                break;
            }
            *pending = Some(false);
        }
    });
}

fn recompute(state: &AppState) -> Result<Vec<Value>, StoreError> {
    let inputs = state.leads.learning_inputs()?;
    let settings = Settings::new(state.settings.all()?);
    let ranking = RankingService::new(
        settings,
        ScoringContext {
            embedder: Some(state.embedder.clone()),
            // Feedback embeds lead examples and does not query profile vectors.
            vectors: None,
        },
    );
    let changes = ranking.recompute_feedback_signals(&inputs.examples, &inputs.leads);
    state.leads.save_learning_scores(&changes)
}
