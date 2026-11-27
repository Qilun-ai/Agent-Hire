//! Application analytics, pipeline rows and the shared lead drawer.

mod funnel;

use maud::{Markup, html};
use serde_json::Value;

use self::funnel::Funnel;
use crate::view;

const STAGES: &[(&str, &str)] = &[
    ("discovered", "Discovered"),
    ("evaluating", "Evaluating"),
    ("tailoring", "Tailoring"),
    ("approved", "Approved"),
    ("applied", "Applied"),
    ("interviewing", "Interviewing"),
    ("accepted", "Accepted"),
    ("rejected", "Rejected"),
    ("discarded", "Discarded"),
];

/// Scope and paging for the pipeline view.
#[derive(Clone)]
pub struct PipelineQuery {
    /// A specific application stage, or all tracked stages.
    pub stage: String,
    /// Number of rows to show before offering another page.
    pub limit: usize,
}

impl PipelineQuery {
    /// Recognizes the application stages exposed by the page.
    pub fn new(stage: &str, limit: usize) -> Self {
        let stage = if STAGES[4..].iter().any(|&(key, _)| key == stage) {
            stage
        } else {
            "all"
        };
        Self {
            stage: stage.to_owned(),
            limit: limit.clamp(100, 100_000),
        }
    }

    /// Whether an application with this status belongs in the list.
    pub fn includes(&self, status: &str) -> bool {
        STAGES[4..].iter().any(|&(key, _)| key == status)
            && (self.stage == "all" || self.stage == status)
    }
}

/// Whether a pipeline stage belongs on the shortlist.
pub fn shortlisted(status: &str) -> bool {
    STAGES[..4].iter().any(|&(key, _)| key == status)
}

/// Server-rendered pipeline data.
pub struct Pipeline<'a> {
    /// Summary covering all leads, even while the list is filtered.
    pub summary: &'a Value,
    /// Number of jobs in the extension queue.
    pub cart: u64,
    /// The rows in the current view.
    pub leads: &'a [Value],
    /// The selected filter and page size.
    pub query: &'a PipelineQuery,
    /// Whether the list extends past the visible rows.
    pub more: bool,
}

/// Renders the application page.
pub fn page(pipeline: &Pipeline<'_>) -> Markup {
    html! {
        div.workspace {
            header.page-heading { div { h1 { "Applications" } p { "Track replies, interviews and outcomes." } }
                form method="post" data-action="/ui/actions/email/poll?since_days=14" data-busy="Checking…" data-effects="leads" {
                    button.secondary { "Check email now" }
                    output aria-live="polite" {}
                }
            }
            (analytics(pipeline.summary, pipeline.cart))
            (table(pipeline))
        }
    }
}

/// Renders the headline tiles and funnel.
pub fn analytics(summary: &Value, cart: u64) -> Markup {
    let counts = &summary["counts"];
    let funnel = Funnel::new(counts);
    let percent = |value: u64| (100.0 * value as f64 / funnel.total.max(1) as f64).round() as u64;
    let moved = funnel.total - funnel.awaiting;
    html! {
        section #application-analytics aria-label="Application overview" {
            div.stat-grid {
                (tile("Jobs saved", &view::number(view::count(summary, "total")), "found and kept", ""))
                (tile("In apply cart", &view::number(cart), "set aside to apply", ""))
                (tile("Hidden", &view::number(view::count(counts, "discarded")), "not interested", ""))
            }
            @if funnel.total > 0 {
                div.stat-grid.pipeline-stats {
                    (tile("Total applications", &view::number(funnel.total), "tracked in your pipeline", ""))
                    (tile("Awaiting reply", &view::number(funnel.awaiting), &format!("{}% of applications", percent(funnel.awaiting)), ""))
                    (tile("Response rate", &format!("{}%", percent(moved)), &format!("{moved} of {} moved forward", funnel.total), ""))
                    (tile("In interviews", &view::number(funnel.interviewing), &format!("{}% of applications", percent(funnel.interviewing)), ""))
                    (tile("Outcomes", &format!("{}↑ {}↓", funnel.accepted, funnel.rejected), &format!("{} accepted · {} rejected", funnel.accepted, funnel.rejected), if funnel.rejected > funnel.accepted { "warn" } else { "ok" }))
                }
                (funnel.chart())
            } @else {
                div.empty.card { h2 { "No applications yet" } p { "Applications you submit through the extension and status emails the agent reads will show up here." } }
            }
        }
    }
}

fn tile(label: &str, value: &str, note: &str, tone: &str) -> Markup {
    html! { div.stat.card { span { (label) } strong class=(tone) { (value) } small { (note) } } }
}

