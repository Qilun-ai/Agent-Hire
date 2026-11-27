//! Skill extraction against the curated 50-category vocabulary (exact phrase lookup over
//! token n-grams, so "distributed systems", "c++", "node.js" and "ci/cd" survive intact).

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

const VOCABULARY: &str = include_str!("../data/skill_vocabulary.json");
const MAX_NGRAM: usize = 4;
const MAX_SKILLS_PER_JOB: usize = 60;

/// Tokens keep the characters of technical names: c++, c#, .net, node.js, ci/cd, ml-ops.
static TOKEN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"[a-z0-9][a-z0-9+#./-]*[a-z0-9+#]|[a-z0-9]").expect("valid token pattern")
});

/// Real skills that are far too generic to count as evidence of a match.
const STOP: &[&str] = &[
    "communication",
    "teamwork",
    "english",
    "design",
    "testing",
    "analysis",
    "research",
    "development",
    "management",
    "planning",
    "documentation",
    "support",
    "operations",
    "leadership",
    "training",
    "reporting",
    "sales",
    "writing",
    "go",
    "r",
    "c",
    "excel",
    "microsoft office",
    "computer skills",
];

/// Unmistakably software-engineering terms, used to judge ambiguous titles.
const CORE_SOFTWARE: &[&str] = &[
    "python",
    "java",
    "javascript",
    "typescript",
    "c++",
    "c#",
    "golang",
    "rust",
    "ruby",
    "kotlin",
    "swift",
    "scala",
    "php",
    "react",
    "angular",
    "vue",
    "node.js",
    "django",
    "flask",
    "fastapi",
    "spring",
    "spring boot",
    ".net",
    "kubernetes",
    "docker",
    "microservices",
    "rest api",
    "rest apis",
    "graphql",
    "git",
    "ci/cd",
    "distributed systems",
    "data structures",
    "algorithms",
    "object-oriented programming",
    "unit testing",
    "software development",
    "software engineering",
    "backend development",
    "frontend development",
    "full stack development",
    "machine learning",
    "deep learning",
    "pytorch",
    "tensorflow",
    "kafka",
    "apache spark",
    "spark",
    "terraform",
    "devops",
    "aws",
    "azure",
    "gcp",
    "linux",
    "system design",
    "api development",
    "web development",
    "mobile development",
    "android",
    "ios",
    "html",
    "css",
    "selenium",
    "test automation",
    "embedded c",
    "postgresql",
    "mongodb",
    "redis",
];

/// Lower-cased technical tokens of `text`.
pub fn tokens(text: &str) -> Vec<String> {
    let lowered = text.to_lowercase();
    TOKEN
        .find_iter(&lowered)
        .map(|m| m.as_str().to_owned())
        .collect()
}

/// A skill phrase as compared: its tokens joined by single spaces.
pub fn normalize_skill(skill: &str) -> String {
    tokens(skill).join(" ")
}

/// `{category: {normalized skill}}`, plus `certs` and `langs`.
static VOCAB: LazyLock<HashMap<String, HashSet<String>>> = LazyLock::new(|| {
    let parsed: Value = serde_json::from_str(VOCABULARY).unwrap_or(Value::Null);
    let mut out: HashMap<String, HashSet<String>> = HashMap::new();
    let collect = |items: &Value| -> HashSet<String> {
        items
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(normalize_skill)
            .filter(|item| !item.is_empty() && !STOP.contains(&item.as_str()))
            .collect()
    };
    if let Some(categories) = parsed["skills"].as_object() {
        for (category, items) in categories {
            out.insert(category.clone(), collect(items));
        }
    }
    for name in ["certs", "langs"] {
        out.insert(name.to_owned(), collect(&parsed[name]));
    }
    out
});

static ALL_SKILLS: LazyLock<HashSet<String>> =
    LazyLock::new(|| VOCAB.values().flatten().cloned().collect());

static SOFTWARE: LazyLock<HashSet<String>> = LazyLock::new(|| {
    CORE_SOFTWARE
        .iter()
        .map(|term| normalize_skill(term))
        .collect()
});

/// The core software-engineering skills.
pub fn software_skills() -> &'static HashSet<String> {
    &SOFTWARE
}

/// Skills mentioned in `text`, longest phrase first per position, in order of first appearance.
pub fn extract_skills(text: &str) -> Vec<String> {
    let toks = tokens(text);
    // In insertion order (longer phrases first), so ties on position keep that order, as a
    // stable sort over an insertion-ordered dict does.
    let mut seen: Vec<(String, usize)> = Vec::new();
    let mut seen_set: HashSet<String> = HashSet::new();
    let mut covered: HashSet<usize> = HashSet::new();
    for size in (1..=MAX_NGRAM).rev() {
        if toks.len() < size {
            continue;
        }
        for start in 0..=toks.len() - size {
            if size == 1 && covered.contains(&start) {
                continue;
            }
            let phrase = toks[start..start + size].join(" ");
            if ALL_SKILLS.contains(&phrase) && !seen_set.contains(&phrase) {
                seen_set.insert(phrase.clone());
                seen.push((phrase, start));
                covered.extend(start..start + size);
            }
        }
    }
    let mut ordered = seen;
    ordered.sort_by_key(|entry| entry.1);
    ordered
        .into_iter()
        .take(MAX_SKILLS_PER_JOB)
        .map(|(skill, _)| skill)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_known_phrases() {
        assert_eq!(tokens("Node.js, C++ & CI/CD!"), ["node.js", "c++", "ci/cd"]);
        let found = extract_skills("Build services in Go with Kafka and PostgreSQL on Kubernetes.");
        assert!(found.contains(&"kafka".to_owned()), "{found:?}");
        assert!(found.contains(&"postgresql".to_owned()), "{found:?}");
        assert!(
            !found.contains(&"go".to_owned()),
            "generic words are stopped: {found:?}"
        );
    }
}
