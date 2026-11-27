//! Deterministic application documents and outreach (§8).

use std::collections::HashSet;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

use crate::selection::Selection;
use crate::{fields, keywords, markdown};

/// Contact-line settings passed by the route. Absent fields preserve legacy behavior.
#[derive(Clone, Copy, Debug, Default)]
pub struct ContactLine<'a> {
    /// The explicitly enabled contact fields, if supplied.
    pub allowed: Option<&'a [String]>,
    /// The explicit location visibility setting, if supplied.
    pub show_location: Option<bool>,
}

impl ContactLine<'_> {
    pub(crate) fn active(self) -> bool {
        self.allowed.is_some() || self.show_location.is_some()
    }
    pub(crate) fn permits(self, kind: &str) -> bool {
        !self.active()
            || self
                .allowed
                .is_some_and(|values| values.iter().any(|value| value == kind))
    }
}

#[derive(Clone, Default)]
pub(crate) struct Draft {
    pub(crate) resume: String,
    pub(crate) letter: String,
    pub(crate) projects: Vec<String>,
    pub(crate) founder: String,
    pub(crate) linkedin: String,
    pub(crate) email: String,
}

pub(crate) fn build(
    lead: &Value,
    profile: &Value,
    selection: Option<&Selection>,
    contact: ContactLine<'_>,
    terms: &[&str],
) -> Draft {
    let name = or(fields::string(profile, &["n"]), "Candidate");
    let title = fields::string(lead, &["title"]);
    let company = fields::string(lead, &["company"]);
    let summary = markdown::squash(fields::string(profile, &["s"]));
    let summary_role = markdown::clip(
        summary.split(['.', ',']).next().unwrap_or_default().trim(),
        60,
    );
    let role = or(
        title,
        or(
            fields::string(profile, &["desired_position"]),
            or(summary_role, "the advertised role"),
        ),
    );
    let company_label = or(company, "the company");
    let skills = skills(profile, terms);
    let names: Vec<&str> = skills.iter().map(|skill| skill.name.as_str()).collect();
    let selected = selection.filter(|selection| selection.is_active());
    let projects = if selected.is_some() {
        fields::list(profile, "projects").iter().collect()
    } else {
        rank_projects(lead, profile, 4)
    };
    let project_names: Vec<String> = projects
        .iter()
        .map(|project| fields::label(project).to_owned())
        .collect();
    let mut resume = format!("# {name}");
    let contacts = contact_parts(profile, contact);
    if !contacts.is_empty() {
        resume.push_str(&format!("\n{}", contacts.join(" | ")));
    }
    let sections = document_sections(profile, &skills, &projects, selected);
    for (heading, body) in sections {
        resume.push_str(&format!("\n\n## {heading}\n{body}"));
    }
    let background = if names.is_empty() {
        or(&summary, "this field").to_owned()
    } else {
        names[..names.len().min(5)].join(", ")
    };
    let recent = if project_names.is_empty() {
        "relevant work in this field".to_owned()
    } else {
        project_names[..project_names.len().min(3)].join(", ")
    };
    let letter = format!(
        "Dear {company_label} team,\n\nI am writing to apply for the {role} position at {company_label}. My background in {background} aligns directly with the requirements outlined in your posting.\n\nIn my recent work on {recent}, I have built directly relevant experience and a track record of delivering the outcomes your team is looking for.\n\nI would welcome the opportunity to discuss how my experience maps to your needs. Thank you for your consideration.\n\nSincerely,\n{name}"
    );
    let outreach_title = or(title, "the role");
    let outreach_company = or(company, "your company");
    let first = names[..names.len().min(4)].join(", ");
    let first = or(&first, "software engineering");
    let three = names[..names.len().min(3)].join(", ");
    let three = or(&three, "full-stack development");
    let founder = format!(
        "Your {outreach_title} role caught my eye — the problem space is compelling.\nI bring hands-on {first} experience with shipped projects that map to your stack.\nHappy to share specifics or jump on a quick call."
    );
    let linkedin = format!(
        "Hi! Saw the {outreach_title} opening at {outreach_company}. My background in {three} maps well to the role. Would love to connect and share more."
    );
    let email = format!(
        "Subject: {outreach_title} at {outreach_company} — relevant {first} background\n\nHi {outreach_company} team,\n\nI came across the {outreach_title} role and it aligns closely with my work in {first}. I have shipped production systems that mirror the requirements in your posting. I would welcome the chance to share specific project examples that demonstrate direct fit.\n\nBest regards,\n{name}"
    );
    Draft {
        resume,
        letter,
        projects: project_names,
        founder: cap_chars(&founder, 280),
        linkedin: cap_chars(&linkedin, 300),
        email: cap_chars(&email, 600),
    }
}

