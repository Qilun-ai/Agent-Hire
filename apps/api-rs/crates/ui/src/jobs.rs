//! Search filters, picker fragments and job index results.

use maud::{Markup, html};
use serde_json::Value;

use crate::applications;
use crate::status;
use crate::view;

const LEVELS: &[&str] = &["intern", "fresher", "junior", "mid", "senior", "lead"];

/// The fields carried through a job search and its pager.
#[derive(Clone, serde::Deserialize)]
#[serde(default)]
pub struct Search {
    /// Target role.
    pub role: String,
    /// Look-back window, in hours.
    pub since_hours: String,
    /// Comma-separated locations.
    pub locations: String,
    /// Comma-separated skill terms.
    pub keywords: String,
    /// Comma-separated title terms.
    pub title_keywords: String,
    /// Comma-separated company identifiers.
    pub company_slugs: String,
    /// Comma-separated department codes.
    pub departments: String,
    /// Comma-separated experience levels.
    pub levels: String,
    /// Whether to rank by profile skill match.
    pub match_profile: bool,
    /// Results page starting at one.
    pub page: u64,
}

impl Default for Search {
    fn default() -> Self {
        Self {
            role: "software engineer".to_owned(),
            since_hours: String::new(),
            locations: String::new(),
            keywords: String::new(),
            title_keywords: String::new(),
            company_slugs: String::new(),
            departments: String::new(),
            levels: String::new(),
            match_profile: false,
            page: 1,
        }
    }
}

impl Search {
    /// Constructs the existing job search input from the UI fields.
    pub fn request(&self) -> Value {
        serde_json::json!({
            "role": self.role, "since_hours": self.since_hours.parse::<u64>().ok(),
            "locations": split(&self.locations).collect::<Vec<_>>(),
            "keywords": split(&self.keywords).collect::<Vec<_>>(),
            "title_keywords": split(&self.title_keywords).collect::<Vec<_>>(),
            "company_slugs": split(&self.company_slugs).collect::<Vec<_>>(),
            "departments": split(&self.departments).collect::<Vec<_>>(),
            "levels": split(&self.levels).collect::<Vec<_>>(), "match_profile": self.match_profile,
            "sort": if self.match_profile { "match" } else { "recent" },
            "page": self.page.max(1), "page_size": 25, "use_decision_model": false,
        })
    }

    fn page_url(&self, page: u64) -> String {
        let query = url::form_urlencoded::Serializer::new(String::new())
            .extend_pairs([
                ("role", self.role.as_str()),
                ("since_hours", &self.since_hours),
                ("locations", &self.locations),
                ("keywords", &self.keywords),
                ("title_keywords", &self.title_keywords),
                ("company_slugs", &self.company_slugs),
                ("departments", &self.departments),
                ("levels", &self.levels),
                (
                    "match_profile",
                    if self.match_profile { "true" } else { "false" },
                ),
                ("page", &page.to_string()),
            ])
            .finish();
        format!("/jobs?{query}")
    }
}

fn split(input: &str) -> impl Iterator<Item = &str> {
    input
        .split(',')
        .map(str::trim)
        .filter(|term| !term.is_empty())
}

/// Renders the Jobs page with a search or the shortlist.
pub fn page(
    search: &Search,
    data: &Value,
    index: &Value,
    shortlist: &[Value],
    showing_shortlist: bool,
) -> Markup {
    html! {
        div.workspace {
            header.page-heading { div { h1 { "Jobs" } p { "Search company career sites and build your shortlist." } } }
            (status::jobs_strip(index))
            div #job-views { (tabs(shortlist.len(), showing_shortlist)) }
            @if showing_shortlist { (applications::shortlist(shortlist)) }
            @else {
                form.card.filters #job-search method="get" action="/jobs" {
                    div.field-grid {
                        (view::input("Role", "role", &search.role, "Software engineer"))
                        (view::select("Posted", "since_hours", &search.since_hours, &[("", "Any time"), ("24", "Last 24 hours"), ("72", "Last 3 days"), ("168", "Last week"), ("720", "Last month")]))
                        (view::input("Locations", "locations", &search.locations, "City, country, remote"))
                        (view::input("Skills / keywords", "keywords", &search.keywords, "Python, SQL"))
                        div title="Any listed word matches. A leading minus excludes a word."
                            { (view::input("Title has", "title_keywords", &search.title_keywords, "backend, python, -senior")) }
                        div.inline.pickers {
                            (picker("company_slugs", &search.company_slugs, "All companies", "/ui/pickers/companies", ""))
                            (picker("departments", &search.departments, "All departments", "/ui/pickers/departments", "Filter by the department model’s verdict. Jobs it was unsure about are excluded."))
                        }
                    }
                    div.search-options {
                        fieldset.chips { legend { "Experience level" }
                            input type="hidden" name="levels" value=(&search.levels);
                            @for &level in LEVELS {
                                label.choice { input type="checkbox" data-level=(level) checked[split(&search.levels).any(|term| term == level)]; (level) }
                            }
                        }
                        label.check { input type="checkbox" name="match_profile" value="true" checked[search.match_profile]; "Match my skills" }
                        button.primary { "Search" }
                    }
                    output aria-live="polite" {}
                }
                (results(search, data, view::count(&index["index"], "jobs_active") > 0))
            }
        }
    }
}

