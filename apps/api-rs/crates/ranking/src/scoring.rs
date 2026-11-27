//! Deterministic scoring and the three criterion-weight branches.

use std::cmp::Reverse;
use std::collections::HashSet;

use serde_json::Value;

use crate::candidate::Candidate;
use crate::criteria::{self, Coverage};
use crate::posting::Posting;
use crate::score_result::{Criterion, ScoreResult};
use crate::semantic::{self, ScoringContext};
use crate::{caps, duration, json_fields};

const SEMANTIC_UNAVAILABLE: &str =
    "Semantic matching unavailable; used deterministic keyword/rubric scoring.";

/// Scores a job document against the profile using local profile evidence.
#[must_use]
pub fn score_job_lead(jd: &str, profile: &Value) -> ScoreResult {
    score_job_lead_with_context(jd, profile, &ScoringContext::default())
}

/// Scores a job document with optional real embeddings and stored profile vectors.
#[must_use]
pub fn score_job_lead_with_context(
    jd: &str,
    profile: &Value,
    context: &ScoringContext,
) -> ScoreResult {
    let mut candidate = Candidate::from_profile(profile, &duration::today_month());
    let mut posting = Posting::from_document(jd);
    let originally_technical = !posting.terms.is_empty();
    let originally_wrong = posting.wrong_field;
    let generalized = posting.apply_domain_phrases(jd, &mut candidate);
    if !originally_wrong && !generalized {
        posting.wrong_field = semantic::field_mismatch(jd, &candidate.text, context);
    }
    let semantic = semantic::semantic_fit(jd, Some(profile), context);
    let weights = match (semantic.is_some(), originally_technical) {
        (false, _) => [18, 27, 20, 20, 15, 0],
        (true, false) => [12, 8, 15, 18, 12, 45],
        (true, true) => [15, 20, 18, 20, 12, 15],
    };

    let coverage = Coverage::new(&posting, &candidate);
    let mut criteria = vec![
        criteria::role(&posting, &candidate, &coverage, weights[0]),
        criteria::stack(&posting, &coverage, weights[1]),
        criteria::proof(&posting, &candidate, weights[2]),
        criteria::seniority(&posting, &candidate, weights[3]),
        criteria::constraints(&posting, &candidate, weights[4]),
    ];
    if let Some(semantic) = &semantic {
        criteria.push(Criterion {
            name: "Semantic fit",
            score: semantic.score,
            weight: weights[5],
            reason: semantic.reason(),
        });
    }
    let caps = caps::for_posting(&posting, &candidate, &coverage);
    let applied_cap = caps.first().map(|cap| cap.limit);
    let score = weighted(&criteria).min(applied_cap.unwrap_or(100));

    let reason = reason(&criteria, &candidate, &coverage, caps.first());
    let mut match_points = Vec::new();
    let mut gaps = Vec::new();
    if !coverage.missing.is_empty() {
        gaps.push(format!(
            "Missing or weak evidence for: {}",
            criteria::formatted(&coverage.missing)
        ));
    }
    for criterion in &criteria {
        let Criterion {
            name,
            score,
            weight,
            reason,
        } = criterion;
        if *score >= 58 {
            match_points.push(format!("{name} {score}/100 (weight {weight}%): {reason}"));
        } else {
            gaps.push(format!("{name} {score}/100: {reason}"));
        }
    }
    if !coverage.adjacent.is_empty() {
        match_points.push(format!(
            "Adjacent transferable stack: {}",
            criteria::formatted(&coverage.adjacent)
        ));
    }
    match_points.truncate(7);
    gaps.extend(caps.into_iter().map(|cap| cap.reason));
    dedup(&mut gaps);
    gaps.truncate(8);
    if semantic.is_none() {
        gaps.push(SEMANTIC_UNAVAILABLE.to_owned());
    }
    ScoreResult {
        score,
        reason,
        match_points,
        gaps,
        applied_cap,
        criteria,
    }
}

fn weighted(criteria: &[Criterion]) -> i64 {
    let sum: i64 = criteria
        .iter()
        .map(|criterion| criterion.score * criterion.weight)
        .sum();
    let weight: i64 = criteria.iter().map(|criterion| criterion.weight).sum();
    ((sum as f64 / weight as f64).round_ties_even() as i64).clamp(0, 100)
}

