//! Errors from reading model metadata, tokenizing text, or running inference.

use std::error::Error as StdError;
use std::io::Error as IoError;
use std::path::PathBuf;

use thiserror::Error;

/// A failure to load or run an encoder.
#[derive(Debug, Error)]
#[non_exhaustive]
pub enum Error {
    /// A required model file could not be read.
    #[error("could not read {path:?}: {source}")]
    Read {
        /// The path that could not be read.
        path: PathBuf,
        /// The underlying I/O error.
        source: IoError,
    },
    /// The model metadata could not be parsed.
    #[error("could not parse {path:?}: {source}")]
    Metadata {
        /// The metadata file's path.
        path: PathBuf,
        /// The underlying JSON error.
        source: serde_json::Error,
    },
    /// The tokenizer could not be loaded, configured, or run.
    #[error("could not {action}: {source}")]
    Tokenizer {
        /// The operation in progress when the error occurred.
        action: &'static str,
        /// The underlying tokenizer error.
        source: Box<dyn StdError + Send + Sync>,
    },
    /// ONNX Runtime could not initialize, load, or run the model.
    #[error("ONNX Runtime: {0}")]
    Runtime(String),
    /// The metadata or logits do not meet the encoder's requirements.
    #[error("invalid encoder model: {0}")]
    InvalidModel(&'static str),
}
