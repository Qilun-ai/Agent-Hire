//! Weighted-score caps and their user-visible explanations.

use crate::candidate::{Candidate, Level};
use crate::criteria::Coverage;
use crate::posting::Posting;

pub(crate) struct Cap {
    pub(crate) limit: i64,
    pub(crate) reason: String,
}

pub(crate) fn for_posting(
    posting: &Posting,
    candidate: &Candidate,
    coverage: &Coverage,
) -> Vec<Cap> {
    let mut caps = Vec::new();
    if posting.wrong_field {
        caps.push(Cap {
            limit: 15,
            reason: "wrong-field cap: posting is not a technical/software opportunity".to_owned(),
        });
    }
    if let Some(cap) = seniority(posting, candidate) {
        caps.push(cap);
    }
    if posting.terms.len() >= 2 && coverage.direct.is_empty() {
        caps.push(Cap {
            limit: if coverage.adjacent.is_empty() { 42 } else { 52 },
            reason: "stack cap: no exact evidence for requested primary stack".to_owned(),
        });
    }
    if posting.terms.is_empty() && posting.squashed_len < 160 {
        caps.push(Cap {
            limit: 68,
            reason: "confidence cap: posting is too thin for a high rating".to_owned(),
        });
    }
    caps.sort_by_key(|cap| cap.limit);
    caps
}

fn seniority(posting: &Posting, candidate: &Candidate) -> Option<Cap> {
    let years = posting.effective_years();
    if candidate.work_months < 6 && years >= 3.0 {
        let months = candidate.work_months;
        return Some(Cap {
            limit: 30,
            reason: format!(
                "seniority cap: {months} months professional experience vs {years}+ year requirement"
            ),
        });
    }
    let limit = match candidate.level {
        Level::Fresher if years >= 3.0 => 30,
        Level::Junior if years >= 5.0 => 38,
        Level::Junior if years >= 3.0 => 45,
        Level::Mid if years >= 7.0 => 48,
        _ => return None,
    };
    let level = candidate.level.label();
    Some(Cap {
        limit,
        reason: format!("seniority cap: {level} profile vs {years}+ year requirement"),
    })
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::seniority;
    use crate::candidate::Candidate;
    use crate::posting::Posting;

    #[test]
    fn uses_the_first_matching_seniority_cap() {
        let fresher = Candidate::from_profile(&json!({"s":"6 years of coursework"}), "jun 2026");
        let junior = Candidate::from_profile(
            &json!({"exp":[{"role":"Engineer","period":"2025-2025"}]}),
            "jun 2026",
        );
        let mid = Candidate::from_profile(
            &json!({"exp":[{"role":"Engineer","period":"2024-2025"}]}),
            "jun 2026",
        );
        for (profile, title, limit, reason) in [
            (
                &fresher,
                "Engineer, 3 years",
                30,
                "seniority cap: fresher profile vs 3+ year requirement",
            ),
            (
                &junior,
                "Engineer, 3 years",
                45,
                "seniority cap: junior profile vs 3+ year requirement",
            ),
            (
                &junior,
                "Senior Engineer, 3 years",
                38,
                "seniority cap: junior profile vs 5+ year requirement",
            ),
            (
                &mid,
                "Engineer, 7 years",
                48,
                "seniority cap: mid profile vs 7+ year requirement",
            ),
        ] {
            let cap = seniority(&Posting::from_document(title), profile).expect("seniority cap");
            assert_eq!((cap.limit, cap.reason.as_str()), (limit, reason));
        }
    }
}
