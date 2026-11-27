//! The read-only profile and editable application answers.

use std::collections::BTreeMap;

use maud::{Markup, html};
use serde_json::Value;

use crate::view;

fn named(value: &Value) -> &str {
    value
        .as_str()
        .or_else(|| value["n"].as_str())
        .or_else(|| value["name"].as_str())
        .or_else(|| value["title"].as_str())
        .unwrap_or_default()
}

/// Renders the profile panel and its application answers.
pub fn panel(profile: &Value, answers: &Value) -> Markup {
    let name = view::text(profile, "n");
    let summary = view::text(profile, "s");
    let identity = &profile["identity"];
    let city = view::text(identity, "city");
    let phone = view::text(identity, "phone");
    let mut skills: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for skill in view::list(profile, "skills") {
        let category = skill["cat"]
            .as_str()
            .or_else(|| skill["category"].as_str())
            .filter(|key| !key.is_empty())
            .unwrap_or("general");
        skills.entry(category).or_default().push(named(skill));
    }
    let experience = view::list(profile, "exp");
    let projects = view::list(profile, "projects");
    html! {
        article #profile-panel {
            header.drawer-heading {
                h2 { (if name.is_empty() { "Your profile" } else { name }) }
                p {
                    (city)
                    @if !city.is_empty() && !phone.is_empty() { " · " }
                    (phone)
                }
                div.inline {
                    @let email = view::text(identity, "email");
                    @if !email.is_empty() {
                        a href=(format!("mailto:{}", view::segment(email).replace("%40", "@"))) { "Email" }
                    }
                    @for (label, key) in [("LinkedIn", "linkedin_url"), ("GitHub", "github_url"), ("Website", "website_url")] {
                        @if let Some(url) = view::safe_url(view::text(identity, key)) { (view::external(label, url)) }
                    }
                }
            }
            @if !profile["detail"].is_null() {
                p.bad { "Could not load the profile: " (profile["detail"].as_str().unwrap_or("try again")) }
            } @else if name.is_empty() && skills.is_empty() && experience.is_empty() && projects.is_empty() {
                div.empty { "Nothing here yet. Attach your résumé in Agent chat and the agent fills this in." }
            }
            @if !summary.is_empty() { section { h3 { "Summary" } p.prose-line { (summary) } } }
            (answer_form(answers))
            @if !skills.is_empty() {
                section {
                    h3 { "Skills (" (view::list(profile, "skills").len()) ")" }
                    @for (category, names) in skills {
                        h4 { (category) }
                        div.chips { @for name in names { span.chip { (name) } } }
                    }
                }
            }
            @if !experience.is_empty() {
                section {
                    h3 { "Experience (" (experience.len()) ")" }
                    @for role in experience {
                        div.record {
                            h4.inline { span { (view::text(role, "role")) " · " (view::text(role, "co")) }
                                small { (view::text(role, "period")) }
                            }
                            @let description = role["d"].as_str().or_else(|| role["description"].as_str()).unwrap_or_default();
                            ul {
                                @for line in description.lines().map(|line| line.trim().trim_start_matches(['-', '•', '*']).trim()).filter(|line| !line.is_empty()) {
                                    li { (line) }
                                }
                            }
                        }
                    }
                }
            }
            @if !projects.is_empty() {
                section {
                    h3 { "Projects (" (projects.len()) ")" }
                    @for project in projects {
                        div.record {
                            h4 { (view::text(project, "title")) @if let Some(url) = view::safe_url(view::text(project, "repo")) { " " (view::external("repo", url)) } }
                            p.muted { (stack(&project["stack"])) }
                            p.prose-line { (view::text(project, "impact")) }
                        }
                    }
                }
            }
            @for (key, label) in [("education", "Education"), ("certifications", "Certifications"), ("achievements", "Achievements")] {
                @let entries = view::list(profile, key);
                @if !entries.is_empty() { section { h3 { (label) } ul { @for entry in entries { li { (named(entry)) } } } } }
            }
            footer { p.muted { "Ask the agent to add, change or delete anything here." } a.button.primary href="/?compose=1" { "Edit with the agent" } }
        }
    }
}

fn stack(value: &Value) -> String {
    if let Some(items) = value.as_array() {
        items
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join(", ")
    } else {
        value.as_str().unwrap_or_default().to_owned()
    }
}

fn answer_form(data: &Value) -> Markup {
    let mut groups: Vec<(&str, Vec<&Value>)> = Vec::new();
    for field in view::list(&data["schema"], "facts") {
        let group = view::text(field, "group");
        if let Some((_, fields)) = groups.iter_mut().find(|(name, _)| *name == group) {
            fields.push(field);
        } else {
            groups.push((group, vec![field]));
        }
    }
    groups.push((
        "Default answers",
        view::list(&data["schema"], "defaults").iter().collect(),
    ));
    html! {
        section #profile-answers {
            h3 { "Application answers" }
            @if data["schema"].is_null() {
                p.bad { "Could not load your application answers." }
            } @else {
                p { "These answers are copied exactly. The decision model picks which answer goes in which box. The AI model writes unmatched answers or leaves them for you. Empty answers leave the corresponding form fields blank." }
                form method="post" data-action="/ui/actions/profile/answers" data-method="PUT" data-busy="Saving…" data-save {
                    @for (group, fields) in groups {
                        @let namespace = if group == "Default answers" { "defaults" } else { "facts" };
                        @let saved = &data[namespace];
                        @let filled = fields.iter().filter(|field| !view::text(saved, view::text(field, "key")).is_empty()).count();
                        details.answer-group name="answer-group" {
                            summary { (group) " · " span.muted { (filled) "/" (fields.len()) } }
                            div.field-grid {
                                @for field in fields { (answer_input(field, saved, namespace)) }
                            }
                        }
                    }
                    button.primary { "Save answers" }
                    output aria-live="polite" {}
                }
            }
        }
    }
}

fn answer_input(field: &Value, saved: &Value, namespace: &str) -> Markup {
    let key = view::text(field, "key");
    let id = format!("{namespace}-{key}");
    let name = format!("{namespace}.{key}");
    let default = view::text(field, "default");
    let placeholder = (!default.is_empty()).then(|| format!("default: {default}"));
    let options = view::list(field, "options");
    let value = view::text(saved, key);
    html! {
        label.field {
            span { (view::text(field, "label")) }
            @if matches!(key, "summary" | "why_interested") {
                textarea rows="3" name=(&name) placeholder=[placeholder] { (value) }
            } @else {
                input name=(&name) value=(value) placeholder=[placeholder] list=[(!options.is_empty()).then_some(&id)];
            }
        }
        @if !options.is_empty() {
            datalist id=(id) { @for option in options { option value=(option.as_str().unwrap_or_default()) {} } }
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::panel;

    #[test]
    fn profile_escapes_fields_and_keeps_fact_and_default_names_distinct() {
        let rendered = panel(&json!({"n":"<script>", "identity":{"website_url":"javascript:alert(1)"}}), &json!({
            "schema":{"facts":[{"key":"summary","label":"Summary","group":"About"}],"defaults":[{"key":"summary","label":"Summary"}]},
            "facts":{"summary":"</textarea>"}
        })).into_string();
        assert!(rendered.contains("&lt;script&gt;"));
        assert!(rendered.contains("&lt;/textarea&gt;"));
        assert!(rendered.contains("name=\"facts.summary\""));
        assert!(rendered.contains("name=\"defaults.summary\""));
        assert!(!rendered.contains("javascript:"));
    }
}
