//! Live scrape, classification and notebook widgets.

use maud::{Markup, html};
use serde_json::Value;

use crate::view;

const DEPLOYING: &[&str] = &[
    "starting",
    "installing",
    "starting_models",
    "warming_models",
];

/// Whether the notebook is currently starting or loading models.
pub fn deploying(kaggle: &Value) -> bool {
    DEPLOYING.contains(&view::text(kaggle, "phase"))
}

/// A user-facing phase name from the notebook manager.
pub fn phase(value: &str) -> &str {
    match value {
        "starting" => "Starting…",
        "installing" => "Downloading the model",
        "starting_models" => "Starting the model on the GPUs",
        "warming_models" => "Warming up",
        "ready" => "Ready",
        "failed" => "Deployment failed",
        "stopped" => "Stopped",
        "not_deployed" => "Connected — model not loaded",
        "unreachable" => "Link expired or notebook stopped",
        "" => "Connecting",
        other => other,
    }
}

/// Renders the live indicators in the top bar.
pub fn widgets(scrape: &Value, classifiers: &Value, kaggle: &Value) -> Markup {
    html! {
        div #status-widgets {
            (notebook_widget(kaggle))
            (classifier_widget(classifiers))
            (scrape_widget(scrape))
        }
    }
}

/// Renders the replaceable scrape indicator.
pub fn scrape_widget(scrape: &Value) -> Markup {
    html! { div #scrape-widget { (scrape_pill(scrape)) } }
}

/// Renders the replaceable classifier indicator.
pub fn classifier_widget(classifiers: &Value) -> Markup {
    html! { div #classifier-widget { (classification(classifiers)) } }
}

/// Renders the replaceable notebook indicator.
pub fn notebook_widget(kaggle: &Value) -> Markup {
    html! { div #notebook-widget data-deploying=(deploying(kaggle)) { (kaggle_pill(kaggle)) } }
}

fn active_run(scrape: &Value) -> Option<&Value> {
    scrape.get("active").filter(|run| !run.is_null())
}

fn last_run(scrape: &Value) -> &str {
    let Some(run) = view::list(scrape, "recent")
        .iter()
        .find(|run| view::text(run, "status") != "running")
    else {
        return "";
    };
    run["finished_at"]
        .as_str()
        .filter(|time| !time.is_empty())
        .unwrap_or_else(|| view::text(run, "started_at"))
}

fn scrape_pill(scrape: &Value) -> Markup {
    html! {
        button type="button" data-open="/ui/companies" class=(if active_run(scrape).is_some() { "pill live" } else { "pill" }) title="Click to open Companies" {
            span.dot {}
            @if let Some(run) = active_run(scrape) {
                span { (run_label(run)) }
                (run_progress(run))
            } @else if scrape.is_null() || !scrape["detail"].is_null() {
                span { "Scraper status unavailable" }
            } @else if last_run(scrape).is_empty() {
                span { "Not scraped yet" }
            } @else {
                span { "Last scrape " (view::time(last_run(scrape), "widget")) }
            }
        }
    }
}

pub(crate) fn run_label(run: &Value) -> String {
    format!(
        "Scraping {}/{} · {} new",
        view::count(run, "companies_done"),
        view::count(run, "companies_planned"),
        view::number(view::count(run, "new_jobs"))
    )
}

pub(crate) fn run_progress(run: &Value) -> Markup {
    view::progress(
        view::count(run, "companies_done"),
        view::count(run, "companies_planned"),
        "Companies scraped",
    )
}

fn classification(data: &Value) -> Markup {
    let active = view::count(data, "active");
    let classified = view::count(data, "classified");
    let remaining = view::count(data, "remaining");
    let installed = data["models"]["department"] == true || data["models"]["level"] == true;
    let device = match view::text(data, "device") {
        "kaggle" => "the Kaggle GPU",
        "cuda" => "this computer’s GPU",
        _ => "this computer’s CPU",
    };
    let detail = if remaining > 0 {
        format!("{remaining} waiting, running on {device}")
    } else {
        "New jobs are classified as they arrive".to_owned()
    };
    html! {
        @if active > 0 && installed {
            span class=(if remaining > 0 { "pill live" } else { "pill ok" })
                title=(format!("{classified} of {active} open jobs classified by both models. Department model: {} · Experience-level model: {}. {detail}", view::count(data, "department_done"), view::count(data, "level_done"))) {
                span.dot {}
                span { @if remaining > 0 { "Classifying " (view::number(classified)) "/" (view::number(active)) } @else { "Classified " (view::number(classified)) } }
                (view::progress(classified, active, "Jobs classified"))
            }
        }
    }
}

fn percent(kaggle: &Value) -> u64 {
    (100.0
        * kaggle["progress"]
            .as_f64()
            .unwrap_or_default()
            .clamp(0.0, 1.0))
    .round() as u64
}

fn kaggle_pill(kaggle: &Value) -> Markup {
    let current = view::text(kaggle, "phase");
    let linked = kaggle["has_link"] == true;
    let tone = if !linked || matches!(current, "failed" | "unreachable") {
        "bad"
    } else if current == "ready" {
        "ok"
    } else if deploying(kaggle) {
        "live"
    } else {
        ""
    };
    html! {
        @if kaggle["enabled"] == true {
            button type="button" class=(format!("pill {tone}")) data-popover="/ui/kaggle" {
                span.dot {}
                span {
                    @if !linked { "Paste Kaggle link" }
                    @else if current == "ready" {
                        "Kaggle ready" @if view::count(kaggle, "replicas") > 0 { " · " (view::count(kaggle, "replicas")) "×GPU" }
                    } @else if current == "installing" { "Kaggle: downloading " (percent(kaggle)) "%" }
                    @else if current == "unreachable" { "Kaggle link expired" }
                    @else { "Kaggle: " (phase(current)) }
                }
            }
        }
    }
}

/// Renders the Jobs summary and scrape controls.
pub fn jobs_strip(status: &Value) -> Markup {
    let index = &status["index"];
    let scrape = &status["scrape"];
    let refresh = scrape["refresh_hours"].as_f64().unwrap_or(12.0);
    let last = last_run(scrape);
    html! {
        section #jobs-status .card.status-strip aria-label="Job index status" {
            div.index-metrics {
                span { strong { (view::number(view::count(index, "jobs_active"))) } " open jobs" }
                span { strong { (view::number(view::count(index, "jobs_last_24h"))) } " last 24h" }
                span { strong { (view::number(view::count(index, "companies_indexed"))) "/" (view::number(view::count(status, "companies_available").max(view::count(index, "companies_available")))) } " companies" }
            }
            div.scrape-state {
                @if let Some(run) = active_run(scrape) {
                    span.live { (run_label(run)) (run_progress(run)) }
                    (scrape_action(true, "Stop"))
                } @else {
                    span.muted {
                        @if last.is_empty() { "Not scraped yet" }
                        @else { "Last scrape " (view::time(last, "jobs")) }
                        @if refresh == 0.0 { " · automatic refresh off" }
                        @else if !last.is_empty() { " · refresh every " (refresh) "h" }
                    }
                    (scrape_action(false, "Scrape all companies"))
                }
            }
        }
    }
}

pub(crate) fn scrape_action(stop: bool, label: &str) -> Markup {
    html! {
        form.inline method="post" data-action=(if stop { "/ui/actions/scrape/stop" } else { "/ui/actions/scrape" }) data-effects="scrape" {
            button.secondary { (label) }
            output aria-live="polite" {}
        }
    }
}

/// Renders the notebook connection and model controls.
pub fn kaggle_panel(kaggle: &Value) -> Markup {
    let current = view::text(kaggle, "phase");
    let gpus = view::list(kaggle, "gpus");
    let classifiers = &kaggle["classifiers"];
    let encoder_phase = view::text(classifiers, "phase");
    html! {
        section #kaggle-panel {
            h2 { "Kaggle notebook" }
            p { "Paste the notebook’s connection link each session. Enable GPU (T4 ×2) and Internet in the notebook first. Agent-Hire loads its job classifiers and decision model on the notebook GPUs." }
            form method="post" data-action="/ui/actions/kaggle/connect" data-busy="Connecting…" data-effects="kaggle" {
                label.field { span { "Notebook link" } input type="password" name="link" autocomplete="off" placeholder="https://…jupyter-proxy.kaggle.net?token=…" required; }
                button.primary { "Connect" }
                output aria-live="polite" {}
            }
            div #kaggle-state {
                @if kaggle["has_link"] == true {
                    p { strong { (phase(current)) } @if !gpus.is_empty() { " · " (gpus.len()) " GPU" } }
                    @if matches!(current, "installing" | "starting") {
                        (view::progress(percent(kaggle), 100, "Model download"))
                        p.muted { (percent(kaggle)) "% of 9.5 GB · first load takes ~10 minutes. Later loads in the same session are quick." }
                    } @else if deploying(kaggle) { p.muted { "Almost there…" } }
                    p {
                        "Job models: " (match encoder_phase { "ready" => "ready", "failed" => "failed", _ => "loading…" })
                        @if encoder_phase == "ready" {
                            @if let Some(rate) = classifiers["jobs_per_s"].as_f64() { " · ~" (rate.round()) " jobs/s" }
                            " · new jobs are classified on the notebook GPU"
                        }
                    }
                    @if encoder_phase == "failed" {
                        p.bad { (classifiers["error"].as_str().unwrap_or("The job models failed to load. Jobs are classified on this computer instead.")) }
                    }
                    @if current == "ready" { p.ok { "Decision model ready for form filling." } }
                    @if matches!(current, "failed" | "unreachable") { p.bad { (kaggle["error"].as_str().unwrap_or("The deployment failed.")) } }
                    @if current == "failed" && !kaggle["log_tail"].is_null() {
                        pre { (kaggle["log_tail"].as_str().map_or_else(|| kaggle["log_tail"].to_string(), str::to_owned)) }
                    }
                    form title="Auto sends backlogs of at least 1,000 jobs to the notebook." data-action="/ui/actions/kaggle/encoder-device" data-effects="kaggle" {
                        label.field { span { "Job models run on" } select name="device" data-submit {
                            @for (key, name) in [("auto", "Auto (Kaggle for big backlogs)"), ("kaggle", "Kaggle notebook"), ("local", "This computer")] {
                                option value=(key) selected[view::text(kaggle, "encoder_device") == key] { (name) }
                            }
                        } }
                        output aria-live="polite" {}
                    }
                    div.inline {
                        @if matches!(current, "not_deployed" | "stopped" | "failed") {
                            (kaggle_action("connect", "Load model with the saved link"))
                        }
                        @if current == "ready" || deploying(kaggle) { (kaggle_action("stop", "Stop model")) }
                        (kaggle_action("forget", "Forget link"))
                    }
                }
            }
        }
    }
}

fn kaggle_action(action: &str, label: &str) -> Markup {
    html! { form.inline method="post" data-action=(format!("/ui/actions/kaggle/{action}")) data-effects="kaggle" {
        @if action == "connect" { input type="hidden" name="link" value=""; }
        button.secondary { (label) }
        output aria-live="polite" {}
    } }
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::jobs_strip;

    #[test]
    fn describes_disabled_and_enabled_scrape_refresh() {
        let mut status = json!({"scrape": {"refresh_hours": 0}});
        let render = |status: &Value| jobs_strip(status).into_string();
        let empty = render(&status);
        assert!(empty.contains("Not scraped yet · automatic refresh off"));

        status["scrape"]["recent"] = json!([{
            "status": "completed",
            "finished_at": "2026-11-20T12:00:00Z"
        }]);
        let disabled = render(&status);
        assert!(disabled.contains("Last scrape"));
        assert!(disabled.contains(" · automatic refresh off"));
        assert!(!disabled.contains("refresh every 0h"));

        status["scrape"]["refresh_hours"] = json!(12);
        assert!(render(&status).contains(" · refresh every 12h"));
    }
}