/// Renders the stage filters and current application rows.
pub fn table(pipeline: &Pipeline<'_>) -> Markup {
    let counts = &pipeline.summary["counts"];
    let funnel = Funnel::new(counts);
    let discarded = view::count(counts, "discarded");
    let query = pipeline.query;
    html! {
        section.card #applications-table {
            header.section-bar {
                h2 { "Tracked applications" }
                nav.chips aria-label="Filter by stage" {
                    @for (key, label, count) in [
                        ("all", "All", funnel.total + discarded),
                        ("applied", "Applied", funnel.awaiting),
                        ("interviewing", "Interviewing", funnel.interviewing),
                        ("accepted", "Accepted", funnel.accepted),
                        ("rejected", "Rejected", funnel.rejected),
                        ("discarded", "Discarded", discarded),
                    ] {
                        a.chip href=(format!("/applications?stage={key}")) aria-current=[(query.stage == key).then_some("page")] { (label) " (" (view::number(count)) ")" }
                    }
                }
            }
            @if pipeline.leads.is_empty() {
                p.empty { "No applications here yet. Applications you submit through the extension and status emails the agent reads will show up here." }
            } @else {
                (rows(pipeline.leads, false))
            }
            @if pipeline.more {
                a.button.secondary.load-more href=(format!("/applications?stage={}&limit={}", query.stage, query.limit + 100)) { "Load more" }
            }
        }
    }
}

/// Renders the table of leads in pre-application stages.
pub fn shortlist(leads: &[Value]) -> Markup {
    html! {
        section.card #shortlisted-table {
            @if leads.is_empty() {
                p.empty { "Nothing here yet. Ask the agent to shortlist jobs, or tick jobs in “All scraped jobs”." }
            } @else { (rows(leads, true)) }
        }
    }
}

fn rows(leads: &[Value], shortlist: bool) -> Markup {
    html! {
        div.table-scroll role="region" aria-label=(if shortlist { "Shortlisted jobs" } else { "Application rows" }) tabindex="0" {
            table.pipeline-table {
                thead { tr { th scope="col" { "Role" } th scope="col" { "Company" } th scope="col" { "Stage" }
                    th scope="col" { @if shortlist { "Résumé" } @else { "Updated" } }
                    th scope="col" { "Actions" }
                } }
                tbody {
                    @for lead in leads {
                        @let url = route(lead);
                        @let status = view::text(lead, "status");
                        tr data-row-open=(&url) {
                            td.title-cell {
                                (view::external(view::text(lead, "title"), view::text(lead, "url")))
                                button.quiet.row-detail type="button" data-open=(&url) aria-label=(format!("Details for {}", view::text(lead, "title"))) { (view::icon("arrow")) }
                            }
                            td.company-cell { (view::text(lead, "company")) }
                            td { (stage_badge(status)) }
                            td {
                                @if shortlist { (resume(lead, "not tailored")) }
                                @else { (last_updated(lead)) }
                            }
                            td {
                                @if shortlist {
                                    form.inline method="post" data-action=(format!("{url}/status")) data-method="PUT" data-effects="leads" {
                                        button.secondary name="status" value="applied" { "Mark applied" }
                                        button.quiet name="status" value="discarded" { "Discard" }
                                        output aria-live="polite" {}
                                    }
                                } @else { (stage_form(lead, false)) }
                            }
                        }
                    }
                }
            }
        }
    }
}

fn stage_badge(status: &str) -> Markup {
    let label = STAGES
        .iter()
        .find(|&&(key, _)| key == status)
        .map_or(status, |&(_, label)| label);
    html! { span class=(format!("badge stage-{status}")) { (label) } }
}

fn stage_form(lead: &Value, expanded: bool) -> Markup {
    let status = view::text(lead, "status");
    let options = if expanded { STAGES } else { &STAGES[4..] };
    html! {
        form method="post" data-action=(format!("{}/status", route(lead))) data-method="PUT" data-effects="leads" {
            select aria-label="Change application stage" name="status" data-submit {
                @for &(value, label) in options { option value=(value) selected[value == status] { (label) } }
            }
            output aria-live="polite" {}
        }
    }
}

fn route(lead: &Value) -> String {
    format!("/ui/leads/{}", view::segment(view::text(lead, "job_id")))
}

fn last_updated(lead: &Value) -> &str {
    view::text(lead, "updated_at").get(..10).unwrap_or("—")
}

fn resume(lead: &Value, empty: &str) -> Markup {
    html! {
        @if ["resume_asset", "asset"].iter().any(|key| !view::text(lead, key).is_empty()) {
            a href=(format!("{}/pdf", route(lead))) data-pdf target="_blank" rel="noreferrer noopener" { "View PDF" }
        } @else { span.muted { (empty) } }
    }
}

