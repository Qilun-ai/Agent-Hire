//! MiniLM tokenization, fixed-length batching, and masked mean pooling.

use std::path::Path;
use std::thread;

use ort::session::Session;
use ort::value::Tensor;
use tokenizers::{PaddingParams, PaddingStrategy, Tokenizer, TruncationParams};

use crate::Error;
use crate::embedder::MINILM_DIMENSIONS;
use crate::onnx_cpu;

const LENGTH: usize = 128;
const BATCH: usize = 32;

pub(crate) struct MiniLm {
    tokenizer: Tokenizer,
    session: Session,
}

impl MiniLm {
    pub(crate) fn load(directory: &Path) -> Result<Self, Error> {
        let mut tokenizer =
            Tokenizer::from_file(directory.join("tokenizer.json")).map_err(|source| {
                Error::Tokenizer {
                    action: "load MiniLM tokenizer.json",
                    source,
                }
            })?;
        tokenizer
            .with_truncation(Some(TruncationParams {
                max_length: LENGTH,
                ..TruncationParams::default()
            }))
            .map_err(|source| Error::Tokenizer {
                action: "configure MiniLM truncation",
                source,
            })?;
        tokenizer.with_padding(Some(PaddingParams {
            strategy: PaddingStrategy::Fixed(LENGTH),
            pad_id: 0,
            pad_token: "[PAD]".to_owned(),
            ..PaddingParams::default()
        }));
        let threads = thread::available_parallelism().map_or(1, |threads| threads.get().min(4));
        let session = onnx_cpu::session(&directory.join("model.onnx"), threads)?;
        Ok(Self { tokenizer, session })
    }

    pub(crate) fn embed(&mut self, texts: &[&str]) -> Result<Vec<Vec<f32>>, Error> {
        let mut vectors = Vec::with_capacity(texts.len());
        for batch in texts.chunks(BATCH) {
            vectors.extend(self.batch(batch)?);
        }
        Ok(vectors)
    }

    fn batch(&mut self, texts: &[&str]) -> Result<Vec<Vec<f32>>, Error> {
        let encodings = self
            .tokenizer
            .encode_batch(texts.to_vec(), true)
            .map_err(|source| Error::Tokenizer {
                action: "encode MiniLM inputs",
                source,
            })?;
        if encodings.iter().any(|encoding| encoding.len() != LENGTH) {
            return Err(Error::InvalidModel(
                "MiniLM inputs must be padded to 128 tokens",
            ));
        }
        let shape = [texts.len(), LENGTH];
        let mut ids = Vec::with_capacity(texts.len() * LENGTH);
        let mut mask = Vec::with_capacity(texts.len() * LENGTH);
        for encoding in &encodings {
            ids.extend(encoding.get_ids().iter().copied().map(i64::from));
            mask.extend(encoding.get_attention_mask().iter().copied().map(i64::from));
        }

        let input_ids =
            Tensor::from_array((shape, ids.into_boxed_slice())).map_err(onnx_cpu::error)?;
        let attention = Tensor::from_array((shape, mask.clone().into_boxed_slice()))
            .map_err(onnx_cpu::error)?;
        let types = Tensor::from_array((shape, vec![0_i64; mask.len()].into_boxed_slice()))
            .map_err(onnx_cpu::error)?;
        let outputs = self
            .session
            .run(ort::inputs![
                "input_ids" => input_ids, "attention_mask" => attention, "token_type_ids" => types
            ])
            .map_err(onnx_cpu::error)?;
        if outputs.len() == 0 {
            return Err(Error::InvalidModel("MiniLM must return token embeddings"));
        }
        let output = match outputs.get("last_hidden_state") {
            Some(output) => output,
            None => &outputs[0],
        };
        let (shape, values) = output
            .try_extract_tensor::<f32>()
            .map_err(onnx_cpu::error)?;
        if **shape != [texts.len() as i64, LENGTH as i64, MINILM_DIMENSIONS as i64] {
            return Err(Error::InvalidModel(
                "MiniLM output must have shape [batch, 128, 384]",
            ));
        }
        if values.iter().any(|value| !value.is_finite()) {
            return Err(Error::InvalidModel("MiniLM produced non-finite embeddings"));
        }
        let mut pooled = Vec::with_capacity(texts.len());
        let sequences = values.chunks_exact(LENGTH * MINILM_DIMENSIONS);
        for (sequence, attention) in sequences.zip(mask.chunks_exact(LENGTH)) {
            pooled.push(mean_pool(sequence, attention));
        }
        Ok(pooled)
    }
}

fn mean_pool(sequence: &[f32], attention: &[i64]) -> Vec<f32> {
    let mut mean = vec![0.0; MINILM_DIMENSIONS];
    let mut count = 0.0_f32;
    for (embedding, &keep) in sequence.chunks_exact(MINILM_DIMENSIONS).zip(attention) {
        if keep > 0 {
            count += 1.0;
            for (sum, value) in mean.iter_mut().zip(embedding) {
                *sum += value;
            }
        }
    }
    for value in &mut mean {
        *value /= count.max(1e-9);
    }
    let norm = mean
        .iter()
        .map(|value| value * value)
        .sum::<f32>()
        .sqrt()
        .max(1e-9);
    for value in &mut mean {
        *value /= norm;
    }
    mean
}

#[cfg(test)]
mod tests {
    use super::{MINILM_DIMENSIONS, mean_pool};

    #[test]
    fn excludes_padding_before_l2_normalization() {
        let mut values = vec![0.0; 3 * MINILM_DIMENSIONS];
        values[0] = 3.0;
        values[MINILM_DIMENSIONS + 1] = 4.0;
        values[2 * MINILM_DIMENSIONS] = 200.0;
        let result = mean_pool(&values, &[1, 1, 0]);
        assert!((result[0] - 0.6).abs() < 1e-6);
        assert!((result[1] - 0.8).abs() < 1e-6);
        assert_eq!(mean_pool(&values, &[0, 0, 0]), vec![0.0; MINILM_DIMENSIONS]);
    }
}
