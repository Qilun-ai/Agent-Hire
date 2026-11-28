//! Deterministic résumé parsing when the legacy model is unavailable.

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Value, json};

/// Extracts candidate fields and recognizable résumé sections from plain text.
pub fn local_profile(text: &str) -> Value {
    static SECTION: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
        r"(?i)^(summary|profile|objective|skills|technical skills|technologies|tools|experience|work experience|employment|projects|selected projects|personal projects|education|certifications|credentials|certificates|achievements|awards|honors)\s*:?$"
    ).expect("valid résumé headings")
    });
    static TECH: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
        r"(?i)\b(?:Python|TypeScript|JavaScript|React|FastAPI|PostgreSQL|Docker|AWS|LangChain|RAG|PyTorch|Rust|Playwright|Kubernetes|Redis|MongoDB|MySQL|Terraform|Linux|Kafka|Django|Flask|Azure|GitHub|GCP|HTML|CSS|SQLite|Java|Git|SQL)\b"
    ).expect("valid résumé technology list")
    });
    let mut sections = HashMap::<String, Vec<&str>>::new();
    let mut section = String::new();
    let mut name = String::new();
    let mut summary = String::new();
    for (index, line) in text
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .enumerate()
    {
        let heading = line.trim_matches(['#', '-', ' ']).trim();
        if let Some(found) = SECTION.find(heading) {
            section = found
                .as_str()
                .trim_end_matches(':')
                .trim()
                .to_ascii_lowercase();
            continue;
        }
        if section.is_empty()
            && let Some((field, value)) = line.split_once(':')
        {
            if field.eq_ignore_ascii_case("name")
                || (name.is_empty() && field.eq_ignore_ascii_case("targetRole"))
            {
                name = value.trim().to_owned();
                continue;
            }
            if field.eq_ignore_ascii_case("summary") {
                summary = value.trim().to_owned();
                continue;
            }
        }
        if index < 8
            && name.is_empty()
            && section.is_empty()
            && let Some(candidate) = candidate_name(line)
        {
            name = candidate.to_owned();
        }
        sections.entry(section.clone()).or_default().push(line);
    }
    let section_rows = |names: &[&str]| {
        names
            .iter()
            .filter_map(|name| sections.get(*name))
            .flatten()
            .copied()
            .collect::<Vec<_>>()
    };
    if summary.is_empty() {
        summary = section_rows(&["summary", "profile", "objective"])
            .into_iter()
            .take(3)
            .map(unbullet)
            .collect::<Vec<_>>()
            .join(" ");
    }
    let project_rows = local_projects(&section_rows(&[
        "projects",
        "selected projects",
        "personal projects",
    ]));
    let mut skills = Vec::new();
    let mut seen = HashSet::new();
    let mut add_skill = |name: &str| {
        let name = unbullet(name).trim();
        if !name.is_empty() && seen.insert(name.to_lowercase()) {
            skills.push(json!({"n": name, "cat": "resume"}));
        }
    };
    for line in section_rows(&["skills", "technical skills", "technologies", "tools"]) {
        let line = line.split_once(':').map_or(line, |(_, values)| values);
        for skill in line.split([',', '|', ';']) {
            add_skill(skill);
        }
    }
    for item in TECH.find_iter(text) {
        add_skill(item.as_str());
    }
    for project in &project_rows {
        if let Some(stack) = project["stack"].as_str() {
            for skill in stack.split(',') {
                add_skill(skill);
            }
        }
    }
    skills.truncate(40);
    let entries = |names: &[&str], max| {
        section_rows(names)
            .into_iter()
            .take(max)
            .map(unbullet)
            .collect::<Vec<_>>()
    };
    json!({
        "n": if name.is_empty() { "Candidate" } else { &name }, "s": summary,
        "skills": skills,
        "exp": local_experience(&section_rows(&["experience", "work experience", "employment"])),
        "projects": project_rows,
        "education": entries(&["education"], 30),
        "certifications": entries(&["certifications", "credentials", "certificates"], 8),
        "achievements": entries(&["achievements", "awards", "honors"], 8),
    })
}

fn unbullet(line: &str) -> &str {
    line.trim_start_matches(['-', '*', '•', '‣', '◦', ' '])
        .trim()
}