fn reason(
    criteria: &[Criterion],
    candidate: &Candidate,
    coverage: &Coverage,
    cap: Option<&caps::Cap>,
) -> String {
    let mut order: Vec<&Criterion> = criteria.iter().collect();
    order.sort_by_key(|criterion| Reverse(criterion.weight));
    let breakdown: Vec<String> = order
        .iter()
        .map(|criterion| {
            let first_word = criterion.name.split(' ').next().unwrap_or(criterion.name);
            format!("{first_word} {}", criterion.score)
        })
        .collect();
    let mut reason = format!(
        "Custom deterministic score from weighted criteria: {}.",
        breakdown.join(", ")
    );
    if !coverage.direct.is_empty() {
        let mut evidence = criteria::evidence_line(candidate, &coverage.direct);
        if evidence.is_empty() {
            evidence = criteria::formatted(&coverage.direct);
        }
        reason.push_str(&format!(" Strongest evidence: {evidence}."));
    }
    if !coverage.missing.is_empty() {
        reason.push_str(&format!(
            " Main gaps: {}.",
            criteria::formatted(&coverage.missing)
        ));
    }
    if let Some(cap) = cap {
        reason.push_str(&format!(" Limit noted: {}.", cap.reason));
    }
    json_fields::clip(&reason, 500).to_owned()
}

pub(crate) fn dedup(items: &mut Vec<String>) {
    let mut seen = HashSet::new();
    items.retain(|item| seen.insert(item.clone()));
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::{SEMANTIC_UNAVAILABLE, score_job_lead};

    #[test]
    fn pins_empty_profile_weights_and_explanation() {
        let scored = score_job_lead("Job Title: Clerk", &json!({}));
        assert_eq!(scored.score, 47);
        assert_eq!(
            scored
                .criteria
                .iter()
                .map(|criterion| criterion.weight)
                .collect::<Vec<_>>(),
            [18, 27, 20, 20, 15]
        );
        assert_eq!(scored.applied_cap, Some(68));
        assert_eq!(
            scored.reason,
            "Custom deterministic score from weighted criteria: Stack 25, Proof 30, Seniority 84, Role 30, Constraints 78. Limit noted: confidence cap: posting is too thin for a high rating."
        );
        assert_eq!(
            scored.match_points[0],
            "Seniority fit 84/100 (weight 20%): no hard seniority requirement; profile reads fresher"
        );
        assert_eq!(
            scored.gaps.last().map(String::as_str),
            Some(SEMANTIC_UNAVAILABLE)
        );
        assert_eq!(scored.as_dict().get("criteria"), None);
    }

    #[test]
    fn pins_semantic_branches_and_domain_generalization() {
        let technical = score_job_lead(
            "Python PostgreSQL",
            &json!({"skills":[{"n":"Python"},{"n":"PostgreSQL"}]}),
        );
        let domain = score_job_lead(
            "Title: Nurse\nDescription: Surgical nursing",
            &json!({
                "certifications":["Surgical nursing"],"s":"Nurse with clinical experience",
            }),
        );
        for (result, expected) in [
            (&technical, [15, 20, 18, 20, 12, 15]),
            (&domain, [12, 8, 15, 18, 12, 45]),
        ] {
            assert_eq!(
                result
                    .criteria
                    .iter()
                    .map(|criterion| criterion.weight)
                    .collect::<Vec<_>>(),
                expected
            );
            assert!(
                result.criteria[5].reason.starts_with(
                    "embedding similarity vs current profile (local-profile, hashing)"
                )
            );
        }
        assert!(
            domain
                .gaps
                .iter()
                .all(|gap| !gap.starts_with("wrong-field cap"))
        );
        assert_eq!(domain.criteria[1].reason, "matched surgical nursing");
    }

    #[test]
    fn orders_caps_and_limits_adjacency() {
        let wrong = score_job_lead(
            "Title: Nurse Manager\nDescription: 8 years, onsite only.",
            &Value::Null,
        );
        assert_eq!(wrong.score, 15);
        assert_eq!(wrong.applied_cap, Some(15));
        let cap_gaps: Vec<_> = wrong
            .gaps
            .iter()
            .filter(|gap| gap.contains(" cap:"))
            .collect();
        assert_eq!(
            cap_gaps,
            &[
                "wrong-field cap: posting is not a technical/software opportunity",
                "seniority cap: 0 months professional experience vs 8+ year requirement",
                "confidence cap: posting is too thin for a high rating",
            ]
        );
        let adjacent = score_job_lead("Django and FastAPI", &json!({"skills":[{"n":"Flask"}]}));
        assert_eq!(adjacent.applied_cap, Some(52));
        assert_eq!(adjacent.criteria[1].score, 30);
        assert_eq!(
            adjacent.criteria[1].reason,
            "matched none; adjacent Django, FastAPI"
        );
        let missing = score_job_lead("Python and Java", &json!({"skills":[{"n":"Rust"}]}));
        assert_eq!(missing.applied_cap, Some(42));
        assert_eq!(
            missing.gaps[0],
            "Missing or weak evidence for: Java, Python"
        );
    }
}
