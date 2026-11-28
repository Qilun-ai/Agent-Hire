//! Merging an agent import or a parsed résumé into the canonical snapshot.

use std::collections::HashSet;

use rusqlite::Connection;
use serde_json::{Map, Value, json};

use crate::database::StoreError;
use crate::profile::ProfileStore;
use crate::profile_deletions::{Collection, Tombstones};
use crate::profile_normalization as normalize;
use crate::{profile_points, profile_text, settings};

/// No graph or vector rebuild is needed for a SQLite snapshot.
fn success(report: Value) -> Value {
    let mut stats = report["imported"].clone();
    let vector_sync = json!({"status": "skipped", "synced": 0, "deleted_bad_rows": 0});
    stats["vector_sync"] = vector_sync.clone();
    stats["graph_sync"] = json!({
        "status": "skipped",
        "relationships": {"status": "skipped", "linked": 0},
        "vectors": vector_sync,
    });
    let mut clauses = Vec::new();
    let labels = [
        ("skill", "skills"),
        ("role", "roles"),
        ("project", "projects"),
        ("education entry", "education entries"),
        ("certification", "certifications"),
        ("achievement", "achievements"),
    ];
    for ((field, _, _), (singular, plural)) in normalize::FIELDS.into_iter().zip(labels) {
        let count = stats[field].as_u64().unwrap_or_default();
        if count > 0 {
            let label = if count == 1 { singular } else { plural };
            clauses.push(format!("{count} {label}"));
        }
    }
    let mut summary = if clauses.is_empty() {
        "Nothing was imported".to_owned()
    } else {
        format!("Imported {}", clauses.join(", "))
    };
    let skipped: u64 = normalize::list(&report["skipped"])
        .iter()
        .filter_map(|item| item["count"].as_u64())
        .sum();
    let mut tail = Vec::new();
    if skipped > 0 {
        tail.push(format!("skipped {skipped}"));
    }
    for item in normalize::list(&report["capped"]) {
        tail.push(format!(
            "capped {} {}->{}",
            normalize::text(&item["field"]),
            item["original"],
            item["kept"]
        ));
    }
    if !tail.is_empty() {
        summary.push_str("; ");
        summary.push_str(&tail.join(", "));
    }
    json!({"status": "ok", "stats": stats, "errors": [], "summary": summary, "report": report})
}

impl ProfileStore {
    /// Normalizes and merges a JSON profile provided by the agent.
    pub fn import_profile(&self, payload: Value) -> Result<Value, StoreError> {
        let import = normalize::normalize(payload);
        self.change(|connection, profile, tombstones| {
            merge(connection, profile, tombstones, import.profile, "", "")?;
            Ok(success(import.report))
        })
    }

    /// Links a parsed résumé to canonical profile entities and holds its provenance in SQLite.
    pub fn import_resume(
        &self,
        mut payload: Value,
        resume_id: &str,
        tag_id: &str,
    ) -> Result<Value, StoreError> {
        if let Some(experience) = payload.as_object_mut().and_then(|body| body.remove("exp")) {
            payload["experience"] = experience;
        }
        for (key, field) in [("experience", "description"), ("projects", "impact")] {
            if let Some(rows) = payload.get_mut(key).and_then(Value::as_array_mut) {
                for row in rows.iter_mut().filter(|row| row.is_object()) {
                    let points = normalize::lines(&row["points"]);
                    if !points.is_empty() {
                        row[field] = json!(points);
                    }
                }
            }
        }
        let summary = normalize::text(&payload["s"]);
        let mut import = normalize::normalize(payload);
        if normalize::text(&import.profile["s"]).is_empty() && !summary.is_empty() {
            import.profile["s"] = json!(profile_text::summary(&summary));
        }
        self.change(|connection, profile, tombstones| {
            merge(
                connection,
                profile,
                tombstones,
                import.profile,
                resume_id,
                tag_id,
            )
        })
    }
}

