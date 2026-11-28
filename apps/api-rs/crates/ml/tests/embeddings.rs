//! Profile embedding tests for the hash fallback and the supplied MiniLM model.

use std::env;
use std::path::{Path, PathBuf};

use agent_hire_ml::{self as ml, Embedder, EmbeddingMode, MINILM_DIMENSIONS};

#[test]
fn hash_fallback_uses_signed_blake2b_buckets() {
    let vector = ml::hash_embedding("RUST rust Python SQL x", MINILM_DIMENSIONS);
    let norm = 6_f32.sqrt();
    assert_eq!(vector.len(), 384);
    assert!((vector[124] + 2.0 / norm).abs() < 1e-6);
    assert!((vector[168] + 1.0 / norm).abs() < 1e-6);
    assert!((vector[276] + 1.0 / norm).abs() < 1e-6);
    assert_eq!(
        vector
            .iter()
            .filter(|coordinate| **coordinate != 0.0)
            .count(),
        3
    );
    assert_eq!(ml::hash_embedding("x y Z", 384), vec![0.0; 384]);
    assert_eq!(ml::cosine(&[3.0, 4.0], &[3.0, 4.0, 100.0]), 1.0);
}

#[test]
fn missing_model_and_key_fall_back_without_changing_local_dimensions() {
    let absent = env::temp_dir().join(format!(
        "agent-hire-embedding-absent-{}",
        std::process::id()
    ));
    for provider in ["onnx", "unknown", "OpenAI"] {
        let embedder = Embedder::from_model_dir(&absent, provider, Some(""));
        assert!(!embedder.model_ready());
        assert!(embedder.runtime_status().is_none());
        assert!(embedder.embed(&[] as &[&str]).is_empty());
        assert!(embedder.runtime_status().is_none());
        let batch = embedder.embed_with_mode(&["Rust"]);
        assert_eq!(batch.mode, EmbeddingMode::Hash);
        assert_eq!(batch.vectors[0].len(), 384);
        assert_eq!(embedder.status()["onnx_error"], "ONNX model not downloaded");
        assert_eq!(
            embedder.runtime_status().expect("hash runtime used")["mode"],
            "hashing"
        );
        embedder.reset();
        assert!(embedder.runtime_status().is_none());
    }
    let configured_openai = Embedder::from_model_dir(&absent, "openai", Some("test-key"));
    assert_eq!(configured_openai.dimensions(), 1536);
    assert_eq!(configured_openai.status()["active_provider"], "openai");
}

#[test]
#[ignore = "requires the supplied MiniLM model directory"]
fn real_minilm_produces_normalized_semantic_vectors() {
    let directory = env::var_os("AGENT_HIRE_TEST_MINILM_DIR").map_or_else(
        || {
            PathBuf::from(concat!(
                env!("CARGO_MANIFEST_DIR"),
                "/../../target/contract-cache/all-MiniLM-L6-v2"
            ))
        },
        PathBuf::from,
    );
    let embedder = Embedder::from_model_dir(Path::new(&directory), "onnx", Some(""));
    assert!(embedder.model_ready());
    assert_eq!(embedder.status()["mode"], "onnx");
    let examples = [
        "Rust software engineer building scalable backend services.",
        "Backend developer who writes scalable software in Rust.",
        "A yellow banana and a bowl of fresh strawberries for breakfast.",
    ];
    let batch = embedder.embed_with_mode(&examples);
    assert_eq!(batch.mode, EmbeddingMode::Onnx, "{}", embedder.status());
    assert_eq!(batch.vectors.len(), 3);
    for vector in &batch.vectors {
        assert_eq!(vector.len(), 384);
        let norms = vector
            .iter()
            .map(|coordinate| coordinate * coordinate)
            .sum::<f32>();
        assert!((norms - 1.0).abs() < 1e-5);
    }
    let related = ml::cosine(&batch.vectors[0], &batch.vectors[1]);
    let unrelated = ml::cosine(&batch.vectors[0], &batch.vectors[2]);
    assert!(
        related > unrelated + 0.2,
        "related={related}, unrelated={unrelated}"
    );
    let single = embedder.embed(&examples[..1]);
    assert!(ml::cosine(&batch.vectors[0], &single[0]) > 0.99999);
}
