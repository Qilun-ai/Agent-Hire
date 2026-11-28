//! Routes backed by the process that owns the job-index workers and Kaggle manager.

use std::convert::Infallible;
use std::sync::Arc;

use agent_hire_jobindex::kaggle::{KaggleError, KaggleManager};
use axum::Json;
use axum::extract::{RawQuery, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::Value;

use crate::app_state::AppState;
use crate::respond;
use crate::validation::{self, DictBody, ModelBody, ModelReader, QueryReader};

type JsonResponse = Result<Json<Value>, Response>;

/// Reports index, scrape, decision and encoder status.
pub async fn status(State(state): State<Arc<AppState>>) -> JsonResponse {
    respond::blocking("job-index status", move || state.job_index_runtime.status())
        .await
        .map(Json)
}

/// Reports the number of active jobs processed by both encoders.
pub async fn progress(State(state): State<Arc<AppState>>) -> JsonResponse {
    respond::blocking("encoder progress", move || {
        state.job_index_runtime.progress()
    })
    .await
    .map(Json)
}

/// Classifies one requested batch using the configured decision model.
pub async fn classify(
    State(state): State<Arc<AppState>>,
    ModelBody(body): ModelBody,
) -> JsonResponse {
    let mut reader = ModelReader::new(&body);
    let limit = reader.integer("limit", Some(1), Some(400)).unwrap_or(96);
    reader.forbid_extra(&["limit"]);
    reader.finish()?;
    respond::blocking("classify jobs", move || {
        state.job_index_runtime.classify(limit)
    })
    .await
    .map(Json)
}

/// Probes the configured decision endpoints.
pub async fn probe(State(state): State<Arc<AppState>>) -> JsonResponse {
    respond::blocking("probe job decisions", move || {
        Ok::<_, Infallible>(state.job_index_runtime.probe())
    })
    .await
    .map(Json)
}

/// Reports notebook status, optionally bypassing the cache.
pub async fn kaggle(State(state): State<Arc<AppState>>, RawQuery(query): RawQuery) -> JsonResponse {
    let pairs = validation::query_pairs(query.as_deref());
    // The HTTP query parameter is `refresh`; the runtime calls it `force`.
    let name = if validation::query_value(&pairs, "refresh").is_some() {
        "refresh"
    } else {
        "force"
    };
    let mut reader = QueryReader::default();
    let force = reader.boolean(&pairs, name, false);
    reader.finish()?;
    respond::blocking("Kaggle status", move || {
        Ok::<_, Infallible>(state.job_index_runtime.kaggle().refresh(force))
    })
    .await
    .map(Json)
}

/// Connects to a supplied notebook link, or reuses the current link.
pub async fn kaggle_connect(
    State(state): State<Arc<AppState>>,
    ModelBody(body): ModelBody,
) -> JsonResponse {
    let mut reader = ModelReader::new(&body);
    let link = reader.string("link", Some(4000), false).unwrap_or_default();
    reader.forbid_extra(&["link"]);
    reader.finish()?;
    kaggle_operation("connect Kaggle", state, move |manager| {
        manager.connect(&link)
    })
    .await
}

/// Enables or disables inference through Kaggle.
pub async fn kaggle_toggle(
    State(state): State<Arc<AppState>>,
    ModelBody(body): ModelBody,
) -> JsonResponse {
    let mut reader = ModelReader::new(&body);
    let enabled = reader.required_boolean("enabled");
    reader.forbid_extra(&["enabled"]);
    reader.finish()?;
    let enabled = enabled.expect("validated required boolean");
    kaggle_operation("toggle Kaggle", state, move |manager| {
        manager.toggle(enabled)
    })
    .await
}

/// Chooses where the two encoders run.
pub async fn kaggle_encoder_device(
    State(state): State<Arc<AppState>>,
    DictBody(body): DictBody,
) -> JsonResponse {
    let device = body
        .get("device")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    kaggle_operation("set encoder device", state, move |manager| {
        manager.set_encoder_device(&device)
    })
    .await
}

/// Stops the notebook's model services.
pub async fn kaggle_stop(State(state): State<Arc<AppState>>) -> JsonResponse {
    kaggle_operation("stop Kaggle", state, KaggleManager::stop).await
}

/// Clears the saved notebook link and deployment credentials.
pub async fn kaggle_forget(State(state): State<Arc<AppState>>) -> JsonResponse {
    kaggle_operation("forget Kaggle", state, KaggleManager::forget).await
}

async fn kaggle_operation(
    context: &'static str,
    state: Arc<AppState>,
    work: impl FnOnce(&KaggleManager) -> Result<Value, KaggleError> + Send + 'static,
) -> JsonResponse {
    match tokio::task::spawn_blocking(move || work(state.job_index_runtime.kaggle())).await {
        Ok(Ok(value)) => Ok(Json(value)),
        Ok(Err(error)) => {
            let status = StatusCode::from_u16(error.status())
                .expect("Kaggle errors have valid HTTP statuses");
            if status.is_server_error() {
                Err(respond::internal_error(context, &error))
            } else {
                Err((status, Json(error.body())).into_response())
            }
        }
        Err(error) => Err(respond::internal_error(context, &error)),
    }
}
