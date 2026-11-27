//! Exact input documents used to embed profile entities.

use serde_json::Value;

/// Builds a candidate summary document.
#[must_use]
pub fn profile_text(name: &str, summary: &str) -> String {
    format!("Candidate profile\nName: {name}\nSummary: {summary}")
}

/// Builds a skill document.
#[must_use]
pub fn skill_text(name: &str, category: &str) -> String {
    format!("Skill: {name}\nCategory: {category}")
}

/// Converts a recorded stack into text, preserving list order.
#[must_use]
pub fn stack_text(stack: &Value) -> String {
    if let Some(items) = stack.as_array() {
        let parts: Vec<String> = items
            .iter()
            .map(|item| match item {
                Value::String(text) => text.clone(),
                Value::Null => "None".to_owned(),
                Value::Bool(flag) => if *flag { "True" } else { "False" }.to_owned(),
                other => other.to_string(),
            })
            .collect();
        parts.join(", ")
    } else {
        stack.as_str().unwrap_or_default().to_owned()
    }
}

/// Builds a project document from its name, stack and impact.
#[must_use]
pub fn project_text(title: &str, stack: &Value, impact: &str) -> String {
    format!(
        "Project: {title}\nStack: {}\nImpact: {impact}",
        stack_text(stack)
    )
}

/// Builds an experience document.
#[must_use]
pub fn experience_text(role: &str, company: &str, period: &str, description: &str) -> String {
    format!("Experience: {role}\nCompany: {company}\nPeriod: {period}\nDescription: {description}")
}

/// Builds a credential document, preserving the spelling of its kind.
#[must_use]
pub fn credential_text(title: &str, kind: &str) -> String {
    format!("{kind}: {title}")
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{credential_text, experience_text, profile_text, project_text, skill_text};

    #[test]
    fn preserves_profile_vector_input_formats() {
        assert_eq!(
            profile_text("Priya", "Engineer"),
            "Candidate profile\nName: Priya\nSummary: Engineer"
        );
        assert_eq!(
            skill_text("Rust", "Language"),
            "Skill: Rust\nCategory: Language"
        );
        assert_eq!(
            project_text("Ledger", &json!(["Go", "SQL"]), "Fast"),
            "Project: Ledger\nStack: Go, SQL\nImpact: Fast"
        );
        assert_eq!(
            experience_text("Dev", "Acme", "2024", "Built APIs"),
            "Experience: Dev\nCompany: Acme\nPeriod: 2024\nDescription: Built APIs"
        );
        assert_eq!(
            credential_text("AWS", "Certification"),
            "Certification: AWS"
        );
    }
}
