//! Optional Hunter and Proxycurl enrichment of application packages (§5.2, §10).

use std::cmp::Reverse;
use std::sync::LazyLock;
use std::time::Duration;

use agent_hire_llm::Settings;
use regex::Regex;
use serde_json::{Value, json};
use url::Url;

use crate::fallback;
use crate::{fields, keywords};

const TIMEOUT: Duration = Duration::from_secs(12);
const PRIORITY: &[&str] = &[
    "founder",
    "co-founder",
    "ceo",
    "cto",
    "head of engineering",
    "vp engineering",
    "engineering manager",
    "hiring manager",
    "recruiter",
    "talent",
    "people",
    "hr",
];
const BLOCKED: &[&str] = &[
    "boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "apply.workable.com",
    "wellfound.com",
    "linkedin.com",
    "www.linkedin.com",
    "indeed.com",
    "www.indeed.com",
];

/// Looks up company contacts and prepares text for the primary contact. This function
/// returns a status object for network and configuration failures.
pub async fn lookup_contacts(lead: &Value, profile: &Value, settings: &Settings) -> Value {
    if disabled(settings) {
        return json!({"status":"disabled","contacts":[]});
    }
    let Some(domain) = infer_domain(lead, settings) else {
        return json!({"status":"no_domain","contacts":[],"message":"Could not infer company domain from this job URL."});
    };
    let key = settings.get_or_env("hunter_api_key", "HUNTER_API_KEY", "");
    if key.trim().is_empty() {
        return json!({"status":"missing_hunter_key","domain":domain,"contacts":[],"message":"Add a Hunter.io API key in Settings to find company contacts."});
    }
    let raw = match hunter(&domain, &key).await {
        Ok(raw) => raw,
        Err(error) => {
            return json!({"status":"error","domain":domain,"contacts":[],"message":format!("Hunter lookup failed: {error}")});
        }
    };
    let mut contacts = clean_contacts(&raw);
    let Some(primary) = contacts.first_mut() else {
        return json!({"status":"not_found","domain":domain,"contacts":[],"message":"Hunter did not return usable contacts for this domain."});
    };
    let proxy_key = settings.get_or_env("proxycurl_api_key", "PROXYCURL_API_KEY", "");
    if !proxy_key.trim().is_empty() && fields::string(primary, &["linkedin_url"]).is_empty() {
        let name = fields::string(primary, &["name"]);
        let name = if name.is_empty() {
            manager_name(fields::string(lead, &["description"])).unwrap_or("")
        } else {
            name
        };
        if let Some((first, last)) = name.split_once(' ') {
            match linkedin(&domain, first, last.trim(), &proxy_key).await {
                Ok(url) if !url.is_empty() => primary["linkedin_url"] = json!(url),
                Ok(_) => (),
                Err(error) => primary["linkedin_error"] = json!(error),
            }
        }
    }
    primary["personalized_email"] = json!(email(lead, profile, settings, primary));
    json!({"status":"found","domain":domain,"primary_contact":primary.clone(),"contacts":contacts})
}

pub(crate) fn disabled(settings: &Settings) -> bool {
    matches!(
        settings
            .get("contact_lookup_enabled")
            .trim()
            .to_lowercase()
            .as_str(),
        "0" | "false" | "off" | "no"
    )
}

fn infer_domain(lead: &Value, settings: &Settings) -> Option<String> {
    let meta = &lead["source_meta"];
    [
        settings.get("contact_lookup_domain"),
        settings.get("company_domain_override"),
        fields::string(meta, &["company_domain"]),
        fields::string(meta, &["domain"]),
        fields::string(meta, &["website"]),
        fields::string(lead, &["url"]),
    ]
    .into_iter()
    .find_map(domain)
}

fn domain(value: &str) -> Option<String> {
    let value = value.trim();
    let full = if value.contains("://") {
        value.to_owned()
    } else {
        format!("https://{value}")
    };
    let url = Url::parse(&full).ok()?;
    let host = url.host_str()?.to_lowercase();
    let host = host.strip_prefix("www.").unwrap_or(&host);
    if BLOCKED.contains(&host) || host.is_empty() {
        return None;
    }
    let mut labels = host.rsplitn(3, '.');
    let last = labels.next()?;
    Some(
        labels
            .next()
            .map_or_else(|| last.to_owned(), |prior| format!("{prior}.{last}")),
    )
}

async fn hunter(domain: &str, key: &str) -> Result<Value, String> {
    http()?
        .get("https://api.hunter.io/v2/domain-search")
        .query(&[("domain", domain), ("api_key", key.trim()), ("limit", "10")])
        .timeout(TIMEOUT)
        .send()
        .await
        .map_err(http_error)?
        .error_for_status()
        .map_err(http_error)?
        .json()
        .await
        .map_err(http_error)
}

