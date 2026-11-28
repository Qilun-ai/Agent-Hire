//! Candidate terms, role lanes, work history, and evidence used by the rubric.

use std::collections::BTreeMap;
use std::sync::LazyLock;

use agent_hire_ml::embedding_text;
use regex::Regex;
use serde_json::Value;

use crate::taxonomy::Terms;
use crate::{duration, json_fields, taxonomy};

pub(crate) const CREDENTIALS: &[&str] = &[
    "education",
    "certifications",
    "certs",
    "achievements",
    "awards",
];

const DOMAIN_STOPWORDS: &[&str] = &[
    "and",
    "or",
    "the",
    "with",
    "for",
    "of",
    "in",
    "to",
    "a",
    "an",
    "skills",
    "experience",
    "proficient",
    "proficiency",
    "knowledge",
    "strong",
    "excellent",
    "ability",
    "etc",
    "various",
    "other",
    "general",
    "basic",
    "advanced",
];
static DOMAIN_PUNCTUATION: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[^a-z0-9+#./ -]+").expect("valid domain normalization pattern"));

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Level {
    Fresher,
    Junior,
    Mid,
    Senior,
}

impl Level {
    pub(crate) fn label(self) -> &'static str {
        match self {
            Self::Fresher => "fresher",
            Self::Junior => "junior",
            Self::Mid => "mid",
            Self::Senior => "senior",
        }
    }

    pub(crate) fn years(self) -> f64 {
        match self {
            Self::Fresher => 0.0,
            Self::Junior => 1.5,
            Self::Mid => 3.5,
            Self::Senior => 7.0,
        }
    }
}

pub(crate) struct Candidate {
    pub(crate) skills: Terms,
    pub(crate) project_evidence: BTreeMap<String, Vec<String>>,
    pub(crate) experience_evidence: BTreeMap<String, Vec<String>>,
    pub(crate) terms: Terms,
    pub(crate) lanes: Terms,
    pub(crate) deliverables: Terms,
    pub(crate) project_count: usize,
    pub(crate) level: Level,
    pub(crate) work_months: i64,
    pub(crate) location: &'static str,
    pub(crate) text: String,
    pub(crate) domain_phrases: Terms,
}

impl Candidate {
    pub(crate) fn from_profile(profile: &Value, today: &str) -> Self {
        let summary = json_fields::string(profile, &["s"]);
        let desired = json_fields::string(profile, &["desired_position"]);
        let skills = json_fields::items(profile, "skills");
        let skill_names: Vec<&str> = skills
            .iter()
            .map(skill_name)
            .filter(|name| !name.is_empty())
            .collect();
        let skill_terms: Terms = skill_names
            .iter()
            .flat_map(|name| taxonomy::skill_tech(name))
            .collect();
        let mut phrases: Vec<&str> = skill_names.clone();
        for key in CREDENTIALS {
            phrases.extend(json_fields::items(profile, key).iter().map(credential_name));
        }
        let domain_phrases = phrases
            .into_iter()
            .map(normalize_domain)
            .filter(|phrase| {
                (3..=48).contains(&phrase.len())
                    && !DOMAIN_STOPWORDS.contains(&phrase.as_str())
                    && !taxonomy::resolves_tech(phrase)
            })
            .collect();

        let projects = json_fields::items(profile, "projects");
        let mut proof = Vec::new();
        let mut project_evidence = BTreeMap::new();
        for project in projects {
            let title = json_fields::string(project, &["title", "name"]);
            let stack = embedding_text::stack_text(json_fields::first(project, &["stack", "s"]));
            let impact = json_fields::string(project, &["impact", "description"]);
            let text = format!("{title} {stack} {impact}");
            add_evidence(&mut project_evidence, &text, title);
            if !text.trim().is_empty() {
                proof.push(format!(
                    "Project: {title} | Stack: {stack} | Impact: {impact}"
                ));
            }
        }

        let mut experience_evidence = BTreeMap::new();
        let mut dated_months = 0;
        let mut stated_months =
            duration::stated_months(summary).max(duration::stated_months(desired));
        let mut real_roles = 0;
        let mut senior_role = false;
        for experience in json_fields::items(profile, "exp") {
            let role = json_fields::string(experience, &["role"]);
            let company = json_fields::string(experience, &["co", "company"]);
            let period = json_fields::string(experience, &["period"]);
            let description = json_fields::string(experience, &["d", "description"]);
            let stack = embedding_text::stack_text(json_fields::first(experience, &["s", "stack"]));
            let label = experience_label(role, company, " at ");
            let text = format!("{label} {period} {stack} {description}");
            if !text.trim().is_empty() {
                add_evidence(&mut experience_evidence, &text, &label);
                proof.push(format!(
                    "Role: {role} at {company} ({period}) | Stack: {stack} | {description}"
                ));
            }

            let role_lower = role.to_lowercase();
            let training = ["intern", "trainee", "student", "assistant only"];
            if role_lower.trim().is_empty() || training.iter().any(|word| role_lower.contains(word))
            {
                continue;
            }
            real_roles += 1;
            let senior = ["senior", "lead", "principal", "staff", "head of", "manager"];
            senior_role |= taxonomy::any(&role_lower, &senior);
            dated_months += duration::months(period, today);
            stated_months = stated_months.max(duration::stated_months(description));
        }
        if !skill_names.is_empty() {
            proof.push(format!("Skills: {}", skill_names.join(", ")));
        }
        let proof_text = if proof.is_empty() {
            "No profile data found.".to_owned()
        } else {
            proof.join("\n")
        };
        let level = match dated_months {
            months if months >= 60 || (senior_role && months >= 36) => Level::Senior,
            months if months >= 24 || real_roles >= 2 => Level::Mid,
            _ if real_roles >= 1 || projects.len() >= 2 => Level::Junior,
            _ => Level::Fresher,
        };
        let text = format!("{summary}\n{desired}\n{proof_text}")
            .trim()
            .to_owned();
        let mut terms = taxonomy::tech(summary);
        terms.extend(skill_terms.iter().cloned());
        terms.extend(project_evidence.keys().cloned());
        terms.extend(experience_evidence.keys().cloned());
        let mut lanes = taxonomy::roles(&text);
        for term in &terms {
            // The ranking spec describes role inference from tech categories without a category-to-role table.
            match taxonomy::category(term) {
                Some(
                    category @ ("ai" | "backend" | "frontend" | "data" | "mobile" | "desktop"
                    | "testing"),
                ) => {
                    lanes.insert(category.to_owned());
                }
                Some("infra") => {
                    lanes.insert("devops".to_owned());
                }
                _ => (),
            }
        }

        let location = detect_location(&format!("{summary}\n{proof_text}"));
        let deliverables = taxonomy::deliverables(&text);
        Self {
            skills: skill_terms,
            project_evidence,
            experience_evidence,
            terms,
            lanes,
            deliverables,
            project_count: projects.len(),
            level,
            work_months: dated_months.max(stated_months),
            location,
            text,
            domain_phrases,
        }
    }
}