/// Navigation between the job views and the current shortlist count.
pub fn tabs(size: usize, shortlist: bool) -> Markup {
    html! {
        nav.chips.subnav aria-label="Job views" {
            a.chip href="/jobs" aria-current=[(!shortlist).then_some("page")] { "All scraped jobs" }
            a.chip href="/jobs?view=shortlisted" aria-current=[shortlist.then_some("page")] { "Shortlisted (" (size) ")" }
        }
    }
}

fn picker(name: &str, selected: &str, label: &str, url: &str, title: &str) -> Markup {
    let picked = split(selected).count();
    html! {
        input type="hidden" name=(name) value=(selected);
        button.secondary type="button" data-popover=(url) data-picker=(name) data-empty=(label) title=(title) {
            @if picked > 0 { (picked) " selected" } @else { (label) }
        }
    }
}

/// Renders a filtered results page for the Jobs screen.
pub fn results(search: &Search, data: &Value, index_has_jobs: bool) -> Markup {
    let page = view::count(data, "page").max(1);
    let pages = view::count(data, "pages");
    let classification = view::text(&data["classification"], "source");
    html! {
        section.card #job-results {
            @if !data["detail"].is_null() {
                p.empty.bad role="alert" { (data["detail"].as_str().unwrap_or("Search failed")) }
            } @else {
                form method="post" data-action="/ui/actions/jobs/promote" data-effects="leads search" data-busy="Adding…" {
                    header.section-bar.inline {
                        h2 { (view::number(view::count(data, "total_matches"))) " jobs"
                            @if !classification.is_empty() && classification != "rules" { small { " · classified by " (classification) } }
                        }
                        button.primary data-count-jobs disabled { "Add to applications" }
                        output aria-live="polite" {}
                    }
                    @if view::list(data, "jobs").is_empty() {
                        div.empty { h3 { "No jobs match." } p { @if index_has_jobs { "Loosen the filters." } @else { "Start a scrape to fill the index." } } }
                    } @else {
                        (result_table(data, search.match_profile, false))
                        p.footnote { "* No published date — this is when Agent-Hire first saw the posting." }
                    }
                }
                @if pages > 1 {
                    nav.pager aria-label="Search results pages" {
                        @if page > 1 { a.button.secondary href=(search.page_url(page - 1)) data-job-page { "Previous" } }
                        span { "Page " (page) " of " (pages) }
                        @if page < pages { a.button.secondary href=(search.page_url(page + 1)) data-job-page { "Next" } }
                    }
                }
            }
        }
    }
}

pub(crate) fn result_table(data: &Value, skills: bool, compact: bool) -> Markup {
    html! {
        div.table-scroll role=[(!compact).then_some("region")]
            aria-label=[(!compact).then_some("Job search results")] tabindex=[(!compact).then_some("0")] {
            table class=[(!compact).then_some("job-table")] {
                thead { tr {
                    @if !compact { th scope="col" { span.sr-only { "Select" } } }
                    th scope="col" { "Role" } th scope="col" { "Company" }
                    @if !compact { th scope="col" { "Department" } }
                    th scope="col" { "Location" } th scope="col" { "Level" } th scope="col" { "Posted" }
                    th scope="col" { @if compact { "Skills matched" } @else if skills { "Your skills it wants" } @else { "Skills" } }
                } }
                tbody {
                    @for job in view::list(data, "jobs") {
                        @let saved = job["in_pipeline"] == true;
                        @let first_seen = view::text(job, "posted_basis") == "first_seen";
                        @let title = view::text(job, "title");
                        tr class=[(!compact && saved).then_some("dimmed")] {
                            @if !compact {
                                td { input type="checkbox" name="job_refs" value=(view::text(job, "job_ref")) disabled[saved] aria-label=(format!("Select {title}")); }
                            }
                            td.title-cell { (view::external(title, view::text(job, "url"))) @if !compact && saved { " " span.badge { "Shortlisted" } } }
                            td class=[(!compact).then_some("company-cell")] { (view::text(job, "company")) }
                            @if !compact {
                                td title=(job["department_code"].as_str().unwrap_or("Not classified yet, or the model was unsure")) { (job["department"].as_str().unwrap_or("—")) }
                            }
                            td { (view::text(job, "location")) @if let Some(more) = job["also_posted_in"].as_u64() { span title="The same opening is also posted in other locations" { " · +" (more) " more" } } }
                            td { (view::text(job, "level")) @if let Some(years) = job["min_years"].as_f64() { " · " (years) "+y" } }
                            td title=(if first_seen { if compact { "First seen by Agent-Hire" } else { "When Agent-Hire first saw it" } } else { "Published by the company" }) {
                                (view::time(view::text(job, "posted"), if compact { "chat" } else { "jobs" })) @if first_seen { "*" }
                            }
                            td { (skill_summary(job, if skills { "matched_skills" } else { "top_skills" }, if compact { 4 } else { 5 })) }
                        }
                    }
                }
            }
        }
    }
}

