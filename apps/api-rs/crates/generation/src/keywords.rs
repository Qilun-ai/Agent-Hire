//! Deterministic keyword evidence and structured verification (§9).

use std::collections::HashSet;

use serde_json::{Value, json};

use crate::fields;

// §9 supplies the systems terms in full. The main vocabulary is a clean-room subset
// of the technology spellings in the existing Rust ranking taxonomy.
const TERMS: &[(&str, &[&str])] = &[
    ("Python", &["python"]),
    ("TypeScript", &["typescript"]),
    ("JavaScript", &["javascript"]),
    ("C++", &["c++", "cpp"]),
    ("C#", &["c#", "c sharp"]),
    ("Java", &["java"]),
    ("PHP", &["php"]),
    ("Ruby", &["ruby"]),
    ("Go", &["golang", "go lang", "go"]),
    ("Rust", &["rust"]),
    ("React", &["react", "react.js", "reactjs"]),
    ("Next.js", &["next.js", "nextjs", "next js"]),
    ("Vite", &["vite"]),
    ("Tailwind", &["tailwind", "tailwind css"]),
    ("HTML", &["html"]),
    ("CSS", &["css"]),
    ("Vue", &["vue", "vue.js"]),
    ("Angular", &["angular"]),
    ("Svelte", &["svelte"]),
    ("SQL", &["sql"]),
    ("PostgreSQL", &["postgresql", "postgres"]),
    ("MySQL", &["mysql"]),
    ("MongoDB", &["mongodb", "mongo"]),
    ("Redis", &["redis"]),
    ("Prisma", &["prisma"]),
    ("Supabase", &["supabase"]),
    ("Data Pipeline", &["data pipeline", "data pipelines", "etl"]),
    ("Flutter", &["flutter"]),
    ("Swift", &["swift"]),
    ("Kotlin", &["kotlin"]),
    ("Android", &["android"]),
    ("iOS", &["ios"]),
    ("Node.js", &["node.js", "nodejs", "node js"]),
    ("Express", &["express.js", "expressjs", "express"]),
    ("NestJS", &["nestjs", "nest.js"]),
    ("FastAPI", &["fastapi", "fast api"]),
    ("Django", &["django"]),
    ("Flask", &["flask"]),
    ("Laravel", &["laravel"]),
    ("Ruby on Rails", &["ruby on rails", "rails"]),
    (
        "REST API",
        &["rest api", "rest apis", "restful api", "restful"],
    ),
    ("GraphQL", &["graphql"]),
    ("WebSockets", &["websockets", "websocket"]),
    ("Firebase", &["firebase"]),
    ("WordPress", &["wordpress"]),
    ("Qdrant", &["qdrant"]),
    ("Pinecone", &["pinecone"]),
    ("ChromaDB", &["chromadb", "chroma db"]),
    ("Vector DB", &["vector db", "vector database"]),
    ("RAG", &["rag", "retrieval augmented generation"]),
    ("LLM", &["llm", "llms", "large language model"]),
    ("AI Agents", &["ai agents", "ai agent", "agentic"]),
    ("OpenAI", &["openai"]),
    ("Anthropic", &["anthropic", "claude"]),
    ("LangChain", &["langchain"]),
    ("LangGraph", &["langgraph"]),
    ("Machine Learning", &["machine learning", "mlops"]),
    ("NLP", &["nlp", "natural language processing"]),
    ("Computer Vision", &["computer vision"]),
    ("PyTorch", &["pytorch"]),
    ("TensorFlow", &["tensorflow"]),
    ("Groq", &["groq"]),
    ("Automation", &["automation", "n8n"]),
    ("Docker", &["docker"]),
    ("Kubernetes", &["kubernetes", "k8s"]),
    ("Terraform", &["terraform"]),
    ("AWS", &["aws", "amazon web services"]),
    ("GCP", &["gcp", "google cloud"]),
    ("Azure", &["azure"]),
    ("Vercel", &["vercel"]),
    ("CI/CD", &["ci/cd", "continuous integration"]),
    ("Linux", &["linux"]),
    ("LiveKit", &["livekit"]),
    ("Deepgram", &["deepgram"]),
    ("Stripe", &["stripe"]),
    ("Tauri", &["tauri"]),
    ("Electron", &["electron"]),
    ("Playwright", &["playwright"]),
    ("SAP", &["sap"]),
    ("ABAP", &["abap"]),
    ("Salesforce", &["salesforce"]),
    ("ServiceNow", &["servicenow"]),
    ("Kafka", &["kafka"]),
    (
        "Distributed Systems",
        &["distributed systems", "distributed system"],
    ),
    (
        "Event-Driven Architecture",
        &["event-driven", "event driven"],
    ),
    ("Microservices", &["microservices", "microservice"]),
    ("System Design", &["system design"]),
];

