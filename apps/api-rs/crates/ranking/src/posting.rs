//! Signals extracted from the job document before criteria are evaluated.

use crate::candidate::{self, Candidate};
use crate::taxonomy::Terms;
use crate::{duration, json_fields, taxonomy};

const TECHNICAL: &[&str] = &[
    "ai",
    "backend",
    "frontend",
    "fullstack",
    "data",
    "devops",
    "desktop",
];

pub(crate) struct Posting {
    pub(crate) terms: Terms,
    pub(crate) lanes: Terms,
    pub(crate) deliverables: Terms,
    pub(crate) wrong_field_terms: Vec<&'static str>,
    pub(crate) wrong_field: bool,
    pub(crate) max_years: f64,
    pub(crate) seniority_flags: Terms,
    pub(crate) entry_level: bool,
    pub(crate) remote: bool,
    pub(crate) onsite: bool,
    pub(crate) location_limited: bool,
    pub(crate) red_flags: Vec<&'static str>,
    pub(crate) quality: Vec<&'static str>,
    pub(crate) squashed_len: usize,
    _commercial_intent: bool,
}

impl Posting {
    pub(crate) fn from_document(document: &str) -> Self {
        let title = title(document);
        let company = field(document, &["company", "client"]).unwrap_or_default();
        let lower = document.to_lowercase();
        let terms = taxonomy::tech(&lower);
        let title_and_text = format!("{}\n{lower}", title.unwrap_or("Job lead"));
        let lanes = taxonomy::roles(&title_and_text);
        let deliverables = taxonomy::deliverables(&title_and_text);
        let wrong_field_terms = present(&lower, taxonomy::WRONG_FIELD);
        let wrong_field = !wrong_field_terms.is_empty()
            && terms.is_empty()
            && !TECHNICAL.iter().any(|name| lanes.contains(*name));
        let max_years = duration::required_years(&lower);
        let mut seniority_flags = Terms::new();
        if taxonomy::any(&lower, &["senior", "sr.", "lead", "staff", "principal"])
            || lower.match_indices("sr ").any(|(start, _)| {
                start == 0 || !lower.as_bytes()[start - 1].is_ascii_alphanumeric()
            })
        {
            seniority_flags.insert("senior".to_owned());
        }
        if taxonomy::any(&lower, &["manager", "director", "head of"]) {
            seniority_flags.insert("manager".to_owned());
        }
        let entry_level = taxonomy::any(
            &lower,
            &[
                "junior",
                "entry level",
                "entry-level",
                "fresher",
                "graduate",
                "intern",
                "0-2 years",
                "0 to 2 years",
            ],
        );
        let remote = taxonomy::any(&lower, &["remote", "work from home", "wfh", "anywhere"]);
        let onsite = taxonomy::any(&lower, &["onsite", "on-site", "in office", "relocation"]);
        let location_limited = taxonomy::any(
            &lower,
            &[
                "us only",
                "u.s. only",
                "usa only",
                "must be in us",
                "us-based",
                "europe only",
                "uk only",
            ],
        );
        let red_flags = present(&lower, taxonomy::RED_FLAGS);
        let commercial_intent = taxonomy::any(&lower, taxonomy::COMMERCIAL);
        let squashed_len = json_fields::squash(document).chars().count();

        let mut quality = Vec::new();
        if title.is_some() {
            quality.push("clear title");
        }
        if !company.is_empty() {
            quality.push("company/client named");
        }
        if squashed_len >= 240 {
            quality.push("substantive description");
        }
        if terms.len() >= 2 {
            quality.push("specific stack");
        }
        if taxonomy::any(
            &lower,
            &[
                "remote", "hybrid", "onsite", "salary", "budget", "apply", "email", "dm",
                "proposal",
            ],
        ) {
            quality.push("next-step/context present");
        }
        Self {
            terms,
            lanes,
            deliverables,
            wrong_field_terms,
            wrong_field,
            max_years,
            seniority_flags,
            entry_level,
            remote,
            onsite,
            location_limited,
            red_flags,
            quality,
            squashed_len,
            _commercial_intent: commercial_intent,
        }
    }

    pub(crate) fn apply_domain_phrases(
        &mut self,
        document: &str,
        candidate: &mut Candidate,
    ) -> bool {
        let normalized = candidate::normalize_domain(document);
        let mut matched = false;
        for phrase in &candidate.domain_phrases {
            if taxonomy::has(&normalized, phrase) {
                self.terms.insert(phrase.clone());
                candidate.terms.insert(phrase.clone());
                matched = true;
            }
        }
        if self.wrong_field
            && (matched || taxonomy::any(&candidate.text.to_lowercase(), &self.wrong_field_terms))
        {
            self.wrong_field = false;
        }
        matched
    }

    pub(crate) fn effective_years(&self) -> f64 {
        if self.seniority_flags.contains("manager") {
            self.max_years.max(6.0)
        } else if self.seniority_flags.contains("senior") {
            self.max_years.max(5.0)
        } else {
            self.max_years
        }
    }
}

fn present(text: &str, terms: &'static [&'static str]) -> Vec<&'static str> {
    terms
        .iter()
        .copied()
        .filter(|term| taxonomy::has(text, term))
        .collect()
}

fn field<'a>(document: &'a str, keys: &[&str]) -> Option<&'a str> {
    document.lines().find_map(|line| {
        let (label, content) = line.trim_start().split_once(':')?;
        let content = content.trim();
        (!content.is_empty()
            && keys
                .iter()
                .any(|key| label.trim_end().eq_ignore_ascii_case(key)))
        .then_some(content)
    })
}

fn title(document: &str) -> Option<&str> {
    if let Some(title) = field(document, &["job title", "gig title", "title"]) {
        return Some(json_fields::clip(title, 180));
    }
    let excluded = [
        "url:",
        "description:",
        "budget:",
        "company:",
        "client:",
        "http://",
        "https://",
        "www.",
    ];
    for line in document.lines().map(str::trim) {
        if !line.is_empty() && line.chars().count() <= 180 {
            let lower = line.to_lowercase();
            if !excluded.iter().any(|prefix| lower.starts_with(prefix)) {
                return Some(line);
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::Posting;

    #[test]
    fn extracts_years_and_flags_in_taxonomy_order() {
        let posting = Posting::from_document(
            "Job Title: Senior Backend Engineer\nCompany: Acme\nDescription: Remote, 3-5 years. Cheap, unpaid. Rust.",
        );
        assert_eq!(posting.effective_years(), 5.0);
        assert_eq!(posting.red_flags, ["unpaid", "cheap"]);
        assert_eq!(
            posting.quality,
            [
                "clear title",
                "company/client named",
                "next-step/context present"
            ]
        );
        assert!(!posting.wrong_field);
    }
}
