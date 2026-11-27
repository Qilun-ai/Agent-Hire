//! An encoder session with model metadata, tokenization, and CPU batching.

use std::path::Path;
use std::thread;

use ort::session::Session;
use ort::value::Tensor;
use tokenizers::{PaddingParams, Tokenizer, TruncationParams};

use crate::department;
use crate::level;
use crate::metadata::Metadata;
use crate::onnx_cpu;
use crate::prediction;
use crate::{Error, Job, Prediction};

const CPU_BATCH_SIZE: usize = 8;

/// Selects the posting fields and formatting expected by a trained encoder.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModelKind {
    /// Predicts a job's experience level.
    Level,
    /// Predicts a job's department subcategory.
    Department,
}

/// A loaded tokenizer and ONNX model. Reuse it to classify multiple batches.
pub struct Encoder {
    kind: ModelKind,
    metadata: Metadata,
    tokenizer: Tokenizer,
    session: Session,
}

impl Encoder {
    /// Loads `labels.json`, `tokenizer.json`, and the ONNX model from the given directory.
    ///
    /// If the metadata omits `model_file`, loads `model.onnx`.
    ///
    /// # Errors
    ///
    /// Returns an error if the files cannot be read, the configuration is invalid, or
    /// ONNX Runtime cannot load the graph on CPU.
    pub fn load(kind: ModelKind, directory: &Path) -> Result<Self, Error> {
        let metadata = Metadata::read(directory)?;
        let mut tokenizer =
            Tokenizer::from_file(directory.join("tokenizer.json")).map_err(|source| {
                Error::Tokenizer {
                    action: "load tokenizer.json",
                    source,
                }
            })?;
        tokenizer
            .with_truncation(Some(TruncationParams {
                max_length: metadata.max_len,
                ..TruncationParams::default()
            }))
            .map_err(|source| Error::Tokenizer {
                action: "configure truncation",
                source,
            })?;
        tokenizer.with_padding(Some(PaddingParams {
            pad_id: metadata.pad_id,
            ..PaddingParams::default()
        }));

        let parallelism = thread::available_parallelism().map_or(2, |threads| threads.get());
        let intra_threads = (parallelism / 2).clamp(1, 4);
        let session = onnx_cpu::session(&directory.join(metadata.model_file()?), intra_threads)?;

        Ok(Self {
            kind,
            metadata,
            tokenizer,
            session,
        })
    }

    /// Formats a posting using the model's training layout and metadata.
    #[must_use]
    pub fn build_text(&self, job: &Job<'_>) -> String {
        build_model_text(self.kind, &self.metadata, job)
    }

    /// Classifies the postings and returns one verdict per posting in input order.
    ///
    /// Batches group inputs by character count to reduce padding, keeping equal-length
    /// inputs in their original order.
    ///
    /// # Errors
    ///
    /// Returns an error if tokenization or inference fails, or if the model emits invalid logits.
    pub fn predict(&mut self, jobs: &[Job<'_>]) -> Result<Vec<Prediction>, Error> {
        let texts: Vec<String> = jobs.iter().map(|job| self.build_text(job)).collect();
        Ok(self
            .predict_texts_while(&texts, || true)?
            .expect("uninterrupted inference returns a prediction for each job"))
    }

    pub(crate) fn predict_texts_while(
        &mut self,
        texts: &[String],
        keep_running: impl Fn() -> bool,
    ) -> Result<Option<Vec<Prediction>>, Error> {
        let mut order: Vec<usize> = (0..texts.len()).collect();
        order.sort_by_cached_key(|&index| texts[index].chars().count());

        let mut predictions = vec![None; texts.len()];
        for batch in order.chunks(CPU_BATCH_SIZE) {
            if !keep_running() {
                return Ok(None);
            }
            let inputs: Vec<&str> = batch.iter().map(|&index| texts[index].as_str()).collect();
            let verdicts = self.predict_batch(inputs)?;
            for (&index, verdict) in batch.iter().zip(verdicts) {
                predictions[index] = Some(verdict);
            }
        }
        Ok(Some(
            predictions
                .into_iter()
                .map(|verdict| verdict.expect("every job belongs to an inference batch"))
                .collect(),
        ))
    }

    fn predict_batch(&mut self, texts: Vec<&str>) -> Result<Vec<Prediction>, Error> {
        let batch_size = texts.len();
        let encodings =
            self.tokenizer
                .encode_batch(texts, true)
                .map_err(|source| Error::Tokenizer {
                    action: "encode a batch",
                    source,
                })?;
        let width = encodings[0].len();
        let shape = [batch_size, width];
        let mut ids = Vec::with_capacity(batch_size * width);
        let mut mask = Vec::with_capacity(batch_size * width);
        for encoding in &encodings {
            ids.extend(encoding.get_ids().iter().copied().map(i64::from));
            mask.extend(encoding.get_attention_mask().iter().copied().map(i64::from));
        }
        let ids = Tensor::from_array((shape, ids.into_boxed_slice())).map_err(onnx_cpu::error)?;
        let mask = Tensor::from_array((shape, mask.into_boxed_slice())).map_err(onnx_cpu::error)?;
        let outputs = self
            .session
            .run(ort::inputs!["input_ids" => ids, "attention_mask" => mask])
            .map_err(onnx_cpu::error)?;
        let output = outputs
            .values()
            .next()
            .ok_or(Error::InvalidModel("expected an ONNX output"))?;
        let (shape, logits) = output
            .try_extract_tensor::<f32>()
            .map_err(onnx_cpu::error)?;
        let labels = &self.metadata.labels;
        if **shape != [batch_size as i64, labels.len() as i64] {
            return Err(Error::InvalidModel(
                "logits must have shape [batch_size, labels.len()]",
            ));
        }
        if logits.iter().any(|logit| !logit.is_finite()) {
            return Err(Error::InvalidModel("logits must be finite"));
        }
        Ok(logits
            .chunks_exact(labels.len())
            .map(|row| prediction::from_logits(row, labels))
            .collect())
    }
}

pub(crate) fn build_model_text(kind: ModelKind, metadata: &Metadata, job: &Job<'_>) -> String {
    let separator = metadata.separator();
    match kind {
        ModelKind::Level => level::build_text(job, metadata.input_version, separator),
        ModelKind::Department => department::build_text(job, metadata.cuts(), separator),
    }
}
