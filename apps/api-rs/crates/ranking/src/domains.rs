//! Domain hints for the semantic summary, inferred from profile projects and experience.

use serde_json::Value;

use crate::{json_fields, taxonomy};

// Ranking §13 gives domain names and representative aliases. Remaining aliases are
// independently selected for the same domains.
const DOMAINS: &[(&str, &[&str])] = &[
    (
        "fintech",
        &[
            "payments",
            "banking",
            "fintech",
            "lending",
            "trading",
            "financial services",
        ],
    ),
    (
        "healthcare",
        &["clinical", "healthcare", "patient", "pharmacy", "medical"],
    ),
    (
        "e-commerce",
        &[
            "marketplace",
            "e-commerce",
            "ecommerce",
            "retail",
            "shopping cart",
        ],
    ),
    (
        "edtech",
        &[
            "lms",
            "edtech",
            "e-learning",
            "online learning",
            "classroom",
        ],
    ),
    (
        "saas",
        &[
            "subscription",
            "saas",
            "multi-tenant",
            "software as a service",
        ],
    ),
    (
        "ai/ml",
        &[
            "machine learning",
            "artificial intelligence",
            "llm",
            "nlp",
            "deep learning",
        ],
    ),
    (
        "devtools",
        &[
            "devops",
            "developer tools",
            "continuous integration",
            "build system",
        ],
    ),
    (
        "cybersecurity",
        &[
            "infosec",
            "cybersecurity",
            "security operations",
            "threat detection",
        ],
    ),
    (
        "gaming",
        &["unity", "game development", "unreal engine", "video games"],
    ),
    (
        "social",
        &[
            "messaging",
            "social media",
            "online community",
            "social network",
        ],
    ),
    (
        "media",
        &[
            "streaming",
            "video editing",
            "digital publishing",
            "broadcast",
        ],
    ),
    (
        "logistics",
        &[
            "supply chain",
            "logistics",
            "shipping",
            "inventory",
            "warehousing",
        ],
    ),
];

pub(crate) fn lines(profile: &Value) -> Vec<String> {
    let history = json_fields::items(profile, "exp");
    let projects = json_fields::items(profile, "projects");
    let mut evidence = Vec::with_capacity(history.len() + projects.len());
    for row in history {
        let role = json_fields::string(row, &["role"]);
        let company = json_fields::string(row, &["co", "company"]);
        let description = json_fields::string(row, &["d", "description"]);
        evidence.push(format!("{role} {company} {description}").to_lowercase());
    }
    for row in projects {
        let title = json_fields::string(row, &["title", "name"]);
        let impact = json_fields::string(row, &["impact", "description"]);
        evidence.push(format!("{title} {impact}").to_lowercase());
    }
    let total = evidence.len().max(1) as f64;
    let mut matches = Vec::new();
    for &(domain, aliases) in DOMAINS {
        let count = evidence
            .iter()
            .filter(|item| taxonomy::any(item, aliases))
            .count();
        let confidence = json_fields::rounded((count as f64 * 2.0 / total).min(1.0), 2);
        if confidence >= 0.2 {
            matches.push((domain, confidence));
        }
    }
    matches.sort_by(|left, right| right.1.total_cmp(&left.1));
    matches
        .into_iter()
        .take(5)
        .map(|(domain, confidence)| {
            let formatted = if confidence.fract() == 0.0 {
                format!("{confidence:.1}")
            } else {
                confidence.to_string()
            };
            format!("Domain experience: {domain} (confidence: {formatted})")
        })
        .collect()
}
