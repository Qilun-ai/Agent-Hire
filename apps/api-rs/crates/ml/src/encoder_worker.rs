//! Backlog passes for one encoder, with remote execution and local CPU fallback.

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use agent_hire_jobindex::decision;
use agent_hire_jobindex::decision_store::{self, QueuedVerdict};
use agent_hire_jobindex::kaggle::{KAGGLE_MIN_JOBS, KaggleManager};
use agent_hire_jobindex::{self as jobindex, JobIndex};

use crate::encoder;
use crate::metadata::Metadata;
use crate::worker_signal::WorkerSignal;
use crate::{Encoder, Job, ModelKind, Prediction};

const LOCAL_TICK_JOBS: i64 = 512;
const REMOTE_TICK_JOBS: i64 = 4096;
const IDLE: Duration = Duration::from_secs(20);

pub(crate) struct EncoderWorker<'a> {
    pub(crate) index: &'a JobIndex,
    pub(crate) kind: ModelKind,
    pub(crate) path: &'a Path,
    pub(crate) kaggle: &'a KaggleManager,
    pub(crate) loaded: &'a AtomicBool,
}

impl EncoderWorker<'_> {
    pub(crate) fn run(&self, signal: &WorkerSignal, invalidate_counts: impl Fn()) {
        let mut encoder = None;
        let mut revision = 0;
        while signal.wait(IDLE, Some(&mut revision)) {
            while !signal.stopped() {
                match self.classify_backlog(&mut encoder, signal) {
                    Ok(0) => break,
                    Ok(_) => invalidate_counts(),
                    Err(error) => {
                        eprintln!("{} model worker failed: {error}", self.name());
                        break;
                    }
                }
            }
        }
    }

    fn classify_backlog(
        &self,
        encoder: &mut Option<Encoder>,
        signal: &WorkerSignal,
    ) -> Result<usize, Box<dyn std::error::Error>> {
        if !jobindex::model_available(self.path) {
            return Ok(0);
        }
        let mode = self.kaggle.encoder_device();
        let ready = mode != "local" && self.kaggle.classifiers_ready();
        let limit = if ready {
            REMOTE_TICK_JOBS
        } else {
            LOCAL_TICK_JOBS
        };
        let queued = decision_store::pending_jobs(self.index, self.role_key(), limit)?;
        if queued.is_empty() {
            return Ok(0);
        }
        let jobs: Vec<Job<'_>> = queued
            .iter()
            .map(|job| Job {
                title: &job.title,
                description: &job.description,
                department: &job.department,
                company: &job.company,
            })
            .collect();
        let keep_running = || !signal.stopped();
        let remote = ready && (mode == "kaggle" || jobs.len() >= KAGGLE_MIN_JOBS);
        let mut texts = Vec::with_capacity(jobs.len());
        if remote {
            let metadata = Metadata::read(self.path)?;
            for job in &jobs {
                if !keep_running() {
                    return Ok(0);
                }
                texts.push(encoder::build_model_text(self.kind, &metadata, job));
            }
        } else {
            let local = self.local_encoder(encoder)?;
            for job in &jobs {
                if !keep_running() {
                    return Ok(0);
                }
                texts.push(local.build_text(job));
            }
        }
        if !keep_running() {
            return Ok(0);
        }
        let predictions = if remote {
            match self.kaggle.encode(self.name(), &texts) {
                Ok(rows) => Some(
                    rows.into_iter()
                        .map(|row| Prediction {
                            label: row.label,
                            probability: decision::round_probability(row.probability),
                        })
                        .collect(),
                ),
                Err(error) => {
                    eprintln!(
                        "{} model: Kaggle request failed ({error}); using CPU",
                        self.name()
                    );
                    if !keep_running() {
                        return Ok(0);
                    }
                    self.local_encoder(encoder)?
                        .predict_texts_while(&texts, keep_running)?
                }
            }
        } else {
            self.local_encoder(encoder)?
                .predict_texts_while(&texts, keep_running)?
        };
        let Some(predictions) = predictions else {
            return Ok(0);
        };
        let verdicts: Vec<QueuedVerdict<'_>> = queued
            .iter()
            .zip(&predictions)
            .map(|(job, prediction)| QueuedVerdict {
                job,
                role_match: true,
                probability: Some(prediction.probability),
                label: Some(&prediction.label),
            })
            .collect();
        Ok(decision_store::save_verdicts(
            self.index,
            self.role_key(),
            "encoder",
            &verdicts,
        )?)
    }

    fn local_encoder<'s>(
        &self,
        stored: &'s mut Option<Encoder>,
    ) -> Result<&'s mut Encoder, crate::Error> {
        if let Some(encoder) = stored {
            return Ok(encoder);
        }
        let encoder = Encoder::load(self.kind, self.path)?;
        self.loaded.store(true, Ordering::Release);
        Ok(stored.insert(encoder))
    }

    fn role_key(&self) -> &'static str {
        match self.kind {
            ModelKind::Level => jobindex::LEVEL_ROLE_KEY,
            ModelKind::Department => jobindex::DEPARTMENT_ROLE_KEY,
        }
    }

    fn name(&self) -> &'static str {
        match self.kind {
            ModelKind::Level => "level",
            ModelKind::Department => "department",
        }
    }
}
