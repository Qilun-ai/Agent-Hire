//! Metadata-based changes to a lead's signal score.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};
use url::Url;

use crate::json_fields;

static LEARNING_SUFFIX: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?:;\s*)?feedback learning [+-][0-9]+\s*$").expect("valid feedback suffix pattern")
});

/// Maximum metadata feedback adjustment used by the service.
pub const MAX_SIGNAL_DELTA: i64 = 18;

/// Feedback feature aggregates built once for a set of examples.
#[derive(Debug, Default)]
pub struct FeedbackModel {
    features: BTreeMap<String, FeedbackCount>,
}

#[derive(Debug, Default)]
struct FeedbackCount {
    sum: f64,
    count: usize,
}

impl FeedbackModel {
    /// Learns one weighted count per feature and training example.
    #[must_use]
    pub fn from_examples(examples: &[Value]) -> Self {
        let mut model = Self::default();
        for example in examples {
            let label = normalized(json_fields::string(example, &["feedback"]));
            let weight = polarity(&label);
            if weight == 0.0 {
                continue;
            }
            for feature in features(example) {
                let counter = model.features.entry(feature).or_default();
                counter.sum += weight;
                counter.count += 1;
            }
        }
        model
    }

    /// Applies learned metadata preferences to a lead payload.
    #[must_use]
    pub fn apply(&self, lead: &Value, max_delta: i64) -> Value {
        let mut result = lead.as_object().cloned().unwrap_or_default();
        let base = json_fields::integer(&lead["signal_score"]);
        result.entry("base_signal_score").or_insert(json!(base));
        let previous = json_fields::string(lead, &["signal_reason"]);
        let clean = LEARNING_SUFFIX.replace(previous, "");
        let reason = clean.trim_matches(|c: char| c.is_whitespace() || c == ';');
        let mut meta = lead["source_meta"].as_object().cloned().unwrap_or_default();
        meta.remove("learning");

        let mut contributions = Vec::new();
        for feature in features(lead) {
            if let Some(counter) = self.features.get(&feature) {
                let confidence = counter.count.min(5) as f64 / 5.0;
                let average = counter.sum / counter.count.max(1) as f64;
                let value = average * confidence * feature_weight(&feature);
                if value.abs() >= 0.35 {
                    contributions.push((feature, value));
                }
            }
        }
        let limit = max_delta.max(0);
        let delta = (contributions
            .iter()
            .map(|(_, value)| value)
            .sum::<f64>()
            .round_ties_even() as i64)
            .clamp(-limit, limit);
        if delta == 0 {
            if !contributions.is_empty() {
                result.insert("signal_score".to_owned(), json!(base));
            }
            finish_fields(&mut result, reason, 0, "", meta);
            return Value::Object(result);
        }

        contributions.sort_by(|left, right| right.1.abs().total_cmp(&left.1.abs()));
        let top: Vec<_> = contributions
            .iter()
            .take(3)
            .map(|(feature, value)| {
                let rounded = json_fields::rounded(*value, 1);
                format!("{} {rounded:+.1}", feature.replacen(':', " ", 1))
            })
            .collect();
        let direction = if delta > 0 { "boost" } else { "penalty" };
        let learning = format!("Feedback {direction}: {}", top.join(", "));
        let suffix = format!("feedback learning {delta:+}");
        let signal_reason = if reason.is_empty() {
            suffix
        } else {
            format!("{reason}; {suffix}")
        };
        result.insert(
            "signal_score".to_owned(),
            json!(base.saturating_add(delta).clamp(0, 100)),
        );
        meta.insert(
            "learning".to_owned(),
            json!({
                "base_signal_score":base,"delta":delta,"reason":learning,
            }),
        );
        finish_fields(&mut result, &signal_reason, delta, &learning, meta);
        Value::Object(result)
    }
}

/// Applies metadata feedback using the standard delta limit.
#[must_use]
pub fn apply_feedback_learning(lead: &Value, examples: &[Value]) -> Value {
    FeedbackModel::from_examples(examples).apply(lead, MAX_SIGNAL_DELTA)
}

fn finish_fields(
    result: &mut Map<String, Value>,
    reason: &str,
    delta: i64,
    learning: &str,
    meta: Map<String, Value>,
) {
    result.insert("learning_delta".to_owned(), json!(delta));
    result.insert("learning_reason".to_owned(), json!(learning));
    result.insert("signal_reason".to_owned(), json!(reason));
    result.insert("source_meta".to_owned(), Value::Object(meta));
}

