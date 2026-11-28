//! Process-wide ONNX Runtime environment shared by classifiers and embeddings.

use std::fmt::Display;
use std::path::Path;
use std::sync::LazyLock;

use ort::Result as OrtResult;
use ort::environment::Environment;
use ort::ep::CPU;
use ort::session::Session;
use ort::session::builder::GraphOptimizationLevel;

use crate::Error;

static ENVIRONMENT: LazyLock<OrtResult<Environment>> =
    LazyLock::new(|| ort::init().with_telemetry(false).build());

pub(crate) fn session(model: &Path, intra_threads: usize) -> Result<Session, Error> {
    let environment = ENVIRONMENT.as_ref().map_err(error)?;
    // TODO: Add an opt-in CUDA provider with CPU fallback when the required libraries are usable.
    Session::builder(environment)
        .map_err(error)?
        .with_execution_providers([CPU::default().with_arena_allocator(true).build()])
        .map_err(error)?
        .with_optimization_level(GraphOptimizationLevel::All)
        .map_err(error)?
        .with_intra_threads(intra_threads)
        .map_err(error)?
        .commit_from_file(model)
        .map_err(error)
}

pub(crate) fn error(source: impl Display) -> Error {
    Error::Runtime(source.to_string())
}
