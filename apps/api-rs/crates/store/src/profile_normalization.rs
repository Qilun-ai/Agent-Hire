//! Normalization of agent-supplied profiles and JSON Resume payloads.

use std::collections::HashSet;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};
use unicode_normalization::UnicodeNormalization;

use crate::{profile_entries, profile_points, profile_projects, profile_skills, profile_text};

pub(super) const FIELDS: [(&str, &str, usize); 6] = [
    ("skills", "skills", 200),
    ("experience", "exp", usize::MAX),
    ("projects", "projects", 80),
    ("education", "education", 30),
    ("certifications", "certifications", 40),
    ("achievements", "achievements", 40),
];

pub(super) static URL: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)(?:https?://|www\.)[^\s|)]+").expect("valid URL pattern"));

pub(super) struct NormalizedImport {
    pub(super) profile: Map<String, Value>,
    pub(super) report: Value,
}

pub(super) fn normalize(mut payload: Value) -> NormalizedImport {
    json_resume(&mut payload);
    let candidate = payload
        .get("candidate")
        .filter(|v| v.as_object().is_some_and(|object| !object.is_empty()))
        .unwrap_or(&payload);
    let (name, summary) = candidate_fields(candidate);
    let raw = [
        profile_skills::coerce_skills(&payload["skills"]),
        list(first(
            &payload,
            &[
                "experience",
                "work_experience",
                "work",
                "employment",
                "jobs",
                "positions",
            ],
        ))
        .to_vec(),
        list(&payload["projects"]).to_vec(),
        list(&payload["education"]).to_vec(),
        list(first(
            &payload,
            &["certifications", "certs", "certificates"],
        ))
        .to_vec(),
        list(first(&payload, &["achievements", "awards"])).to_vec(),
    ];
    let skills = profile_skills::normalize_skills(&raw[0]);
    let known = skills.iter().map(|skill| key(&text(&skill["n"]))).collect();
    let mut normalized = [
        skills,
        experiences(&raw[1]),
        profile_projects::normalize_projects(&raw[2], &known),
        profile_entries::education(&raw[3]),
        profile_entries::entries(&raw[4], true),
        profile_entries::entries(&raw[5], false),
    ];
    let mut received = Map::new();
    let mut imported = Map::new();
    let mut skipped = Vec::new();
    let mut capped = Vec::new();
    let mut profile = Map::from_iter([
        ("n".to_owned(), json!(name)),
        ("s".to_owned(), json!(summary)),
        ("identity".to_owned(), identity(&payload)),
    ]);
    for (index, (field, storage_key, cap)) in FIELDS.into_iter().enumerate() {
        let original = raw[index].len();
        normalized[index].truncate(cap);
        let kept = normalized[index].len();
        received.insert(field.to_owned(), json!(original));
        imported.insert(field.to_owned(), json!(kept));
        if original > cap {
            capped.push(json!({"field": field, "original": original, "kept": cap}));
        }
        if original > kept {
            let reason = if original > cap && kept == cap {
                format!("over the {cap} cap")
            } else {
                "invalid or duplicate".to_owned()
            };
            skipped.push(json!({"field": field, "count": original - kept, "reason": reason}));
        }
        profile.insert(
            storage_key.to_owned(),
            json!(std::mem::take(&mut normalized[index])),
        );
    }
    NormalizedImport {
        profile,
        report: json!({
            "received": received, "imported": imported, "skipped": skipped, "capped": capped,
        }),
    }
}

fn json_resume(payload: &mut Value) {
    let Some(basics) = payload["basics"].as_object() else {
        return;
    };
    let mut candidate = payload["candidate"]
        .as_object()
        .cloned()
        .unwrap_or_default();
    let mut identity = payload["identity"].as_object().cloned().unwrap_or_default();
    if let Some(name) = basics.get("name") {
        candidate.entry("name").or_insert_with(|| name.clone());
    }
    if let Some(summary) = basics
        .get("summary")
        .filter(|value| !text(value).is_empty())
        .or_else(|| basics.get("label"))
    {
        candidate
            .entry("summary")
            .or_insert_with(|| summary.clone());
    }
    for (from, to) in [
        ("email", "email"),
        ("phone", "phone"),
        ("url", "website_url"),
    ] {
        if let Some(value) = basics.get(from) {
            identity.entry(to).or_insert_with(|| value.clone());
        }
    }
    if let Some(city) = basics
        .get("location")
        .and_then(|location| location.get("city"))
    {
        identity.entry("city").or_insert_with(|| city.clone());
    }
    for profile in list(basics.get("profiles").unwrap_or(&Value::Null)) {
        let network = text(&profile["network"]).to_lowercase();
        let value = text(first(profile, &["url", "username"]));
        let field = if network.contains("linkedin") {
            "linkedin_url"
        } else if network.contains("github") {
            "github_url"
        } else {
            continue;
        };
        if !value.is_empty() {
            identity.entry(field).or_insert_with(|| json!(value));
        }
    }
    payload["candidate"] = Value::Object(candidate);
    payload["identity"] = Value::Object(identity);
}

fn candidate_fields(candidate: &Value) -> (String, String) {
    static NAME_DELIMITER: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\s+[|–—-]\s+").expect("valid name separator"));
    static ROLE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)\b(?:(?:full[ -]stack|software|frontend|backend|ai|ml|data)\s+)?(?:engineer|developer|designer|architect|student|intern)\b").expect("valid role pattern")
    });
    let mut name = text(first(candidate, &["name", "n", "fullName", "full_name"]));
    if name.is_empty() {
        let given = text(first(candidate, &["firstName", "first_name"]));
        let family = text(first(candidate, &["lastName", "last_name"]));
        name = format!("{given} {family}");
    }
    if name
        .get(..5)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("name:"))
    {
        name = name[5..].to_owned();
    }
    let name = inline(&name);
    let name = NAME_DELIMITER.split(&name).next().unwrap_or_default();
    let name = ROLE
        .find(name)
        .map_or(name, |role| &name[..role.start()])
        .trim_matches([' ', '|', '-']);
    let lower = name.to_lowercase();
    let words = name.split_whitespace().count();
    let name = if !name.chars().any(|c| c.is_ascii_alphabetic())
        || words > 5
        || lower.contains('@')
        || lower.contains("http")
        || (words <= 2
            && ["portfolio", "resume", "developer", "engineer", "student"]
                .iter()
                .any(|word| lower.contains(word)))
    {
        String::new()
    } else {
        bounded(name, 160)
    };

    let raw_summary = text(first(
        candidate,
        &["summary", "s", "headline", "label", "bio", "about"],
    ));
    let lines = raw_summary
        .lines()
        .map(inline)
        .filter(|line| {
            let lower = line.to_lowercase();
            !["applying to ", "job url", "url"]
                .iter()
                .any(|prefix| lower.starts_with(prefix))
        })
        .collect::<Vec<_>>();
    let mut summary = profile_text::summary(&lines.join("\n"));
    let lower = summary.to_lowercase();
    if summary.split_whitespace().count() < 4
        && ![
            "engineer",
            "developer",
            "student",
            "designer",
            "analyst",
            "scientist",
            "builder",
            "architect",
        ]
        .iter()
        .any(|word| lower.contains(word))
    {
        summary.clear();
    }
    (name, bounded(&summary, 900))
}