fn contact_parts(profile: &Value, contact: ContactLine<'_>) -> Vec<String> {
    let identity = &profile["identity"];
    [
        ("linkedin", "linkedin_url", "LinkedIn"),
        ("email", "email", "Email"),
        ("github", "github_url", "GitHub"),
        ("phone", "phone", "Phone"),
    ]
    .into_iter()
    .filter_map(|(kind, key, label)| {
        let value = or(
            fields::string(identity, &[key]),
            fields::string(profile, &[key]),
        );
        (contact.permits(kind) && !value.trim().is_empty())
            .then(|| format!("{label}: {}", value.trim()))
    })
    .collect()
}

fn document_sections(
    profile: &Value,
    skills: &[Skill],
    projects: &[&Value],
    selected: Option<&Selection>,
) -> Vec<(&'static str, String)> {
    let mut sections = Vec::new();
    let grouped = skill_lines(skills);
    if !grouped.is_empty() || selected.is_none() {
        sections.push(("SKILLS", grouped));
    }
    let exp = fields::list(profile, "exp")
        .iter()
        .map(experience)
        .filter(|text| !text.is_empty())
        .collect::<Vec<_>>()
        .join("\n");
    if !exp.is_empty() {
        sections.push(("EXPERIENCE", exp));
    }
    let project_text = projects
        .iter()
        .map(|project| project_block(project, selected.is_some()))
        .collect::<Vec<_>>()
        .join("\n");
    if !project_text.is_empty() {
        sections.push(("PROJECTS", project_text));
    } else if selected.is_none() {
        sections.push(("PROJECTS","### Role-Matched Project Evidence\n- Add projects to the Identity Graph for stronger tailoring.".to_owned()));
    }
    for (heading, keys) in [
        ("CERTIFICATES", &["certifications", "certs"][..]),
        ("ACHIEVEMENTS", &["achievements"][..]),
        ("EDUCATION", &["education"][..]),
    ] {
        let labels = keys
            .iter()
            .flat_map(|key| fields::list(profile, key))
            .map(fields::label)
            .map(str::trim)
            .filter(|row| !row.is_empty())
            .collect::<Vec<_>>();
        if !labels.is_empty() {
            sections.push((heading, labels.join("\n")));
        }
    }
    if let Some(selection) = selected.filter(|selection| !selection.sections.is_empty()) {
        sections.retain(|(heading, _)| {
            selection
                .sections
                .iter()
                .any(|key| section_key(key) == *heading)
        });
        sections.sort_by_key(|(heading, _)| {
            selection
                .sections
                .iter()
                .position(|key| section_key(key) == *heading)
                .unwrap_or(usize::MAX)
        });
    }
    sections
}

fn section_key(key: &str) -> String {
    match key.trim().to_ascii_lowercase().as_str() {
        "exp" | "work" | "work_experience" => "EXPERIENCE".to_owned(),
        "certs" | "certifications" => "CERTIFICATES".to_owned(),
        other => other.to_ascii_uppercase(),
    }
}