pub(crate) fn polarity(label: &str) -> f64 {
    match label {
        "good" | "relevant" => 1.0,
        "already_contacted" => 0.7,
        "trash" | "not_relevant" | "incorrect_category" => -1.0,
        "low_quality" => -0.9,
        "too_generic" => -0.8,
        "not_ai" => -1.1,
        "duplicate" => -0.45,
        _ => 0.0,
    }
}

fn feature_weight(feature: &str) -> f64 {
    match feature.split_once(':').map(|(prefix, _)| prefix) {
        Some("platform") => 6.0,
        Some("source" | "ats" | "stack") => 5.0,
        Some("kind" | "company") => 4.0,
        Some("tag") => 3.0,
        _ => 2.0,
    }
}

fn normalized(value: &str) -> String {
    json_fields::clip(&json_fields::squash(value).to_lowercase(), 80).to_owned()
}

fn company_key(lead: &Value) -> String {
    let company = normalized(json_fields::string(lead, &["company"]));
    if !company.is_empty() {
        return company.trim_start_matches('@').to_owned();
    }
    let url = json_fields::string(lead, &["url"]).trim();
    let absolute = if url.contains("://") {
        url.to_owned()
    } else {
        format!("https://{url}")
    };
    let Ok(url) = Url::parse(&absolute) else {
        return String::new();
    };
    let host = url.host_str().unwrap_or_default().to_lowercase();
    let host = host.strip_prefix("www.").unwrap_or(&host);
    host.split('.').next().unwrap_or_default().to_owned()
}

fn features(lead: &Value) -> BTreeSet<String> {
    // Stable lexical order breaks equal-contribution ties, which the spec leaves unspecified.
    let mut features = BTreeSet::new();
    let mut add = |prefix: &str, value: &str| {
        let value = normalized(value);
        if !value.is_empty() {
            features.insert(format!("{prefix}:{value}"));
        }
    };
    let meta = &lead["source_meta"];
    add("platform", json_fields::string(lead, &["platform"]));
    add("kind", json_fields::string(lead, &["kind"]));
    add("company", &company_key(lead));
    add("source", json_fields::string(meta, &["source"]));
    add("ats", json_fields::string(meta, &["ats"]));
    let location = json_fields::string(lead, &["location"]);
    let location = if location.is_empty() {
        json_fields::string(meta, &["location"])
    } else {
        location
    };
    add("location", location);
    let stack = if json_fields::truthy(&lead["tech_stack"]) {
        &lead["tech_stack"]
    } else {
        &meta["tech_stack"]
    };
    for (prefix, values) in [("tag", &lead["signal_tags"]), ("stack", stack)] {
        match values {
            Value::Array(items) => {
                for item in items {
                    add(prefix, &json_fields::stringify(item));
                }
            }
            value if json_fields::truthy(value) => {
                for item in json_fields::stringify(value).split(',') {
                    add(prefix, item);
                }
            }
            _ => (),
        }
    }
    if json_fields::truthy(&lead["budget"]) {
        add("budget", "present");
    }
    if json_fields::truthy(&lead["urgency"]) || json_fields::truthy(&meta["urgency"]) {
        add("urgency", "present");
    }
    features
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{FeedbackModel, apply_feedback_learning};

    #[test]
    fn learns_once_per_feature_and_formats_the_suffix() {
        let lead = json!({"platform":"Board","company":"@Acme","signal_score":69,
                          "signal_reason":"Solid; feedback learning -2"});
        let examples = [json!({"feedback":" GOOD ","platform":" board ","url":"www.acme.example"})];
        let learned = apply_feedback_learning(&lead, &examples);
        assert_eq!(
            learned["learning_reason"],
            "Feedback boost: platform board +1.2, company acme +0.8"
        );
        assert_eq!(learned["signal_score"], 71);
        assert_eq!(learned["signal_reason"], "Solid; feedback learning +2");
        assert_eq!(learned["source_meta"]["learning"]["base_signal_score"], 69);
        let cleared = FeedbackModel::default().apply(&learned, 18);
        assert_eq!(cleared["signal_score"], 71);
        assert_eq!(cleared["signal_reason"], "Solid");
        assert!(cleared["source_meta"].get("learning").is_none());
    }

    #[test]
    fn bounds_the_delta_and_the_signal_score() {
        let lead =
            json!({"platform":"Board","kind":"job","source_meta":{"ats":"ats"},"signal_score":2});
        let cases = vec![
            json!({"feedback":"trash","platform":"Board","kind":"job","source_meta":{"ats":"ats"}});
            7
        ];
        let learned = FeedbackModel::from_examples(&cases).apply(&lead, 3);
        assert_eq!(learned["learning_delta"], -3);
        assert_eq!(learned["signal_score"], 0);
    }
}