pub(crate) fn contains(lower: &str, term: &str) -> bool {
    let word = |character: char| character.is_alphanumeric() || matches!(character, '+' | '#');
    lower.match_indices(term).any(|(start, text)| {
        !lower[..start].chars().next_back().is_some_and(word)
            && !lower[start + text.len()..].chars().next().is_some_and(word)
    })
}

pub(crate) fn jd_text(lead: &Value) -> String {
    ["title", "description", "reason", "match_points", "gaps"]
        .iter()
        .map(|key| fields::compact(&lead[*key]))
        .collect::<Vec<_>>()
        .join("\n")
}

pub(crate) fn candidates(lead: &Value) -> Vec<&'static str> {
    let text = jd_text(lead).to_lowercase();
    TERMS
        .iter()
        .filter_map(|(canonical, aliases)| {
            aliases
                .iter()
                .any(|alias| contains(&text, alias))
                .then_some(*canonical)
        })
        .collect()
}

fn evidence_text(profile: &Value) -> String {
    let mut text = vec![
        fields::compact(&profile["n"]),
        fields::compact(&profile["s"]),
    ];
    for (list, keys) in [
        ("skills", &["n", "name", "cat", "category"][..]),
        (
            "projects",
            &["title", "name", "impact", "description", "stack", "s"][..],
        ),
        ("exp", &["role", "co", "company", "d", "description"][..]),
    ] {
        for row in fields::list(profile, list) {
            text.extend(keys.iter().map(|key| fields::compact(&row[*key])));
        }
    }
    for list in ["certifications", "certs", "education", "achievements"] {
        text.extend(
            fields::list(profile, list)
                .iter()
                .map(|value| fields::label(value).to_owned()),
        );
    }
    text.join("\n").to_lowercase()
}

pub(crate) struct Verified<'a> {
    pub(crate) terms: Vec<&'a str>,
    pub(crate) verification: Value,
}

pub(crate) fn verify<'a>(terms: &[&'a str], draft: &Value) -> Verified<'a> {
    let gather = |key| {
        let mut seen = HashSet::new();
        fields::list(draft, key)
            .iter()
            .filter_map(Value::as_str)
            .filter_map(|name| {
                terms
                    .iter()
                    .copied()
                    .find(|term| term.eq_ignore_ascii_case(name.trim()))
            })
            .filter(|name| seen.insert(*name))
            .collect::<Vec<_>>()
    };
    let mut approved = gather("verified_jd_keywords");
    let rejected = gather("rejected_jd_keywords");
    let status = if approved.is_empty() && rejected.is_empty() {
        approved = terms.to_vec();
        "deterministic_fallback"
    } else {
        "llm_verified"
    };
    let terms = terms
        .iter()
        .copied()
        .filter(|term| approved.contains(term) || !rejected.contains(term))
        .collect();
    Verified {
        terms,
        verification: json!({"status":status,"approved_terms":approved,"rejected_terms":rejected}),
    }
}

pub(crate) fn coverage(
    profile: &Value,
    terms: &[&str],
    resume: &str,
    verification: Option<Value>,
) -> Value {
    let evidence = evidence_text(profile);
    let resume = resume.to_lowercase();
    let mut covered = Vec::new();
    let mut missing = Vec::new();
    let mut incorporated = Vec::new();
    for term in terms {
        let aliases = TERMS
            .iter()
            .find(|(name, _)| name == term)
            .map_or(&[][..], |(_, aliases)| *aliases);
        if aliases.iter().any(|alias| contains(&evidence, alias)) {
            covered.push(*term);
            if contains(&resume, &term.to_lowercase()) {
                incorporated.push(*term);
            }
        } else {
            missing.push(*term);
        }
    }
    let pct = (!terms.is_empty())
        .then(|| (100.0 * covered.len() as f64 / terms.len() as f64).round_ties_even() as i64);
    let mut result = json!({
        "jd_terms":terms[..terms.len().min(24)], "covered_terms":covered[..covered.len().min(18)],
        "missing_terms":missing[..missing.len().min(12)], "incorporated_terms":incorporated[..incorporated.len().min(18)],
        "coverage_pct":pct,
    });
    if let Some(verification) = verification {
        result["verification"] = verification;
    }
    result
}
