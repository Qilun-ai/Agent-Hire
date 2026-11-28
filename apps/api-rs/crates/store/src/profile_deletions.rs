//! Tombstones for profile entities shared with the legacy backend.

use std::collections::{BTreeSet, HashSet};

use serde_json::{Map, Value, json};

use crate::profile_text;

#[derive(Clone, Copy)]
pub(super) enum Collection {
    Skills,
    Projects,
    Experience,
    Education,
    Certifications,
    Achievements,
}

impl Collection {
    pub(super) const ALL: [Collection; 6] = [
        Collection::Skills,
        Collection::Projects,
        Collection::Experience,
        Collection::Education,
        Collection::Certifications,
        Collection::Achievements,
    ];

    pub(super) fn key(self) -> &'static str {
        match self {
            Collection::Skills => "skills",
            Collection::Projects => "projects",
            Collection::Experience => "exp",
            Collection::Education => "education",
            Collection::Certifications => "certifications",
            Collection::Achievements => "achievements",
        }
    }

    fn normalize(self, text: &str) -> String {
        match self {
            Collection::Education | Collection::Certifications | Collection::Achievements => {
                profile_text::collapse(text).to_lowercase()
            }
            _ => profile_text::unquote(text)
                .to_lowercase()
                .chars()
                .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '#' | '.' | '-'))
                .collect(),
        }
    }

    fn tokens(self, text: &str) -> impl Iterator<Item = String> {
        let raw = profile_text::trim(text);
        let decoded = profile_text::unquote(raw);
        let decoded = profile_text::trim(&decoded);
        let has_value = !decoded.is_empty();
        [raw, decoded, &profile_text::id(decoded)]
            .map(|value| self.normalize(value))
            .into_iter()
            .filter(move |token| has_value && !token.is_empty())
    }

    pub(super) fn labels(self, row: &Value) -> Vec<String> {
        let field = |name: &str| row[name].as_str().unwrap_or_default();
        let fields = |names: &[&str]| names.iter().map(|name| field(name).to_owned()).collect();
        match self {
            Collection::Skills => fields(&["id", "n", "name", "title"]),
            Collection::Projects => fields(&["id", "title", "name"]),
            Collection::Experience => {
                let role = field("role");
                let company = field("co");
                vec![
                    field("id").to_owned(),
                    role.to_owned(),
                    company.to_owned(),
                    format!("{role}{company}"),
                    format!("{role} at {company}"),
                    format!("{role} - {company}"),
                ]
            }
            _ => {
                let entry = row.as_str().unwrap_or_else(|| field("title"));
                vec![
                    entry.to_owned(),
                    profile_text::id(profile_text::trim(entry)),
                ]
            }
        }
    }
}

pub(super) struct Tombstones {
    sets: [BTreeSet<String>; Collection::ALL.len()],
    changed: bool,
}

impl Tombstones {
    pub(super) fn from_json(input: Value) -> Tombstones {
        Tombstones {
            sets: Collection::ALL.map(|kind| {
                input[kind.key()]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .map(|text| kind.normalize(text))
                    .filter(|token| !token.is_empty())
                    .collect()
            }),
            changed: false,
        }
    }

    pub(super) fn changed(&self) -> bool {
        self.changed
    }

    pub(super) fn to_json(&self) -> Value {
        Value::Object(
            Collection::ALL
                .iter()
                .map(|kind| (kind.key().to_owned(), json!(self.tokens(*kind))))
                .collect(),
        )
    }

    fn tokens(&self, kind: Collection) -> &BTreeSet<String> {
        &self.sets[kind as usize]
    }

    pub(super) fn delete(&mut self, kind: Collection, value: &str, rows: &mut Vec<Value>) {
        let tokens = kind.tokens(value).collect::<BTreeSet<_>>();
        rows.retain(|row| {
            let labels = kind.labels(row);
            let matches = match kind {
                Collection::Skills | Collection::Projects => labels
                    .iter()
                    .flat_map(|label| kind.tokens(label))
                    .any(|token| tokens.contains(&token)),
                Collection::Experience => {
                    labels
                        .iter()
                        .any(|label| tokens.contains(&kind.normalize(label)))
                        || tokens.contains(&profile_text::id(&format!(
                            "{}{}",
                            row["role"].as_str().unwrap_or_default(),
                            row["co"].as_str().unwrap_or_default()
                        )))
                }
                _ => labels
                    .iter()
                    .any(|label| tokens.contains(&kind.normalize(label))),
            };
            if matches && let Some(id) = row["id"].as_str() {
                for token in kind.tokens(id) {
                    self.changed |= self.sets[kind as usize].insert(token);
                }
            }
            !matches
        });
        for token in tokens {
            self.changed |= self.sets[kind as usize].insert(token);
        }
    }

    pub(super) fn forget(&mut self, kind: Collection, labels: &[&str]) {
        self.changed = true;
        for label in labels {
            for token in kind.tokens(label) {
                self.sets[kind as usize].remove(&token);
            }
        }
    }

    pub(super) fn filter(&self, profile: &mut Map<String, Value>) {
        for kind in Collection::ALL {
            let tokens = self.tokens(kind);
            if !tokens.is_empty()
                && let Some(rows) = profile.get_mut(kind.key()).and_then(Value::as_array_mut)
            {
                rows.retain(|row| {
                    kind.labels(row)
                        .iter()
                        .all(|label| !tokens.contains(&kind.normalize(label)))
                });
            }
        }

        let project_skills: HashSet<String> = profile["projects"]
            .as_array()
            .into_iter()
            .flatten()
            .flat_map(|project| profile_text::stack(&project["stack"]))
            .map(|skill| Collection::Skills.normalize(&skill))
            .filter(|skill| !skill.is_empty())
            .collect();
        if let Some(skills) = profile.get_mut("skills").and_then(Value::as_array_mut) {
            skills.retain(|skill| {
                skill["cat"].as_str() != Some("project_stack")
                    || ["n", "name", "title"].iter().any(|key| {
                        skill[key].as_str().is_some_and(|name| {
                            project_skills.contains(&Collection::Skills.normalize(name))
                        })
                    })
            });
        }
    }
}