fn merge(
    connection: &Connection,
    profile: &mut Map<String, Value>,
    tombstones: &mut Tombstones,
    incoming: Map<String, Value>,
    resume_id: &str,
    tag_id: &str,
) -> Result<Value, StoreError> {
    for key in ["n", "s"] {
        let value = normalize::text(&incoming[key]);
        if !value.is_empty()
            && (key != "n" || !matches!(value.to_lowercase().as_str(), "unknown" | "candidate"))
        {
            profile[key] = json!(value);
        }
    }
    for (key, value) in incoming["identity"].as_object().into_iter().flatten() {
        let value = normalize::text(value);
        if !value.is_empty() {
            settings::write(connection, key, &value)?;
            profile["identity"][key] = json!(value);
        }
    }

    let mut parsed = json!({
        "n": incoming["n"], "s": incoming["s"], "resume_id": resume_id,
        "skills": [], "exp": [], "projects": [], "education": incoming["education"],
        "certifications": incoming["certifications"], "achievements": incoming["achievements"],
    });
    for kind in Collection::ALL {
        let key = kind.key();
        let rows = profile
            .get_mut(key)
            .and_then(Value::as_array_mut)
            .expect("profile list exists");
        match kind {
            Collection::Skills => {
                for mut skill in normalize::list(&incoming[key]).iter().cloned() {
                    add_provenance(&mut skill, resume_id, tag_id);
                    let mark = normalize::key(&normalize::text(&skill["n"]));
                    let existing = rows.iter_mut().find(|row| {
                        normalize::key(&normalize::text(normalize::first(
                            row,
                            &["n", "name", "id"],
                        ))) == mark
                    });
                    let id = if let Some(existing) = existing {
                        union_fields(existing, &skill, &["source_resume_ids", "tag_ids"]);
                        normalize::text(&existing["id"])
                    } else {
                        let id = normalize::text(&skill["id"]);
                        rows.push(skill.clone());
                        id
                    };
                    forget(tombstones, kind, &skill, &id);
                    tag(connection, "skill", &id, tag_id)?;
                    push(
                        &mut parsed,
                        key,
                        json!({"id": id, "n": skill["n"], "cat": skill["cat"]}),
                    );
                }
            }
            Collection::Experience | Collection::Projects => {
                let experience = matches!(kind, Collection::Experience);
                merge_entities(
                    connection,
                    rows,
                    normalize::list(&incoming[key]),
                    &mut parsed,
                    experience,
                    resume_id,
                    tag_id,
                )?;
                for row in normalize::list(&parsed[key]) {
                    forget(tombstones, kind, row, &normalize::text(&row["id"]));
                }
            }
            _ => {
                let mut seen: HashSet<_> = rows
                    .iter()
                    .map(|item| normalize::key(&entry_text(item)))
                    .collect();
                for entry in normalize::list(&incoming[key]) {
                    let title = normalize::text(entry);
                    let id = profile_text::id(&title);
                    tombstones.forget(kind, &[&title, &id]);
                    if seen.insert(normalize::key(&title)) {
                        rows.push(entry.clone());
                    }
                }
            }
        }
    }
    Ok(parsed)
}

fn entry_text(item: &Value) -> String {
    if item.is_object() {
        normalize::text(&item["title"])
    } else {
        normalize::text(item)
    }
}

fn forget(tombstones: &mut Tombstones, kind: Collection, row: &Value, id: &str) {
    let labels = kind.labels(row);
    let mut labels = labels.iter().map(String::as_str).collect::<Vec<_>>();
    labels.push(id);
    tombstones.forget(kind, &labels);
}

#[derive(Clone, Copy)]
struct Entity {
    list: &'static str,
    kind: &'static str,
    description: &'static str,
    variants: &'static str,
    fill: &'static str,
}

const EXPERIENCE: Entity = Entity {
    list: "exp",
    kind: "experience",
    description: "d",
    variants: "role_variants",
    fill: "period",
};
const PROJECT: Entity = Entity {
    list: "projects",
    kind: "project",
    description: "impact",
    variants: "title_variants",
    fill: "repo",
};

