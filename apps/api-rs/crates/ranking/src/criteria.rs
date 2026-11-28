//! Deterministic role, stack, evidence, seniority, and logistics criteria.

use std::collections::BTreeSet;

use crate::candidate::{Candidate, Level};
use crate::posting::Posting;
use crate::score_result::Criterion;
use crate::taxonomy::{self, Terms};

pub(crate) struct Coverage {
    pub(crate) direct: Terms,
    pub(crate) adjacent: Terms,
    pub(crate) missing: Terms,
}

impl Coverage {
    pub(crate) fn new(posting: &Posting, candidate: &Candidate) -> Self {
        let direct = posting
            .terms
            .intersection(&candidate.terms)
            .cloned()
            .collect();
        let categories: BTreeSet<&str> = candidate
            .terms
            .iter()
            .filter_map(|term| taxonomy::category(term))
            .collect();
        let mut adjacent = Terms::new();
        let mut missing = Terms::new();
        for term in posting.terms.difference(&candidate.terms) {
            if taxonomy::category(term).is_some_and(|category| {
                categories.contains(category) && !taxonomy::ADJACENCY_BLOCKLIST.contains(&category)
            }) {
                adjacent.insert(term.clone());
            } else {
                missing.insert(term.clone());
            }
        }
        Self {
            direct,
            adjacent,
            missing,
        }
    }
}

pub(crate) fn formatted(terms: &Terms) -> String {
    if terms.is_empty() {
        return "none".to_owned();
    }
    let mut terms: Vec<&str> = terms.iter().map(String::as_str).collect();
    terms.sort_by_cached_key(|term| term.to_lowercase());
    terms.truncate(8);
    terms.join(", ")
}

pub(crate) fn role(
    posting: &Posting,
    candidate: &Candidate,
    coverage: &Coverage,
    weight: i64,
) -> Criterion {
    let overlap: Vec<&str> = posting
        .lanes
        .intersection(&candidate.lanes)
        .map(String::as_str)
        .collect();
    let (score, reason) = if posting.wrong_field {
        (
            0,
            format!(
                "non-technical/non-target field: {}",
                first_labels(&posting.wrong_field_terms)
            ),
        )
    } else if posting.lanes.is_empty() && posting.terms.is_empty() {
        (30, "posting has no clear technical role signal".to_owned())
    } else if !overlap.is_empty() {
        let roles = overlap.join(", ");
        if !coverage.direct.is_empty() {
            (
                88,
                format!("same role lane ({roles}) with direct stack overlap"),
            )
        } else if !posting.terms.is_empty() {
            (
                64,
                format!("role lane label matches ({roles}) but stacks differ"),
            )
        } else {
            (78, format!("same role lane: {roles}"))
        }
    } else if !candidate.terms.is_empty()
        && ["backend", "frontend", "fullstack", "ai", "data"]
            .iter()
            .any(|lane| posting.lanes.contains(*lane))
    {
        (
            55,
            "technical role is adjacent to candidate profile".to_owned(),
        )
    } else if !coverage.direct.is_empty() {
        (
            60,
            "technical stack overlap exists but role lane is weakly specified".to_owned(),
        )
    } else if !posting.terms.is_empty() && !candidate.terms.is_empty() {
        (
            38,
            "stack mentioned but no shared tools and no role label".to_owned(),
        )
    } else {
        (22, "role does not map to candidate target".to_owned())
    };
    Criterion {
        name: "Role alignment",
        score,
        weight,
        reason,
    }
}

pub(crate) fn stack(posting: &Posting, coverage: &Coverage, weight: i64) -> Criterion {
    let (score, reason) = if posting.terms.is_empty() {
        (
            if posting.lanes.is_empty() { 25 } else { 55 },
            "no explicit stack to compare".to_owned(),
        )
    } else {
        let direct = coverage.direct.len();
        let adjacent = coverage.adjacent.len();
        let coverage_score = (direct as f64 + 0.30 * adjacent as f64) / posting.terms.len() as f64;
        let mut score =
            (coverage_score * 88.0 + (direct * 2).min(10) as f64).round_ties_even() as i64;
        if direct != 0 {
            score = score.max(if coverage.missing.is_empty() { 86 } else { 55 });
        } else if adjacent != 0 {
            score = (22 + adjacent as i64 * 4).clamp(22, 38);
        }
        let mut reason = vec![format!("matched {}", formatted(&coverage.direct))];
        if adjacent != 0 {
            reason.push(format!("adjacent {}", formatted(&coverage.adjacent)));
        }
        if !coverage.missing.is_empty() {
            reason.push(format!("missing {}", formatted(&coverage.missing)));
        }
        (score.clamp(0, 100), reason.join("; "))
    };
    Criterion {
        name: "Stack overlap",
        score,
        weight,
        reason,
    }
}

