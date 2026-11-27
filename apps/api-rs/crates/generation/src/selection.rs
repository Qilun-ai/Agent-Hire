//! Selection validation and profile scoping for generation and suggestions (§2.1, §6.2–6.3).

use std::collections::HashSet;

use md5::Md5;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::{fields, markdown};

/// The optional résumé maker selection in generation requests.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct Selection {
    /// Selection schema version.
    pub version: i64,
    /// Visible section keys in display order.
    pub sections: Vec<String>,
    /// Experience parent ordering.
    pub experience_order: Vec<String>,
    /// Enabled experience parent IDs.
    pub experience_on: Vec<String>,
    /// Project parent ordering.
    pub projects_order: Vec<String>,
    /// Enabled project parent IDs.
    pub projects_on: Vec<String>,
    /// Enabled point IDs in display order.
    pub points_on: Vec<String>,
    /// Enabled skill IDs.
    pub skills_on: Vec<String>,
}

impl Default for Selection {
    fn default() -> Self {
        Self {
            version: 1,
            sections: Vec::new(),
            experience_order: Vec::new(),
            experience_on: Vec::new(),
            projects_order: Vec::new(),
            projects_on: Vec::new(),
            points_on: Vec::new(),
            skills_on: Vec::new(),
        }
    }
}

impl Selection {
    /// Returns the first request field that exceeds its allowed item count.
    #[must_use]
    pub fn invalid_field(&self) -> Option<&'static str> {
        for (field, ids, limit) in [
            ("sections", &self.sections, 16),
            ("experience_order", &self.experience_order, 64),
            ("experience_on", &self.experience_on, 64),
            ("projects_order", &self.projects_order, 64),
            ("projects_on", &self.projects_on, 64),
            ("points_on", &self.points_on, 512),
            ("skills_on", &self.skills_on, 256),
        ] {
            if ids.len() > limit {
                return Some(field);
            }
        }
        None
    }

    /// Whether this selection binds the evidence used in the résumé.
    #[must_use]
    pub fn is_active(&self) -> bool {
        [
            &self.sections,
            &self.experience_order,
            &self.experience_on,
            &self.projects_order,
            &self.projects_on,
            &self.points_on,
            &self.skills_on,
        ]
        .into_iter()
        .any(|field| !field.is_empty())
    }
}

/// Profile scope and repository exclusions supplied by the app.
#[derive(Clone, Copy, Debug, Default)]
pub struct ProfileScope<'a> {
    /// The selected tag. Empty means the entire profile.
    pub tag_id: &'a str,
    /// Skill IDs excluded by the tag query.
    pub excluded_skills: &'a [String],
    /// Project or project-point IDs excluded by the tag query.
    pub excluded_projects: &'a [String],
    /// Experience or experience-point IDs excluded by the tag query.
    pub excluded_experiences: &'a [String],
    /// Conflict group objects with statuses, members and optional picked IDs.
    pub conflicts: &'a [Value],
}

/// A scoped copy of the profile and selection bookkeeping.
#[derive(Clone, Debug)]
pub struct PreparedProfile {
    /// The JSON profile after tags, conflict policies, and selection are applied.
    pub profile: Value,
    /// Whether selected evidence must be rendered verbatim.
    pub verbatim: bool,
    /// Number of stale selection IDs discarded.
    pub stale_ids: usize,
}

#[derive(Clone, Copy)]
enum Group {
    Experience,
    Project,
}

impl Group {
    fn key(self) -> &'static str {
        match self {
            Self::Experience => "exp",
            Self::Project => "projects",
        }
    }
    fn field(self) -> &'static str {
        match self {
            Self::Experience => "d",
            Self::Project => "impact",
        }
    }
    fn aliases(self) -> &'static [&'static str] {
        match self {
            Self::Experience => &["d", "description"],
            Self::Project => &["impact", "description"],
        }
    }
    fn member(self, kind: &str) -> bool {
        match self {
            Self::Experience => matches!(
                kind,
                "exp" | "experience" | "experience_point" | "exp_point"
            ),
            Self::Project => matches!(kind, "project" | "project_point"),
        }
    }
}

