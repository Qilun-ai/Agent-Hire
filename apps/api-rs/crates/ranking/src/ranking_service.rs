//! Blocking-pool scoring and batch entry points for the server.

use std::cmp::Reverse;
use std::sync::Arc;

use agent_hire_llm::Settings;
use serde_json::{Value, json};
use tokio::task::JoinError;

use crate::evaluator::EvaluatorFailure;
use crate::feedback::{FeedbackModel, MAX_SIGNAL_DELTA};
use crate::feedback_semantic::MAX_PREFERENCE_DELTA;
use crate::score_result::ScoreResult;
use crate::semantic::ScoringContext;
use crate::{evaluator, feedback_semantic, json_fields, scoring};

type Reporter = dyn Fn(&EvaluatorFailure) + Send + Sync;

/// One application's ranking settings and embedding resources.
#[derive(Clone, Default)]
pub struct RankingService {
    settings: Settings,
    context: ScoringContext,
    reporter: Option<Arc<Reporter>>,
}

/// Output of a reevaluation pass, with successful scores merged into their leads.
#[derive(Debug)]
pub struct Reevaluation {
    /// Number of leads in the original input.
    pub total: usize,
    /// Number of evaluated leads.
    pub scored: usize,
    /// Number of leads that failed evaluation.
    pub failed: usize,
    /// Successful lead objects with the new scores.
    pub leads: Vec<Value>,
}

impl RankingService {
    /// Creates a service with the current settings snapshot.
    #[must_use]
    pub fn new(settings: Settings, context: ScoringContext) -> Self {
        Self {
            settings,
            context,
            reporter: None,
        }
    }

    /// Registers a callback for LLM fallback telemetry.
    #[must_use]
    pub fn with_error_reporter(
        mut self,
        reporter: impl Fn(&EvaluatorFailure) + Send + Sync + 'static,
    ) -> Self {
        self.reporter = Some(Arc::new(reporter));
        self
    }

    /// Runs the deterministic rubric for a lead on the current thread.
    #[must_use]
    pub fn score_lead(&self, lead: &Value, profile: &Value) -> ScoreResult {
        scoring::score_job_lead_with_context(&job_document(lead), profile, &self.context)
    }

    /// Evaluates a lead, using an LLM only when configured and allowed.
    pub async fn evaluate_lead(
        &self,
        lead: &Value,
        profile: &Value,
        use_llm: bool,
    ) -> Result<Value, JoinError> {
        self.evaluate_document(&job_document(lead), profile, use_llm)
            .await
    }

    /// Evaluates an already assembled job document.
    pub async fn evaluate_document(
        &self,
        jd: &str,
        profile: &Value,
        use_llm: bool,
    ) -> Result<Value, JoinError> {
        let document = jd.to_owned();
        let candidate = profile.clone();
        let context = self.context.clone();
        let baseline = tokio::task::spawn_blocking(move || {
            scoring::score_job_lead_with_context(&document, &candidate, &context)
        })
        .await?;
        Ok(evaluator::evaluate(
            jd,
            profile,
            &self.settings,
            &baseline,
            use_llm,
            self.reporter.as_deref(),
        )
        .await)
    }

    /// Chooses nonempty IDs for LLM evaluation, preserving input order on equal scores.
    #[must_use]
    pub fn select_llm_eval_ids(
        &self,
        leads: &[Value],
        profile: &Value,
        max_llm: i64,
    ) -> Vec<String> {
        if max_llm <= 0 || leads.len() <= max_llm as usize {
            return leads.iter().filter_map(job_id).map(str::to_owned).collect();
        }
        let mut scores: Vec<(i64, &str)> = leads
            .iter()
            .filter_map(|lead| {
                let id = job_id(lead)?;
                Some((self.score_lead(lead, profile).score, id))
            })
            .collect();
        scores.sort_by_key(|(score, _)| Reverse(*score));
        scores
            .into_iter()
            .take(max_llm as usize)
            .map(|(_, id)| id.to_owned())
            .collect()
    }