fn experience(row: &Value) -> String {
    let role = fields::string(row, &["role"]);
    if role.is_empty() {
        return String::new();
    }
    let company = fields::string(row, &["co", "company"]);
    let period = fields::string(row, &["period"]);
    let mut text = format!("### {role}");
    if !company.is_empty() {
        text.push_str(&format!(" - {company}"));
    }
    if !period.is_empty() {
        text.push_str(&format!(" - {period}"));
    }
    append_points(&mut text, row);
    text
}

fn project_block(row: &Value, selected: bool) -> String {
    let mut text = format!("### {}", fields::label(row));
    if !selected {
        let stack = fields::compact(fields::first(row, &["stack", "s"]));
        if !stack.trim().is_empty() {
            text.push_str(&format!("\n- Tech: {stack}"));
        }
    }
    append_points(&mut text, row);
    text
}

fn append_points(text: &mut String, row: &Value) {
    for point in fields::list(row, "points") {
        let line = fields::string(point, &["text"]);
        if !line.is_empty() {
            text.push_str(&format!("\n- {line}"));
        }
    }
}

struct Skill {
    name: String,
    category: &'static str,
    priority: u8,
}
const GROUPS: [&str; 6] = [
    "Languages",
    "Frameworks & Libraries",
    "Databases & Data Tools",
    "Tools & Platforms",
    "Core Concepts",
    "AI Skills",
];

fn skills(profile: &Value, terms: &[&str]) -> Vec<Skill> {
    let mut seen = HashSet::new();
    let lower = terms
        .iter()
        .map(|term| term.to_lowercase())
        .collect::<Vec<_>>();
    let mut skills = Vec::new();
    for row in fields::list(profile, "skills") {
        let name = markdown::squash(fields::string(row, &["n", "name"]));
        let key = name.to_lowercase();
        if key.is_empty() || !seen.insert(key.clone()) {
            continue;
        }
        let priority = if lower.contains(&key) {
            0
        } else if lower
            .iter()
            .any(|term| term.contains(&key) || key.contains(term))
        {
            1
        } else {
            2
        };
        let category = skill_group(fields::string(row, &["cat", "category"]), &key);
        skills.push(Skill {
            name,
            category,
            priority,
        });
        if skills.len() == 80 {
            break;
        }
    }
    skills.sort_by_key(|skill| skill.priority);
    skills.truncate(28);
    skills
}

fn skill_group(category: &str, name: &str) -> &'static str {
    match category.to_lowercase().as_str() {
        "language" | "languages" | "programming_languages" => GROUPS[0],
        "framework" | "frameworks" | "frontend" | "backend" | "library" | "libraries" => GROUPS[1],
        "database" | "databases" | "data" => GROUPS[2],
        "tool" | "tools" | "platform" | "infra" | "devops" | "cloud" => GROUPS[3],
        "concept" | "concepts" | "technical" => GROUPS[4],
        "ai" | "ml" | "ai/ml" => GROUPS[5],
        "" | "general" | "project_stack" => {
            if [
                "python",
                "rust",
                "java",
                "javascript",
                "typescript",
                "go",
                "ruby",
                "c++",
                "c#",
                "php",
            ]
            .contains(&name)
            {
                GROUPS[0]
            } else if [
                "react", "django", "fastapi", "flask", "next.js", "vue", "angular", "node.js",
                "express",
            ]
            .contains(&name)
            {
                GROUPS[1]
            } else if [
                "postgresql",
                "mysql",
                "redis",
                "sql",
                "mongodb",
                "kafka",
                "sqlite",
            ]
            .contains(&name)
            {
                GROUPS[2]
            } else if [
                "llm",
                "rag",
                "pytorch",
                "tensorflow",
                "nlp",
                "langchain",
                "machine learning",
                "ai",
            ]
            .contains(&name)
            {
                GROUPS[5]
            } else if [
                "distributed systems",
                "system design",
                "microservices",
                "algorithms",
            ]
            .contains(&name)
            {
                GROUPS[4]
            } else {
                GROUPS[3]
            }
        }
        _ => GROUPS[3],
    }
}

