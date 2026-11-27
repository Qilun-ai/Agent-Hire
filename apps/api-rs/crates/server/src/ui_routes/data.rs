//! Adapts backend data to the page and fragment views in process.

use std::sync::Arc;

use agent_hire_store::LeadFilters;
use agent_hire_ui::applications::{self, PipelineQuery};
use agent_hire_ui::jobs::Search;
use agent_hire_ui::settings::{Section, Settings};
use agent_hire_ui::shell::Badges;
use axum::body::{Bytes, to_bytes};
use axum::extract::{RawQuery, State};
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};

use crate::app_state::AppState;
use crate::cart_routes;
use crate::email_routes;
use crate::environment_settings;
use crate::events_routes;
use crate::jobindex_routes;
use crate::jobindex_runtime_routes;
use crate::respond;
use crate::settings_routes;

/// Reuses the existing route validation and services without forwarding a request over HTTP.
pub(super) async fn json_response(response: impl IntoResponse) -> Value {
    let response = response.into_response();
    let bytes = to_bytes(response.into_body(), 32 * 1024 * 1024).await;
    bytes
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_else(|| json!({"detail": "Could not load data"}))
}

pub(super) async fn search(state: Arc<AppState>, search: &Search) -> Value {
    let input = Bytes::from(search.request().to_string());
    json_response(jobindex_routes::search(State(state), input).await).await
}

pub(super) async fn index(state: Arc<AppState>) -> Value {
    json_response(jobindex_runtime_routes::status(State(state)).await).await
}

pub(super) async fn scrape(state: Arc<AppState>) -> Value {
    json_response(jobindex_routes::scrape_status(State(state), RawQuery(None)).await).await
}

pub(super) async fn classification(state: Arc<AppState>) -> Value {
    json_response(jobindex_runtime_routes::progress(State(state)).await).await
}

pub(super) async fn notebook(state: Arc<AppState>) -> Value {
    tokio::task::spawn_blocking(move || state.job_index_runtime.kaggle().public_status())
        .await
        .unwrap_or_default()
}

pub(super) async fn kaggle(state: Arc<AppState>) -> Value {
    json_response(jobindex_runtime_routes::kaggle(State(state), RawQuery(None)).await).await
}

pub(super) async fn settings(state: Arc<AppState>, section: Section) -> Settings {
    let mut data = Settings::default();
    if matches!(section, Section::Ai | Section::JobAgent) {
        let (values, environment, previews) = tokio::join!(
            settings_routes::get_settings(State(Arc::clone(&state))),
            environment_settings::get_environment(State(Arc::clone(&state))),
            settings_routes::secret_previews(State(Arc::clone(&state))),
        );
        data.values = json_response(values).await;
        data.environment = json_response(environment).await;
        data.previews = json_response(previews).await;
    }
    match section {
        Section::Ai => {}
        Section::Email => {
            data.email = json_response(email_routes::status(State(state)).await).await;
        }
        Section::JobAgent => {
            (data.index, data.kaggle) = tokio::join!(index(Arc::clone(&state)), notebook(state));
        }
        Section::General => {
            let query = RawQuery(Some("limit=200".to_owned()));
            data.activity = json_response(events_routes::list(State(state), query).await).await;
        }
    }
    data
}

pub(super) struct Pipeline {
    pub summary: Value,
    pub cart: u64,
    pub leads: Vec<Value>,
    pub more: bool,
}

impl Pipeline {
    pub(super) fn badges(&self) -> Badges {
        Badges {
            cart: self.cart,
            applied: self.summary["counts"]["applied"]
                .as_u64()
                .unwrap_or_default(),
        }
    }

    pub(super) fn view<'a>(&'a self, query: &'a PipelineQuery) -> applications::Pipeline<'a> {
        applications::Pipeline {
            summary: &self.summary,
            cart: self.cart,
            leads: &self.leads,
            query,
            more: self.more,
        }
    }
}

#[derive(Clone)]
pub(super) enum Rows {
    None,
    Shortlist,
    Applications(PipelineQuery),
}

pub(super) async fn pipeline(state: Arc<AppState>, rows: Rows) -> Result<Pipeline, Response> {
    respond::blocking("load UI pipeline", move || {
        let filters = LeadFilters::default();
        let summary = state.leads.summary(&filters)?;
        let cart = cart_routes::read_cart(&state.settings).len() as u64;
        let mut leads = Vec::new();
        let mut more = false;
        if !matches!(rows, Rows::None) {
            let records = state.leads.filtered_leads(&filters)?;
            let relevant = |status: &str| match &rows {
                Rows::None => false,
                Rows::Shortlist => applications::shortlisted(status),
                Rows::Applications(query) => query.includes(status),
            };
            for record in records {
                if !relevant(
                    record
                        .get("status")
                        .and_then(Value::as_str)
                        .unwrap_or_default(),
                ) {
                    continue;
                }
                if matches!(&rows, Rows::Applications(query) if leads.len() >= query.limit) {
                    more = true;
                    break;
                }
                leads.push(Value::Object(record));
            }
        }
        if matches!(rows, Rows::Applications(_)) {
            let ids: Vec<&str> = leads
                .iter()
                .map(|lead| lead["job_id"].as_str().unwrap_or_default())
                .collect();
            let timestamps = state.events.latest_timestamps(&ids)?;
            for (lead, timestamp) in leads.iter_mut().zip(timestamps) {
                if let Some(timestamp) = timestamp {
                    lead["updated_at"] = Value::String(timestamp);
                }
            }
        }
        Ok::<_, agent_hire_store::LeadError>(Pipeline {
            summary,
            cart,
            leads,
            more,
        })
    })
    .await
}
