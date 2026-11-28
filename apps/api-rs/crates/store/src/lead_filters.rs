//! Filtering, sorting and grouping the applications list (`GET /leads`, `/leads/summary`).

use std::cmp::Ordering;
use std::collections::HashSet;
use std::sync::LazyLock;

use agent_hire_leadtext::{Seniority, resolve_lead_seniority};
use regex::Regex;
use serde_json::{Map, Value};
use unicode_normalization::UnicodeNormalization;

use crate::clock;

/// The tabs of the applications list.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum LeadView {
    /// Every job lead.
    #[default]
    All,
    /// Strong matches: signal score 80+ or match score 85+.
    Hot,
    /// Not looked at yet (`discovered`).
    Found,
    /// Scored in any way.
    Evaluated,
    /// A tailored résumé is being made or ready.
    Generated,
    /// Applied, interviewing, accepted or rejected.
    Applied,
    /// Thrown away.
    Discarded,
}

impl LeadView {
    /// The view named `name`.
    pub fn parse(name: &str) -> Option<LeadView> {
        Some(match name {
            "all" => LeadView::All,
            "hot" => LeadView::Hot,
            "found" => LeadView::Found,
            "evaluated" => LeadView::Evaluated,
            "generated" => LeadView::Generated,
            "applied" => LeadView::Applied,
            "discarded" => LeadView::Discarded,
            _ => return None,
        })
    }

    fn admits(self, lead: &Map<String, Value>) -> bool {
        let status = text(lead, "status");
        match self {
            LeadView::All => true,
            LeadView::Hot => number(lead, "signal_score") >= 80.0 || number(lead, "score") >= 85.0,
            LeadView::Found => status == "discovered",
            LeadView::Evaluated => {
                number(lead, "score") > 0.0 || number(lead, "signal_score") > 0.0
            }
            LeadView::Generated => matches!(status, "tailoring" | "approved"),
            LeadView::Applied => is_applied_status(status),
            LeadView::Discarded => status == "discarded",
        }
    }
}

/// Statuses that mean the user applied.
pub fn is_applied_status(status: &str) -> bool {
    matches!(status, "applied" | "interviewing" | "accepted" | "rejected")
}

/// The orders the list can be sorted in.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum LeadSort {
    /// Best score first, then learning, contact and budget signals.
    Recommended,
    /// Newest first.
    #[default]
    Newest,
    /// Highest signal score first.
    Signal,
    /// Highest match score first.
    Match,
    /// By company, then title.
    Company,
}

impl LeadSort {
    /// The sort named `name`.
    pub fn parse(name: &str) -> Option<LeadSort> {
        Some(match name {
            "recommended" => LeadSort::Recommended,
            "newest" => LeadSort::Newest,
            "signal" => LeadSort::Signal,
            "match" => LeadSort::Match,
            "company" => LeadSort::Company,
            _ => return None,
        })
    }

    /// Compares two leads; every order ends with newest first, then job id.
    pub fn compare(self, a: &Map<String, Value>, b: &Map<String, Value>) -> Ordering {
        let primary = match self {
            LeadSort::Recommended => {
                let best = |lead: &Map<String, Value>| {
                    number(lead, "signal_score").max(number(lead, "score"))
                };
                let flag = |lead: &Map<String, Value>, key: &str| !text(lead, key).is_empty();
                desc_f64(best(a), best(b))
                    .then_with(|| {
                        desc_f64(number(a, "learning_delta"), number(b, "learning_delta"))
                    })
                    .then_with(|| flag(b, "last_contacted_at").cmp(&flag(a, "last_contacted_at")))
                    .then_with(|| flag(b, "budget").cmp(&flag(a, "budget")))
            }
            LeadSort::Newest => Ordering::Equal,
            LeadSort::Signal => desc_f64(number(a, "signal_score"), number(b, "signal_score")),
            LeadSort::Match => desc_f64(number(a, "score"), number(b, "score")),
            LeadSort::Company => {
                let key = |lead: &Map<String, Value>| {
                    format!("{} {}", text(lead, "company"), text(lead, "title"))
                        .to_ascii_lowercase()
                };
                key(a).cmp(&key(b))
            }
        };
        primary
            .then_with(|| text(b, "created_at").cmp(text(a, "created_at")))
            .then_with(|| text(a, "job_id").cmp(text(b, "job_id")))
    }
}

fn desc_f64(a: f64, b: f64) -> Ordering {
    b.partial_cmp(&a).unwrap_or(Ordering::Equal)
}