/// Applies tag, conflict and explicit selection rules without mutating the master profile.
#[must_use]
pub fn prepare_profile(
    profile: &Value,
    scope: ProfileScope<'_>,
    selection: Option<&Selection>,
) -> PreparedProfile {
    let selection = selection.filter(|value| value.is_active());
    let mut profile = profile.as_object().cloned().unwrap_or_default();
    let mut available_skills = HashSet::new();
    let mut available_points = HashSet::new();
    let mut available_experience = HashSet::new();
    let mut available_projects = HashSet::new();
    let mut skills = rows(&profile, "skills");
    skills.retain_mut(|skill| {
        let name = fields::string(skill, &["n", "name"]);
        if name.is_empty() {
            return false;
        }
        let id = ensure_id(skill, &name.to_lowercase());
        if !scope.tag_id.is_empty()
            && (scope.excluded_skills.contains(&id) || !visible(skill, scope.tag_id))
        {
            return false;
        }
        available_skills.insert(id.clone());
        selection.is_none_or(|selected| selected.skills_on.contains(&id))
    });
    if let Some(selected) = selection {
        order(&mut skills, &selected.skills_on);
    }
    profile.insert("skills".to_owned(), json!(skills));

    let mut experiences = rows(&profile, Group::Experience.key());
    let mut projects = rows(&profile, Group::Project.key());
    experiences.retain_mut(|row| {
        prepare_row(
            row,
            Group::Experience,
            scope.tag_id,
            scope.excluded_experiences,
        )
    });
    projects
        .retain_mut(|row| prepare_row(row, Group::Project, scope.tag_id, scope.excluded_projects));
    let removed_experience =
        conflict_removals(&experiences, &projects, Group::Experience, scope.conflicts);
    let removed_project =
        conflict_removals(&projects, &experiences, Group::Project, scope.conflicts);
    for (group, mut parents, removed, available) in [
        (
            Group::Experience,
            experiences,
            removed_experience,
            &mut available_experience,
        ),
        (
            Group::Project,
            projects,
            removed_project,
            &mut available_projects,
        ),
    ] {
        for row in &mut parents {
            let parent = fields::string(row, &["id"]).to_owned();
            available.insert(parent.clone());
            let mut points = points_of(row);
            points.retain(|p| !removed.contains(fields::string(p, &["id"])));
            available_points.extend(points.iter().map(|p| fields::string(p, &["id"]).to_owned()));
            if let Some(selected) = selection {
                points.retain(|point| {
                    selected
                        .points_on
                        .iter()
                        .any(|id| id == fields::string(point, &["id"]))
                });
                order(&mut points, &selected.points_on);
            }
            set_points(row, group, points);
        }
        if let Some(selected) = selection {
            let (on, arranged) = match group {
                Group::Experience => (&selected.experience_on, &selected.experience_order),
                Group::Project => (&selected.projects_on, &selected.projects_order),
            };
            parents.retain(|row| {
                let id = fields::string(row, &["id"]);
                if on.is_empty() {
                    !fields::list(row, "points").is_empty()
                } else {
                    on.iter().any(|item| item == id)
                }
            });
            order(
                &mut parents,
                if arranged.is_empty() { on } else { arranged },
            );
        }
        profile.insert(group.key().to_owned(), json!(parents));
    }
    let stale_ids = selection.map_or(0, |selection| {
        [
            (&selection.skills_on, &available_skills),
            (&selection.experience_on, &available_experience),
            (&selection.experience_order, &available_experience),
            (&selection.projects_on, &available_projects),
            (&selection.projects_order, &available_projects),
            (&selection.points_on, &available_points),
        ]
        .into_iter()
        .map(|(ids, available)| ids.iter().filter(|id| !available.contains(*id)).count())
        .sum()
    });
    PreparedProfile {
        profile: Value::Object(profile),
        verbatim: selection.is_some(),
        stale_ids,
    }
}

