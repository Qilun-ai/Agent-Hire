//! What a job email means, and which application it is about.
//!
//! Two separate questions: is this about a job I applied to (sender and subject against the
//! pipeline), and what happened (rejection, interview, offer, assessment, acknowledgement).
//! Deterministic rules come first; the decision model only settles what they cannot.

use std::sync::LazyLock;

use regex::{Regex, RegexBuilder};
use serde_json::{Map, Value};
use url::Url;

/// What an email says happened.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// Turned down.
    Rejected,
    /// A job offer.
    Offer,
    /// An interview invitation or scheduling.
    Interview,
    /// A test or take-home to complete.
    Assessment,
    /// Application received.
    Acknowledged,
    /// Nothing recognisable.
    Unknown,
}

impl Outcome {
    /// The outcome's name.
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::Rejected => "rejected",
            Outcome::Offer => "offer",
            Outcome::Interview => "interview",
            Outcome::Assessment => "assessment",
            Outcome::Acknowledged => "acknowledged",
            Outcome::Unknown => "unknown",
        }
    }
}

/// The pipeline status an outcome moves a lead to; `None` records the event only.
pub fn outcome_status(outcome: Outcome) -> Option<&'static str> {
    match outcome {
        Outcome::Rejected => Some("rejected"),
        Outcome::Offer => Some("accepted"),
        Outcome::Interview => Some("interviewing"),
        Outcome::Assessment | Outcome::Acknowledged | Outcome::Unknown => None,
    }
}

const REJECTION: &[&str] = &[
    r"we (?:have )?(?:decided|chosen) (?:to )?(?:move forward|proceed) with (?:other|another)",
    r"not (?:be )?(?:moving|proceeding|going) forward",
    r"not (?:been )?(?:selected|successful|shortlisted)",
    r"unfortunately[^.]{0,80}(?:not|unable|other candidates)",
    r"we(?:'| a)re unable to (?:offer|proceed|move)",
    r"(?:position|role|vacancy) has been filled",
    r"pursuing other candidates",
    r"will not be (?:progressing|continuing)",
    r"regret to inform",
    r"no longer under consideration",
];

const OFFER: &[&str] = &[
    r"(?:pleased|delighted|happy|excited) to (?:offer|extend)",
    r"(?:job|employment|formal) offer",
    r"offer of employment",
    r"we(?:'| woul)d like to offer you",
    r"welcome (?:aboard|to the team)",
];

const INTERVIEW: &[&str] = &[
    r"(?:schedule|set ?up|arrange|book)(?:e?d)? (?:a |an |your )?(?:call|chat|interview|screen)",
    r"invit(?:e|ing|ation) (?:you )?(?:to|for) (?:an? )?(?:interview|call|conversation)",
    r"would (?:you )?(?:like|be available) (?:to )?(?:chat|talk|meet|speak)",
    r"next (?:step|stage) (?:is|will be) (?:an? )?(?:interview|call)",
    r"(?:phone|technical|onsite|final)[- ](?:screen|interview)",
    r"availability for (?:an? )?(?:call|interview|chat)",
];

const ASSESSMENT: &[&str] = &[
    r"(?:take[- ]home|coding|technical|online) (?:assessment|challenge|exercise|test)",
    r"complete (?:the |this |an )?(?:assessment|challenge|exercise|questionnaire)",
    r"hackerrank|codility|coderpad|karat|hackerearth",
];

const ACKNOWLEDGED: &[&str] = &[
    r"(?:we |have )?received your application",
    r"thank(?:s| you) for (?:your interest|applying|your application)",
    r"application (?:has been )?(?:received|submitted)",
    r"we(?:'| a)re reviewing your",
];

/// Applicant-tracking-system mail domains: mail from these is about an application even when
/// no company name appears in the address.
const ATS_MAIL_DOMAINS: &[&str] = &[
    "greenhouse.io",
    "greenhouse-mail.io",
    "us.greenhouse-mail.io",
    "lever.co",
    "hire.lever.co",
    "ashbyhq.com",
    "otta.com",
    "workday.com",
    "myworkday.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workable.com",
    "recruitee.com",
    "teamtailor.com",
    "bamboohr.com",
    "jazz.co",
    "applytojob.com",
    "breezy.hr",
    "icims.com",
    "taleo.net",
    "successfactors.com",
    "avature.net",
];

/// Words too generic to identify a company.
const STOP_COMPANY_WORDS: &[&str] = &[
    "the",
    "inc",
    "llc",
    "ltd",
    "gmbh",
    "corp",
    "co",
    "company",
    "group",
    "labs",
    "technologies",
    "tech",
    "solutions",
    "systems",
    "software",
    "digital",
    "global",
    "international",
    "team",
    "careers",
    "jobs",
    "recruiting",
    "talent",
    "hr",
    "people",
    "and",
];