/// The seniority filter.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SeniorityFilter {
    /// Exactly this band.
    Band(Seniority),
    /// Fresher or junior.
    Beginner,
}

/// One requirement extracted from a posting (`lead_requirements`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Requirement {
    /// The skill or item name.
    pub name: String,
    /// `required` or `preferred`.
    pub requirement: String,
    /// `skill`, `certification`, `licence`, `language` and so on.
    pub item_type: String,
}

/// What enrichment says about a posting (`lead_enrichment` and its memberships).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Enrichment {
    /// The experience band.
    pub experience_level: Option<String>,
    /// `resolved`, `pending`, `failed`, `unresolved`.
    pub experience_status: Option<String>,
    /// Role classification status.
    pub role_status: Option<String>,
    /// Requirement extraction status.
    pub requirements_status: Option<String>,
    /// `yes`, `no` or another stated value.
    pub sponsorship: Option<String>,
    /// `full_time`, `contract` and so on.
    pub employment_type: Option<String>,
    /// Role taxonomy node ids the posting belongs to.
    pub member_roles: Vec<String>,
    /// Role node ids whose membership is unresolved.
    pub unresolved_roles: Vec<String>,
}

/// A lead with what filtering needs beyond its JSON.
#[derive(Clone, Debug)]
pub struct LeadRecord {
    /// The standard read model (with enrichment fields when there are any).
    pub lead: Map<String, Value>,
    /// Enrichment, when the posting has been enriched.
    pub enrichment: Option<Enrichment>,
    /// Extracted requirements.
    pub requirements: Vec<Requirement>,
}

/// Every filter the list and the summary accept.
#[derive(Clone, Debug, Default)]
pub struct LeadFilters {
    /// Free-text search.
    pub q: String,
    /// Exact platform.
    pub platform: String,
    /// Seniority band.
    pub seniority: Option<SeniorityFilter>,
    /// A place; remote postings always match.
    pub location: String,
    /// Only leads saved in this discovery session.
    pub session_id: Option<String>,
    /// Role taxonomy node ids.
    pub role_ids: Vec<String>,
    /// Experience bands from enrichment.
    pub experience_levels: Vec<String>,
    /// Skills the posting must require.
    pub required_skills: Vec<String>,
    /// Whether postings that are not enriched yet pass the semantic filters.
    pub include_unresolved: bool,
    /// Posted (or found) within this many hours.
    pub posted_within_hours: Option<i64>,
    /// Exact company, compared case-insensitively.
    pub company: String,
    /// `yes` or `not_no`.
    pub sponsorship: Option<String>,
    /// Employment types from enrichment.
    pub employment_types: Vec<String>,
    /// Count preferred skills as well as required ones.
    pub include_preferred: bool,
    /// Only postings asking for one of these (normalised) profile skills.
    pub profile_skills: Option<Vec<String>>,
}

/// The four-valued result of one semantic filter.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Verdict {
    Match,
    NoMatch,
    Unresolved,
    Pending,
}

impl LeadFilters {
    /// Whether the record passes every filter. `session_members` is the session's job ids when
    /// a session filter is set; `now` is Unix seconds.
    pub fn admits(
        &self,
        record: &LeadRecord,
        session_members: Option<&HashSet<String>>,
        now: i64,
    ) -> bool {
        let lead = &record.lead;
        let q = self.q.trim().to_ascii_lowercase();
        if !q.is_empty() && !search_text(lead).contains(&q) {
            return false;
        }
        let platform = self.platform.trim();
        if !platform.is_empty() && text(lead, "platform").trim() != platform {
            return false;
        }
        if let Some(wanted) = self.seniority {
            let band = resolve_lead_seniority(lead);
            let ok = match wanted {
                SeniorityFilter::Beginner => band.is_beginner(),
                SeniorityFilter::Band(wanted) => band == wanted,
            };
            if !ok {
                return false;
            }
        }
        if !self.location.trim().is_empty() {
            let requested = self.location.split(',').next().unwrap_or("");
            let place = normalize_location(requested);
            let have = normalize_location(text(lead, "location"));
            if !(place.is_empty() || have.contains("remote") || have.contains(&place)) {
                return false;
            }
        }
        if let Some(members) = session_members
            && !members.contains(text(lead, "job_id"))
        {
            return false;
        }
        if !self.semantic_admits(record) {
            return false;
        }
        if let Some(profile_skills) = &self.profile_skills {
            let wanted: HashSet<&str> = profile_skills
                .iter()
                .take(400)
                .map(String::as_str)
                .collect();
            let hit = record.requirements.iter().any(|item| {
                matches!(item.requirement.as_str(), "required" | "preferred")
                    && wanted.contains(normalize_skill(&item.name).as_str())
            });
            if !hit {
                return false;
            }
        }
        if let Some(hours) = self.posted_within_hours {
            let meta_date = lead
                .get("source_meta")
                .and_then(|meta| meta.get("date_posted"))
                .and_then(Value::as_str)
                .and_then(clock::parse_sqlite_datetime);
            let posted =
                meta_date.or_else(|| clock::parse_sqlite_datetime(text(lead, "created_at")));
            if posted.is_none_or(|posted| posted < now - hours * 3600) {
                return false;
            }
        }
        let company = self
            .company
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .to_lowercase();
        if !company.is_empty() && text(lead, "company").trim().to_lowercase() != company {
            return false;
        }
        let enrichment = record.enrichment.as_ref();
        let sponsorship = enrichment.and_then(|e| e.sponsorship.as_deref());
        match self.sponsorship.as_deref() {
            Some("yes") if sponsorship != Some("yes") => return false,
            Some("not_no") if sponsorship == Some("no") => return false,
            _ => {}
        }
        if !self.employment_types.is_empty() {
            let kind = enrichment
                .and_then(|e| e.employment_type.as_deref())
                .unwrap_or("");
            if !self.employment_types.iter().any(|wanted| wanted == kind) {
                return false;
            }
        }
        true
    }

