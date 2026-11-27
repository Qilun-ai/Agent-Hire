//! Resolving the provider, key and model for a call.

use std::collections::HashMap;
use std::env;

use crate::endpoint::{EndpointConfig, resolve_endpoints};
use crate::environment_keys;
use crate::provider::{self, Provider};
use crate::url_guard;

/// A snapshot of the settings table.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Settings {
    values: HashMap<String, String>,
}

impl Settings {
    /// Settings from `(key, value)` pairs.
    pub fn new(pairs: impl IntoIterator<Item = (String, String)>) -> Settings {
        Settings {
            values: pairs.into_iter().collect(),
        }
    }

    /// The stored value, or `""`.
    pub fn get(&self, key: &str) -> &str {
        self.values.get(key).map_or("", String::as_str)
    }

    /// The stored value if it is not empty, else `default`.
    pub fn get_or<'a>(&'a self, key: &str, default: &'a str) -> &'a str {
        let value = self.get(key);
        if value.is_empty() { default } else { value }
    }

    /// The stored value, else the environment variable, else `default` (empty counts as unset).
    pub fn get_or_env(&self, key: &str, env_name: &str, default: &str) -> String {
        let value = self.get(key);
        if !value.is_empty() {
            return value.to_owned();
        }
        non_empty_env(env_name).unwrap_or_else(|| default.to_owned())
    }

    /// The user's key for `provider`: its setting, then its environment variables (see
    /// [`environment_keys::candidate_variables`]), trimmed; `""` if none.
    pub fn provider_key(&self, provider: &str) -> String {
        let Some(found) = Provider::find(provider) else {
            return String::new();
        };
        let key = self.get(found.key_setting).trim();
        if !key.is_empty() {
            return key.to_owned();
        }
        environment_keys::environment_key(found).map_or_else(String::new, |(_, value)| value)
    }

    /// The custom OpenAI-compatible endpoint's configuration.
    pub fn custom_endpoint(&self) -> EndpointConfig {
        EndpointConfig::custom(
            &self.get_or_env(
                "custom_base_url",
                "OPENAI_COMPAT_BASE_URL",
                "https://api.openai.com/v1",
            ),
            self.get("custom_protocol"),
            self.get("custom_endpoint_type"),
            self.get("custom_reasoning_effort"),
        )
    }

    /// The OpenCode endpoint's configuration.
    pub fn opencode_endpoint(&self) -> EndpointConfig {
        EndpointConfig::opencode(
            &self.get_or_env(
                "opencode_base_url",
                "OPENCODE_BASE_URL",
                "https://opencode.ai/zen/go/v1",
            ),
            self.get("opencode_protocol"),
            self.get("opencode_endpoint_type"),
            self.get("opencode_reasoning_effort"),
        )
    }

    /// The validated API base for `provider`'s OpenAI-compatible client.
    ///
    /// Errors carry the message the app shows, for example
    /// `Cannot use localhost as LLM base URL: http://127.0.0.1:9/v1`.
    pub fn provider_base_url(&self, provider: &str) -> Result<String, String> {
        let base = match provider {
            "custom" => {
                let endpoint = self.custom_endpoint();
                resolve_endpoints(&endpoint.url, endpoint.endpoint_type).base_url
            }
            "opencode" => {
                let endpoint = self.opencode_endpoint();
                resolve_endpoints(&endpoint.url, endpoint.endpoint_type).base_url
            }
            "azure" => {
                let endpoint =
                    self.get_or_env("azure_openai_endpoint", "AZURE_OPENAI_ENDPOINT", "");
                let endpoint = endpoint.trim().trim_end_matches('/');
                if endpoint.is_empty() {
                    return Err("Azure OpenAI endpoint is required".to_owned());
                }
                if endpoint.ends_with("/openai/v1") {
                    endpoint.to_owned()
                } else {
                    format!("{endpoint}/openai/v1")
                }
            }
            "ollama" => self
                .get_or("ollama_url", "http://localhost:11434/v1")
                .to_owned(),
            "openai" => "https://api.openai.com/v1".to_owned(),
            other => match Provider::find(other).and_then(|found| found.base_url) {
                Some(base) => base.to_owned(),
                None => return Err(format!("unknown provider: {other}")),
            },
        };
        if provider == "ollama" {
            return Ok(base);
        }
        url_guard::validate_base_url(&base)?;
        Ok(base)
    }

    /// The provider, key and model for a call, optionally for a named pipeline `step` (whose
    /// `<step>_provider`, `<step>_api_key` and `<step>_model` settings win when set).
    pub fn resolve(&self, step: Option<&str>) -> ResolvedConfig {
        let step_value = |suffix: &str| -> &str {
            step.map_or("", |step| self.get(&format!("{step}_{suffix}")))
        };
        let mut provider = step_value("provider").to_owned();
        if provider.is_empty() {
            provider = self.get_or("llm_provider", "ollama").to_owned();
        }
        let mut api_key = step_value("api_key").to_owned();
        if api_key.is_empty() {
            api_key = self.provider_key(&provider);
        }
        let mut model = step_value("model").to_owned();
        if model.is_empty() {
            model = self
                .get_or(
                    &format!("{provider}_model"),
                    provider::default_model(&provider),
                )
                .to_owned();
        }
        ResolvedConfig {
            provider,
            api_key,
            model,
        }
    }
}

/// What a call will use.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResolvedConfig {
    /// The provider id.
    pub provider: String,
    /// The API key; `""` when none is configured.
    pub api_key: String,
    /// The model id.
    pub model: String,
}

fn non_empty_env(name: &str) -> Option<String> {
    env::var(name).ok().filter(|value| !value.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings(pairs: &[(&str, &str)]) -> Settings {
        Settings::new(
            pairs
                .iter()
                .map(|(k, v)| ((*k).to_owned(), (*v).to_owned())),
        )
    }

    #[test]
    fn resolves_provider_key_and_model() {
        let config = settings(&[
            ("llm_provider", "nvidia"),
            ("nvidia_api_key", " nvapi-x "),
            ("nvidia_model", "minimaxai/minimax-m3.1-flash"),
        ])
        .resolve(Some("agent"));
        assert_eq!(config.provider, "nvidia");
        assert_eq!(config.api_key, "nvapi-x");
        assert_eq!(config.model, "minimaxai/minimax-m3.1-flash");
    }

    #[test]
    fn step_settings_win_and_ollama_is_the_fallback() {
        let config =
            settings(&[("agent_provider", "groq"), ("agent_model", "m")]).resolve(Some("agent"));
        assert_eq!(
            (config.provider.as_str(), config.model.as_str()),
            ("groq", "m")
        );
        let config = settings(&[]).resolve(None);
        assert_eq!(
            (config.provider.as_str(), config.model.as_str()),
            ("ollama", "llama3")
        );
    }

    #[test]
    fn localhost_custom_base_is_refused() {
        let error = settings(&[("custom_base_url", "http://127.0.0.1:9/v1")])
            .provider_base_url("custom")
            .expect_err("refused");
        assert_eq!(
            error,
            "Cannot use localhost as LLM base URL: http://127.0.0.1:9/v1"
        );
    }
}
