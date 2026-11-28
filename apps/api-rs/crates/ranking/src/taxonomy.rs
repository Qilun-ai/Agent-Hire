//! Ranking vocabularies and the scorer's ASCII-boundary alias matching.

use std::collections::BTreeSet;
use std::sync::LazyLock;

use regex::RegexSet;

pub(crate) type Terms = BTreeSet<String>;

// Core §1 specifies all canonical labels and categories. The aliases beyond its examples
// are independently chosen common spellings because the spec does not enumerate them.
const TECH: &[(&str, &str, &[&str])] = &[
    ("Python", "language", &["python"]),
    (
        "TypeScript",
        "language",
        &["typescript", "type script", "ts"],
    ),
    (
        "JavaScript",
        "language",
        &["javascript", "java script", "ecmascript"],
    ),
    ("C++", "language", &["c++", "cpp"]),
    ("C#", "language", &["c#", "c sharp", "csharp"]),
    ("Java", "language", &["java"]),
    ("PHP", "language", &["php"]),
    ("Ruby", "language", &["ruby"]),
    ("Go", "language", &["go", "golang", "go lang"]),
    ("Rust", "language", &["rust"]),
    ("React", "frontend", &["react", "react.js", "reactjs"]),
    ("Next.js", "frontend", &["next.js", "nextjs", "next js"]),
    ("Vite", "frontend", &["vite"]),
    (
        "Tailwind",
        "frontend",
        &["tailwind", "tailwind css", "tailwindcss"],
    ),
    ("HTML", "frontend", &["html", "html5"]),
    ("CSS", "frontend", &["css", "css3"]),
    ("Vue", "frontend", &["vue", "vue.js", "vuejs"]),
    ("Angular", "frontend", &["angular", "angularjs"]),
    ("Svelte", "frontend", &["svelte", "sveltekit"]),
    ("SQL", "data", &["sql"]),
    (
        "PostgreSQL",
        "data",
        &["postgresql", "postgres", "neon", "neon db", "neondb"],
    ),
    ("MySQL", "data", &["mysql", "my sql"]),
    ("MongoDB", "data", &["mongodb", "mongo db", "mongo"]),
    ("Redis", "data", &["redis"]),
    ("Prisma", "data", &["prisma", "prisma orm"]),
    ("Drizzle", "data", &["drizzle", "drizzle orm"]),
    ("Supabase", "data", &["supabase"]),
    (
        "Data Pipeline",
        "data",
        &["data pipeline", "data pipelines", "etl", "elt"],
    ),
    ("Flutter", "mobile", &["flutter"]),
    ("Swift", "mobile", &["swift", "swiftui"]),
    ("Kotlin", "mobile", &["kotlin"]),
    ("Android", "mobile", &["android"]),
    ("iOS", "mobile", &["ios"]),
    ("Node.js", "backend", &["node.js", "nodejs", "node js"]),
    (
        "Express",
        "backend",
        &["express", "express.js", "expressjs"],
    ),
    ("NestJS", "backend", &["nestjs", "nest.js", "nest js"]),
    ("FastAPI", "backend", &["fastapi", "fast api"]),
    ("Django", "backend", &["django"]),
    ("Flask", "backend", &["flask"]),
    ("Laravel", "backend", &["laravel"]),
    (
        "Ruby on Rails",
        "backend",
        &["ruby on rails", "rails", "ror"],
    ),
    (
        "REST API",
        "backend",
        &[
            "rest api",
            "rest apis",
            "restful api",
            "restful",
            "rest endpoints",
            "api endpoint",
            "api endpoints",
        ],
    ),
    ("GraphQL", "backend", &["graphql", "graph ql"]),
    (
        "WebSockets",
        "backend",
        &["websockets", "websocket", "web sockets", "web socket"],
    ),
    ("Firebase", "backend", &["firebase"]),
    ("WordPress", "cms", &["wordpress", "word press"]),
    ("Qdrant", "ai", &["qdrant"]),
    ("Pinecone", "ai", &["pinecone"]),
    ("ChromaDB", "ai", &["chromadb", "chroma db"]),
    (
        "Vector DB",
        "ai",
        &[
            "vector db",
            "vector database",
            "vector databases",
            "vector store",
        ],
    ),
    ("RAG", "ai", &["rag", "retrieval augmented generation"]),
    (
        "LLM",
        "ai",
        &[
            "llm",
            "llms",
            "large language model",
            "large language models",
        ],
    ),
    (
        "AI Agents",
        "ai",
        &["ai agents", "ai agent", "agentic ai", "agentic"],
    ),
    ("OpenAI", "ai", &["openai", "open ai", "chatgpt"]),
    ("Anthropic", "ai", &["anthropic", "claude"]),
    ("LangChain", "ai", &["langchain", "lang chain"]),
    ("LangGraph", "ai", &["langgraph", "lang graph"]),
    (
        "Machine Learning",
        "ai",
        &[
            "machine learning",
            "ml engineer",
            "ml ops",
            "mlops",
            "ai/ml",
        ],
    ),
    ("NLP", "ai", &["nlp", "natural language processing"]),
    ("Computer Vision", "ai", &["computer vision"]),
    ("PyTorch", "ai", &["pytorch", "py torch"]),
    ("TensorFlow", "ai", &["tensorflow", "tensor flow"]),
    ("Groq", "ai", &["groq"]),
    (
        "Automation",
        "automation",
        &["automation", "workflow automation", "n8n", "zapier"],
    ),
    ("Docker", "infra", &["docker"]),
    ("Kubernetes", "infra", &["kubernetes", "k8s"]),
    ("Terraform", "infra", &["terraform"]),
    ("AWS", "infra", &["aws", "amazon web services"]),
    (
        "GCP",
        "infra",
        &["gcp", "google cloud", "google cloud platform"],
    ),
    ("Azure", "infra", &["azure", "microsoft azure"]),
    ("Vercel", "infra", &["vercel"]),
    (
        "CI/CD",
        "infra",
        &[
            "ci/cd",
            "ci cd",
            "continuous integration",
            "continuous delivery",
        ],
    ),
    ("Linux", "infra", &["linux"]),
    ("LiveKit", "realtime", &["livekit", "live kit"]),
    ("Deepgram", "realtime", &["deepgram", "deep gram"]),
    ("Stripe", "product", &["stripe"]),
    ("Tauri", "desktop", &["tauri"]),
    ("Electron", "desktop", &["electron", "electron.js"]),
    ("Playwright", "testing", &["playwright"]),
    ("SAP", "enterprise", &["sap", "s/4hana"]),
    ("ABAP", "enterprise", &["abap"]),
    ("Salesforce", "enterprise", &["salesforce", "sales force"]),
    ("ServiceNow", "enterprise", &["servicenow", "service now"]),
];

