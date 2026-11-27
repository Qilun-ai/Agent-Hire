//! Settings that come from the environment instead of the app: provider keys exported in the
//! user's shell, and the decision model configured in the install folder's `.env`.

use std::env;
use std::sync::Arc;

use agent_hire_jobindex::decision;
use agent_hire_store::SettingsStore;
use axum::Json;
use axum::extract::State;
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};

use crate::app_state::AppState;
use crate::log;
use crate::respond;

/// Chooses the AI provider on first start when the user has not picked one but a provider's key
/// is in the environment, so the app works without pasting a key. Returns the chosen provider.
pub fn choose_provider_from_environment(settings: &SettingsStore) -> Option<&'static str> {
    let chosen = settings.get("llm_provider", "").ok()?;
    if !chosen.trim().is_empty() {
        return None;
    }
    let detected = agent_hire_llm::detected_providers();
    let first = detected.first()?;
    if let Err(error) = settings.save(&[("llm_provider".to_owned(), first.provider.to_owned())]) {
        log::warning(
            "settings",
            &format!("cannot select {}: {error}", first.provider),
        );
        return None;
    }
    log::info(
        "settings",
        &format!(
            "using {} with the key from {}",
            first.provider, first.variable
        ),
    );
    Some(first.provider)
}

/// `GET /api/v1/settings/environment`: which provider keys and decision-model settings come
/// from the environment. Never returns a key, only variable names.
pub async fn get_environment(State(state): State<Arc<AppState>>) -> Response {
    let store = state.settings.clone();
    let stored_url =
        match respond::blocking("read settings", move || store.get("decision_api_url", "")).await {
            Ok(url) => url,
            Err(response) => return response,
        };
    Json(environment_report(
        &agent_hire_llm::detected_providers(),
        &stored_url,
        env::var(decision::DECISION_URL_VARIABLE).ok().as_deref(),
    ))
    .into_response()
}

fn environment_report(
    detected: &[agent_hire_llm::EnvironmentKey],
    stored_decision_url: &str,
    environment_decision_url: Option<&str>,
) -> Value {
    let providers: Vec<Value> = detected
        .iter()
        .map(|found| {
            json!({
                "id": found.provider,
                "variable": found.variable,
                "key_setting": found.key_setting,
            })
        })
        .collect();
    let decision_source = if !stored_decision_url.trim().is_empty() {
        "settings"
    } else if environment_decision_url.is_some_and(|url| !url.trim().is_empty()) {
        "environment"
    } else {
        "none"
    };
    json!({
        "providers": providers,
        "decision_api": {
            "source": decision_source,
            "variable": decision::DECISION_URL_VARIABLE,
        },
    })
}

#[cfg(test)]
mod tests {
    use agent_hire_llm::EnvironmentKey;
    use serde_json::json;

    use super::environment_report;

    #[test]
    fn report_names_variables_and_the_decision_source() {
        let detected = [EnvironmentKey {
            provider: "openai",
            variable: "OPENAI_API".to_owned(),
            key_setting: "openai_api_key",
        }];
        let report = environment_report(&detected, "", Some("https://decide.example/v1"));
        assert_eq!(
            report,
            json!({
                "providers": [{"id": "openai", "variable": "OPENAI_API", "key_setting": "openai_api_key"}],
                "decision_api": {"source": "environment", "variable": "AGENT_HIRE_DECISION_API_URL"},
            })
        );
        let report = environment_report(&[], "https://app.example", None);
        assert_eq!(report["decision_api"]["source"], "settings");
        assert_eq!(
            environment_report(&[], " ", None)["decision_api"]["source"],
            "none"
        );
    }
}
