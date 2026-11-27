//! Internal scoring detail and the stable public score dictionary.

use serde::Serialize;
use serde_json::{Value, json};

/// One weighted criterion in the deterministic scoring rubric.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Criterion {
    /// The rubric's display label.
    pub name: &'static str,
    /// The criterion score on the 0–100 scale.
    pub score: i64,
    /// The branch-specific weight.
    pub weight: i64,
    /// The evidence or finding used for this score.
    pub reason: String,
}

/// The full deterministic result, including criterion detail.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ScoreResult {
    /// The final 0–100 score after all caps.
    pub score: i64,
    /// The summarized criteria, evidence, gaps and limit.
    pub reason: String,
    /// Positive criterion and stack explanations.
    pub match_points: Vec<String>,
    /// Weak evidence, risks, and cap explanations.
    pub gaps: Vec<String>,
    /// The tightest active rubric cap.
    pub applied_cap: Option<i64>,
    /// The evaluated criteria in rubric order.
    pub criteria: Vec<Criterion>,
}

impl ScoreResult {
    /// Returns the public dictionary used by the evaluator and lead routes.
    #[must_use]
    pub fn as_dict(&self) -> Value {
        json!({
            "score": self.score, "reason": self.reason,
            "match_points": self.match_points, "gaps": self.gaps, "applied_cap": self.applied_cap,
        })
    }
}