    fn semantic_admits(&self, record: &LeadRecord) -> bool {
        if self.role_ids.is_empty()
            && self.experience_levels.is_empty()
            && self.required_skills.is_empty()
        {
            return true;
        }
        let empty = Enrichment::default();
        let enrichment = record.enrichment.as_ref().unwrap_or(&empty);
        let verdicts = [
            self.role_verdict(enrichment, record.enrichment.is_some()),
            self.experience_verdict(enrichment),
            self.skills_verdict(record, enrichment),
        ];
        let combined = if verdicts.contains(&Verdict::NoMatch) {
            Verdict::NoMatch
        } else if verdicts.contains(&Verdict::Pending) {
            Verdict::Pending
        } else if verdicts.contains(&Verdict::Unresolved) {
            Verdict::Unresolved
        } else {
            Verdict::Match
        };
        combined == Verdict::Match
            || (self.include_unresolved
                && matches!(combined, Verdict::Unresolved | Verdict::Pending))
    }

    fn role_verdict(&self, enrichment: &Enrichment, enriched: bool) -> Verdict {
        if self.role_ids.is_empty() {
            return Verdict::Match;
        }
        if self
            .role_ids
            .iter()
            .any(|id| enrichment.member_roles.contains(id))
        {
            return Verdict::Match;
        }
        let status = enrichment.role_status.as_deref();
        if matches!(status, Some("pending" | "failed")) {
            return Verdict::Pending;
        }
        let unresolved_requested = self
            .role_ids
            .iter()
            .any(|id| enrichment.unresolved_roles.contains(id));
        if unresolved_requested
            || !enriched
            || matches!(status, Some("unresolved" | "not_enriched") | None)
        {
            return Verdict::Unresolved;
        }
        Verdict::NoMatch
    }

    fn experience_verdict(&self, enrichment: &Enrichment) -> Verdict {
        if self.experience_levels.is_empty() {
            return Verdict::Match;
        }
        match enrichment.experience_status.as_deref() {
            Some("pending" | "failed") => Verdict::Pending,
            Some("resolved") => match &enrichment.experience_level {
                Some(level) if self.experience_levels.contains(level) => Verdict::Match,
                Some(_) => Verdict::NoMatch,
                None => Verdict::Unresolved,
            },
            _ => Verdict::Unresolved,
        }
    }

    fn skills_verdict(&self, record: &LeadRecord, enrichment: &Enrichment) -> Verdict {
        if self.required_skills.is_empty() {
            return Verdict::Match;
        }
        let status = enrichment.requirements_status.as_deref();
        if matches!(status, Some("pending" | "failed")) {
            return Verdict::Pending;
        }
        let wanted: HashSet<String> = self
            .required_skills
            .iter()
            .map(|name| normalize_skill(name))
            .collect();
        let hit = record.requirements.iter().any(|item| {
            let counts = item.requirement == "required"
                || (self.include_preferred && item.requirement == "preferred");
            counts && wanted.contains(&normalize_skill(&item.name))
        });
        if hit {
            Verdict::Match
        } else if status == Some("complete") {
            Verdict::NoMatch
        } else {
            Verdict::Unresolved
        }
    }

