//! Normalizing project titles, repositories, technology stacks and impact text.

use std::collections::HashSet;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Value, json};
use url::Url;

use crate::profile_entries;
use crate::profile_normalization as normalize;
use crate::profile_skills;
use crate::profile_text;

pub(super) fn normalize_projects(raw: &[Value], known: &HashSet<String>) -> Vec<Value> {
    static NUMBERED: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^\d+[.)]\s*").expect("valid project numbering"));
    static ANNOTATION: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)\s*\([^)]*(?:github|gitlab|bitbucket|gitea|repo|source code|live|demo|website|www\.|https?://|\.[a-z]{2,})[^)]*\)\s*$").expect("valid project annotation")
    });
    static PREFIX: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)^(?:featured|selected)\s+(?:projects?|work|case study)\b[: -]*")
            .expect("valid project prefix")
    });
    static TRAILING: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)\b(?:github|repo(?:sitory)?|live|demo|source code|certificate link):?\s*$")
            .expect("valid project suffix")
    });
    let mut rows = Vec::<Value>::new();
    for item in raw {
        let title = normalize::text(normalize::first(item, &["title", "name", "n"]));
        let impact = normalize::text(normalize::first(item, &["impact", "description"]));
        let repo = normalize::text(normalize::first(item, &["repo", "url"]));
        let repo = if repo.is_empty() {
            normalize::URL
                .find(&title)
                .or_else(|| normalize::URL.find(&impact))
                .map_or("", |url| url.as_str())
        } else {
            normalize::URL
                .find(&repo)
                .map_or(repo.as_str(), |url| url.as_str())
        };
        let repo = repo.trim_end_matches(['.', ',', ';', ')']);
        let repo = if repo.starts_with("www.") {
            format!("https://{repo}")
        } else {
            normalize::inline(repo)
        };
        let title = normalize::inline(&title);
        let title = NUMBERED.replace(&title, "");
        let title = ANNOTATION.replace(&title, "");
        let title = normalize::URL.replace_all(&title, "");
        let title = TRAILING.replace(&title, "");
        let title = PREFIX.replace(&title, "");
        let mut title = title
            .trim_matches([' ', ':', '-', '|', '.', ','])
            .to_owned();
        let mut impact = impact
            .replace("\r\n", "\n")
            .replace('\r', "\n")
            .lines()
            .map(|line| {
                let line = normalize::inline(line);
                let line = normalize::URL.replace_all(&line, "");
                let line = TRAILING.replace(&line, "");
                line.trim_matches([' ', ':', '-', '|']).to_owned()
            })
            .filter(|line| !line.is_empty())
            .collect::<Vec<_>>()
            .join("\n");
        let mut stack = Vec::new();
        for field in [
            "stack",
            "s",
            "skills",
            "tech",
            "technologies",
            "tools",
            "tech_stack",
            "keywords",
        ] {
            let raw = &item[field];
            let parts: Vec<String> = if raw.is_array() {
                normalize::list(raw).iter().map(normalize::text).collect()
            } else {
                normalize::text(raw)
                    .split([',', ';', '|', '/'])
                    .map(str::to_owned)
                    .collect()
            };
            for part in parts {
                normalize::union_stack(&mut stack, profile_skills::skill_parts(&part));
            }
        }
        let impact_skills = profile_skills::skill_parts(&impact);
        if !impact_skills.is_empty()
            && impact_skills.iter().all(|s| {
                profile_skills::canonical_skill(s).is_some() || known.contains(&normalize::key(s))
            })
        {
            normalize::union_stack(&mut stack, impact_skills);
            impact.clear();
        }
        let substance =
            !repo.is_empty() || stack.len() >= 2 || impact.split_whitespace().count() >= 8;
        if !substance && (normalize::action(&title) || title.starts_with(['-', '*', '•'])) {
            if let Some(previous) = rows.last_mut() {
                let detail = if impact.is_empty() { &title } else { &impact };
                normalize::merge_description(previous, &json!({"impact": detail}), "impact");
                let mut merged = profile_text::stack(&previous["stack"]);
                normalize::union_stack(&mut merged, stack);
                previous["stack"] = json!(merged);
            }
            continue;
        }
        if !valid_title(&title, known) {
            title = repo_title(&repo)
                .filter(|name| valid_title(name, known))
                .unwrap_or_default();
            if title.is_empty() && substance {
                title = impact
                    .split(['.', '\n', ';', ':', '|'])
                    .next()
                    .unwrap_or_default()
                    .split_whitespace()
                    .take(8)
                    .collect::<Vec<_>>()
                    .join(" ");
            }
        }
        if title.is_empty()
            || (impact.is_empty() && repo.is_empty() && stack.is_empty() && !projectish(&title))
        {
            continue;
        }
        let title = normalize::bounded(&title, 200);
        let stack = profile_text::stack(&json!(normalize::bounded(&stack.join(", "), 500)));
        let mut row = json!({
            "id": profile_text::id(&title), "title": title, "stack": stack,
            "repo": normalize::bounded(&repo, 500), "impact": normalize::bounded(&impact, 1000),
        });
        normalize::copy_point_fields(item, &mut row);
        let marker = normalize::project_key(&row);
        if let Some(previous) = rows
            .iter_mut()
            .find(|old| normalize::project_key(old) == marker)
        {
            normalize::merge_description(previous, &row, "impact");
            if normalize::text(&previous["repo"]).is_empty() {
                previous["repo"] = row["repo"].clone();
            }
            let mut stack = profile_text::stack(&previous["stack"]);
            normalize::union_stack(&mut stack, profile_text::stack(&row["stack"]));
            previous["stack"] = json!(stack);
        } else {
            rows.push(row);
        }
    }
    rows
}

fn valid_title(title: &str, known: &HashSet<String>) -> bool {
    !title.is_empty()
        && title.chars().count() <= 120
        && title.split_whitespace().count() <= 10
        && !title.contains('@')
        && !normalize::URL.is_match(title)
        && !normalize::section_noise(title)
        && !profile_entries::education_detail(title)
        && !normalize::action(title)
        && !known.contains(&normalize::key(title))
        && profile_skills::canonical_skill(title).is_none()
        && !matches!(
            title.to_lowercase().as_str(),
            "project" | "selected work" | "highlights" | "summary" | "source code" | "live demo"
        )
}

fn repo_title(repo: &str) -> Option<String> {
    let url = Url::parse(repo).ok()?;
    if !url.host_str()?.contains("github.com") {
        return None;
    }
    let name = profile_text::unquote(url.path_segments()?.nth(1)?);
    let name = if name.to_lowercase().ends_with(".git") {
        &name[..name.len() - 4]
    } else {
        &name
    };
    Some(normalize::inline(&name.replace(['-', '_'], " ")))
}

fn projectish(title: &str) -> bool {
    let lower = title.to_lowercase();
    [
        "project",
        "system",
        "platform",
        "app",
        "service",
        "engine",
        "tool",
        "monitor",
        "dashboard",
        "gateway",
        "website",
        "bot",
        "pipeline",
        "library",
        "simulator",
    ]
    .iter()
    .any(|word| lower.contains(word))
}
