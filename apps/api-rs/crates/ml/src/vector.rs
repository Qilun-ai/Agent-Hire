//! Deterministic BLAKE2b hashing and cosine operations on embedding vectors.

use std::sync::LazyLock;

use blake2::Blake2bVar;
use regex::Regex;

static TOKEN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[a-z0-9+#.-]{2,}").expect("valid hash token pattern"));

/// Produces a normalized signed feature-hash vector.
#[must_use]
pub fn hash_embedding(text: &str, dimensions: usize) -> Vec<f32> {
    use blake2::digest::{Update as _, VariableOutput as _};

    let mut vector = vec![0.0; dimensions];
    if dimensions == 0 {
        return vector;
    }
    let text = text.to_lowercase();
    for token in TOKEN.find_iter(&text) {
        let mut digest = Blake2bVar::new(8).expect("8 is a valid BLAKE2b output size");
        digest.update(token.as_str().as_bytes());
        let mut bytes = [0; 8];
        digest
            .finalize_variable(&mut bytes)
            .expect("output has the selected digest size");
        let bucket =
            u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]) as usize % dimensions;
        vector[bucket] += if bytes[4] & 1 == 1 { 1.0 } else { -1.0 };
    }
    normalize(&mut vector);
    vector
}

/// L2-normalizes a vector in place, leaving a zero or invalid vector as all zeros.
pub fn normalize(vector: &mut [f32]) {
    let norm = norm(vector);
    if norm.is_finite() && norm > 0.0 {
        for value in vector {
            *value = (f64::from(*value) / norm) as f32;
        }
    } else {
        vector.fill(0.0);
    }
}

fn norm(vector: &[f32]) -> f64 {
    vector
        .iter()
        .map(|&value| f64::from(value).powi(2))
        .sum::<f64>()
        .sqrt()
}

/// Returns cosine similarity. Unlike-length vectors use only their shared prefix.
#[must_use]
pub fn cosine(left: &[f32], right: &[f32]) -> f64 {
    let len = left.len().min(right.len());
    let left = &left[..len];
    let right = &right[..len];
    let dot: f64 = left
        .iter()
        .zip(right)
        .map(|(&a, &b)| f64::from(a) * f64::from(b))
        .sum();
    let scale = norm(left) * norm(right);
    if scale > 0.0 && dot.is_finite() && scale.is_finite() {
        (dot / scale).clamp(-1.0, 1.0)
    } else {
        0.0
    }
}