const TITLE_STOP: &[&str] = &[
    "the", "and", "for", "with", "role", "position", "job", "new", "remote", "hybrid",
];

fn compile(patterns: &[&str]) -> Vec<(String, Regex)> {
    patterns
        .iter()
        .map(|pattern| {
            let regex = RegexBuilder::new(pattern)
                .case_insensitive(true)
                .build()
                .expect("outcome patterns are valid");
            ((*pattern).to_owned(), regex)
        })
        .collect()
}

/// One outcome's patterns, each with its source text (reported as what matched).
type Rule = (Outcome, Vec<(String, Regex)>);

static RULES: LazyLock<Vec<Rule>> = LazyLock::new(|| {
    vec![
        (Outcome::Rejected, compile(REJECTION)),
        (Outcome::Offer, compile(OFFER)),
        (Outcome::Interview, compile(INTERVIEW)),
        (Outcome::Assessment, compile(ASSESSMENT)),
        (Outcome::Acknowledged, compile(ACKNOWLEDGED)),
    ]
});
static SENDER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[\w.+-]+@([\w.-]+)").expect("valid pattern"));
static WORD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[a-z0-9]+").expect("valid pattern"));

/// What the rules decided, and how sure they are.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OutcomeVerdict {
    /// The outcome.
    pub outcome: Outcome,
    /// The pattern that matched (or `decision_model`).
    pub matched: String,
    /// `high` (matched in the subject), `medium` or `low`.
    pub confidence: String,
}

/// Which application an email is about.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LeadMatch {
    /// The lead.
    pub job_id: String,
    /// Its company.
    pub company: String,
    /// Why it matched.
    pub reason: String,
    /// `high` or `medium`.
    pub confidence: String,
}

/// What the message says happened. Rejections are checked first because they routinely use
/// every other category's words ("we won't be scheduling an interview"); offers second because
/// an offer often also proposes a call.
pub fn classify_outcome(subject: &str, body: &str) -> OutcomeVerdict {
    let text = format!("{subject}\n{body}");
    for (outcome, patterns) in RULES.iter() {
        if let Some((pattern, regex)) = patterns.iter().find(|(_, regex)| regex.is_match(&text)) {
            let confidence = if regex.is_match(subject) {
                "high"
            } else {
                "medium"
            };
            return OutcomeVerdict {
                outcome: *outcome,
                matched: pattern.clone(),
                confidence: confidence.to_owned(),
            };
        }
    }
    OutcomeVerdict {
        outcome: Outcome::Unknown,
        matched: String::new(),
        confidence: "low".to_owned(),
    }
}

/// The domain of a `From` header, with or without a display name.
pub fn sender_domain(address: &str) -> String {
    SENDER
        .captures(address)
        .and_then(|captures| captures.get(1))
        .map(|domain| {
            domain
                .as_str()
                .to_lowercase()
                .trim_end_matches('.')
                .to_owned()
        })
        .unwrap_or_default()
}

/// Whether the domain is (or is under) a known ATS mail domain.
pub fn is_ats_domain(domain: &str) -> bool {
    let domain = domain.to_lowercase();
    ATS_MAIL_DOMAINS
        .iter()
        .any(|ats| domain == *ats || domain.ends_with(&format!(".{ats}")))
}

/// The words of a company name that identify it (longer than two letters, not generic).
pub fn company_tokens(company: &str) -> Vec<String> {
    let lowered = company.to_lowercase();
    let mut out: Vec<String> = Vec::new();
    for word in WORD.find_iter(&lowered).map(|m| m.as_str()) {
        if word.len() > 2
            && !STOP_COMPANY_WORDS.contains(&word)
            && !out.iter().any(|seen| seen == word)
        {
            out.push(word.to_owned());
        }
    }
    out
}

/// The lower-cased host of a URL, or `""`.
pub fn url_domain(url: &str) -> String {
    Url::parse(url)
        .ok()
        .and_then(|url| url.host_str().map(str::to_lowercase))
        .unwrap_or_default()
}

fn field(lead: &Map<String, Value>, key: &str) -> String {
    match lead.get(key) {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    }
}

/// Whether `word` occurs in `text` with no letter or digit on either side.
fn contains_word(text: &str, word: &str) -> bool {
    text.match_indices(word).any(|(start, _)| {
        let before = text[..start].chars().next_back();
        let after = text[start + word.len()..].chars().next();
        let boundary =
            |c: Option<char>| !c.is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit());
        boundary(before) && boundary(after)
    })
}

