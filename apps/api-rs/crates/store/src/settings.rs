//! The key/value settings table (`settings(key, val)`); every value is stored as text.

use std::fmt;

use rusqlite::{Connection, OptionalExtension, params};

use crate::database::{Database, StoreError};

/// Every provider id the provider settings accept, in the order error messages list them.
const PROVIDERS: [&str; 27] = [
    "",
    "ollama",
    "anthropic",
    "gemini",
    "groq",
    "nvidia",
    "openai",
    "deepseek",
    "xai",
    "kimi",
    "mistral",
    "openrouter",
    "together",
    "fireworks",
    "cerebras",
    "perplexity",
    "huggingface",
    "cohere",
    "sambanova",
    "qwen",
    "azure",
    "opencode",
    "custom",
    "claude_cli",
    "codex_cli",
    "gemini_cli",
    "copilot_cli",
];

const EMBEDDING_PROVIDERS: [&str; 3] = ["onnx", "openai", "hash"];

/// What a known setting must look like.
enum Rule {
    Integer { min: i64, max: i64 },
    OneOf(&'static [&'static str]),
}

/// The settings that are checked before saving; any other key is stored unchecked.
fn rule_for(key: &str) -> Option<Rule> {
    let rule = match key {
        "x_max_requests_per_scan" => Rule::Integer { min: 1, max: 50 },
        "x_max_results_per_query" => Rule::Integer { min: 10, max: 100 },
        "free_source_min_signal_score" => Rule::Integer { min: 0, max: 100 },
        "free_source_max_requests" => Rule::Integer { min: 1, max: 80 },
        "board_scan_batch_size" => Rule::Integer { min: 1, max: 12 },
        "x_hot_lead_threshold" => Rule::Integer { min: 1, max: 100 },
        "embedding_provider" => Rule::OneOf(&EMBEDDING_PROVIDERS),
        "llm_provider" | "evaluator_provider" | "scout_provider" | "generator_provider"
        | "ingestor_provider" | "actuator_provider" => Rule::OneOf(&PROVIDERS),
        _ => return None,
    };
    Some(rule)
}

/// Settings that failed validation; the message joins every problem with `; `.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SettingsError {
    /// One or more values broke their rule.
    Invalid(String),
    /// The database failed.
    Store(String),
}

impl fmt::Display for SettingsError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SettingsError::Invalid(message) | SettingsError::Store(message) => {
                formatter.write_str(message)
            }
        }
    }
}

impl std::error::Error for SettingsError {}

impl From<StoreError> for SettingsError {
    fn from(error: StoreError) -> SettingsError {
        SettingsError::Store(error.to_string())
    }
}

/// Reads and writes the `settings` table.
#[derive(Clone, Debug)]
pub struct SettingsStore {
    database: Database,
}

impl SettingsStore {
    /// The settings in `database`.
    pub fn new(database: Database) -> SettingsStore {
        SettingsStore { database }
    }

    /// Every setting, in the table's natural order (the order the Python backend returns too).
    pub fn all(&self) -> Result<Vec<(String, String)>, StoreError> {
        let connection = self.database.connect()?;
        let mut statement = connection.prepare("SELECT key, val FROM settings")?;
        let rows = statement.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?))
        })?;
        let mut out = Vec::new();
        for row in rows {
            let (key, value) = row?;
            out.push((key, value.unwrap_or_default()));
        }
        Ok(out)
    }

    /// One setting, or `default` when it is not stored.
    pub fn get(&self, key: &str, default: &str) -> Result<String, StoreError> {
        let connection = self.database.connect()?;
        Ok(read(&connection, key)?.unwrap_or_else(|| default.to_owned()))
    }

    /// Validates every pair, then stores them all, or none if any is invalid.
    pub fn save(&self, pairs: &[(String, String)]) -> Result<(), SettingsError> {
        let problems: Vec<String> = pairs
            .iter()
            .filter_map(|(key, value)| validate(key, value))
            .collect();
        if !problems.is_empty() {
            return Err(SettingsError::Invalid(problems.join("; ")));
        }
        let mut connection = self.database.connect()?;
        let transaction = connection.transaction().map_err(StoreError::from)?;
        for (key, value) in pairs {
            write(&transaction, key, value)?;
        }
        transaction.commit().map_err(StoreError::from)?;
        Ok(())
    }

    /// Deletes one setting; returns whether it existed.
    pub fn delete(&self, key: &str) -> Result<bool, StoreError> {
        let connection = self.database.connect()?;
        let deleted = connection.execute("DELETE FROM settings WHERE key = ?1", [key])?;
        Ok(deleted > 0)
    }
}

/// Reads a setting on an existing connection, including inside a transaction.
pub(crate) fn read(connection: &Connection, key: &str) -> Result<Option<String>, StoreError> {
    let value: Option<Option<String>> = connection
        .query_row("SELECT val FROM settings WHERE key = ?1", [key], |row| {
            row.get(0)
        })
        .optional()?;
    Ok(value.map(Option::unwrap_or_default))
}

/// Writes a setting on an existing connection, including inside a transaction.
pub(crate) fn write(connection: &Connection, key: &str, value: &str) -> Result<(), StoreError> {
    connection.execute(
        "INSERT OR REPLACE INTO settings(key, val) VALUES (?1, ?2)",
        params![key, value],
    )?;
    Ok(())
}

/// The problem with `value` for `key`, if any.
fn validate(key: &str, value: &str) -> Option<String> {
    match rule_for(key)? {
        Rule::Integer { min, max } => match value.trim().parse::<i64>() {
            Err(_) => Some(format!("{key} must be a number")),
            Ok(number) if number < min || number > max => {
                Some(format!("{key} must be between {min} and {max}"))
            }
            Ok(_) => None,
        },
        Rule::OneOf(allowed) => (!allowed.contains(&value))
            .then(|| format!("{key} must be one of: {}", allowed.join(", "))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validation_messages() {
        assert_eq!(validate("free_source_max_requests", "05"), None);
        assert_eq!(
            validate("free_source_max_requests", "x"),
            Some("free_source_max_requests must be a number".to_owned())
        );
        assert_eq!(
            validate("board_scan_batch_size", "13"),
            Some("board_scan_batch_size must be between 1 and 12".to_owned())
        );
        assert_eq!(validate("llm_provider", ""), None);
        let message = validate("llm_provider", "bogus").expect("rejected");
        assert!(message.starts_with("llm_provider must be one of: , ollama, anthropic"));
        assert_eq!(validate("anything_else", "x"), None);
    }
}