fn skill_summary(job: &Value, key: &str, limit: usize) -> String {
    view::list(job, key)
        .iter()
        .filter_map(Value::as_str)
        .take(limit)
        .collect::<Vec<_>>()
        .join(", ")
}

/// Renders the lazily opened company picker.
pub fn company_picker(data: &Value, selected: &str) -> Markup {
    let chosen: Vec<&str> = split(selected).collect();
    let mut companies: Vec<&Value> = view::list(data, "companies")
        .iter()
        .filter(|row| view::count(row, "active_jobs") > 0)
        .collect();
    companies.sort_by_key(|row| {
        (
            !chosen.contains(&view::text(row, "slug")),
            std::cmp::Reverse(view::count(row, "active_jobs")),
            view::text(row, "company").to_lowercase(),
        )
    });
    html! {
        section data-picker-panel="company_slugs" {
            h2 { "Companies" }
            input type="search" autofocus aria-label="Find a company" placeholder="Find a company…" data-filter;
            div.pick-list {
                @for company in companies {
                    @let slug = view::text(company, "slug");
                    @let name = view::text(company, "company");
                    label.check data-filter-row=(name) { input type="checkbox" value=(slug) data-label=(name) data-pick checked[chosen.contains(&slug)]; (name) span.muted { (view::number(view::count(company, "active_jobs"))) } }
                }
                p data-no-match { "No company matches." }
            }
            (picker_footer(chosen.len()))
        }
    }
}

/// Renders department groups, each with its selectable subdepartments.
pub fn department_picker(data: &Value, selected: &str) -> Markup {
    let chosen: Vec<&str> = split(selected).collect();
    html! {
        section data-picker-panel="departments" {
            h2 { "Departments" }
            @if !data["departments"].is_array() || data["department_model"] == false {
                p { "The department list isn't available yet. It appears after Agent-Hire restarts with the department model." }
            }
            div.pick-list {
                @for department in view::list(data, "departments") {
                    @let subs = view::list(department, "subdepartments");
                    fieldset data-pick-group {
                        label.check { input type="checkbox" data-group-check data-label=(view::text(department, "name")); (view::text(department, "name")) span.muted { (view::count(department, "count")) } }
                        div.subchoices hidden[subs.len() == 1] {
                            @for sub in subs {
                                @let code = view::text(sub, "code");
                                @let name = view::text(sub, "name");
                                label.check { input type="checkbox" value=(code) data-label=(name) data-pick checked[chosen.contains(&code)]; (name) span.muted { (view::count(sub, "count")) } }
                            }
                        }
                    }
                }
            }
            (picker_footer(chosen.len()))
        }
    }
}

fn picker_footer(count: usize) -> Markup {
    html! {
        footer.inline {
            span.muted data-picked aria-live="polite" { @if count == 0 { "None picked = all" } @else { (count) " picked" } }
            button.quiet type="button" data-clear-picker { "Clear" }
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{Search, page, results};

    #[test]
    fn search_normalizes_csv_and_sets_fixed_options() {
        let search = Search {
            locations: " Delhi, , Remote ".to_owned(),
            match_profile: true,
            page: 0,
            ..Search::default()
        };
        let body = search.request();
        assert_eq!(body["locations"], json!(["Delhi", "Remote"]));
        assert_eq!(body["page"], 1);
        assert_eq!(body["page_size"], 25);
        assert_eq!(body["sort"], "match");
        assert_eq!(body["use_decision_model"], false);
    }

    #[test]
    fn jobs_render_before_script_and_escape_filters() {
        let search = Search {
            role: "\"<script>".to_owned(),
            ..Search::default()
        };
        let rendered = page(&search, &json!({}), &json!({}), &[], false).into_string();
        assert!(rendered.contains("All scraped jobs"));
        assert!(rendered.contains("&quot;&lt;script&gt;"));
        assert!(rendered.contains("No jobs match."));
    }

    #[test]
    fn shortlist_button_starts_disabled_without_a_zero_count() {
        let rendered = results(
            &Search::default(),
            &json!({"jobs": [{"job_ref": "example", "title": "Software engineer"}]}),
            true,
        )
        .into_string();
        assert!(rendered.contains("data-count-jobs disabled>Add to applications</button>"));
    }
}