fn merge_entities(
    connection: &Connection,
    rows: &mut Vec<Value>,
    incoming: &[Value],
    parsed: &mut Value,
    experience: bool,
    resume_id: &str,
    tag_id: &str,
) -> Result<(), StoreError> {
    let entity = if experience { EXPERIENCE } else { PROJECT };
    let mut canonical = Vec::new();
    for mut row in std::mem::take(rows).into_iter().filter(Value::is_object) {
        let id = row_id(&row, experience);
        construct_entity(&mut row, entity, &id, "", "", false);
        if let Some(existing) = canonical
            .iter_mut()
            .find(|existing| same_entity(existing, &row, experience))
        {
            merge_entity(existing, &row, entity);
        } else {
            canonical.push(row);
        }
    }
    for mut row in incoming.iter().cloned() {
        let requested = normalize::text(normalize::first(&row, &["matched_entity_id", "id"]));
        let index = canonical
            .iter()
            .position(|existing| existing["id"] == requested)
            .or_else(|| {
                canonical
                    .iter()
                    .position(|existing| same_entity(existing, &row, experience))
            });
        let id = index.map_or_else(
            || row_id(&row, experience),
            |index| normalize::text(&canonical[index]["id"]),
        );
        if let Some(index) = index {
            validate_references(&mut row, &canonical[index], &id);
        } else {
            row["matched_entity_id"] = json!("");
            row["exact_matches"] = json!([]);
            row["similar_pairs"] = json!([]);
        }
        construct_entity(&mut row, entity, &id, resume_id, tag_id, true);
        let point_ids = normalize::list(&row["points"])
            .iter()
            .map(|point| normalize::text(&point["id"]))
            .collect::<Vec<_>>();
        push(parsed, entity.list, row.clone());
        if let Some(index) = index {
            merge_entity(&mut canonical[index], &row, entity);
        } else {
            canonical.push(row);
        }
        for id in point_ids {
            tag(connection, entity.kind, &id, tag_id)?;
        }
    }
    *rows = canonical;
    Ok(())
}

fn validate_references(row: &mut Value, existing: &Value, parent_id: &str) {
    if !normalize::text(&row["matched_entity_id"]).is_empty() {
        row["matched_entity_id"] = json!(parent_id);
    }
    let points = normalize::list(&existing["points"]);
    for key in ["exact_matches", "similar_pairs"] {
        let values = normalize::list(&row[key])
            .iter()
            .filter_map(|item| {
                let id = normalize::text(&item["existing_point_id"]);
                let point = points.iter().find(|point| point["id"] == id)?;
                let mut item = item.clone();
                let field = if key == "exact_matches" {
                    "text"
                } else {
                    "existing_text"
                };
                item[field] = point["text"].clone();
                Some(item)
            })
            .collect::<Vec<_>>();
        row[key] = json!(values);
    }
}

fn row_id(row: &Value, experience: bool) -> String {
    let id = normalize::text(&row["id"]);
    if !id.is_empty() {
        return id;
    }
    if experience {
        profile_text::id(&format!(
            "{}{}",
            normalize::text(&row["role"]),
            normalize::text(&row["co"])
        ))
    } else {
        profile_text::id(&normalize::text(&row["title"]))
    }
}

fn same_entity(first: &Value, second: &Value, experience: bool) -> bool {
    if experience {
        normalize::experience_key(first) == normalize::experience_key(second)
    } else {
        normalize::project_key(first) == normalize::project_key(second)
    }
}

