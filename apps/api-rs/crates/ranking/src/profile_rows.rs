//! Constructs profile rows from JSON and scopes stored vectors to the current profile.

use std::collections::BTreeSet;

use agent_hire_ml::embedding_text;
use md5::Md5;
use serde_json::Value;

use crate::candidate::{self, CREDENTIALS};
use crate::semantic::VectorKind;
use crate::{domains, json_fields};

/// One profile row ready for embedding and caller-managed storage.
#[derive(Clone, Debug)]
pub struct ProfileText {
    /// Explicit ID when supplied, otherwise the profile's stable twelve-hex ID.
    pub id: String,
    /// The kind of profile entity.
    pub kind: VectorKind,
    /// Display label for ranking evidence.
    pub label: String,
    /// Text to pass to the embedding provider.
    pub text: String,
}

/// Builds the exact profile-vector documents from the current profile.
#[must_use]
pub fn profile_embedding_rows(profile: &Value) -> Vec<ProfileText> {
    rows(profile, RowFormat::Stored)
}

pub(crate) fn local(profile: &Value) -> Vec<ProfileText> {
    rows(profile, RowFormat::Local)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum RowFormat {
    Stored,
    Local,
}

fn rows(profile: &Value, format: RowFormat) -> Vec<ProfileText> {
    let mut rows = Vec::new();
    let mut skill_names = BTreeSet::new();
    let name = json_fields::string(profile, &["n"]);
    let summary = json_fields::string(profile, &["s"]);
    let desired = json_fields::string(profile, &["desired_position"]);
    let domain_lines = if format == RowFormat::Local {
        domains::lines(profile)
    } else {
        Vec::new()
    };
    let has_summary = [name, summary].iter().any(|field| !field.trim().is_empty());
    if has_summary
        || (format == RowFormat::Local && (!desired.trim().is_empty() || !domain_lines.is_empty()))
    {
        let local = format == RowFormat::Local;
        let mut text = embedding_text::profile_text(name, summary);
        if local {
            if !desired.is_empty() {
                text.push_str(&format!("\nDesired position: {desired}"));
            }
            for line in &domain_lines {
                text.push('\n');
                text.push_str(line);
            }
        }
        let label = if local {
            "Profile summary"
        } else if name.is_empty() {
            "Candidate"
        } else {
            name
        };
        rows.push(ProfileText {
            id: if local {
                "profile:local".to_owned()
            } else {
                id(profile, name)
            },
            kind: VectorKind::Profile,
            label: label.to_owned(),
            text,
        });
    }

    for skill in json_fields::items(profile, "skills") {
        let name = candidate::skill_name(skill);
        if name.trim().is_empty() {
            continue;
        }
        skill_names.insert(name.to_lowercase());
        let category = json_fields::string(skill, &["cat", "category"]);
        let category = if category.is_empty() {
            "general"
        } else {
            category
        };
        let mut text = embedding_text::skill_text(name, category);
        if format == RowFormat::Local {
            let sources: Vec<&str> = json_fields::items(skill, "evidence_sources")
                .iter()
                .filter_map(Value::as_str)
                .filter(|source| !source.is_empty())
                .take(3)
                .collect();
            if !sources.is_empty() {
                text.push_str(&format!("\nEvidence: {}", sources.join("; ")));
            }
        }
        rows.push(ProfileText {
            id: id(skill, name),
            kind: VectorKind::Skill,
            label: name.to_owned(),
            text,
        });
    }
    if format == RowFormat::Local {
        // Related-skill edges are available only when the caller supplied them with the profile.
        for skill in json_fields::items(profile, "skills") {
            let name = candidate::skill_name(skill);
            for related in json_fields::items(skill, "related_skills") {
                let label = related
                    .as_str()
                    .unwrap_or_else(|| candidate::skill_name(related));
                if label.trim().is_empty() || !skill_names.insert(label.to_lowercase()) {
                    continue;
                }
                let mut text = embedding_text::skill_text(label, "graph_related");
                text.push_str(&format!("\nEvidence: related to: {name}"));
                rows.push(ProfileText {
                    id: id(related, label),
                    kind: VectorKind::Skill,
                    label: label.to_owned(),
                    text,
                });
            }
        }
    }

    for project in json_fields::items(profile, "projects") {
        let title = json_fields::string(project, &["title", "name"]);
        let stack = json_fields::first(project, &["stack", "s"]);
        let impact = json_fields::string(project, &["impact", "description"]);
        if !title.trim().is_empty() {
            rows.push(ProfileText {
                id: id(project, title),
                label: title.to_owned(),
                kind: VectorKind::Project,
                text: embedding_text::project_text(title, stack, impact),
            });
        }
    }

    for history in json_fields::items(profile, "exp") {
        let role = json_fields::string(history, &["role"]);
        let company = json_fields::string(history, &["co", "company"]);
        let period = json_fields::string(history, &["period"]);
        let description = json_fields::string(history, &["d", "description"]);
        if role.trim().is_empty() && company.trim().is_empty() {
            continue;
        }
        let local = format == RowFormat::Local;
        let label = candidate::experience_label(role, company, if local { " at " } else { " - " });
        let text = if local {
            let stack = embedding_text::stack_text(json_fields::first(history, &["s", "stack"]));
            format!(
                "Experience: {role}\nCompany: {company}\nPeriod: {period}\nStack: {stack}\nDescription: {description}"
            )
        } else {
            embedding_text::experience_text(role, company, period, description)
        };
        rows.push(ProfileText {
            id: id(history, &format!("{role}{company}")),
            label,
            kind: VectorKind::Experience,
            text,
        });
    }

    for key in CREDENTIALS {
        for credential in json_fields::items(profile, key) {
            let title = candidate::credential_name(credential);
            if !title.trim().is_empty() {
                let kind = if format == RowFormat::Local {
                    key
                } else {
                    match json_fields::string(credential, &["kind"]) {
                        "" => match *key {
                            "certs" | "certifications" => "certification",
                            "awards" => "award",
                            "achievements" => "achievement",
                            _ => key,
                        },
                        kind => kind,
                    }
                };
                rows.push(ProfileText {
                    id: digest(title),
                    label: title.to_owned(),
                    kind: VectorKind::Credential,
                    text: embedding_text::credential_text(title, kind),
                });
            }
        }
    }
    rows
}

pub(crate) fn allowed_ids(profile: &Value) -> BTreeSet<(VectorKind, String)> {
    let mut ids = BTreeSet::new();
    for row in rows(profile, RowFormat::Stored) {
        if row.kind != VectorKind::Profile {
            ids.insert((row.kind, row.id));
        }
    }
    for (key, kind) in [
        ("skills", VectorKind::Skill),
        ("projects", VectorKind::Project),
        ("exp", VectorKind::Experience),
    ] {
        for row in json_fields::items(profile, key) {
            if let Some(id) = row["id"].as_str().filter(|id| !id.is_empty()) {
                ids.insert((kind, id.to_owned()));
            }
        }
    }
    ids
}

fn id(value: &Value, seed: &str) -> String {
    value["id"]
        .as_str()
        .filter(|id| !id.is_empty())
        .map_or_else(|| digest(seed), str::to_owned)
}

fn digest(seed: &str) -> String {
    use md5::Digest as _;

    let hex = format!("{:x}", Md5::digest(seed.as_bytes()));
    hex[..12].to_owned()
}