// The specs give counts and examples for these vocabularies. The remaining aliases
// below are independent approximations.
const ROLES: &[(&str, &[&str])] = &[
    (
        "ai",
        &[
            "ai engineer",
            "llm engineer",
            "rag engineer",
            "chatbot developer",
            "ai developer",
            "ai researcher",
            "machine learning engineer",
            "ml engineer",
            "nlp engineer",
            "computer vision engineer",
            "ai agent developer",
            "generative ai engineer",
            "applied ai engineer",
            "applied scientist",
            "data scientist",
            "prompt engineer",
            "mlops engineer",
            "deep learning engineer",
            "ai specialist",
        ],
    ),
    (
        "backend",
        &[
            "backend",
            "back end",
            "backend developer",
            "backend engineer",
            "server engineer",
            "server side",
            "api developer",
            "api engineer",
            "platform backend",
            "distributed systems",
        ],
    ),
    (
        "frontend",
        &[
            "frontend",
            "front end",
            "react developer",
            "ui engineer",
            "ui developer",
            "web frontend",
            "frontend engineer",
            "frontend developer",
            "client side engineer",
        ],
    ),
    (
        "fullstack",
        &[
            "full stack",
            "fullstack",
            "software engineer",
            "software developer",
            "saas engineer",
            "web developer",
            "application engineer",
        ],
    ),
    (
        "data",
        &[
            "data engineer",
            "etl engineer",
            "data platform engineer",
            "analytics engineer",
        ],
    ),
    (
        "devops",
        &[
            "devops",
            "cloud engineer",
            "sre",
            "site reliability",
            "infrastructure engineer",
            "platform engineer",
        ],
    ),
    (
        "mobile",
        &[
            "mobile developer",
            "flutter developer",
            "ios developer",
            "android developer",
            "mobile engineer",
        ],
    ),
    (
        "desktop",
        &["desktop app", "tauri developer", "electron developer"],
    ),
    (
        "testing",
        &[
            "qa automation",
            "playwright engineer",
            "test automation engineer",
        ],
    ),
];
const DELIVERABLES: &[(&str, &[&str])] = &[
    (
        "ai agent",
        &["ai agent", "ai agents", "agentic", "multi agent"],
    ),
    (
        "rag",
        &[
            "rag",
            "knowledge base",
            "retrieval augmented generation",
            "document q&a",
        ],
    ),
    (
        "chatbot",
        &["chatbot", "chat bot", "conversational assistant"],
    ),
    (
        "voice ai",
        &["voice ai", "voice agent", "voice assistant", "speech bot"],
    ),
    (
        "automation",
        &[
            "automation",
            "automated workflow",
            "workflow automation",
            "robotic process automation",
        ],
    ),
    (
        "dashboard",
        &[
            "dashboard",
            "admin panel",
            "analytics dashboard",
            "reporting portal",
        ],
    ),
    (
        "saas",
        &[
            "saas",
            "multi-tenant",
            "subscription platform",
            "software as a service",
        ],
    ),
    (
        "api",
        &["rest api", "graphql api", "web api", "api endpoints"],
    ),
    (
        "frontend app",
        &["landing page", "frontend app", "website", "web interface"],
    ),
    (
        "full-stack app",
        &["full-stack app", "full stack app", "web application"],
    ),
    (
        "data pipeline",
        &[
            "data pipeline",
            "etl pipeline",
            "data ingestion",
            "stream processing",
        ],
    ),
    (
        "fintech",
        &["fintech", "stripe", "payments platform", "payment gateway"],
    ),
    (
        "desktop app",
        &["desktop app", "desktop application", "electron app"],
    ),
    ("testing", &["playwright", "automated testing", "e2e tests"]),
];
pub(crate) const WRONG_FIELD: &[&str] = &[
    "nurse",
    "accountant",
    "lawyer",
    "teacher",
    "sales representative",
    "customer support agent",
    "graphic designer",
    "construction",
    "welder",
    "security guard",
    "babysitter",
    "registered nurse",
    "doctor",
    "physician",
    "pharmacist",
    "dentist",
    "dental assistant",
    "caregiver",
    "therapist",
    "physiotherapist",
    "veterinarian",
    "medical assistant",
    "laboratory technician",
    "bookkeeper",
    "auditor",
    "tax preparer",
    "financial advisor",
    "mortgage broker",
    "insurance agent",
    "real estate agent",
    "realtor",
    "paralegal",
    "legal secretary",
    "legal counsel",
    "attorney",
    "tutor",
    "lecturer",
    "school counselor",
    "teaching assistant",
    "preschool",
    "retail associate",
    "cashier",
    "store manager",
    "merchandiser",
    "sales associate",
    "cold calling",
    "telemarketing",
    "call center",
    "customer service representative",
    "receptionist",
    "administrative assistant",
    "executive assistant",
    "human resources",
    "recruiter",
    "copywriter",
    "content writer",
    "video editor",
    "photographer",
    "illustrator",
    "interior designer",
    "makeup artist",
    "electrician",
    "plumber",
    "carpenter",
    "mason",
    "roofer",
    "hvac technician",
    "machinist",
    "forklift operator",
    "warehouse associate",
    "delivery driver",
    "truck driver",
    "bus driver",
    "cleaner",
    "janitor",
    "housekeeper",
    "hotel receptionist",
    "chef",
    "cook",
    "bartender",
    "waiter",
];