fn detect_location(text: &str) -> &'static str {
    let text = text.to_lowercase();
    let places = [
        ("india", "India"),
        ("united states", "US"),
        ("usa", "US"),
        ("us", "US"),
        ("canada", "Canada"),
        ("uk", "Uk"),
        ("europe", "Europe"),
    ];
    places
        .into_iter()
        .find(|(key, _)| taxonomy::has(&text, key))
        .map_or("", |(_, name)| name)
}

fn add_evidence(evidence: &mut BTreeMap<String, Vec<String>>, text: &str, label: &str) {
    for term in taxonomy::tech(text) {
        let labels = evidence.entry(term).or_default();
        if !label.is_empty() && !labels.iter().any(|item| item == label) {
            labels.push(label.to_owned());
        }
    }
}

pub(crate) fn skill_name(skill: &Value) -> &str {
    json_fields::string(skill, &["n", "name"])
}

pub(crate) fn credential_name(credential: &Value) -> &str {
    credential
        .as_str()
        .unwrap_or_else(|| json_fields::string(credential, &["title", "name", "n"]))
}

pub(crate) fn experience_label(role: &str, company: &str, separator: &str) -> String {
    match (role.is_empty(), company.is_empty()) {
        (false, false) => format!("{role}{separator}{company}"),
        (false, true) => role.to_owned(),
        (true, false) => company.to_owned(),
        (true, true) => String::new(),
    }
}

pub(crate) fn normalize_domain(text: &str) -> String {
    json_fields::squash(&DOMAIN_PUNCTUATION.replace_all(&text.to_lowercase(), " "))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{Candidate, Level};

    #[test]
    fn distinguishes_dated_level_from_the_prose_floor() {
        let candidate = Candidate::from_profile(
            &json!({
                "s":"Based in Canada, 6 years of experience.",
                "exp":[
                    {"role":"Software Engineering Intern","period":"2018-2023","d":"10 years"},
                    {"role":"Engineer","period":"6 months","d":"8 years of coding"}
                ],
                "projects":[{"title":"Ledger","stack":["PostgreSQL"]}],
            }),
            "jun 2026",
        );
        assert_eq!(candidate.level, Level::Junior);
        assert_eq!(candidate.work_months, 96);
        assert_eq!(candidate.location, "Canada");
        assert_eq!(candidate.project_evidence["PostgreSQL"], ["Ledger"]);
    }
}