async fn linkedin(domain: &str, first: &str, last: &str, key: &str) -> Result<String, String> {
    let reply: Value = http()?
        .get("https://nubela.co/proxycurl/api/linkedin/profile/resolve")
        .query(&[
            ("company_domain", domain),
            ("first_name", first),
            ("last_name", last),
        ])
        .bearer_auth(key.trim())
        .timeout(TIMEOUT)
        .send()
        .await
        .map_err(http_error)?
        .error_for_status()
        .map_err(http_error)?
        .json()
        .await
        .map_err(http_error)?;
    Ok(fields::string(&reply, &["url", "linkedin_profile_url", "profile_url"]).to_owned())
}

fn http() -> Result<reqwest::Client, String> {
    // Keep lookup credentials on the requested provider origin.
    // The generation spec names the former product. Both providers use the Agent-Hire name.
    reqwest::Client::builder()
        .user_agent("Agent-Hire/1.0")
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(http_error)
}

fn http_error(error: reqwest::Error) -> String {
    error.without_url().to_string()
}

fn clean_contacts(raw: &Value) -> Vec<Value> {
    let mut contacts = Vec::new();
    for row in fields::list(&raw["data"], "emails") {
        let email = fields::string(row, &["value"]);
        if email.trim().is_empty() {
            continue;
        }
        let first = fields::string(row, &["first_name"]);
        let last = fields::string(row, &["last_name"]);
        let name = format!("{first} {last}");
        let name = fallback::or(name.trim(), fields::string(row, &["name"]));
        let first = fallback::or(first, name.split_whitespace().next().unwrap_or(""));
        let title = fields::string(row, &["position", "title"]);
        let confidence = row["confidence"].as_f64().unwrap_or(0.0);
        let text = format!("{title} {name}").to_lowercase();
        let priority = PRIORITY
            .iter()
            .position(|key| text.contains(key))
            .map_or(0, |n| PRIORITY.len() - n);
        contacts.push((priority, confidence, json!({
            "name":name,"first_name":first,"title":title,"email":email,
            "linkedin_url":fields::string(row,&["linkedin","linkedin_url"]),
            "confidence":row.get("confidence").filter(|value| fields::truthy(value)).cloned().unwrap_or(json!(0)),
            "source":"hunter",
        })));
    }
    contacts.sort_by(|left, right| {
        Reverse(left.0)
            .cmp(&Reverse(right.0))
            .then_with(|| right.1.total_cmp(&left.1))
    });
    contacts
        .into_iter()
        .take(5)
        .map(|(_, _, row)| row)
        .collect()
}

fn manager_name(description: &str) -> Option<&str> {
    static MANAGER: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
        r"(?i:\b(?:(?:hiring manager|recruiter|contact)\s*[:\-]\s*|reports to\s+|reporting to\s+))([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})"
    ).expect("valid hiring manager pattern")
    });
    Some(MANAGER.captures(description)?.get(1)?.as_str())
}

fn email(lead: &Value, profile: &Value, settings: &Settings, contact: &Value) -> String {
    let title = fallback::or(fields::string(lead, &["title"]), "the role");
    let company = fallback::or(fields::string(lead, &["company"]), "your team");
    let first = fallback::or(fields::string(contact, &["first_name"]), "there");
    let candidate = fallback::or(
        fields::string(profile, &["n"]),
        fallback::or(
            settings.get("candidate_name"),
            fallback::or(settings.get("first_name"), "Candidate"),
        ),
    );
    let mut skills = if lead["tech_stack"].is_array() {
        fields::list(lead, "tech_stack")
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
    } else {
        let description = fields::string(lead, &["description"]).to_lowercase();
        [
            "Python",
            "FastAPI",
            "React",
            "TypeScript",
            "AWS",
            "Docker",
            "Kubernetes",
            "LLM",
            "AI",
            "PostgreSQL",
            "Kafka",
            "CI/CD",
        ]
        .into_iter()
        .filter(|skill| keywords::contains(&description, &skill.to_lowercase()))
        .collect()
    };
    let mut seen = std::collections::HashSet::new();
    skills.retain(|skill| !skill.trim().is_empty() && seen.insert(skill.to_lowercase()));
    let skills = skills
        .into_iter()
        .take(4)
        .map(|skill| {
            if skill.eq_ignore_ascii_case("ci/cd") {
                "CI/CD"
            } else {
                skill
            }
        })
        .collect::<Vec<_>>()
        .join(", ");
    let skills = fallback::or(&skills, "the stack and product needs in the role");
    format!(
        "Subject: Quick note on {title}\n\nHi {first},\n\nI saw {company} is hiring for {title}. I generated a tailored application package for the role, and the work maps closely to my background in {skills}.\n\nCould I send over the resume and a short role-fit summary?\n\nBest,\n{candidate}"
    )
}