fn contact(value: &Value) -> String {
    match value {
        Value::Object(_) => text(first(
            value,
            &["url", "city", "name", "value", "address", "username"],
        )),
        Value::Array(items) => items.first().map_or_else(String::new, contact),
        _ => text(value),
    }
}

fn identity(payload: &Value) -> Value {
    const ALIASES: &[(&str, &[&str])] = &[
        ("email", &["email", "mail", "e_mail", "emailAddress"]),
        (
            "phone",
            &["phone", "mobile", "tel", "phoneNumber", "phone_number"],
        ),
        (
            "linkedin_url",
            &["linkedin_url", "linkedinUrl", "linkedin", "linkedIn"],
        ),
        ("github_url", &["github_url", "githubUrl", "github"]),
        (
            "website_url",
            &[
                "website_url",
                "websiteUrl",
                "website",
                "url",
                "portfolio",
                "portfolio_url",
                "portfolioUrl",
                "site",
            ],
        ),
        ("city", &["city", "location", "locality", "town"]),
        ("country", &["country"]),
    ];
    let sources = [
        &payload["identity"],
        &payload["contact"],
        &payload["links"],
        &payload["social"],
        payload,
    ];
    let mut out = Map::new();
    for &(field, aliases) in ALIASES {
        let value = payload["identity"].get(field).map(contact).or_else(|| {
            sources
                .iter()
                .flat_map(|source| aliases.iter().filter_map(|alias| source.get(alias)))
                .map(contact)
                .find(|v| !v.is_empty())
        });
        if let Some(value) = value {
            out.insert(field.to_owned(), json!(value));
        }
    }
    Value::Object(out)
}

