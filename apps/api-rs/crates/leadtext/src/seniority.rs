//! Seniority bands (`fresher`, `junior`, `mid`, `senior`, `unknown`) from job text.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value};

/// A seniority band.
// The variants are the band names; documenting each would only repeat them.
#[allow(missing_docs)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Seniority {
    Fresher,
    Junior,
    Mid,
    Senior,
    Unknown,
}

impl Seniority {
    /// The band's name as stored and shown.
    pub fn as_str(self) -> &'static str {
        match self {
            Seniority::Fresher => "fresher",
            Seniority::Junior => "junior",
            Seniority::Mid => "mid",
            Seniority::Senior => "senior",
            Seniority::Unknown => "unknown",
        }
    }

    /// The band named `name` (exactly, lower case).
    pub fn parse(name: &str) -> Option<Seniority> {
        match name {
            "fresher" => Some(Seniority::Fresher),
            "junior" => Some(Seniority::Junior),
            "mid" => Some(Seniority::Mid),
            "senior" => Some(Seniority::Senior),
            "unknown" => Some(Seniority::Unknown),
            _ => None,
        }
    }

    /// Fresher and junior roles count as beginner roles.
    pub fn is_beginner(self) -> bool {
        matches!(self, Seniority::Fresher | Seniority::Junior)
    }
}

const FRESHER_TERMS: &[&str] = &[
    "fresher",
    "new grad",
    "new graduate",
    "graduate",
    "intern",
    "internship",
    "trainee",
    "apprentice",
    "campus",
    "no experience required",
];

/// Ladder numerals and words that mark a first-rung role; most of them are left out of the
/// title-only check so "Senior Engineer I" stays senior.
const JUNIOR_LADDER_TERMS: &[&str] = &[
    "software engineer i",
    "software engineer 1",
    "developer i",
    "developer 1",
    "engineer i",
    "engineer 1",
    "sde i",
    "sde 1",
    "level 1",
    "level i",
    "l1",
    "associate",
];

const JUNIOR_WORD_TERMS: &[&str] = &[
    "junior",
    "jr.",
    "jr",
    "entry level",
    "entry-level",
    "fresher",
    "new grad",
    "new graduate",
    "graduate",
    "intern",
    "internship",
    "trainee",
    "apprentice",
    "early career",
    "campus",
    "0-1 year",
    "0-2 years",
    "0 to 2 years",
    "1-2 years",
    "1 to 2 years",
    "1+ year",
    "no experience required",
];

const MID_TERMS: &[&str] = &[
    "mid-level",
    "mid level",
    "mid senior",
    "intermediate",
    "software engineer ii",
    "software engineer 2",
    "developer ii",
    "developer 2",
    "engineer ii",
    "engineer 2",
    "sde ii",
    "sde 2",
    "level 2",
    "level ii",
    "l2",
    "3+ years",
    "3 years",
    "4+ years",
    "4 years",
];

const SENIOR_TITLE_TERMS: &[&str] = &[
    "senior",
    "sr.",
    "sr",
    "staff",
    "principal",
    "director",
    "head of",
];

const SENIOR_OTHER_TERMS: &[&str] = &[
    "lead",
    "manager",
    "architect",
    "expert",
    "5+ years",
    "5 years",
    "7+ years",
    "7 years",
    "10+ years",
    "10 years",
    "software engineer iii",
    "software engineer 3",
    "developer iii",
    "developer 3",
    "engineer iii",
    "engineer 3",
    "sde iii",
    "sde 3",
    "level 3",
    "level iii",
    "l3",
];

/// A set of terms that must stand alone: no ASCII letter or digit right before or after.
struct TermSet(Regex);

impl TermSet {
    fn new(groups: &[&[&str]]) -> TermSet {
        let alternatives: Vec<String> = groups
            .iter()
            .flat_map(|group| group.iter())
            .map(|term| {
                let words: Vec<String> = term.split_whitespace().map(regex::escape).collect();
                words.join(r"\s+")
            })
            .collect();
        let pattern = format!(
            "(?i)(?:^|[^a-z0-9])(?:{})(?:[^a-z0-9]|$)",
            alternatives.join("|")
        );
        TermSet(Regex::new(&pattern).expect("seniority terms form a valid pattern"))
    }

    fn matches(&self, text: &str) -> bool {
        self.0.is_match(text)
    }
}

static FRESHER: LazyLock<TermSet> = LazyLock::new(|| TermSet::new(&[FRESHER_TERMS]));
static JUNIOR: LazyLock<TermSet> =
    LazyLock::new(|| TermSet::new(&[JUNIOR_WORD_TERMS, JUNIOR_LADDER_TERMS]));
static JUNIOR_TITLE: LazyLock<TermSet> = LazyLock::new(|| TermSet::new(&[JUNIOR_WORD_TERMS]));
static MID: LazyLock<TermSet> = LazyLock::new(|| TermSet::new(&[MID_TERMS]));
static SENIOR: LazyLock<TermSet> =
    LazyLock::new(|| TermSet::new(&[SENIOR_TITLE_TERMS, SENIOR_OTHER_TERMS]));
static SENIOR_TITLE: LazyLock<TermSet> = LazyLock::new(|| TermSet::new(&[SENIOR_TITLE_TERMS]));

static YEAR_RANGE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s*(?:years|yrs|yoe)")
        .expect("valid year-range pattern")
});
static YEAR_SINGLE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(\d{1,2})\s*\+?\s*(?:years|yrs|yoe)").expect("valid year pattern")
});

