//! The scrape controls and company speed groups.

use maud::{Markup, html};
use serde_json::Value;

use crate::status;
use crate::view;

/// Renders the company drawer.
pub fn panel(data: &Value) -> Markup {
    let companies = view::list(data, "companies");
    let verified = |row: &Value| row["status"] == "verified";
    let concurrency = view::count(data, "concurrency").max(1);
    let active = !data["active_run"].is_null();
    let verified_count = companies.iter().filter(|row| verified(row)).count();
    let mut unavailable: Vec<&Value> = companies.iter().filter(|row| !verified(row)).collect();
    let groups = [
        "no_listing",
        "blocked",
        "merged",
        "verification",
        "skipped",
        "review",
    ];
    unavailable.sort_by_key(|row| {
        (
            groups
                .iter()
                .position(|&key| key == view::text(row, "unavailable_group"))
                .unwrap_or(groups.len()),
            view::text(row, "company").to_lowercase(),
        )
    });
    html! {
        article #companies-panel data-concurrency=(concurrency) data-active=(active) {
            header.drawer-heading {
                h2 { "Companies" }
                p { (view::number(verified_count as u64)) " scrapeable · grouped by time per company · " (concurrency) " scraped in parallel" }
            }
            @if !data["detail"].is_null() {
                p.bad role="alert" { "Could not load companies." }
            }
            @if active {
                div.notice.inline {
                    div { (status::run_label(&data["active_run"])) " jobs" (status::run_progress(&data["active_run"])) }
                    (status::scrape_action(true, "Stop"))
                }
            }
            div.inline.company-tools {
                input type="search" data-filter aria-label="Find a company" placeholder="Find a company…";
                form method="post" data-action="/ui/actions/scrape" data-effects="scrape" {
                    button.secondary disabled[active] { "Scrape all" }
                    output aria-live="polite" {}
                }
            }
            @for category in view::list(data, "categories") {
                @let key = view::text(category, "key");
                @let mut rows: Vec<&Value> = companies.iter().filter(|row| verified(row) && row["category"] == key).collect();
                @let () = rows.sort_by(|a, b| seconds(a).total_cmp(&seconds(b)));
                @if !rows.is_empty() {
                    details.company-group id=(format!("company-group-{}", view::segment(key))) {
                        summary {
                            span class=(format!("dot speed-{key}")) {}
                            (view::text(category, "label")) " "
                            span.muted { (view::text(category, "range")) " · " (rows.len()) }
                        }
                        @if key != "unknown" {
                            form.inline.group-action data-action="/ui/actions/scrape" data-effects="scrape"
                                title=(format!("About {} with {concurrency} in parallel (the slowest company takes {})", duration(view::count(category, "est_wall_seconds") as f64), duration(view::count(category, "longest_seconds") as f64))) {
                                button.secondary name="categories" value=(key) disabled[active] { "Scrape · ~" (duration(view::count(category, "est_wall_seconds") as f64)) }
                                output aria-live="polite" {}
                            }
                        }
                        ul { @for row in rows { (company(row, true)) } }
                    }
                }
            }
            @if !unavailable.is_empty() {
                details.company-group #company-group-unavailable {
                    summary { span.dot {} "Not scraped " span.muted { (unavailable.len()) " · why each one is unavailable" } }
                    ul { @for row in unavailable { (company(row, false)) } }
                }
            }
            p.empty data-no-match hidden[!companies.is_empty()] { "No companies match." }
            footer {
                p.muted { "* Time recorded when the scraper was verified. Your measured times replace it after a scrape." }
                form.inline method="post" data-action="/ui/actions/scrape" data-effects="scrape" data-company-selection {
                    button.primary disabled { "Scrape 0 selected" }
                    output aria-live="polite" {}
                }
            }
        }
    }
}

fn company(row: &Value, verified: bool) -> Markup {
    let name = view::text(row, "company");
    let slug = view::text(row, "slug");
    let measured = row["est_basis"] == "measured";
    let jobs = view::count(row, "active_jobs");
    let run_state = view::text(row, "run_state");
    html! {
        li.company-row data-filter-row=(format!("{name} {slug}")) {
            div {
                @if verified {
                    label.check title=(view::text(row, "careers_url")) {
                        input type="checkbox" value=(slug) data-company=(seconds(row)); (name)
                    }
                } @else {
                    span title=(view::text(row, "careers_url")) { (name) }
                }
            }
            @if verified {
                span title=(if measured { "Measured on your last scrape" } else { "Recorded when the scraper was verified" }) {
                    @if seconds(row) > 0.0 { (duration(seconds(row))) @if !measured { "*" } } @else { "—" }
                }
                span { @if jobs > 0 { (view::number(jobs)) " jobs" } @else { "—" } }
                span { @if matches!(run_state, "done" | "queued") { span.badge { (run_state) } } @else { (view::time(view::text(row, "last_import_at"), "never")) } }
            } @else {
                span.muted title=(view::text(row, "review_note")) { (row["unavailable_reason"].as_str().filter(|text| !text.is_empty()).unwrap_or("Waiting for review")) }
            }
        }
    }
}

fn seconds(data: &Value) -> f64 {
    data["est_seconds"].as_f64().unwrap_or_default()
}

fn duration(seconds: f64) -> String {
    if seconds < 60.0 {
        format!("{seconds:.0}s")
    } else if seconds < 3600.0 {
        format!("{:.0} min", seconds / 60.0)
    } else if seconds < 36_000.0 {
        format!("{:.1}h", seconds / 3600.0)
    } else {
        format!("{:.0}h", seconds / 3600.0)
    }
}