fn period(item: &Value) -> String {
    let explicit = text(&item["period"]);
    if !explicit.is_empty() {
        return explicit;
    }
    let start = text(first(item, &["startDate", "start_date", "start"]));
    let end = text(first(item, &["endDate", "end_date", "end"]));
    if start.is_empty() {
        return end;
    }
    let end = if end.is_empty() { "Present" } else { &end };
    format!("{start} – {end}")
}

fn experiences(raw: &[Value]) -> Vec<Value> {
    let mut rows = Vec::<Value>::new();
    for item in raw {
        let role = bounded(
            &inline(&text(first(item, &["role", "position", "title"]))),
            200,
        );
        let company = bounded(
            &inline(&text(first(
                item,
                &["company", "co", "employer", "organization", "name"],
            ))),
            200,
        );
        if role.is_empty() && company.is_empty() {
            continue;
        }
        let period = bounded(&period(item), 100);
        let description = text(first(item, &["description", "d", "summary"]));
        let description = if description.is_empty() {
            lines(first(item, &["highlights", "points", "bullets"]))
        } else {
            description
        };
        let description = bounded(&description, 5000);
        let id = profile_text::id(&format!("{role}{company}"));
        let mut row =
            json!({"id": id, "role": role, "co": company, "period": period, "d": description});
        copy_point_fields(item, &mut row);
        let marker = experience_key(&row);
        if marker.0.is_empty() && marker.1.is_empty() {
            continue;
        }
        if let Some(previous) = rows.iter_mut().find(|old| experience_key(old) == marker) {
            merge_description(previous, &row, "d");
            if text(&previous["period"]).is_empty() {
                previous["period"] = json!(period);
            }
        } else {
            rows.push(row);
        }
    }
    rows
}

pub(super) fn copy_point_fields(from: &Value, to: &mut Value) {
    for field in [
        "points",
        "matched_entity_id",
        "exact_matches",
        "similar_pairs",
        "new_points",
        "source_resume_ids",
        "tag_ids",
    ] {
        if let Some(value) = from.get(field) {
            to[field] = value.clone();
        }
    }
}

pub(super) fn experience_key(row: &Value) -> (String, String) {
    (
        parent_key(&text(&row["role"]), false),
        parent_key(&text(first(row, &["co", "company"])), true),
    )
}

pub(super) fn project_key(row: &Value) -> String {
    let title = parent_key(&text(first(row, &["title", "name"])), false);
    if title.is_empty() {
        format!("repo:{}", text(&row["repo"]).to_lowercase())
    } else {
        title
    }
}

