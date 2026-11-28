//! The best label and its softmax probability.

use serde::Serialize;

/// An encoder verdict in the model's label space.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Prediction {
    /// The winning label from `labels.json`.
    pub label: String,
    /// Its softmax probability, rounded to four decimal places.
    pub probability: f64,
}

pub(crate) fn from_logits(logits: &[f32], labels: &[String]) -> Prediction {
    let maximum = logits.iter().copied().fold(f32::NEG_INFINITY, f32::max);
    let sum: f32 = logits.iter().map(|logit| (logit - maximum).exp()).sum();
    let mut best_index = 0;
    let mut best_probability = 0.0;
    for (index, &logit) in logits.iter().enumerate() {
        let probability = (logit - maximum).exp() / sum;
        if index == 0 || probability > best_probability {
            best_index = index;
            best_probability = probability;
        }
    }
    Prediction {
        label: labels[best_index].clone(),
        probability: (f64::from(best_probability) * 10_000.0).round_ties_even() / 10_000.0,
    }
}

#[cfg(test)]
mod tests {
    use super::from_logits;

    #[test]
    fn softmax_is_stable_and_breaks_ties_by_label_order() {
        let labels = ["first".to_owned(), "second".to_owned(), "third".to_owned()];
        let verdict = from_logits(&[1000.0, 1000.0, 999.0], &labels);
        assert_eq!(verdict.label, "first");
        assert_eq!(verdict.probability, 0.4223);
    }
}