    /// Applies the view as well as the filters.
    pub fn admits_view(view: LeadView, lead: &Map<String, Value>) -> bool {
        view.admits(lead)
    }
}

/// A skill name as compared: case-folded, whitespace collapsed.
pub fn normalize_skill(name: &str) -> String {
    name.to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

static BANGALORE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\bbangalore\b").expect("valid pattern"));

/// A location as compared: lower case, `bangalore` read as `bengaluru`, whitespace collapsed.
pub fn normalize_location(location: &str) -> String {
    let lowered = location.to_lowercase();
    let renamed = BANGALORE.replace_all(&lowered, "bengaluru");
    renamed.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// The text free-text search looks in (ASCII lower case, as SQLite's `lower()` does).
fn search_text(lead: &Map<String, Value>) -> String {
    let mut parts: Vec<String> = ["title", "company", "platform", "status"]
        .iter()
        .map(|key| text(lead, key).to_owned())
        .collect();
    let kind = text(lead, "kind");
    parts.push(if kind.is_empty() {
        "job".to_owned()
    } else {
        kind.to_owned()
    });
    for key in [
        "budget",
        "location",
        "urgency",
        "feedback",
        "description",
        "reason",
        "signal_reason",
        "learning_reason",
    ] {
        parts.push(text(lead, key).to_owned());
    }
    for key in ["signal_tags", "tech_stack"] {
        for item in lead
            .get(key)
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            parts.push(match item {
                Value::String(text) => text.clone(),
                other => other.to_string(),
            });
        }
    }
    parts.join(" ").to_ascii_lowercase()
}

/// A string field, or `""`.
pub fn text<'a>(lead: &'a Map<String, Value>, key: &str) -> &'a str {
    lead.get(key).and_then(Value::as_str).unwrap_or("")
}

/// A numeric field, or 0.
pub fn number(lead: &Map<String, Value>, key: &str) -> f64 {
    match lead.get(key) {
        Some(Value::Number(value)) => value.as_f64().unwrap_or(0.0),
        Some(Value::String(value)) => value.trim().parse().unwrap_or(0.0),
        Some(Value::Bool(true)) => 1.0,
        _ => 0.0,
    }
}

fn group_part(value: &str) -> String {
    let normalized: String = value.nfkc().collect::<String>().to_lowercase();
    let dashed = normalized.replace(['\u{2013}', '\u{2014}'], "-");
    dashed.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// The key that groups near-identical postings: company, title and place.
pub fn similarity_key(lead: &Map<String, Value>) -> String {
    let company = group_part(text(lead, "company"));
    let title = group_part(text(lead, "title"));
    if company.is_empty() || title.is_empty() {
        return format!("job:{}", text(lead, "job_id"));
    }
    let place = normalize_location(&group_part(text(lead, "location")));
    format!("{company}\u{1f}{title}\u{1f}{place}")
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn lead(value: Value) -> LeadRecord {
        LeadRecord {
            lead: value.as_object().cloned().unwrap_or_default(),
            enrichment: None,
            requirements: Vec::new(),
        }
    }

    #[test]
    fn location_matches_remote_and_city() {
        assert_eq!(
            normalize_location("  Bangalore,  India "),
            "bengaluru, india"
        );
        let filters = LeadFilters {
            location: "Bengaluru, Karnataka".to_owned(),
            ..LeadFilters::default()
        };
        assert!(filters.admits(&lead(json!({"location": "Bangalore"})), None, 0));
        assert!(filters.admits(&lead(json!({"location": "Remote (US)"})), None, 0));
        assert!(!filters.admits(&lead(json!({"location": "Pune"})), None, 0));
    }

    #[test]
    fn unenriched_rows_pass_only_when_unresolved_is_included() {
        let mut filters = LeadFilters {
            role_ids: vec!["backend".to_owned()],
            include_unresolved: true,
            ..LeadFilters::default()
        };
        assert!(filters.admits(&lead(json!({})), None, 0));
        filters.include_unresolved = false;
        assert!(!filters.admits(&lead(json!({})), None, 0));
    }

    #[test]
    fn sorts_end_with_newest_then_id() {
        let a = json!({"job_id": "a", "created_at": "2026-01-01"});
        let b = json!({"job_id": "b", "created_at": "2026-01-02"});
        let (a, b) = (a.as_object().expect("a"), b.as_object().expect("b"));
        assert_eq!(LeadSort::Newest.compare(a, b), Ordering::Greater);
        assert_eq!(LeadSort::Match.compare(a, a), Ordering::Equal);
    }
}
