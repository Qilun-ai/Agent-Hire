//! Profile-relative ranking and feedback learning.

mod candidate;
mod caps;
mod criteria;
mod domains;
mod duration;
mod evaluator;
mod feedback;
mod feedback_semantic;
mod json_fields;
mod posting;
mod profile_rows;
mod ranking_service;
mod score_result;
mod scoring;
mod semantic;
mod taxonomy;

pub use evaluator::EvaluatorFailure;
pub use feedback::{FeedbackModel, MAX_SIGNAL_DELTA, apply_feedback_learning};
pub use feedback_semantic::{MAX_PREFERENCE_DELTA, preference_deltas};
pub use profile_rows::{ProfileText, profile_embedding_rows};
pub use ranking_service::{RankingService, Reevaluation, job_document};
pub use score_result::{Criterion, ScoreResult};
pub use scoring::{score_job_lead, score_job_lead_with_context};
pub use semantic::{
    ProfileVector, ProfileVectors, ScoringContext, SemanticFit, SemanticMatch, VectorKind,
    semantic_fit, semantic_fit_with_limits,
};
