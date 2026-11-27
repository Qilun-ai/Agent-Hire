//! Server-rendered pages, fragments and content-addressed UI assets.

mod actions;
mod data;
mod live;

use std::sync::Arc;

use agent_hire_ui::applications::{self, PipelineQuery};
use agent_hire_ui::assets;
use agent_hire_ui::chat;
use agent_hire_ui::companies;
use agent_hire_ui::jobs::{self, Search};
use agent_hire_ui::profile;
use agent_hire_ui::settings::{self, Section};
use agent_hire_ui::shell::{self, Tab};
use agent_hire_ui::status;
use axum::Router;
use axum::extract::{Path, Query, Request, State};
use axum::http::{HeaderValue, Method, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::{Html, IntoResponse, Redirect, Response};
use axum::routing::{get, post};
use maud::Markup;
use serde::Deserialize;
use serde_json::Value;

use self::data::Rows;
use crate::app_state::AppState;
use crate::auth;
use crate::fill_routes;
use crate::jobindex_routes;
use crate::profile_routes;
use crate::respond;

/// Registers UI routes alongside the existing API.
pub fn router() -> Router<Arc<AppState>> {
    Router::new()
        .route("/", get(chat_page))
        .route("/jobs", get(jobs_page))
        .route("/applications", get(applications_page))
        .route(
            "/settings",
            get(|| async { Redirect::temporary("/settings/ai") }),
        )
        .route("/settings/{section}", get(settings_page))
        .route("/ui/search", get(search_fragment))
        .route("/ui/settings/{section}", get(settings_fragment))
        .route("/ui/companies", get(companies_panel))
        .route("/ui/kaggle", get(kaggle_panel))
        .route("/ui/profile", get(profile_panel))
        .route("/ui/leads/{job_id}", get(lead_panel))
        .route("/ui/pickers/companies", get(company_picker))
        .route("/ui/pickers/departments", get(department_picker))
        .route("/ui/chat/render/{kind}", post(chat_render))
        .route("/ui/refresh", get(live::refresh))
        .route("/ui/live", get(live::stream))
        .merge(actions::router())
        .route_layer(middleware::from_fn(protect_ui))
        .route("/static/{hash}/{file}", get(asset))
}

async fn protect_ui(request: Request, next: Next) -> Response {
    // The proxy rewrites Host to the internal port. Fetch Metadata still reflects the public
    // origin, and rejects cross-origin requests even between two loopback ports.
    if request
        .headers()
        .get("sec-fetch-site")
        .is_some_and(|value| value != "same-origin" && value != "none")
    {
        return StatusCode::FORBIDDEN.into_response();
    }
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(
        "cross-origin-resource-policy",
        HeaderValue::from_static("same-origin"),
    );
    headers.insert(header::CONTENT_SECURITY_POLICY, HeaderValue::from_static(
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    ));
    response
}

/// Checks the API bearer token on mutating UI requests, including chat rendering.
pub(super) async fn authorize_ui_action(
    State(state): State<Arc<AppState>>,
    request: Request,
    next: Next,
) -> Response {
    if request.uri().path().starts_with("/ui/")
        && !matches!(
            *request.method(),
            Method::GET | Method::HEAD | Method::OPTIONS
        )
        && !auth::has_valid_token(&state, request.headers())
    {
        return auth::unauthorized();
    }
    next.run(request).await
}

async fn asset(Path((hash, file)): Path<(String, String)>) -> Response {
    let Some(asset) = assets::get(&format!("/static/{hash}/{file}")) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    (
        [
            (header::CONTENT_TYPE, asset.mime),
            (header::CACHE_CONTROL, "public, max-age=31536000, immutable"),
            (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
        ],
        asset.bytes,
    )
        .into_response()
}

#[derive(Clone, Default, Deserialize)]
#[serde(default)]
struct PageQuery {
    tab: String,
    view: String,
    stage: String,
    limit: usize,
    section: String,
    signals: String,
}

impl PageQuery {
    fn tab(&self) -> Tab {
        match self.tab.as_str() {
            "jobs" => Tab::Jobs,
            "applications" => Tab::Applications,
            "settings" => Tab::Settings,
            _ => Tab::Chat,
        }
    }

    fn pipeline(&self) -> PipelineQuery {
        PipelineQuery::new(&self.stage, self.limit)
    }

    fn shortlisted(&self) -> bool {
        self.view == "shortlisted"
    }
}

fn markup(view: Markup) -> Response {
    Html(view.into_string()).into_response()
}

async fn widgets(state: Arc<AppState>) -> Markup {
    let (scrape, progress, kaggle) = tokio::join!(
        data::scrape(Arc::clone(&state)),
        data::classification(Arc::clone(&state)),
        data::notebook(state),
    );
    status::widgets(&scrape, &progress, &kaggle)
}

async fn chat_page(State(state): State<Arc<AppState>>) -> Response {
    let (pipeline, widgets) = tokio::join!(
        data::pipeline(Arc::clone(&state), Rows::None),
        widgets(Arc::clone(&state))
    );
    let badges = match pipeline {
        Ok(pipeline) => pipeline.badges(),
        Err(response) => return response,
    };
    markup(shell::page(
        Tab::Chat,
        state.token.as_str(),
        "/ui/live?tab=chat",
        &badges,
        widgets,
        chat::page(),
    ))
}

async fn jobs_page(
    State(state): State<Arc<AppState>>,
    Query(search): Query<Search>,
    Query(query): Query<PageQuery>,
) -> Response {
    let showing_shortlist = query.shortlisted();
    let result = async {
        if showing_shortlist {
            Value::Null
        } else {
            data::search(Arc::clone(&state), &search).await
        }
    };
    let (pipeline, index, widgets, results) = tokio::join!(
        data::pipeline(Arc::clone(&state), Rows::Shortlist),
        data::index(Arc::clone(&state)),
        widgets(Arc::clone(&state)),
        result,
    );
    let pipeline = match pipeline {
        Ok(pipeline) => pipeline,
        Err(response) => return response,
    };
    let live = if showing_shortlist {
        "/ui/live?tab=jobs&view=shortlisted"
    } else {
        "/ui/live?tab=jobs"
    };
    let content = jobs::page(
        &search,
        &results,
        &index,
        &pipeline.leads,
        showing_shortlist,
    );
    markup(shell::page(
        Tab::Jobs,
        state.token.as_str(),
        live,
        &pipeline.badges(),
        widgets,
        content,
    ))
}

async fn search_fragment(
    State(state): State<Arc<AppState>>,
    Query(search): Query<Search>,
) -> Response {
    let result = data::search(state, &search).await;
    let has_jobs = result["index"]["jobs_active"].as_u64().unwrap_or_default() > 0;
    markup(jobs::results(&search, &result, has_jobs))
}

async fn applications_page(
    State(state): State<Arc<AppState>>,
    Query(params): Query<PageQuery>,
) -> Response {
    let query = params.pipeline();
    let (pipeline, widgets) = tokio::join!(
        data::pipeline(Arc::clone(&state), Rows::Applications(query.clone())),
        widgets(Arc::clone(&state)),
    );
    let pipeline = match pipeline {
        Ok(pipeline) => pipeline,
        Err(response) => return response,
    };
    let live = format!(
        "/ui/live?tab=applications&stage={}&limit={}",
        query.stage, query.limit
    );
    let content = applications::page(&pipeline.view(&query));
    markup(shell::page(
        Tab::Applications,
        state.token.as_str(),
        &live,
        &pipeline.badges(),
        widgets,
        content,
    ))
}

async fn settings_page(
    State(state): State<Arc<AppState>>,
    Path(section): Path<String>,
) -> Response {
    let Some(section) = Section::parse(&section) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let (pipeline, widgets, model) = tokio::join!(
        data::pipeline(Arc::clone(&state), Rows::None),
        widgets(Arc::clone(&state)),
        data::settings(Arc::clone(&state), section),
    );
    let badges = match pipeline {
        Ok(pipeline) => pipeline.badges(),
        Err(response) => return response,
    };
    let live = format!("/ui/live?tab=settings&section={}", section.slug());
    markup(shell::page(
        Tab::Settings,
        state.token.as_str(),
        &live,
        &badges,
        widgets,
        settings::page(section, &model),
    ))
}

async fn settings_fragment(
    State(state): State<Arc<AppState>>,
    Path(section): Path<String>,
) -> Response {
    let Some(section) = Section::parse(&section) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    markup(settings::content(
        section,
        &data::settings(state, section).await,
    ))
}

async fn companies_panel(State(state): State<Arc<AppState>>) -> Response {
    let data = data::json_response(jobindex_routes::companies(State(state)).await).await;
    markup(companies::panel(&data))
}

async fn kaggle_panel(State(state): State<Arc<AppState>>) -> Response {
    markup(status::kaggle_panel(&data::kaggle(state).await))
}

async fn profile_panel(State(state): State<Arc<AppState>>) -> Response {
    let (profile, facts) = tokio::join!(
        profile_routes::get_profile(State(Arc::clone(&state))),
        fill_routes::get_facts(State(state)),
    );
    let profile = data::json_response(profile).await;
    let facts = data::json_response(facts).await;
    markup(profile::panel(&profile, &facts))
}

async fn lead_panel(State(state): State<Arc<AppState>>, Path(id): Path<String>) -> Response {
    // The drawer uses the stored ID as-is, including IDs from older releases.
    let leads = state.leads.clone();
    match respond::blocking("read lead for drawer", move || leads.by_id(&id)).await {
        Ok(Some(lead)) => markup(applications::detail(&Value::Object(lead))),
        Ok(None) => respond::detail(
            StatusCode::NOT_FOUND,
            "This job is no longer in your pipeline.",
        ),
        Err(response) => response,
    }
}

#[derive(Default, Deserialize)]
#[serde(default)]
struct PickerQuery {
    selected: String,
}

async fn company_picker(
    State(state): State<Arc<AppState>>,
    Query(query): Query<PickerQuery>,
) -> Response {
    let data = data::json_response(jobindex_routes::companies(State(state)).await).await;
    markup(jobs::company_picker(&data, &query.selected))
}

async fn department_picker(
    State(state): State<Arc<AppState>>,
    Query(query): Query<PickerQuery>,
) -> Response {
    let data = data::json_response(jobindex_routes::departments(State(state)).await).await;
    markup(jobs::department_picker(&data, &query.selected))
}

async fn chat_render(Path(kind): Path<String>, axum::Json(body): axum::Json<Value>) -> Response {
    if !matches!(
        kind.as_str(),
        "item" | "thread" | "history" | "schedules" | "starting"
    ) {
        return respond::not_found("/ui/chat/render");
    }
    markup(chat::render(&kind, &body))
}

#[cfg(test)]
mod tests {
    use std::env;
    use std::fs;
    use std::sync::Arc;

    use agent_hire_store::{AppPaths, Database};
    use axum::body;
    use axum::extract::{Path, State};
    use axum::http::StatusCode;
    use serde_json::json;

    use super::data;
    use super::lead_panel;
    use crate::app_state::AppState;
    use crate::leads_routes;
    use crate::token::Token;

    #[tokio::test]
    async fn drawer_looks_up_legacy_ids_and_reports_removed_leads() {
        let token = Token::generate();
        let directory = env::temp_dir().join(format!("agent-hire-ui-{}", token.as_str()));
        let paths = AppPaths {
            data_dir: directory.clone(),
        };
        let database = Database::open(&paths.database()).expect("open a fresh test database");
        let state = Arc::new(AppState::new(token, None, paths, database));
        let id = "google:1001";
        let lead = json!({"job_id": id, "title": "Existing <role>"});
        assert!(
            state
                .leads
                .save(lead.as_object().expect("lead fixture"))
                .expect("save legacy lead")
        );

        let drawer = lead_panel(State(Arc::clone(&state)), Path(id.to_owned())).await;
        let removed =
            lead_panel(State(Arc::clone(&state)), Path("google:removed".to_owned())).await;
        let api = leads_routes::detail(State(state), Path(id.to_owned())).await;
        fs::remove_dir_all(directory).expect("clean up the legacy lead fixture");

        assert_eq!(drawer.status(), StatusCode::OK);
        let rendered = body::to_bytes(drawer.into_body(), 32_768)
            .await
            .expect("read the drawer");
        let rendered = std::str::from_utf8(&rendered).expect("drawer uses UTF-8");
        assert!(rendered.contains("Existing &lt;role&gt;"));
        assert!(rendered.contains("/ui/leads/google%3A1001/status"));
        assert_eq!(removed.status(), StatusCode::NOT_FOUND);
        let error = data::json_response(removed).await;
        assert_eq!(error["detail"], "This job is no longer in your pipeline.");
        assert_eq!(api.status(), StatusCode::BAD_REQUEST);
    }
}