    /// Returns the changed lead rows to persist in a single feedback-update transaction.
    #[must_use]
    pub fn recompute_feedback_signals(&self, examples: &[Value], leads: &[Value]) -> Vec<Value> {
        if examples.is_empty() {
            return Vec::new();
        }
        let model = FeedbackModel::from_examples(examples);
        let deltas = self
            .context
            .embedder
            .as_ref()
            .map_or_else(Default::default, |embedder| {
                feedback_semantic::preference_deltas(
                    examples,
                    leads,
                    embedder,
                    MAX_PREFERENCE_DELTA,
                )
            });
        let mut changes = Vec::new();
        for lead in leads {
            let Some(id) = job_id(lead) else { continue };
            let signal_score = json_fields::integer(&lead["signal_score"]);
            let previous_delta = json_fields::integer(&lead["learning_delta"]);
            let base_signal = if previous_delta == 0 {
                signal_score
            } else {
                json_fields::integer(&lead["base_signal_score"])
            };
            let mut seed = lead.as_object().cloned().unwrap_or_default();
            seed.insert("signal_score".to_owned(), json!(base_signal));
            seed.insert("base_signal_score".to_owned(), json!(base_signal));
            seed.remove("learning_delta");
            seed.remove("learning_reason");
            let mut update = model.apply(&Value::Object(seed), MAX_SIGNAL_DELTA);
            let score = json_fields::integer(&lead["score"]);
            let base = if json_fields::truthy(&lead["base_score"]) {
                json_fields::integer(&lead["base_score"])
            } else {
                score
            };
            let shift = deltas.get(id).copied().unwrap_or_default();
            let ranked = base.saturating_add(shift).clamp(0, 100);
            update["base_score"] = json!(base);
            update["score"] = json!(ranked);
            if json_fields::integer(&update["signal_score"]) != signal_score
                || json_fields::integer(&update["learning_delta"]) != previous_delta
                || ranked != score
            {
                changes.push(update);
            }
        }
        changes
    }

    /// Reevaluates leads in input order, stopping before the next lead when requested.
    pub async fn reevaluate_all(
        &self,
        leads: &[Value],
        profile: &Value,
        stopped: impl Fn() -> bool,
    ) -> Reevaluation {
        let mut pass = Reevaluation {
            total: leads.len(),
            scored: 0,
            failed: 0,
            leads: Vec::new(),
        };
        for lead in leads {
            if stopped() {
                break;
            }
            match self.evaluate_lead(lead, profile, true).await {
                Ok(score) => {
                    let mut updated = lead.as_object().cloned().unwrap_or_default();
                    if let Value::Object(score) = score {
                        updated.extend(score);
                    }
                    pass.leads.push(Value::Object(updated));
                    pass.scored += 1;
                }
                Err(_) => pass.failed += 1,
            }
        }
        pass
    }
}

/// Formats the exact job document passed to the ranking rubric.
#[must_use]
pub fn job_document(lead: &Value) -> String {
    let text = |key| {
        lead.get(key)
            .map_or_else(String::new, json_fields::stringify)
    };
    let title = text("title");
    let company = text("company");
    let url = text("url");
    let mut jd = format!("Job Title: {title}\nCompany: {company}\nURL: {url}");
    if json_fields::truthy(&lead["description"]) {
        let description = text("description");
        if !description.trim().is_empty() {
            jd.push_str(&format!("\nDescription: {}", description.trim()));
        }
    }
    jd
}

fn job_id(lead: &Value) -> Option<&str> {
    lead["job_id"].as_str().filter(|id| !id.is_empty())
}

#[cfg(test)]
mod tests {
    use agent_hire_llm::Settings;
    use serde_json::{Value, json};

    use super::{RankingService, ScoringContext, job_document};

    #[tokio::test]
    async fn missing_provider_key_uses_baseline_without_network() {
        let settings = Settings::new([
            ("evaluator_provider".to_owned(), "openai".to_owned()),
            ("openai_api_key".to_owned(), " ".to_owned()),
        ]);
        let service = RankingService::new(settings, ScoringContext::default());
        let lead = json!({"title":"Backend Engineer","company":"Acme", "description":"  Rust developer.  "});
        let profile = Value::Null;
        let deterministic = service
            .evaluate_lead(&lead, &profile, false)
            .await
            .expect("scores");
        let fallback = service
            .evaluate_lead(&lead, &profile, true)
            .await
            .expect("scores");
        assert_eq!(deterministic["scored_by"], "deterministic");
        assert_eq!(fallback["scored_by"], "deterministic_fallback");
        assert_eq!(fallback["score"], deterministic["score"]);
        assert_eq!(fallback["reason"], deterministic["reason"]);
        assert_eq!(
            job_document(&lead),
            "Job Title: Backend Engineer\nCompany: Acme\nURL: \nDescription: Rust developer."
        );
    }

    #[test]
    fn recomputes_from_base_and_only_writes_changes() {
        let service = RankingService::default();
        let examples = [json!({"feedback":"good","platform":"Board"})];
        let leads = [json!({
            "job_id":"a","platform":"Board","score":51,"base_score":50,"signal_score":72,
            "learning_delta":2,"base_signal_score":70,"signal_reason":"Useful; feedback learning +2",
        })];
        let updates = service.recompute_feedback_signals(&examples, &leads);
        assert_eq!(updates[0]["signal_score"], 71);
        assert_eq!(updates[0]["signal_reason"], "Useful; feedback learning +1");
        assert_eq!(updates[0]["score"], 50);
        assert!(
            service
                .recompute_feedback_signals(&examples, &updates)
                .is_empty()
        );
        assert!(service.recompute_feedback_signals(&[], &leads).is_empty());
    }
}