fn prepare_row(row: &mut Value, group: Group, tag: &str, exclusions: &[String]) -> bool {
    let seed = match group {
        Group::Experience => format!(
            "{}{}",
            fields::string(row, &["role"]),
            fields::string(row, &["co", "company"])
        ),
        Group::Project => fields::string(row, &["title", "name"]).to_owned(),
    };
    if seed.is_empty() || !row.is_object() {
        return false;
    }
    let id = ensure_id(row, &seed);
    let original = fields::list(row, "points");
    let mut points: Vec<Value> = original
        .iter()
        .filter(|point| !fields::string(point, &["text"]).is_empty())
        .cloned()
        .collect();
    if points.is_empty() {
        // Legacy blobs have no selectable IDs. Keep each nonempty line as one point.
        points = fields::string(row, group.aliases())
            .lines()
            .map(str::trim)
            .filter(|line| !line.is_empty())
            .map(|line| json!({"text":markdown::bullet(line).unwrap_or(line)}))
            .collect();
    }
    for point in &mut points {
        let text = fields::string(point, &["text"]);
        let seed = format!("{id}|{}", markdown::squash(text).to_lowercase());
        ensure_id(point, &seed);
    }
    if !tag.is_empty() {
        points.retain(|point| {
            visible(point, tag)
                && !exclusions
                    .iter()
                    .any(|excluded| excluded == fields::string(point, &["id"]))
        });
        if points.is_empty() && (exclusions.contains(&id) || !visible(row, tag)) {
            return false;
        }
    }
    set_points(row, group, points);
    true
}

fn visible(row: &Value, tag: &str) -> bool {
    let tags = fields::list(row, "tag_ids");
    tags.is_empty() || tags.iter().any(|value| value.as_str() == Some(tag))
}

fn conflict_removals(
    parents: &[Value],
    other_parents: &[Value],
    group: Group,
    conflicts: &[Value],
) -> HashSet<String> {
    let other_group = match group {
        Group::Experience => Group::Project,
        Group::Project => Group::Experience,
    };
    let other_ids: HashSet<_> = other_parents
        .iter()
        .flat_map(|parent| fields::list(parent, "points"))
        .map(|point| fields::string(point, &["id"]))
        .collect();
    let mut removed = HashSet::new();
    for conflict in conflicts {
        let status = fields::string(conflict, &["status"]);
        if matches!(status, "keep_both" | "dismissed") {
            continue;
        }
        let members = fields::list(conflict, "members");
        if members.iter().any(|member| {
            other_group.member(fields::string(member, &["point_kind"]))
                && other_ids.contains(fields::string(member, &["point_id"]))
        }) {
            continue;
        }
        let members: HashSet<&str> = members
            .iter()
            .filter(|m| group.member(fields::string(m, &["point_kind"])))
            .map(|m| fields::string(m, &["point_id"]))
            .collect();
        let mut surviving = Vec::new();
        for (index, parent) in parents.iter().enumerate() {
            for point in fields::list(parent, "points") {
                let id = fields::string(point, &["id"]);
                if members.contains(id) && !removed.contains(id) {
                    surviving.push((index, id));
                }
            }
        }
        if surviving.len() < 2 || surviving.iter().any(|item| item.0 != surviving[0].0) {
            continue;
        }
        let picked = fields::string(conflict, &["picked_id"]);
        let keep = if status == "picked"
            && group.member(fields::string(conflict, &["picked_kind"]))
            && surviving.iter().any(|(_, id)| *id == picked)
        {
            picked
        } else {
            surviving[0].1
        };
        removed.extend(
            surviving
                .into_iter()
                .filter(|(_, id)| *id != keep)
                .map(|(_, id)| id.to_owned()),
        );
    }
    removed
}

fn set_points(row: &mut Value, group: Group, points: Vec<Value>) {
    let text: Vec<&str> = points
        .iter()
        .map(|point| fields::string(point, &["text"]))
        .collect();
    let text = text.join("\n");
    row[group.field()] = json!(text);
    if row.get("description").is_some() {
        row["description"] = json!(text);
    }
    row["points"] = Value::Array(points);
}

fn rows(profile: &Map<String, Value>, key: &str) -> Vec<Value> {
    profile
        .get(key)
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

fn points_of(row: &Value) -> Vec<Value> {
    fields::list(row, "points").to_vec()
}

fn ensure_id(row: &mut Value, seed: &str) -> String {
    use md5::Digest as _;

    let stored = fields::string(row, &["id"]);
    if !stored.is_empty() {
        return stored.to_owned();
    }
    let id = format!("{:x}", Md5::digest(seed.as_bytes()))[..12].to_owned();
    if let Some(row) = row.as_object_mut() {
        row.insert("id".to_owned(), json!(id));
    }
    id
}

fn order(rows: &mut [Value], order: &[String]) {
    rows.sort_by_key(|row| {
        let id = fields::string(row, &["id"]);
        order
            .iter()
            .position(|value| value == id)
            .unwrap_or(usize::MAX)
    });
}