pub(crate) fn proof(posting: &Posting, candidate: &Candidate, weight: i64) -> Criterion {
    let similar: Terms = posting
        .deliverables
        .intersection(&candidate.deliverables)
        .cloned()
        .collect();
    let (score, reason) = if posting.terms.is_empty() {
        let score = if similar.is_empty() {
            if candidate.project_count > 0 { 50 } else { 30 }
        } else {
            (56 + similar.len() as i64 * 12).min(80)
        };
        (
            score,
            format!(
                "{} profile projects; no exact stack requested",
                candidate.project_count
            ),
        )
    } else {
        let mut project_or_experience = Terms::new();
        let mut listed = Terms::new();
        let mut points = 0.0;
        for term in &posting.terms {
            if candidate.project_evidence.contains_key(term) {
                project_or_experience.insert(term.clone());
                points += 1.0;
            } else if candidate.experience_evidence.contains_key(term) {
                project_or_experience.insert(term.clone());
                points += 0.75;
            } else if candidate.skills.contains(term) {
                listed.insert(term.clone());
                points += 0.45;
            }
        }
        let term_score = points / posting.terms.len() as f64 * 100.0;
        let deliverable_score = if similar.is_empty() {
            45
        } else {
            (55 + similar.len() * 15).min(100)
        };
        let score = (term_score * 0.78 + deliverable_score as f64 * 0.22).round_ties_even() as i64;
        let mut reason = if !project_or_experience.is_empty() {
            let mut reason = format!(
                "project/experience proof for {}",
                formatted(&project_or_experience)
            );
            let evidence = evidence_line(candidate, &project_or_experience);
            if !evidence.is_empty() {
                reason.push_str(&format!(" ({evidence})"));
            }
            reason
        } else if !listed.is_empty() {
            format!("listed skills only for {}", formatted(&listed))
        } else {
            "no direct proof for requested stack".to_owned()
        };
        if !similar.is_empty() {
            let labels: Vec<&str> = similar.iter().take(3).map(String::as_str).collect();
            reason.push_str(&format!("; similar deliverable: {}", labels.join(", ")));
        }
        (score.clamp(0, 100), reason)
    };
    Criterion {
        name: "Proof of work",
        score,
        weight,
        reason,
    }
}

pub(crate) fn evidence_line(candidate: &Candidate, terms: &Terms) -> String {
    let mut sorted: Vec<&str> = terms.iter().map(String::as_str).collect();
    sorted.sort_by_cached_key(|term| term.to_lowercase());
    let mut chunks = Vec::new();
    for term in sorted {
        let projects = candidate
            .project_evidence
            .get(term)
            .map_or(&[][..], Vec::as_slice);
        let experiences = candidate
            .experience_evidence
            .get(term)
            .map_or(&[][..], Vec::as_slice);
        if !projects.is_empty() {
            chunks.push(format!(
                "{term} in {}",
                projects[..projects.len().min(2)].join(", ")
            ));
        } else if let Some(experience) = experiences.first() {
            chunks.push(format!("{term} in {experience}"));
        }
        if chunks.len() == 4 {
            break;
        }
    }
    chunks.join("; ")
}

pub(crate) fn seniority(posting: &Posting, candidate: &Candidate, weight: i64) -> Criterion {
    let level = candidate.level.label();
    let years = posting.effective_years();
    let (score, reason) =
        if posting.entry_level && matches!(candidate.level, Level::Fresher | Level::Junior) {
            (92, format!("{level} profile matches entry-level signal"))
        } else if years == 0.0 && posting.seniority_flags.is_empty() {
            (
                84,
                format!("no hard seniority requirement; profile reads {level}"),
            )
        } else {
            let gap = years - candidate.level.years();
            let score = match gap {
                gap if gap <= 0.5 => 86,
                gap if gap <= 1.5 => 62,
                gap if gap <= 3.0 => 34,
                gap if gap <= 5.0 => 18,
                _ => 10,
            };
            let mut reason = format!("requires about {years}+ years; profile reads {level}");
            if !posting.seniority_flags.is_empty() {
                let flags: Vec<&str> = posting.seniority_flags.iter().map(String::as_str).collect();
                reason.push_str(&format!(" with {} title signal", flags.join(", ")));
            }
            (score, reason)
        };
    Criterion {
        name: "Seniority fit",
        score,
        weight,
        reason,
    }
}

pub(crate) fn constraints(posting: &Posting, candidate: &Candidate, weight: i64) -> Criterion {
    let mut score = 78;
    let mut reasons = Vec::new();
    if posting.remote {
        score += 8;
        reasons.push("remote-friendly".to_owned());
    } else if posting.onsite {
        score -= 14;
        reasons.push("onsite-only constraint".to_owned());
    }
    if posting.location_limited && !matches!(candidate.location, "" | "US" | "United States") {
        score -= 28;
        reasons.push(format!(
            "location-limited while profile hints {}",
            candidate.location
        ));
    }
    if !posting.red_flags.is_empty() {
        score -= (posting.red_flags.len() as i64 * 14).min(35);
        reasons.push(format!("red flags: {}", first_labels(&posting.red_flags)));
    }
    if posting.quality.is_empty() {
        score -= 18;
        reasons.push("thin scraped posting".to_owned());
    } else if posting.quality.len() >= 3 {
        score += 5;
        reasons.push("good posting detail".to_owned());
    }
    let reason = if reasons.is_empty() {
        "no major constraints found".to_owned()
    } else {
        reasons.join("; ")
    };
    Criterion {
        name: "Constraints and lead quality",
        score: score.clamp(0, 100),
        weight,
        reason,
    }
}

fn first_labels(labels: &[&str]) -> String {
    labels[..labels.len().min(3)].join(", ")
}