fn skill_lines(skills: &[Skill]) -> String {
    let mut lines = Vec::new();
    for category in GROUPS {
        let names: Vec<&str> = skills
            .iter()
            .filter(|skill| skill.category == category)
            .map(|skill| skill.name.as_str())
            .collect();
        if !names.is_empty() {
            lines.push(format!("**{category}:** {}", names.join(", ")));
        }
    }
    lines.join("\n")
}

pub(crate) fn rank_projects<'a>(lead: &Value, profile: &'a Value, limit: usize) -> Vec<&'a Value> {
    static TOKEN: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"[a-zA-Z][a-zA-Z0-9+#.\-]{1,}").expect("valid project ranking token")
    });
    const STOP: &[&str] = &[
        "and",
        "the",
        "with",
        "for",
        "from",
        "that",
        "this",
        "you",
        "are",
        "job",
        "role",
        "engineer",
        "developer",
        "company",
        "team",
        "will",
        "have",
        "has",
        "using",
        "build",
        "work",
        "your",
        "their",
    ];
    let job = keywords::jd_text(lead).to_lowercase();
    let tokens: HashSet<&str> = TOKEN
        .find_iter(&job)
        .map(|token| token.as_str())
        .filter(|token| !STOP.contains(token))
        .collect();
    let mut ranked: Vec<(usize, &Value)> = fields::list(profile, "projects")
        .iter()
        .filter_map(|row| {
            let title = markdown::squash(fields::label(row));
            let lower_title = title.to_lowercase();
            if title.is_empty()
                || title.chars().count() > 120
                || title.contains('@')
                || lower_title.contains("http://")
                || lower_title.contains("https://")
                || lower_title.contains("www.")
                || [
                    "api",
                    "apis",
                    "conditioning",
                    "github",
                    "repo",
                    "repository",
                    "project",
                    "projects",
                ]
                .contains(&lower_title.as_str())
            {
                return None;
            }
            let stack = fields::compact(fields::first(row, &["stack", "s"])).to_lowercase();
            let impact = fields::string(row, &["impact", "description"]).to_lowercase();
            let text = format!("{lower_title} {stack} {impact}");
            let score = tokens
                .iter()
                .map(|token| {
                    usize::from(keywords::contains(&text, token))
                        + 3 * usize::from(keywords::contains(&stack, token))
                })
                .sum();
            Some((score, row))
        })
        .collect();
    ranked.sort_by_key(|(score, _)| std::cmp::Reverse(*score));
    ranked.into_iter().take(limit).map(|(_, row)| row).collect()
}

pub(crate) fn cap_chars(input: &str, max: usize) -> String {
    let text = input.trim();
    let clipped = markdown::clip(text, max);
    if clipped.len() == text.len() {
        return clipped.to_owned();
    }
    let clipped = clipped.rsplit_once(' ').map_or(clipped, |(text, _)| text);
    clipped
        .trim_end_matches([' ', ',', ';', ':', '-', '–', '—'])
        .to_owned()
}

pub(crate) fn cap_words(text: &str, limit: usize) -> String {
    let text = text.trim();
    if text.split_whitespace().count() <= limit {
        return text.to_owned();
    }
    let mut remaining = limit;
    let mut lines = Vec::new();
    for line in text.lines() {
        let words = line.split_whitespace().collect::<Vec<_>>();
        if words.len() <= remaining {
            lines.push(line.to_owned());
            remaining -= words.len();
        } else {
            lines.push(words[..remaining].join(" "));
            break;
        }
        if remaining == 0 {
            break;
        }
    }
    lines
        .join("\n")
        .trim_end_matches([' ', ',', ';', ':', '-'])
        .to_owned()
}

pub(crate) fn or<'a>(value: &'a str, fallback: &'a str) -> &'a str {
    if value.trim().is_empty() {
        fallback
    } else {
        value
    }
}
