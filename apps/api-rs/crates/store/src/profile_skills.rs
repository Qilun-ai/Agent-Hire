//! Skill list coercion, token splitting and canonical names.

use std::collections::HashSet;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Value, json};

use crate::profile_entries;
use crate::profile_normalization as normalize;
use crate::profile_text;

pub(super) fn coerce_skills(raw: &Value) -> Vec<Value> {
    if let Some(groups) = raw.as_object() {
        let mut out = Vec::new();
        for (group, value) in groups {
            for item in value
                .as_array()
                .map_or_else(|| std::slice::from_ref(value), Vec::as_slice)
            {
                if let Some(map) = item.as_object() {
                    let mut map = map.clone();
                    map.entry("category").or_insert_with(|| json!(group));
                    out.push(Value::Object(map));
                } else if !normalize::text(item).is_empty() {
                    out.push(json!({"name": normalize::text(item), "category": group}));
                }
            }
        }
        return out;
    }
    let mut out = Vec::new();
    for item in normalize::list(raw) {
        let words = normalize::list(normalize::first(
            item,
            &["keywords", "technologies", "items"],
        ));
        if words.is_empty() {
            out.push(item.clone());
        } else {
            let category = normalize::text(normalize::first(item, &["name", "category", "group"]));
            for word in words
                .iter()
                .map(normalize::text)
                .filter(|word| !word.is_empty())
            {
                out.push(json!({"name": word, "category": category}));
            }
        }
    }
    out
}

pub(super) fn normalize_skills(raw: &[Value]) -> Vec<Value> {
    let mut seen = HashSet::new();
    let mut skills = Vec::new();
    for item in raw {
        let name = if item.is_string() {
            normalize::text(item)
        } else {
            normalize::text(normalize::first(
                item,
                &["name", "n", "skill", "title", "label"],
            ))
        };
        let category = item
            .get("category")
            .or_else(|| item.get("cat"))
            .map(normalize::text)
            .unwrap_or_default();
        let category = normalize::inline(&category);
        let category = if category.is_empty() {
            "general"
        } else {
            &category
        };
        for name in skill_parts(&name) {
            if seen.insert(normalize::key(&name)) {
                skills.push(
                    json!({"id": profile_text::id(&name), "n": name, "cat": normalize::bounded(category, 80)}),
                );
            }
        }
    }
    skills
}

pub(super) fn skill_parts(input: &str) -> Vec<String> {
    static LABEL: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^[A-Za-z /&+-]{2,35}:\s*").expect("valid skill label"));
    let input = LABEL.replace(input, "");
    if normalize::action(&input) && input.split_whitespace().count() > 5 {
        return Vec::new();
    }
    let input = input.replace(['•', '|', ';'], ",");
    let mut depth = 0_usize;
    let pieces = input.split(|c| match c {
        '(' | '[' => {
            depth += 1;
            false
        }
        ')' | ']' => {
            depth = depth.saturating_sub(1);
            false
        }
        ',' | '\n' => depth == 0,
        _ => false,
    });
    let mut parts = Vec::new();
    for piece in pieces {
        let acronyms = piece.split('/').collect::<Vec<_>>();
        let acronym_pair = acronyms.windows(2).all(|pair| {
            let before = pair[0].split_whitespace().last().unwrap_or_default();
            let after = pair[1].split_whitespace().next().unwrap_or_default();
            [before, after].into_iter().all(|token| {
                !token.is_empty()
                    && token.len() <= 4
                    && token.chars().all(|c| c.is_ascii_uppercase())
            })
        });
        if piece.contains('(') || acronym_pair {
            parts.push(clean_skill(piece));
        } else {
            parts.extend(acronyms.into_iter().map(clean_skill));
        }
    }
    let single = parts.len() == 1;
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for part in parts {
        let hits = skill_hits(&part);
        if hits.len() >= 2 && (single || part.split_whitespace().count() > 2) {
            out.extend(
                hits.into_iter()
                    .filter(|name| seen.insert(name.to_lowercase()))
                    .map(str::to_owned),
            );
        } else if valid_skill(&part, !hits.is_empty()) && seen.insert(part.to_lowercase()) {
            out.push(part);
        }
    }
    out
}

fn clean_skill(part: &str) -> String {
    let cleaned = normalize::inline(part);
    let mut name = cleaned
        .trim_matches(|c: char| !(c.is_alphanumeric() || "_+#.".contains(c)))
        .to_owned();
    if name.matches('(').count() > name.matches(')').count() {
        name.push(')');
    }
    canonical_skill(&name).map_or(name, str::to_owned)
}

