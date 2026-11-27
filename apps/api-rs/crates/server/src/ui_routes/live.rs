//! One server event stream per UI page and the shared fragment refresh path.

use std::convert::Infallible;
use std::sync::Arc;
use std::time::Duration;

use agent_hire_ui::applications;
use agent_hire_ui::jobs;
use agent_hire_ui::settings;
use agent_hire_ui::shell;
use agent_hire_ui::status;
use axum::extract::{Query, State};
use axum::response::Response;
use axum::response::sse::{Event, KeepAlive, Sse};
use futures_util::stream;
use maud::{Markup, html};
use serde_json::Value;
use tokio::sync::{broadcast, watch};
use tokio::time::Instant;

use super::{PageQuery, data, markup};
use crate::app_state::AppState;
use crate::ui_routes::Rows;

#[derive(Default)]
struct Update {
    leads: bool,
    scrape: bool,
    profile: bool,
    message: String,
}

impl Update {
    fn receive(&mut self, message: &str) {
        let Ok(data) = serde_json::from_str::<Value>(message) else {
            return;
        };
        match data["type"].as_str().unwrap_or_default() {
            "LEAD_UPDATED" | "LEADS_CHANGED" | "LEADS_REFRESH" | "CART_UPDATED" => {
                self.leads = true
            }
            "scrape_progress" | "scrape_changed" => self.scrape = true,
            "ingest_resolved" => self.profile = true,
            "agent" => {
                let event = data["event"].as_str().unwrap_or_default();
                self.leads |= matches!(
                    event,
                    "scan_done"
                        | "reevaluate_done"
                        | "cleanup_done"
                        | "auto_discard_done"
                        | "email_outcomes"
                        | "applied"
                );
                if let Some(text) = data["msg"].as_str().or_else(|| data["message"].as_str()) {
                    self.message = text.to_owned();
                }
            }
            _ => {}
        }
    }

    fn lagged(&mut self) {
        self.leads = true;
        self.scrape = true;
        self.profile = true;
    }

    fn flags(&self) -> String {
        let mut names = Vec::new();
        if self.leads {
            names.push("leads");
        }
        if self.scrape {
            names.push("scrape");
        }
        if self.profile {
            names.push("profile");
        }
        names.join(",")
    }
}

struct Subscription {
    state: Arc<AppState>,
    receiver: broadcast::Receiver<String>,
    shutdown: watch::Receiver<bool>,
    query: PageQuery,
    deadline: Instant,
    initial: bool,
}