fn candidate_name(line: &str) -> Option<&str> {
    static NAME: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^[A-Za-z][A-Za-z.'’-]*(?:\s+[A-Za-z][A-Za-z.'’-]*){0,4}$")
            .expect("valid résumé name")
    });
    let lower = line.to_lowercase();
    if [
        "@",
        "http",
        "linkedin",
        "github",
        "resume",
        "curriculum",
        "engineer |",
        "developer |",
    ]
    .iter()
    .any(|word| lower.contains(word))
    {
        return None;
    }
    let candidate = line.trim_start_matches(|c: char| !c.is_ascii_alphabetic());
    NAME.is_match(candidate).then_some(candidate)
}

fn local_experience(lines: &[&str]) -> Vec<Value> {
    static ROLE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
        r"(?i)\b(?:engineer|developer|manager|architect|designer|analyst|intern|trainee|volunteer|founder|owner|lead|head|director|consultant|associate|specialist|teacher|scientist|researcher)\b"
    ).expect("valid occupation pattern")
    });
    static DATE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
        r"(?i)(?:\b[a-z]{3,9}\s+)?(?:19|20)\d{2}(?:\s*[-–—]\s*(?:(?:[a-z]{3,9}\s+)?(?:19|20)\d{2}|present|current))?$"
    ).expect("valid résumé dates")
    });
    let mut rows = Vec::<Value>::new();
    for line in lines.iter().take(30) {
        let clean = unbullet(line);
        if clean.chars().count() < 6 {
            continue;
        }
        let header = line.strip_prefix("Experience: ").unwrap_or(line);
        let date = DATE.find(header);
        if let Some(date) = date.filter(|date| date.start() == 0) {
            if let Some(row) = rows.last_mut() {
                row["period"] = json!(date.as_str());
            }
            continue;
        }
        let is_header = clean == *line
            && !header.ends_with('.')
            && header.split_whitespace().count() <= 16
            && (header.contains(" at ") || ROLE.is_match(header));
        if is_header {
            if rows.len() >= 10 {
                break;
            }
            let label = date.map_or(header, |date| {
                header[..date.start()].trim_end_matches([' ', '-', '–', '—', '|', '('])
            });
            let (role, company) = label
                .split_once(" at ")
                .or_else(|| {
                    [" | ", " – ", " — ", " - "]
                        .into_iter()
                        .find_map(|delimiter| label.split_once(delimiter))
                })
                .unwrap_or((label, ""));
            rows.push(json!({"role": role.trim(), "co": company.trim(), "period": date.map_or("", |date| date.as_str()), "d": ""}));
        } else if let Some(row) = rows.last_mut() {
            if let Some(period) = clean.strip_prefix("Period: ") {
                row["period"] = json!(period);
            } else {
                append_line(
                    row,
                    "d",
                    clean.strip_prefix("Description: ").unwrap_or(clean),
                    900,
                );
            }
        }
    }
    rows
}

fn local_projects(lines: &[&str]) -> Vec<Value> {
    let mut rows = Vec::<Value>::new();
    for line in lines {
        let clean = unbullet(line);
        if let Some(row) = rows.last_mut() {
            if let Some((label, value)) = clean.split_once(':') {
                match label.to_ascii_lowercase().as_str() {
                    "stack" | "tech stack" | "built with" => {
                        row["stack"] = json!(value.trim());
                        continue;
                    }
                    "repo" | "live" | "url" => {
                        row["repo"] = json!(value.trim());
                        continue;
                    }
                    "impact" | "description" => {
                        append_line(row, "impact", value.trim(), 1000);
                        continue;
                    }
                    _ => {}
                }
            }
            if clean != *line || clean.split_whitespace().count() > 8 || clean.ends_with('.') {
                append_line(row, "impact", clean, 1000);
                continue;
            }
        }
        if rows.len() >= 8 {
            break;
        }
        let clean = clean.strip_prefix("Project: ").unwrap_or(clean);
        let (title, detail) = [" - ", " – ", " — ", " | ", ": "]
            .into_iter()
            .find_map(|delimiter| clean.split_once(delimiter))
            .unwrap_or((clean, ""));
        rows.push(json!({"title": title.trim(), "impact": detail.trim(), "stack": "", "repo": ""}));
    }
    rows
}

fn append_line(row: &mut Value, field: &str, line: &str, max: usize) {
    let previous = row[field].as_str().unwrap_or_default();
    let separator = if previous.is_empty() { "" } else { "\n" };
    row[field] = json!(
        format!("{previous}{separator}{line}")
            .chars()
            .take(max)
            .collect::<String>()
    );
}