fn title_overlap(title: &str, haystack: &str) -> f64 {
    let lowered = title.to_lowercase();
    let words: Vec<&str> = WORD
        .find_iter(&lowered)
        .map(|m| m.as_str())
        .filter(|word| word.len() > 1 && !TITLE_STOP.contains(word))
        .collect();
    if words.is_empty() {
        return 0.0;
    }
    let present = words
        .iter()
        .filter(|word| contains_word(haystack, word))
        .count();
    let trimmed = lowered.trim();
    let exact = if !trimmed.is_empty() && haystack.contains(trimmed) {
        1.0
    } else {
        0.0
    };
    present as f64 / words.len() as f64 + exact
}

/// Which applied-to job an email is about, or `None` when it is ambiguous or unrelated.
///
/// Signals, strongest first: the sender's domain is the one the user applied through; a company
/// word is in the sender domain; the sender is an ATS and the company is named in the mail.
pub fn match_lead(
    from_address: &str,
    subject: &str,
    body: &str,
    leads: &[Map<String, Value>],
) -> Option<LeadMatch> {
    let domain = sender_domain(from_address);
    if domain.is_empty() {
        return None;
    }
    let haystack = format!("{from_address}\n{subject}\n{body}").to_lowercase();
    let ats = is_ats_domain(&domain);
    let mut strong: Vec<LeadMatch> = Vec::new();
    let mut weak: Vec<LeadMatch> = Vec::new();
    for lead in leads {
        let job_id = field(lead, "job_id");
        if job_id.is_empty() {
            continue;
        }
        let company = field(lead, "company");
        let tokens = company_tokens(&company);
        let applied = url_domain(&field(lead, "url"));
        let candidate = |reason: String, confidence: &str| LeadMatch {
            job_id: job_id.clone(),
            company: company.clone(),
            reason,
            confidence: confidence.to_owned(),
        };
        if !applied.is_empty() && (domain == applied || domain.ends_with(&format!(".{applied}"))) {
            strong.push(candidate(
                format!("sender domain matches {applied}"),
                "high",
            ));
        } else if tokens.iter().any(|token| domain.contains(token.as_str())) {
            strong.push(candidate(
                format!("company name in sender domain {domain}"),
                "high",
            ));
        } else if ats && tokens.iter().any(|token| haystack.contains(token.as_str())) {
            weak.push(candidate(
                format!("{domain} (ATS) mentions {company}"),
                "medium",
            ));
        }
    }
    let candidates = if strong.is_empty() { weak } else { strong };
    if candidates.len() <= 1 {
        return candidates.into_iter().next();
    }
    // Several applications at one company or ATS: the email has to name the job; a tie is
    // ambiguous, and ambiguity loses.
    let title_of = |job_id: &str| {
        leads
            .iter()
            .find(|lead| field(lead, "job_id") == job_id)
            .map(|lead| field(lead, "title"))
            .unwrap_or_default()
    };
    let mut scored: Vec<(f64, LeadMatch)> = candidates
        .into_iter()
        .map(|candidate| {
            (
                title_overlap(&title_of(&candidate.job_id), &haystack),
                candidate,
            )
        })
        .collect();
    scored.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    let best_score = scored[0].0;
    if best_score <= 0.0 || scored.get(1).is_some_and(|second| second.0 >= best_score) {
        return None;
    }
    let (_, best) = scored.swap_remove(0);
    Some(LeadMatch {
        reason: format!("{}; title matched", best.reason),
        ..best
    })
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn rejection_beats_interview_words() {
        let verdict = classify_outcome(
            "Update",
            "Unfortunately we will not be scheduling an interview.",
        );
        assert_eq!(verdict.outcome, Outcome::Rejected);
        assert_eq!(verdict.confidence, "medium");
        assert_eq!(
            classify_outcome("Interview invitation", "").outcome,
            Outcome::Unknown
        );
        assert_eq!(
            classify_outcome("Let's schedule a call", "").confidence,
            "high"
        );
    }

    #[test]
    fn matching_by_domain_and_ats() {
        let leads: Vec<Map<String, Value>> = [
            json!({"job_id": "a", "company": "Nimbus Robotics", "url": "https://jobs.nimbus.io/1", "title": "Backend Engineer"}),
            json!({"job_id": "b", "company": "Pixel Forge", "url": "https://boards.greenhouse.io/x", "title": "Frontend Developer"}),
        ]
        .into_iter()
        .filter_map(|value| value.as_object().cloned())
        .collect();
        let hit =
            match_lead("Jobs <noreply@jobs.nimbus.io>", "Hi", "", &leads).expect("domain match");
        assert_eq!(hit.job_id, "a");
        let hit = match_lead(
            "no-reply@us.greenhouse-mail.io",
            "Pixel Forge application",
            "",
            &leads,
        )
        .expect("ats match");
        assert_eq!(
            (hit.job_id.as_str(), hit.confidence.as_str()),
            ("b", "medium")
        );
        assert!(match_lead("friend@gmail.com", "hello", "", &leads).is_none());
    }
}