pub(crate) const RED_FLAGS: &[&str] = &[
    "unpaid",
    "for exposure",
    "equity only",
    "college assignment",
    "homework",
    "no budget",
    "cheap",
    "lowest bidder",
    "free trial",
    "commission only",
    "crypto token",
    "urgent cheap",
    "do not apply",
    "training course",
];
pub(crate) const COMMERCIAL: &[&str] = &[
    "hiring",
    "job opening",
    "open role",
    "role available",
    "apply",
    "we're hiring",
    "we are hiring",
    "is hiring",
    "internship",
    "junior",
    "entry level",
    "new grad",
    "graduate",
];
pub(crate) const ADJACENCY_BLOCKLIST: &[&str] = &[
    "language",
    "frontend",
    "mobile",
    "desktop",
    "cms",
    "enterprise",
];

static TECH_PATTERNS: LazyLock<RegexSet> =
    LazyLock::new(|| compile(TECH.iter().map(|(_, _, aliases)| *aliases)));
static ROLE_PATTERNS: LazyLock<RegexSet> =
    LazyLock::new(|| compile(ROLES.iter().map(|(_, aliases)| *aliases)));
static DELIVERABLE_PATTERNS: LazyLock<RegexSet> =
    LazyLock::new(|| compile(DELIVERABLES.iter().map(|(_, aliases)| *aliases)));