pub(super) async fn stream(
    State(state): State<Arc<AppState>>,
    Query(query): Query<PageQuery>,
) -> Sse<impl futures_util::Stream<Item = Result<Event, Infallible>>> {
    let stream = stream::unfold(
        Subscription {
            receiver: state.hub.subscribe(),
            shutdown: state.shutdown_requested(),
            state,
            query,
            deadline: Instant::now() + Duration::from_secs(300),
            initial: true,
        },
        async |mut subscription| {
            loop {
                if *subscription.shutdown.borrow() {
                    return None;
                }
                let mut update = Update::default();
                if subscription.initial {
                    subscription.initial = false;
                    update.leads = true;
                } else {
                    tokio::select! {
                        _ = subscription.shutdown.changed() => return None,
                        _ = tokio::time::sleep_until(subscription.deadline) => return None,
                        message = subscription.receiver.recv() => match message {
                            Ok(message) => update.receive(&message),
                            Err(broadcast::error::RecvError::Lagged(_)) => update.lagged(),
                            Err(broadcast::error::RecvError::Closed) => return None,
                        },
                    }
                    if update.flags().is_empty() && update.message.is_empty() {
                        continue;
                    }
                    // A bulk promotion or an application scan can publish several nearby updates.
                    tokio::time::sleep(Duration::from_millis(75)).await;
                    for _ in 0..256 {
                        match subscription.receiver.try_recv() {
                            Ok(message) => update.receive(&message),
                            Err(broadcast::error::TryRecvError::Lagged(_)) => update.lagged(),
                            Err(_) => break,
                        }
                    }
                }
                let fragments = render(
                    Arc::clone(&subscription.state),
                    &subscription.query,
                    &update.flags(),
                )
                .await;
                let markup = html! {
                    (fragments)
                    @if update.profile { span #profile-refresh data-refresh="profile" hidden {} }
                    @if update.scrape { span #companies-refresh data-refresh="scrape" hidden {} }
                    @if !update.message.is_empty() {
                        div #toast role="status" aria-live="polite" { (update.message) }
                    }
                }
                .into_string();
                let lines = markup
                    .lines()
                    .map(|line| format!("elements {line}"))
                    .collect::<Vec<_>>()
                    .join("\n");
                return Some((
                    Ok(Event::default()
                        .event("datastar-patch-elements")
                        .data(lines)),
                    subscription,
                ));
            }
        },
    );
    Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
}

pub(super) async fn refresh(
    State(state): State<Arc<AppState>>,
    Query(query): Query<PageQuery>,
) -> Response {
    markup(render(state, &query, &query.signals).await)
}

async fn render(state: Arc<AppState>, query: &PageQuery, signals: &str) -> Markup {
    let mut output = String::new();
    let flags: Vec<&str> = signals.split(',').collect();
    if flags.contains(&"leads") {
        let scope = query.pipeline();
        let rows = match query.tab() {
            shell::Tab::Jobs => Rows::Shortlist,
            shell::Tab::Applications => Rows::Applications(scope.clone()),
            _ => Rows::None,
        };
        if let Ok(pipeline) = data::pipeline(Arc::clone(&state), rows).await {
            let fragments = html! {
                (shell::navigation(query.tab(), &pipeline.badges()))
                @match query.tab() {
                    shell::Tab::Applications => {
                        (applications::analytics(&pipeline.summary, pipeline.cart))
                        (applications::table(&pipeline.view(&scope)))
                    }
                    shell::Tab::Jobs => {
                        div #job-views { (jobs::tabs(pipeline.leads.len(), query.shortlisted())) }
                        @if query.shortlisted() { (applications::shortlist(&pipeline.leads)) }
                    }
                    _ => {}
                }
            };
            output.push_str(&fragments.into_string());
        }
    }
    let (scrape, progress, kaggle, index) = tokio::join!(
        async {
            if flags.contains(&"scrape") {
                Some(data::scrape(Arc::clone(&state)).await)
            } else {
                None
            }
        },
        async {
            if flags.contains(&"classifiers") {
                Some(data::classification(Arc::clone(&state)).await)
            } else {
                None
            }
        },
        async {
            if flags.contains(&"kaggle") {
                Some(data::kaggle(Arc::clone(&state)).await)
            } else {
                None
            }
        },
        async {
            if query.tab() == shell::Tab::Jobs
                && (flags.contains(&"jobs-status") || flags.contains(&"scrape"))
            {
                Some(data::index(Arc::clone(&state)).await)
            } else {
                None
            }
        },
    );
    let fragments = html! {
        @if let Some(scrape) = scrape { (status::scrape_widget(&scrape)) }
        @if let Some(progress) = progress { (status::classifier_widget(&progress)) }
        @if let Some(kaggle) = kaggle {
            (status::notebook_widget(&kaggle))
            @if query.section == "job-agent" { (settings::notebook_setting(&kaggle)) }
        }
        @if let Some(index) = index { (status::jobs_strip(&index)) }
    };
    output.push_str(&fragments.into_string());
    maud::PreEscaped(output)
}

#[cfg(test)]
mod tests {
    use super::Update;

    #[test]
    fn coalesces_updates_and_handles_the_current_promotion_event() {
        let mut update = Update::default();
        update.receive(r#"{"type":"LEADS_CHANGED"}"#);
        update.receive(r#"{"type":"ingest_resolved"}"#);
        update.receive(r#"{"type":"agent","event":"scan_done","msg":"Scan complete"}"#);
        assert_eq!(update.flags(), "leads,profile");
        assert_eq!(update.message, "Scan complete");
        update.lagged();
        assert_eq!(update.flags(), "leads,scrape,profile");
    }
}