fn parent_key(text: &str, company: bool) -> String {
    static TRAILING_DATE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)\s*[-–—|,(\[]*\s*(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+)?(?:19\d{2}|20\d{2}|present|current)\s*[)\]]?\s*$").expect("valid parent date")
    });
    let ascii = text
        .nfkd()
        .filter(char::is_ascii)
        .collect::<String>()
        .to_ascii_lowercase();
    let text = URL.replace_all(&ascii, "");
    let mut text = text.as_ref();
    while let Some(date) = TRAILING_DATE.find(text) {
        text = &text[..date.start()];
    }
    let mut words: Vec<&str> = text
        .split(|c: char| !c.is_ascii_alphanumeric() && !"+#".contains(c))
        .filter(|word| !word.is_empty())
        .collect();
    while company
        && words.len() > 1
        && words.last().is_some_and(|word| {
            [
                "inc",
                "incorporated",
                "corp",
                "corporation",
                "co",
                "company",
                "llc",
                "ltd",
                "limited",
                "plc",
                "gmbh",
                "ag",
                "sa",
            ]
            .contains(word)
        })
    {
        words.pop();
    }
    words.concat()
}

pub(super) fn merge_description(previous: &mut Value, row: &Value, field: &str) {
    let existing = text(&previous[field]);
    let incoming = text(&row[field]);
    if existing.is_empty() {
        previous[field] = json!(incoming);
        return;
    }
    if incoming.is_empty() || existing == incoming {
        return;
    }
    let mut points = profile_points::split(&existing);
    let mut seen: HashSet<String> = points.iter().map(|text| key(text)).collect();
    points.extend(
        profile_points::split(&incoming)
            .into_iter()
            .filter(|text| seen.insert(key(text))),
    );
    previous[field] = json!(points.join("\n"));
}

pub(super) fn union_stack(stack: &mut Vec<String>, incoming: impl IntoIterator<Item = String>) {
    let mut seen: HashSet<String> = stack.iter().map(|name| name.to_lowercase()).collect();
    stack.extend(
        incoming
            .into_iter()
            .filter(|name| seen.insert(name.to_lowercase())),
    );
}

pub(super) fn section_noise(text: &str) -> bool {
    static NOISE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)^\d+[\d,.]*(?:%|x|k)?$|\b(?:show all|view all|open|menu|close|copyright|privacy)\b")
            .expect("valid section noise pattern")
    });
    const HEADINGS: &[&str] = &[
        "about",
        "achievements",
        "certifications",
        "contact",
        "education",
        "experience",
        "featured projects",
        "featured work",
        "portfolio",
        "projects",
        "selected projects",
        "selected work",
        "skills",
        "technical expertise",
        "technical skills",
        "work",
    ];
    let lower = text.trim_matches([' ', ':', '-']).to_lowercase();
    lower.is_empty() || HEADINGS.contains(&lower.as_str()) || NOISE.is_match(&lower)
}

pub(super) fn action(text: &str) -> bool {
    static ACTION: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)^(?:built|created|developed|designed|engineered|implemented|integrated|led|launched|shipped|supports?|automated|optimized|improved|reduced|increased|features?|worked|used|using|maintained|deployed)\b")
            .expect("valid action verb pattern")
    });
    ACTION.is_match(text)
}

pub(super) fn list(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}

pub(super) fn text(value: &Value) -> String {
    profile_text::trim(&profile_text::truthy_string(value)).to_owned()
}

pub(super) fn lines(value: &Value) -> String {
    list(value)
        .iter()
        .map(|value| {
            value
                .as_object()
                .map_or_else(|| text(value), |_| text(&value["text"]))
        })
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

pub(super) fn first<'a>(value: &'a Value, keys: &[&str]) -> &'a Value {
    keys.iter()
        .filter_map(|key| value.get(key))
        .find(|v| match v {
            Value::Null | Value::Bool(false) => false,
            Value::String(s) => !s.is_empty(),
            Value::Array(items) => !items.is_empty(),
            Value::Object(fields) => !fields.is_empty(),
            Value::Number(n) => n.as_f64() != Some(0.0),
            _ => true,
        })
        .unwrap_or(&Value::Null)
}

pub(super) fn bounded(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

pub(super) fn inline(text: &str) -> String {
    profile_points::clean_inline(text)
}

pub(super) fn key(text: &str) -> String {
    text.to_lowercase()
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect()
}