fn construct_entity(
    row: &mut Value,
    entity: Entity,
    id: &str,
    resume_id: &str,
    tag_id: &str,
    incoming: bool,
) {
    row["id"] = json!(id);
    if row[entity.variants].as_array().is_none_or(Vec::is_empty) {
        let title = if entity.kind == "experience" {
            &row["role"]
        } else {
            &row["title"]
        };
        let mut variants = Vec::new();
        if !normalize::text(title).is_empty() {
            let mut variant = json!({"title": title, "resume_id": resume_id, "tag_id": tag_id});
            if entity.kind == "experience" {
                variant["company"] = row["co"].clone();
                variant["period"] = row["period"].clone();
            }
            variants.push(variant);
        }
        row[entity.variants] = json!(variants);
    }
    let explicit = normalize::list(&row["points"]);
    let description = normalize::text(&row[entity.description]);
    let points = if !explicit.is_empty() {
        explicit.to_vec()
    } else {
        profile_points::split(&description)
            .into_iter()
            .map(Value::String)
            .collect()
    };
    let similar: HashSet<String> = normalize::list(&row["similar_pairs"])
        .iter()
        .map(|pair| normalize::key(&normalize::text(&pair["new_text"])))
        .collect();
    let existing_id = |point: &str| {
        normalize::list(&row["exact_matches"])
            .iter()
            .find(|item| normalize::key(&normalize::text(&item["text"])) == normalize::key(point))
            .map(|item| normalize::text(&item["existing_point_id"]))
    };
    let mut seen = HashSet::new();
    let points = points
        .into_iter()
        .filter_map(|item| {
            let text = if item.is_object() {
                normalize::text(&item["text"])
            } else {
                normalize::text(&item)
            };
            let text = profile_points::clean_inline(&text);
            let key = normalize::key(&text);
            if text.is_empty() || similar.contains(&key) || !seen.insert(key) {
                return None;
            }
            let id = existing_id(&text)
                .or_else(|| {
                    item["id"]
                        .as_str()
                        .filter(|id| !id.is_empty())
                        .map(str::to_owned)
                })
                .unwrap_or_else(|| {
                    profile_text::id(&format!(
                        "{id}|{}",
                        profile_text::collapse(&text).to_lowercase()
                    ))
                });
            let mut point = if item.is_object() { item } else { json!({}) };
            point["id"] = json!(id);
            point["text"] = json!(text);
            if !resume_id.is_empty() || !tag_id.is_empty() {
                add_provenance(&mut point, resume_id, tag_id);
            }
            Some(point)
        })
        .collect::<Vec<_>>();
    if incoming || !points.is_empty() {
        row[entity.description] = json!(normalize::lines(&json!(points)));
    }
    row["points"] = json!(points);
    if incoming {
        add_provenance(row, resume_id, tag_id);
        for field in ["exact_matches", "similar_pairs", "new_points"] {
            if !row[field].is_array() {
                row[field] = json!([]);
            }
        }
        if row["matched_entity_id"].is_null() {
            row["matched_entity_id"] = json!("");
        }
    }
}

fn merge_entity(existing: &mut Value, incoming: &Value, entity: Entity) {
    if normalize::text(&existing[entity.fill]).is_empty() {
        existing[entity.fill] = incoming[entity.fill].clone();
    }
    if entity.kind == "project" {
        let mut stack = profile_text::stack(&existing["stack"]);
        normalize::union_stack(&mut stack, profile_text::stack(&incoming["stack"]));
        existing["stack"] = json!(stack);
    }
    let mut points = normalize::list(&existing["points"]).to_vec();
    for incoming in normalize::list(&incoming["points"]) {
        let key = normalize::key(&normalize::text(&incoming["text"]));
        if let Some(point) = points.iter_mut().find(|point| {
            point["id"] == incoming["id"] || normalize::key(&normalize::text(&point["text"])) == key
        }) {
            union_fields(point, incoming, &["source_resume_ids", "tag_ids"]);
        } else {
            points.push(incoming.clone());
        }
    }
    existing["points"] = json!(points);
    let description = normalize::lines(&existing["points"]);
    if !description.is_empty() {
        existing[entity.description] = json!(description);
        if existing.get("description").is_some() {
            existing["description"] = json!(description);
        }
    }
    union_fields(
        existing,
        incoming,
        &[entity.variants, "source_resume_ids", "tag_ids"],
    );
}

fn add_provenance(row: &mut Value, resume_id: &str, tag_id: &str) {
    for (field, value) in [("source_resume_ids", resume_id), ("tag_ids", tag_id)] {
        let mut values = normalize::list(&row[field]).to_vec();
        if !value.is_empty() && !values.iter().any(|existing| existing == value) {
            values.push(json!(value));
        }
        row[field] = json!(values);
    }
}

fn union_fields(existing: &mut Value, incoming: &Value, fields: &[&str]) {
    for &field in fields {
        let mut values = normalize::list(&existing[field]).to_vec();
        let incoming = normalize::list(&incoming[field]);
        if incoming.is_empty() && existing[field].is_null() {
            continue;
        }
        for value in incoming {
            if !values.contains(value) {
                values.push(value.clone());
            }
        }
        existing[field] = json!(values);
    }
}

fn tag(connection: &Connection, kind: &str, id: &str, tag_id: &str) -> Result<(), StoreError> {
    if !id.is_empty() && !tag_id.is_empty() {
        connection.execute(
            "INSERT OR IGNORE INTO point_tags(point_kind, point_id, tag_id) VALUES (?1, ?2, ?3)",
            [kind, id, tag_id],
        )?;
    }
    Ok(())
}

fn push(value: &mut Value, key: &str, row: Value) {
    value[key]
        .as_array_mut()
        .expect("parsed résumé list exists")
        .push(row);
}
