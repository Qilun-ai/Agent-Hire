//! Match-score feedback learned from the meanings of rated postings.

use std::collections::BTreeMap;

use agent_hire_ml::{self as ml, Embedder};
use serde_json::Value;

use crate::{feedback, json_fields};

/// Maximum semantic preference adjustment used by the service.
pub const MAX_PREFERENCE_DELTA: i64 = 12;

/// Computes match-score deltas keyed by job ID using real embeddings.
#[must_use]
pub fn preference_deltas(
    examples: &[Value],
    leads: &[Value],
    embedder: &Embedder,
    max_delta: i64,
) -> BTreeMap<String, i64> {
    let mut texts = Vec::new();
    let mut weights = Vec::new();
    for example in examples {
        let label = json_fields::string(example, &["feedback"])
            .trim()
            .to_lowercase();
        let weight = feedback::polarity(&label);
        let text = content(example);
        if weight != 0.0 && !text.is_empty() {
            texts.push(text);
            weights.push(weight);
        }
    }
    if weights.is_empty() || leads.is_empty() {
        return BTreeMap::new();
    }
    texts.extend(leads.iter().map(content));
    let batch = embedder.embed_with_mode(&texts);
    if !batch.mode.is_semantic() {
        return BTreeMap::new();
    }
    from_vectors(&weights, leads, batch.vectors, max_delta)
}

fn content(lead: &Value) -> String {
    let title = json_fields::string(lead, &["title"]);
    let description = json_fields::string(lead, &["description"]);
    format!("{title} {description}").trim().to_owned()
}

fn from_vectors(
    weights: &[f64],
    leads: &[Value],
    mut vectors: Vec<Vec<f32>>,
    max_delta: i64,
) -> BTreeMap<String, i64> {
    let mut deltas = BTreeMap::new();
    if vectors.len() != weights.len() + leads.len() || vectors.is_empty() {
        return deltas;
    }
    let dimensions = vectors[0].len();
    if dimensions == 0 || vectors.iter().any(|vector| vector.len() != dimensions) {
        return deltas;
    }
    for vector in &mut vectors {
        ml::normalize(vector);
    }
    let (examples, postings) = vectors.split_at(weights.len());
    let mut preference = vec![0.0; dimensions];
    for (vector, weight) in examples.iter().zip(weights) {
        for (value, sample) in preference.iter_mut().zip(vector) {
            *value += sample * *weight as f32;
        }
    }
    ml::normalize(&mut preference);
    if preference.iter().all(|value| *value == 0.0) {
        return deltas;
    }
    for (lead, vector) in leads.iter().zip(postings) {
        let id = json_fields::string(lead, &["job_id"]);
        if !id.is_empty() {
            let delta = (ml::cosine(&preference, vector) * max_delta.max(0) as f64)
                .round_ties_even() as i64;
            if delta != 0 {
                deltas.insert(id.to_owned(), delta);
            }
        }
    }
    deltas
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::from_vectors;

    #[test]
    fn normalizes_each_vector_before_building_preference() {
        let leads = [
            json!({"job_id":"positive"}),
            json!({"job_id":"negative"}),
            json!({}),
        ];
        let vectors = vec![
            vec![10.0, 0.0],
            vec![0.0, 4.0],
            vec![2.0, 0.0],
            vec![0.0, 8.0],
            vec![1.0, 0.0],
        ];
        let deltas = from_vectors(&[1.0, -1.0], &leads, vectors, 12);
        assert_eq!(deltas.get("positive"), Some(&8));
        assert_eq!(deltas.get("negative"), Some(&-8));
        assert_eq!(deltas.len(), 2);
    }
}