fn skill_hits(name: &str) -> Vec<&'static str> {
    static ALIASES: LazyLock<Vec<(&str, &str, String)>> = LazyLock::new(|| {
        let mut aliases = SKILL_ALIASES
            .iter()
            .map(|&(alias, name)| (alias, name, normalize::key(alias)))
            .filter(|(_, _, key)| key.len() >= 2)
            .collect::<Vec<_>>();
        aliases.sort_by_key(|(alias, _, _)| std::cmp::Reverse(alias.len()));
        aliases
    });
    let lower = name.to_lowercase();
    let compact = normalize::key(&lower);
    let word = |c: char| c.is_ascii_alphanumeric() || matches!(c, '+' | '#' | '.' | '-');
    let mut seen = HashSet::new();
    ALIASES
        .iter()
        .filter_map(|(alias, canonical, key)| {
            let matches = key.len() > 2 && compact.contains(key)
                || lower.match_indices(alias).any(|(offset, _)| {
                    !lower[..offset].ends_with(word)
                        && !lower[offset + alias.len()..].starts_with(word)
                });
            (matches && seen.insert(*canonical)).then_some(*canonical)
        })
        .collect()
}

const SKILL_ALIASES: &[(&str, &str)] = &[
    ("go", "Go"),
    ("javascript", "JavaScript"),
    ("js", "JavaScript"),
    ("typescript", "TypeScript"),
    ("ts", "TypeScript"),
    ("postgresql", "PostgreSQL"),
    ("postgres", "PostgreSQL"),
    ("node.js", "Node.js"),
    ("nodejs", "Node.js"),
    ("node", "Node.js"),
    ("react", "React"),
    ("react.js", "React"),
    ("reactjs", "React"),
    ("kubernetes", "Kubernetes"),
    ("k8s", "Kubernetes"),
    ("google cloud platform", "GCP"),
    ("gcp", "GCP"),
    ("amazon web services", "AWS"),
    ("aws", "AWS"),
    ("kafka", "Kafka"),
    ("python", "Python"),
    ("rust", "Rust"),
    ("java", "Java"),
    ("c++", "C++"),
    ("sql", "SQL"),
    ("nosql", "NoSQL"),
    ("php", "PHP"),
    ("docker", "Docker"),
    ("fastapi", "FastAPI"),
    ("django", "Django"),
    ("git", "Git"),
    ("github", "GitHub"),
    ("linux", "Linux"),
    ("redis", "Redis"),
    ("mongodb", "MongoDB"),
    ("mysql", "MySQL"),
    ("sqlite", "SQLite"),
    ("azure", "Azure"),
    ("terraform", "Terraform"),
    ("langchain", "LangChain"),
    ("pytorch", "PyTorch"),
    ("playwright", "Playwright"),
    ("rag", "RAG"),
    ("html", "HTML"),
    ("css", "CSS"),
    ("ci/cd", "CI/CD"),
    ("tcp/ip", "TCP/IP"),
    ("ui/ux", "UI/UX"),
    ("next.js", "Next.js"),
    ("nextjs", "Next.js"),
    ("pandas", "Pandas"),
    ("numpy", "NumPy"),
    ("tensorflow", "TensorFlow"),
    ("vite", "Vite"),
    ("pydantic", "Pydantic"),
    ("flask", "Flask"),
];

pub(super) fn canonical_skill(name: &str) -> Option<&'static str> {
    SKILL_ALIASES
        .iter()
        .find(|(alias, _)| alias.eq_ignore_ascii_case(name))
        .map(|(_, name)| *name)
}

fn valid_skill(name: &str, has_hits: bool) -> bool {
    static REPO_METADATA: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)^\d+(?:\.\d+)?\s+(?:forks?|stars?|watchers?|issues?|prs?|pull\s+requests?|commits?|branches?|repos?)$|\b(?:maintained|updated|pushed|created)\s+(?:through|until|on|at)\s+(?:19|20)\d{2}(?:-\d{2}){0,2}\b|\b(?:last\s+pushed|pushed\s+at|updated\s+at|created\s+at|live\s+preview|deployed\s+live|accessible\s+via|fully\s+client-side)\b")
            .expect("valid repository metadata pattern")
    });
    const GENERIC: &[&str] = &[
        "copy",
        "fork",
        "forks",
        "maintain",
        "maintained",
        "preview",
        "send",
        "sent",
        "star",
        "stars",
        "updated",
    ];
    let lower = name.to_lowercase();
    let words = name.split_whitespace().count();
    let key = normalize::key(name);
    let unsupported_name = ((words == 1 && lower == name) || (words > 2 && has_hits))
        && canonical_skill(name).is_none()
        && !(key.len() >= 2
            && SKILL_ALIASES.iter().any(|(alias, canonical)| {
                key == normalize::key(alias) || key == normalize::key(canonical)
            }));
    words > 0
        && words <= 5
        && name.chars().count() <= 60
        && !name.contains('@')
        && !lower.contains("http")
        && !normalize::section_noise(name)
        && !normalize::action(name)
        && !profile_entries::education_detail(name)
        && !REPO_METADATA.is_match(name)
        && !unsupported_name
        && !GENERIC.contains(&lower.as_str())
}
