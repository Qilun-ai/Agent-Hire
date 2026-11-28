//! Finding provider API keys in the environment.
//!
//! Each provider's conventional variable (`OPENAI_API_KEY`, `GEMINI_API_KEY`, ...) is checked
//! first. People also export shorter or numbered spellings such as `OPENAI_API` or
//! `OPENCODE_API_1`, so those are accepted as well.

use std::env;

use crate::provider::{KEYED_PROVIDERS, Provider};

/// Providers that need a base URL besides the key, so a key alone cannot select them.
const NEEDS_BASE_URL: [&str; 2] = ["custom", "azure"];

/// A provider whose key is set in the environment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EnvironmentKey {
    /// The provider id, as stored in `llm_provider`.
    pub provider: &'static str,
    /// The variable the key was found in.
    pub variable: String,
    /// The setting that would hold a key typed into the app.
    pub key_setting: &'static str,
}

/// The variable names checked for `provider`, most conventional first.
pub fn candidate_variables(provider: &Provider) -> Vec<String> {
    let mut names = vec![provider.key_env.to_owned()];
    if provider.id == "gemini" {
        names.push("GOOGLE_API_KEY".to_owned());
    }
    if let Some(stem) = provider.key_env.strip_suffix("_API_KEY") {
        names.push(format!("{stem}_API"));
        names.push(format!("{stem}_KEY"));
        names.push(format!("{}_1", provider.key_env));
        names.push(format!("{stem}_API_1"));
    }
    names.dedup();
    names
}

/// The first non-empty key for `provider` among its candidate variables, with that variable.
pub fn environment_key(provider: &Provider) -> Option<(String, String)> {
    key_from(provider, |name| env::var(name).ok())
}

/// Providers that can be used with only an environment key, in the order the app lists them.
pub fn detected_providers() -> Vec<EnvironmentKey> {
    detected_with(|name| env::var(name).ok())
}

fn key_from(
    provider: &Provider,
    lookup: impl Fn(&str) -> Option<String>,
) -> Option<(String, String)> {
    candidate_variables(provider).into_iter().find_map(|name| {
        let value = lookup(&name)?.trim().to_owned();
        (!value.is_empty()).then_some((name, value))
    })
}

fn detected_with(lookup: impl Fn(&str) -> Option<String>) -> Vec<EnvironmentKey> {
    let usable = KEYED_PROVIDERS
        .iter()
        .filter(|provider| !NEEDS_BASE_URL.contains(&provider.id));
    let mut found = Vec::new();
    for provider in usable {
        if let Some((variable, _)) = key_from(provider, &lookup) {
            found.push(EnvironmentKey {
                provider: provider.id,
                variable,
                key_setting: provider.key_setting,
            });
        }
    }
    found
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;

    fn lookup(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
            .collect();
        move |name| map.get(name).cloned()
    }

    #[test]
    fn conventional_names_come_first_then_aliases() {
        let opencode = Provider::find("opencode").expect("opencode is listed");
        assert_eq!(
            candidate_variables(opencode),
            [
                "OPENCODE_API_KEY",
                "OPENCODE_API",
                "OPENCODE_KEY",
                "OPENCODE_API_KEY_1",
                "OPENCODE_API_1"
            ]
        );
        let gemini = Provider::find("gemini").expect("gemini is listed");
        assert_eq!(candidate_variables(gemini)[1], "GOOGLE_API_KEY");
    }

    #[test]
    fn aliases_and_numbered_names_are_found_and_blank_values_skipped() {
        let opencode = Provider::find("opencode").expect("opencode is listed");
        let found = key_from(
            opencode,
            lookup(&[("OPENCODE_API_KEY", "  "), ("OPENCODE_API_1", " sk-1 ")]),
        );
        assert_eq!(
            found,
            Some(("OPENCODE_API_1".to_owned(), "sk-1".to_owned()))
        );
    }

    #[test]
    fn providers_needing_a_base_url_are_not_detected() {
        let found = detected_with(lookup(&[
            ("OPENAI_COMPAT_API_KEY", "x"),
            ("AZURE_OPENAI_API_KEY", "y"),
            ("GEMINI_API", "z"),
        ]));
        assert_eq!(
            found,
            [EnvironmentKey {
                provider: "gemini",
                variable: "GEMINI_API".to_owned(),
                key_setting: "gemini_api_key",
            }]
        );
    }
}
