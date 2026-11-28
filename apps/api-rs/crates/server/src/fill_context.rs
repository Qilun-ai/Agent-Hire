//! Builds the extension's application hand-off from the native profile and lead.

use std::collections::HashSet;
use std::path::Path;

use agent_hire_store::UtcTime;
use serde_json::{Map, Value, json};

/// Shapes a profile snapshot into the fields used by the extension.
pub fn build(lead: &Value, profile: &Map<String, Value>) -> Value {
    let identity = profile.get("identity").unwrap_or(&Value::Null);
    let links: Vec<_> = ["linkedin_url", "github_url", "website_url"]
        .into_iter()
        .map(|key| first(identity, &[key]))
        .filter(|link| !link.is_empty())
        .collect();
    let locations: Vec<_> = ["city"]
        .into_iter()
        .map(|key| first(identity, &[key]))
        .filter(|location| !location.is_empty())
        .collect();
    let mut seen_skills = HashSet::new();
    let skills: Vec<_> = list(profile.get("skills").unwrap_or(&Value::Null))
        .iter()
        .map(|skill| {
            skill
                .as_str()
                .map(str::trim)
                .unwrap_or_else(|| first(skill, &["n", "name"]))
        })
        .filter(|skill| !skill.is_empty() && seen_skills.insert(*skill))
        .collect();
    let projects: Vec<_> = list(profile.get("projects").unwrap_or(&Value::Null))
        .iter()
        .filter(|row| row.is_object())
        .map(|row| {
            json!({
                "title":first(row, &["title", "name"]),
                "bullets":bullets(first(row, &["impact", "description"])),
                "skills":stack(row),
            })
        })
        .collect();
    let experience: Vec<_> = list(profile.get("exp").unwrap_or(&Value::Null))
        .iter()
        .filter(|row| row.is_object())
        .map(|row| {
            json!({
                "title":first(row, &["role", "title"]),
                "company":first(row, &["co", "company"]),
                "dates":first(row, &["period", "dates"]),
                "bullets":bullets(first(row, &["d", "description"])),
            })
        })
        .collect();
    let id = first(lead, &["job_id"]);
    let apply_url = first(&lead["source_meta"], &["apply_url"]);
    let apply_url = if apply_url.is_empty() {
        first(lead, &["url"])
    } else {
        apply_url
    };
    let resume = first(lead, &["resume_asset", "asset"]);
    let (resume, resume_url, resume_paths) = if !resume.is_empty() && Path::new(resume).is_file() {
        (
            resume,
            format!("/api/v1/leads/{id}/pdf"),
            json!({"pdf":resume}),
        )
    } else {
        ("", String::new(), json!({}))
    };
    let score = lead["score"]
        .as_f64()
        .or_else(|| {
            lead["score"]
                .as_str()
                .and_then(|score| score.trim().parse().ok())
        })
        .unwrap_or_default();

    json!({
        "job_id":id,
        "company":first(lead, &["company"]),
        "title":first(lead, &["title"]),
        "apply_url":apply_url,
        "identity":{
            "name":profile.get("n").and_then(Value::as_str).unwrap_or_default(),
            "email":first(identity, &["email"]),
            "phone":first(identity, &["phone"]),
            "links":links,"locations":locations,
            "citizenship":null,"work_authorization":null,"willing_to_relocate":null,
        },
        "anything_else":profile.get("s").and_then(Value::as_str).unwrap_or_default(),
        "skills":skills,"projects":projects,"experience":experience,
        "resume_ref":"","resume_url":resume_url,"resume_path":resume,"resume_paths":resume_paths,
        "ats_score":score,"prepared_at":UtcTime::now().to_iso_micros(),
    })
}

fn first<'a>(row: &'a Value, keys: &[&str]) -> &'a str {
    keys.iter()
        .filter_map(|key| row[*key].as_str())
        .map(str::trim)
        .find(|value| !value.is_empty())
        .unwrap_or_default()
}

fn list(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}

fn bullets(text: &str) -> Vec<&str> {
    text.lines()
        .map(str::trim)
        .map(|line| line.trim_start_matches(['-', '*', '•', ' ']).trim())
        .filter(|line| !line.is_empty())
        .collect()
}

fn stack(row: &Value) -> Vec<&str> {
    for key in ["stack", "skills"] {
        let value = &row[key];
        let skills: Vec<_> = if let Some(text) = value.as_str() {
            text.split(',')
                .map(str::trim)
                .filter(|skill| !skill.is_empty())
                .collect()
        } else {
            list(value)
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|skill| !skill.is_empty())
                .collect()
        };
        if !skills.is_empty() {
            return skills;
        }
    }
    Vec::new()
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::build;

    #[test]
    fn handoff_uses_profile_evidence_and_legacy_field_aliases() {
        let profile = json!({
            "n":"Ada Example","s":"Builds reliable services",
            "identity":{"city":"Austin","country":"US","email":"ada@example.test","github_url":"https://example.test/ada"},
            "skills":[" Rust ","rust",{"name":"SQL"},{"n":"SQL"}],
            "exp":[{"title":"Engineer","company":"Harbor","dates":"2021–2024","description":"- First line.\n* Second line.",
                    "points":[{"text":"Ignore derived points in the hand-off"}]}],
            "projects":[{"name":"Ledger","stack":[],"skills":[" Rust "],"description":"• Reconciled events."}],
        });
        let lead = json!({"job_id":"test","score":42,"url":"https://example.test","source_meta":{"apply_url":"https://example.test/apply"}});
        let context = build(&lead, profile.as_object().expect("profile object"));
        assert_eq!(context["anything_else"], "Builds reliable services");
        assert_eq!(context["identity"]["locations"], json!(["Austin"]));
        assert_eq!(
            context["identity"]["links"],
            json!(["https://example.test/ada"])
        );
        assert_eq!(context["identity"]["citizenship"], json!(null));
        assert_eq!(context["skills"], json!(["Rust", "rust", "SQL"]));
        assert_eq!(
            context["experience"],
            json!([{"title":"Engineer","company":"Harbor","dates":"2021–2024","bullets":["First line.","Second line."]}])
        );
        assert_eq!(
            context["projects"],
            json!([{"title":"Ledger","skills":["Rust"],"bullets":["Reconciled events."]}])
        );
        assert_eq!(context["apply_url"], "https://example.test/apply");
        assert_eq!(context["ats_score"], json!(42.0));
        assert_eq!(context["resume_url"], "");
        assert_eq!(context["resume_paths"], json!({}));
    }
}