/// Renders job detail, fit, feedback and history for a single lead.
pub fn detail(lead: &Value) -> Markup {
    let route = route(lead);
    let description = view::text(lead, "description");
    let mut events: Vec<&Value> = view::list(lead, "events").iter().collect();
    events.sort_by_key(|event| std::cmp::Reverse(view::text(event, "ts")));
    html! {
        article #lead-detail {
            header.drawer-heading {
                h2 { (view::text(lead, "title")) }
                p { (view::text(lead, "company")) }
                @if let Some(url) = view::safe_url(view::text(lead, "url")) { (view::external("Open posting", url)) }
            }
            section { h3 { "Application stage" } (stage_form(lead, true)) }
            @if !lead["score"].is_null() || !view::text(lead, "reason").is_empty() {
                section { h3 { "Fit" } p { @if !lead["score"].is_null() { strong { (lead["score"].as_str().map_or_else(|| lead["score"].to_string(), str::to_owned)) } " · " } (view::text(lead, "reason")) } }
            }
            @for (key, title) in [("match_points", "Matches"), ("gaps", "Gaps")] {
                @if !view::list(lead, key).is_empty() {
                    section { h3 { (title) } ul { @for item in view::list(lead, key) { li { (item.as_str().unwrap_or_default()) } } } }
                }
            }
            @if !description.is_empty() {
                section { h3 { "Description" }
                    p.description id="lead-description" { (description) }
                    button.quiet type="button" data-expand="lead-description" aria-expanded="false" { "Show more" }
                }
            }
            section { h3 { "Résumé" } (resume(lead, "not tailored yet")) }
            section {
                h3 { "Feedback" }
                form method="post" data-action=(format!("{route}/feedback")) data-method="PUT" {
                    (view::input("Optional note", "note", view::text(lead, "feedback_note"), ""))
                    div.inline {
                        button.secondary name="feedback" value="good" { "Good fit" }
                        button.secondary name="feedback" value="bad" { "Bad fit" }
                    }
                    output aria-live="polite" { (view::text(lead, "feedback")) }
                }
            }
            section {
                h3 { "Follow-up reminder" }
                form.inline method="post" data-action=(format!("{route}/followup")) data-method="PUT" {
                    label { "Remind me in " input type="number" name="days" min="1" max="365" value="7" required; " days" }
                    button.secondary { "Set reminder" }
                    output aria-live="polite" {}
                }
            }
            @if !events.is_empty() {
                section { h3 { "Timeline" }
                    ul.timeline { @for event in events { li { span { (view::text(event, "ts").get(..10).unwrap_or("—")) } (view::text(event, "action")) } } }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{Pipeline, PipelineQuery, detail, page, shortlist, shortlisted};

    #[test]
    fn legacy_ids_are_encoded_in_drawer_links_and_actions() {
        let lead = json!({
            "job_id": "fractal:/job/1001?ref=%2F#tab",
            "status": "discovered",
            "resume_asset": "resume.pdf"
        });
        let route = "/ui/leads/fractal%3A%2Fjob%2F1001%3Fref%3D%252F%23tab";
        let list = shortlist(std::slice::from_ref(&lead)).into_string();
        let drawer = detail(&lead).into_string();
        assert!(list.contains(&format!("data-open=\"{route}\"")));
        for rendered in [&list, &drawer] {
            assert!(rendered.contains(&format!("data-action=\"{route}/status\"")));
            assert!(rendered.contains(&format!("href=\"{route}/pdf\"")));
        }
        for action in ["feedback", "followup"] {
            assert!(drawer.contains(&format!("data-action=\"{route}/{action}\"")));
        }
    }

    #[test]
    fn stage_filters_match_only_the_tracked_statuses() {
        let query = PipelineQuery::new("invalid", 10);
        assert_eq!(query.limit, 100);
        assert!(query.includes("discarded"));
        assert!(!query.includes("approved"));
        assert!(shortlisted("approved"));
        assert!(!shortlisted("discarded"));
        assert!(!PipelineQuery::new("rejected", 100).includes("interviewing"));
    }

    #[test]
    fn application_page_escapes_lead_fields_and_filters_links() {
        let summary = json!({"counts":{"applied":1}});
        let leads = [json!({"title":"<script>", "url":"javascript:alert(1)", "status":"applied"})];
        let query = PipelineQuery::new("all", 100);
        let view = page(&Pipeline {
            summary: &summary,
            cart: 0,
            leads: &leads,
            query: &query,
            more: false,
        })
        .into_string();
        assert!(view.contains("Application funnel"));
        assert!(view.contains("class=\"badge stage-applied\""));
        assert!(view.contains("<use href=\"#icon-arrow\""));
        assert!(view.contains("&lt;script&gt;"));
        assert!(!view.contains("javascript:"));
    }
}
