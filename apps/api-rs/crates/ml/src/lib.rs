//! Local ONNX encoders for job experience levels and departments.

mod department;
mod embedder;
pub mod embedding_text;
mod encoder;
mod encoder_worker;
mod error;
mod job;
mod job_index_runtime;
mod level;
mod metadata;
mod minilm;
mod onnx_cpu;
mod prediction;
mod text;
mod vector;
mod worker_signal;

pub use embedder::{Embedder, EmbeddingBatch, EmbeddingMode, MINILM_DIMENSIONS};
pub use encoder::{Encoder, ModelKind};
pub use error::Error;
pub use job::Job;
pub use job_index_runtime::JobIndexRuntime;
pub use prediction::Prediction;
pub use vector::{cosine, hash_embedding, normalize};
