//! Tool call status, summaries and expandable raw results.

use maud::{Markup, html};
use serde_json::Value;

use super::repeat;
use crate::jobs;
use crate::status;
use crate::view;

pub(super) fn render(call: &Value) -> Markup {
    let name = view::text(call, "name");
    let failed = call["isError"] == true;
    let running = call["result"].is_null() && call["details"].is_null() && !failed;
    let tone = if running {
        "live"
    } else if failed {
        "bad"
    } else {
        "ok"
    };
    html! {
        details.tool-card {
            summary {
                span class=(format!("dot {tone}")) aria-label=(if running { "Running" } else if failed { "Error" } else { "Finished" }) {}
                strong { (label(name)) }
                @if running { " …" }
                span.muted.tool-toggle { "Details" }
            }
            div.tool-raw {
                h4 { "Input" } pre { (pretty(&call["args"])) }
                @if !call["result"].is_null() { h4 { "Result" } pre class=[failed.then_some("bad")] { (pretty(&call["result"])) } }
            }
        }
        @if failed { p.tool-summary.bad { (view::text(call, "result")) } }
        @else if !running {
            div.tool-summary { (rich(name, &call["details"])) }
        }
    }
}

fn pretty(value: &Value) -> String {
    if let Some(raw) = value.as_str() {
        serde_json::from_str::<Value>(raw)
            .ok()
            .and_then(|json| serde_json::to_string_pretty(&json).ok())
            .unwrap_or_else(|| raw.to_owned())
    } else {
        serde_json::to_string_pretty(value).unwrap_or_default()
    }
}

fn label(name: &str) -> &str {
    match name {
        "get_profile" => "Read your profile",
        "import_document" => "Saved & read document",
        "read_document" => "Read document",
        "save_profile" => "Saved profile",
        "set_preferences" => "Saved preferences",
        "job_index_status" => "Checked the job index",
        "start_scrape" => "Started scraping",
        "scrape_status" => "Scrape progress",
        "search_jobs" => "Searched jobs",
        "get_job" => "Read job details",
        "shortlist_jobs" => "Shortlisted jobs",
        "tailor_resume" => "Tailored résumé",
        "extension_queue" => "Extension queue",
        "application_summary" => "Application tracker",
        "check_email" => "Checked email",
        "schedule_check" => "Scheduled a check-in",
        "classify_jobs" => "Classified job levels",
        "add_custom_job" => "Added a job",
        "delete_jobs" => "Deleted jobs",
        "update_job_status" => "Updated application stage",
        "list_pipeline" => "Listed applications",
        "reset_jobs" => "Reset job data",
        "edit_profile" => "Edited profile",
        "list_schedules" => "Scheduled check-ins",
        "cancel_schedule" => "Cancelled a check-in",
        "read_web_page" => "Read web page",
        "save_application_answers" => "Saved application answers",
        _ => name,
    }
}

fn rich(name: &str, data: &Value) -> Markup {
    if !data.is_object() {
        return html! {};
    }
    match name {
        "search_jobs" => search(data),
        "job_index_status" => index(data),
        "start_scrape" | "scrape_status" => scrape(data),
        "import_document" | "read_document" => html! {
            p { strong { (view::text(data, "file")) } " · " (view::count(data, "pages")) " pages · " (view::number(view::count(data, "total_chars"))) " characters" }
            @if !view::text(data, "note").is_empty() { p.muted { "Scanned document read as page images." } }
        },
        "get_profile" | "save_profile" | "edit_profile" => {
            profile(data.get("profile").unwrap_or(data))
        }
        "shortlist_jobs" => html! {
            p { (view::list(data, "added").len()) " added to Applications & the extension queue · " (view::list(data, "already_in_pipeline").len()) " already there" }
        },
        "tailor_resume" => {
            let lead = data.get("lead").unwrap_or(data);
            let ready = data["resume_ready"] == true
                || ["resume_asset", "asset", "asset_path"]
                    .iter()
                    .any(|key| !view::text(lead, key).is_empty());
            html! { p { (view::text(lead, "title")) " — " (view::text(lead, "company")) " · "
                @if ready { span.ok { "Résumé ready" } } @else { (view::text(data, "status")) }
            } }
        }
        "schedule_check" => html! {
            p { "Next " (view::time(view::text(data, "nextRunAt"), "local")) " · " (repeat(data)) }
            blockquote { (view::text(data, "instruction").chars().take(120).collect::<String>()) }
        },
        _ => html! {},
    }
}

fn search(data: &Value) -> Markup {
    let classification = &data["classification"];
    let source = view::text(classification, "source");
    html! {
        p { strong { (view::number(view::count(data, "total_matches"))) " matches" }
            @if view::count(data, "pages") > 1 { " · page " (view::count(data, "page")) "/" (view::count(data, "pages")) }
            @if source == "rules" { " · classified by rules" }
            @else if !source.is_empty() { " · classified by the decision model (" (source) ")" }
            @if view::count(classification, "pending_decisions") > 0 { " · " (view::count(classification, "pending_decisions")) " awaiting the decision model" }
        }
        @if !view::list(data, "jobs").is_empty() { (jobs::result_table(data, true, true)) }
    }
}

fn index(data: &Value) -> Markup {
    let index = &data["index"];
    html! {
        p { (view::number(view::count(index, "jobs_active"))) " open jobs · " (view::number(view::count(index, "jobs_last_24h"))) " in the last 24 h · "
            (view::count(index, "companies_indexed")) "/" (view::count(data, "companies_available")) " companies indexed"
        }
        @if !data["scrape"]["active"].is_null() { p { (status::run_label(&data["scrape"]["active"])) } }
    }
}

fn scrape(data: &Value) -> Markup {
    let run = data
        .get("active")
        .filter(|run| !run.is_null())
        .or_else(|| data.get("run").filter(|run| !run.is_null()))
        .or_else(|| view::list(data, "recent").first())
        .or_else(|| data.get("companies_planned").map(|_| data));
    html! {
        @if let Some(run) = run {
            p { (view::text(run, "status")) " · " (view::count(run, "companies_done")) "/" (view::count(run, "companies_planned")) " companies · "
                (view::number(view::count(run, "new_jobs"))) " new jobs"
                @if !view::text(run, "started_at").is_empty() { " · started " (view::time(view::text(run, "started_at"), "widget")) }
            }
            (status::run_progress(run))
        } @else if let Some(reason) = data["reason"].as_str() { p { "Not started: " (reason) } }
        @else { p { "No scrape has run yet" } }
    }
}

fn profile(data: &Value) -> Markup {
    let skills = view::list(data, "skills");
    html! {
        p { @if !view::text(data, "name").is_empty() { strong { (view::text(data, "name")) } " · " }
            (data["skills_count"].as_u64().unwrap_or(skills.len() as u64)) " skills · "
            (view::list(data, "experience").len()) " roles · " (view::list(data, "projects").len()) " projects"
        }
        div.chips { @for skill in skills.iter().filter_map(Value::as_str).take(18) { span.chip { (skill) } } }
    }
}
