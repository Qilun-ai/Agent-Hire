//! Settings navigation and section templates.

mod ai;
mod general;
mod mailbox;

use maud::{Markup, html};
use serde_json::Value;

use crate::status;
use crate::view;

/// A selected settings section.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Section {
    /// Provider credentials and models.
    Ai,
    /// IMAP setup and polling.
    Email,
    /// Classification and scrape schedule.
    JobAgent,
    /// Appearance, activity and data.
    General,
}

impl Section {
    /// Resolves a settings URL segment.
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "ai" => Some(Self::Ai),
            "email" => Some(Self::Email),
            "job-agent" => Some(Self::JobAgent),
            "general" => Some(Self::General),
            _ => None,
        }
    }

    /// The canonical URL segment.
    pub fn slug(self) -> &'static str {
        match self {
            Self::Ai => "ai",
            Self::Email => "email",
            Self::JobAgent => "job-agent",
            Self::General => "general",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Ai => "AI model",
            Self::Email => "Email updates",
            Self::JobAgent => "Job agent",
            Self::General => "General",
        }
    }
}

/// Settings as displayed by the UI. Only masked stored values and safe previews belong here.
#[derive(Default)]
pub struct Settings {
    /// Stored settings with secrets masked.
    pub values: Value,
    /// Environment provider names and setting identifiers.
    pub environment: Value,
    /// Safe prefixes for existing keys.
    pub previews: Value,
    /// Recent local activity.
    pub activity: Value,
    /// Redacted mailbox status.
    pub email: Value,
    /// Index counts and storage path.
    pub index: Value,
    /// Notebook and classifier deployment state.
    pub kaggle: Value,
}

/// Renders the settings navigation and the current section.
pub fn page(section: Section, settings: &Settings) -> Markup {
    html! {
        div.workspace.settings-layout {
            aside.settings-nav {
                h1 { "Settings" }
                nav aria-label="Settings sections" {
                    @for item in [Section::Ai, Section::Email, Section::JobAgent, Section::General] {
                        a href=(format!("/settings/{}", item.slug())) aria-current=[(item == section).then_some("page")] { (item.label()) }
                    }
                }
            }
            div.settings-content {
                header.page-heading { div { h2 { (section.label()) } } }
                (content(section, settings))
            }
        }
    }
}

/// Renders the selected settings form or settings controls.
pub fn content(section: Section, settings: &Settings) -> Markup {
    html! {
        div #settings-section {
            @match section {
                Section::Ai => { (ai::panel(settings)) }
                Section::Email => { (mailbox::panel(&settings.email)) }
                Section::JobAgent => { (job_agent(settings)) }
                Section::General => { (general::panel(&settings.activity)) }
            }
        }
    }
}

/// Renders the notebook settings toggle and the live phase without replacing other settings fields.
pub fn notebook_setting(kaggle: &Value) -> Markup {
    html! {
        div #notebook-setting {
            form method="post" data-action="/ui/actions/kaggle/toggle" data-effects="kaggle" {
                label.check { input type="checkbox" name="enabled" data-boolean data-submit checked[kaggle["enabled"] == true]; "Use a Kaggle notebook" }
                output aria-live="polite" {}
            }
            @if kaggle["enabled"] == true {
                p.muted {
                    @if kaggle["has_link"] != true { "No link yet. Paste a link in the top bar each session." }
                    @else if kaggle["phase"] == "ready" { "Model ready" }
                    @else { (status::phase(view::text(kaggle, "phase"))) }
                }
            }
        }
    }
}

fn job_agent(settings: &Settings) -> Markup {
    let idx = &settings.index;
    html! {
        section.card.setting-card {
            h3 { "Decision model" }
            p { "The model classifies jobs by target role and experience level. Agent-Hire tries the Kaggle notebook first, then the decision-model API. Built-in rules use the title and required years when neither is configured." }
            (notebook_setting(&settings.kaggle))
            @if settings.environment["decision_api"]["source"] == "environment" {
                p.notice { "Using " (view::text(&settings.environment["decision_api"], "variable")) " from the install folder’s .env file. Values entered here take precedence." }
            }
            form method="post" data-action="/ui/actions/settings" data-busy="Saving…" data-save data-settings="job-agent" {
                div.field-grid.decision-fields {
                    label.field {
                        span { "Decision model API URL" }
                        input name="decision_api_url" value=(view::text(&settings.values, "decision_api_url")) placeholder="https://…/v1/systemone";
                        small { "Use a System One-compatible endpoint." }
                    }
                    (ai::key_input(settings, "decision_api_key", "", "Only if the endpoint requires one"))
                    (view::input("Decision model name", "decision_api_model", view::text(&settings.values, "decision_api_model"), "Optional"))
                }
                div {
                    h3 { "Background scraping" }
                    p { "Agent-Hire can scrape " (view::number(view::count(idx, "companies_available"))) " company career sites. A full pass takes hours, and the agent can search jobs as they arrive. The schedule starts after the first scrape. Set it to 0 to turn off automatic refresh." }
                }
                label.field { span { "Refresh every (hours)" }
                    input type="number" name="scrape_refresh_hours" min="0" max="168" value=(idx["scrape"]["refresh_hours"].as_f64().unwrap_or(12.0)) required;
                }
                p.muted { "Index: " (view::number(view::count(&idx["index"], "jobs_active"))) " open jobs from " (view::number(view::count(&idx["index"], "companies_indexed"))) " companies · stored in " (view::text(idx, "jobs_dir")) }
                div.inline {
                    button.primary { "Save settings" }
                    button.secondary data-probe { "Save & test decision model" }
                }
                output aria-live="polite" {}
                div #settings-result {}
            }
        }
    }
}

/// Renders a settings validation or decision-model probe response.
pub fn result(kind: &str, provider: &str, result: &Value) -> Markup {
    html! {
        div #settings-result {
            @if kind == "probe" {
                @if result["configured"] != true { p { "No endpoint configured — using built-in rules." } }
                @for row in view::list(result, "results") {
                    p class=(if row["ok"] == true { "ok" } else { "bad" }) {
                        (view::text(row, "endpoint")) ": "
                        @if row["ok"] == true {
                            "reachable (control job → software " ((row["role_p"].as_f64().unwrap_or_default() * 100.0).round()) "%, " (view::text(row, "level")) ")"
                        } @else { "failed (" (row["error"].as_str().unwrap_or("no answer")) ")" }
                    }
                }
            } @else {
                (ai::validation(provider, result))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{Section, Settings, page};

    #[test]
    fn all_settings_sections_render_and_escape_user_values() {
        let data = Settings {
            values: json!({"llm_provider":"custom","custom_model":"\"><script>","decision_api_model":"<script>"}),
            email: json!({"mailbox":{"username":"<script>"}}),
            activity: json!([{"action":"<script>","ts":"today"}]),
            ..Settings::default()
        };
        for section in [
            Section::Ai,
            Section::Email,
            Section::JobAgent,
            Section::General,
        ] {
            let rendered = page(section, &data).into_string();
            assert!(rendered.contains(section.label()));
            assert!(rendered.contains("&lt;script&gt;"));
            assert!(!rendered.contains("<script>"));
        }
    }
}