/// The largest number of years of experience the text asks for, if it states any.
fn max_years(text: &str) -> Option<u32> {
    let mut best: Option<u32> = None;
    let mut consider = |value: Option<u32>| {
        if let Some(value) = value {
            best = Some(best.map_or(value, |current| current.max(value)));
        }
    };
    for captures in YEAR_RANGE.captures_iter(text) {
        let low = captures.get(1).and_then(|m| m.as_str().parse::<u32>().ok());
        let high = captures.get(2).and_then(|m| m.as_str().parse::<u32>().ok());
        consider(low.max(high));
    }
    for captures in YEAR_SINGLE.captures_iter(text) {
        consider(captures.get(1).and_then(|m| m.as_str().parse::<u32>().ok()));
    }
    best
}

/// Classifies free text (already lower-cased by callers that care).
pub fn classify_text(text: &str) -> Seniority {
    let years = max_years(text);
    if years.is_some_and(|years| years >= 5) {
        return Seniority::Senior;
    }
    if FRESHER.matches(text) {
        return Seniority::Fresher;
    }
    if JUNIOR.matches(text) {
        return Seniority::Junior;
    }
    match years {
        Some(years) if years <= 1 => return Seniority::Fresher,
        Some(years) if years <= 2 => return Seniority::Junior,
        _ => {}
    }
    if SENIOR.matches(text) {
        return Seniority::Senior;
    }
    if MID.matches(text) || years.is_some_and(|years| years >= 3) {
        return Seniority::Mid;
    }
    Seniority::Unknown
}

/// Python's `str()` of a lead field: missing → `""`, null → `None`.
fn field_text(lead: &Map<String, Value>, key: &str) -> String {
    match lead.get(key) {
        None => String::new(),
        Some(Value::Null) => "None".to_owned(),
        Some(Value::String(text)) => text.clone(),
        Some(other) => other.to_string(),
    }
}

/// The text the classifier reads for a lead: its main fields, then every scalar value in
/// `meta`.
fn lead_text(lead: &Map<String, Value>, meta: &Map<String, Value>) -> String {
    let fields: Vec<String> = ["title", "company", "platform", "description", "posted_date"]
        .iter()
        .map(|key| field_text(lead, key))
        .collect();
    let mut scalars: Vec<String> = Vec::new();
    for value in meta.values() {
        match value {
            Value::String(text) => scalars.push(text.clone()),
            Value::Bool(flag) => scalars.push(if *flag { "True" } else { "False" }.to_owned()),
            Value::Number(number) => scalars.push(number.to_string()),
            _ => {}
        }
    }
    format!("{} {}", fields.join("\n"), scalars.join(" ")).to_lowercase()
}

/// The band for a posting: a clearly senior title wins, otherwise the whole text decides.
pub fn job_seniority(lead: &Map<String, Value>, meta: &Map<String, Value>) -> Seniority {
    let title = field_text(lead, "title").to_lowercase();
    if SENIOR_TITLE.matches(&title) && !JUNIOR_TITLE.matches(&title) && !FRESHER.matches(&title) {
        return Seniority::Senior;
    }
    classify_text(&lead_text(lead, meta))
}

/// The band shown for a lead: a stored band wins (even `unknown`), then the corpus seniority,
/// then the classifier.
pub fn resolve_lead_seniority(lead: &Map<String, Value>) -> Seniority {
    let empty = Map::new();
    let meta = lead
        .get("source_meta")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let stored = |key: &str| -> String {
        meta.get(key)
            .or_else(|| lead.get(key))
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_lowercase()
    };
    if let Some(band) = Seniority::parse(&stored("seniority_level")) {
        return band;
    }
    let corpus = match stored("seniority").as_str() {
        "intern" => Some(Seniority::Fresher),
        "junior" => Some(Seniority::Junior),
        "mid" => Some(Seniority::Mid),
        "senior" | "lead" | "exec" => Some(Seniority::Senior),
        _ => None,
    };
    corpus.unwrap_or_else(|| job_seniority(lead, meta))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn text_rules() {
        assert_eq!(classify_text("need 5+ years of go"), Seniority::Senior);
        assert_eq!(
            classify_text("graduate trainee programme"),
            Seniority::Fresher
        );
        assert_eq!(classify_text("jr. developer"), Seniority::Junior);
        assert_eq!(classify_text("1 years ok"), Seniority::Fresher);
        assert_eq!(classify_text("tech lead"), Seniority::Senior);
        assert_eq!(classify_text("3 yrs"), Seniority::Mid);
        assert_eq!(classify_text("backend engineer"), Seniority::Unknown);
        // A term inside a longer word does not count.
        assert_eq!(classify_text("internal tools"), Seniority::Unknown);
    }

    #[test]
    fn senior_title_beats_ladder_numeral() {
        let lead = json!({"title": "Senior Frontend Engineer I", "description": ""});
        let lead = lead.as_object().expect("object");
        assert_eq!(job_seniority(lead, &Map::new()), Seniority::Senior);
    }

    #[test]
    fn stored_band_wins() {
        let lead = json!({"title": "Intern", "source_meta": {"seniority_level": "Unknown"}});
        assert_eq!(
            resolve_lead_seniority(lead.as_object().expect("object")),
            Seniority::Unknown
        );
        let lead = json!({"title": "x", "source_meta": {"seniority": "lead"}});
        assert_eq!(
            resolve_lead_seniority(lead.as_object().expect("object")),
            Seniority::Senior
        );
    }
}