fn compile<'a>(groups: impl Iterator<Item = &'a [&'a str]>) -> RegexSet {
    let patterns = groups.map(|aliases| {
        let aliases: Vec<String> = aliases.iter().map(|alias| alias_pattern(alias)).collect();
        format!("(?:^|[^a-z0-9])(?:{})(?:$|[^a-z0-9])", aliases.join("|"))
    });
    RegexSet::new(patterns)
        .expect("taxonomy patterns consist of escaped aliases and fixed boundaries")
}

fn alias_pattern(alias: &str) -> String {
    let mut pattern = String::new();
    for character in alias.chars() {
        match character {
            ' ' => pattern.push_str(r"[\s_./-]+"),
            '.' => pattern.push_str(r"[.\s_-]?"),
            literal => pattern.push_str(&regex::escape(&literal.to_string())),
        }
    }
    pattern
}

pub(crate) fn tech(text: &str) -> Terms {
    TECH_PATTERNS
        .matches(&text.to_lowercase())
        .iter()
        .map(|index| TECH[index].0.to_owned())
        .collect()
}

pub(crate) fn skill_tech(name: &str) -> Terms {
    let name = name.trim().to_lowercase();
    if let Some(term) = short_skill(&name) {
        return Terms::from([term.to_owned()]);
    }
    tech(&name)
}

fn short_skill(name: &str) -> Option<&'static str> {
    // These are whole skill names from Core §2. In prose, "js" can be part of Next.js
    // or Node.js, so matching the bare alias there would also report JavaScript.
    match name {
        "js" => Some("JavaScript"),
        "ts" => Some("TypeScript"),
        "next" => Some("Next.js"),
        "node" => Some("Node.js"),
        "rest" => Some("REST API"),
        _ => None,
    }
}

pub(crate) fn roles(text: &str) -> Terms {
    ROLE_PATTERNS
        .matches(&text.to_lowercase())
        .iter()
        .map(|index| ROLES[index].0.to_owned())
        .collect()
}

pub(crate) fn deliverables(text: &str) -> Terms {
    DELIVERABLE_PATTERNS
        .matches(&text.to_lowercase())
        .iter()
        .map(|index| DELIVERABLES[index].0.to_owned())
        .collect()
}

pub(crate) fn category(term: &str) -> Option<&'static str> {
    TECH.iter()
        .find(|(name, _, _)| *name == term)
        .map(|(_, category, _)| *category)
}

pub(crate) fn resolves_tech(phrase: &str) -> bool {
    short_skill(phrase).is_some()
        || TECH.iter().any(|(name, _, aliases)| {
            name.eq_ignore_ascii_case(phrase) || aliases.contains(&phrase)
        })
}

pub(crate) fn has(text: &str, term: &str) -> bool {
    text.match_indices(term).any(|(start, word)| {
        let bytes = text.as_bytes();
        (start == 0 || !bytes[start - 1].is_ascii_alphanumeric())
            && bytes
                .get(start + word.len())
                .is_none_or(|byte| !byte.is_ascii_alphanumeric())
    })
}

pub(crate) fn any(text: &str, terms: &[&str]) -> bool {
    terms.iter().any(|term| has(text, term))
}

#[cfg(test)]
mod tests {
    use super::{TECH, tech};

    #[test]
    fn canonical_taxonomy_and_separator_rules() {
        assert_eq!(TECH.len(), 83);
        let terms = tech("Rustacean C++ c sharp next-js golang restful_api PostgreSQL");
        assert_eq!(
            terms.into_iter().collect::<Vec<_>>(),
            ["C#", "C++", "Go", "Next.js", "PostgreSQL", "REST API"]
        );
    }
}
