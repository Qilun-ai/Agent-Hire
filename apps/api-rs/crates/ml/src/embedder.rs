//! Lazy profile embeddings with provider selection and dimension-preserving fallback.

use std::env;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, PoisonError, Weak};
use std::thread;
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::{Value, json};

use crate::minilm::MiniLm;
use crate::vector;

/// The size of MiniLM and default hash vectors.
pub const MINILM_DIMENSIONS: usize = 384;
const OPENAI_DIMENSIONS: usize = 1536;
const MINILM_MODEL: &str = "all-MiniLM-L6-v2";
const OPENAI_MODEL: &str = "text-embedding-3-small";

/// The provider that actually produced a batch.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[non_exhaustive]
pub enum EmbeddingMode {
    /// Local MiniLM.
    Onnx,
    /// OpenAI text embeddings.
    OpenAi,
    /// Deterministic token hashing.
    Hash,
}

impl EmbeddingMode {
    /// The provider mode used in ranking explanations and status responses.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Onnx => "onnx",
            Self::OpenAi => "openai",
            Self::Hash => "hashing",
        }
    }

    /// Whether this mode can supply semantic evidence.
    #[must_use]
    pub fn is_semantic(self) -> bool {
        self != Self::Hash
    }
}

/// An embedding batch with its actual source, including any fallback.
#[derive(Debug)]
pub struct EmbeddingBatch {
    /// One vector per input in its original order.
    pub vectors: Vec<Vec<f32>>,
    /// The provider that produced every vector in this batch.
    pub mode: EmbeddingMode,
}

/// Reusable embedding provider. Clones share one lazy ONNX session.
#[derive(Clone)]
pub struct Embedder {
    inner: Arc<Inner>,
}

struct Inner {
    directory: PathBuf,
    configured: String,
    key: String,
    idle: Option<Duration>,
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    session: Option<Result<Box<MiniLm>, String>>,
    last_used: Option<Instant>,
    watching: bool,
    onnx_error: Option<String>,
    openai_error: Option<String>,
    runtime_mode: Option<EmbeddingMode>,
}

impl Embedder {
    /// Opens the default local provider under the supplied app data directory.
    #[must_use]
    pub fn new(app_data_dir: &Path) -> Self {
        Self::with_provider(app_data_dir, "onnx", Some(""))
    }

    /// Configures the provider with a local model or the packaged desktop model.
    ///
    /// Pass `None` for the OpenAI key only if settings could not be read. An explicitly empty
    /// key allows local inference and takes precedence over `OPENAI_API_KEY`.
    #[must_use]
    pub fn with_provider(app_data_dir: &Path, provider: &str, openai_key: Option<&str>) -> Self {
        let local = app_data_dir.join("models").join(MINILM_MODEL);
        let directory = env::var_os("AGENT_HIRE_MINILM_MODEL_DIR")
            .filter(|path| !path.is_empty())
            .map(PathBuf::from)
            .or_else(|| {
                if valid_model_dir(&local) {
                    return None;
                }
                env::var_os("AGENT_HIRE_BUNDLED_MODELS_DIR")
                    .filter(|path| !path.is_empty())
                    .map(|path| PathBuf::from(path).join(MINILM_MODEL))
                    .filter(|path| valid_model_dir(path))
            })
            .unwrap_or(local);
        Self::from_model_dir(&directory, provider, openai_key)
    }

    /// Configures the provider with an explicit MiniLM model directory.
    #[must_use]
    pub fn from_model_dir(model_dir: &Path, provider: &str, openai_key: Option<&str>) -> Self {
        let provider = provider.trim().to_lowercase();
        let configured = if provider.is_empty() {
            "onnx".to_owned()
        } else {
            provider
        };
        let key = if configured == "openai" {
            openai_key.map_or_else(
                || env::var("OPENAI_API_KEY").unwrap_or_default(),
                str::to_owned,
            )
        } else {
            String::new()
        };
        let idle = env::var("AGENT_HIRE_EMBEDDING_IDLE_UNLOAD_S")
            .ok()
            .and_then(|seconds| seconds.parse::<f64>().ok())
            .unwrap_or(600.0);
        let idle = Duration::try_from_secs_f64(idle)
            .ok()
            .filter(|duration| !duration.is_zero());
        Self {
            inner: Arc::new(Inner {
                directory: model_dir.to_owned(),
                configured,
                key: key.trim().to_owned(),
                idle,
                state: Mutex::default(),
            }),
        }
    }

    /// Reports whether both files required for local inference are installed.
    #[must_use]
    pub fn model_ready(&self) -> bool {
        valid_model_dir(&self.inner.directory)
    }

    /// Discards the ONNX session and cached failures, for example after model installation.
    pub fn reset(&self) {
        let mut state = self
            .inner
            .state
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        state.session = None;
        state.last_used = None;
        state.onnx_error = None;
        state.openai_error = None;
        state.runtime_mode = None;
    }

    /// Returns the configured embedding width, including when inference falls back.
    #[must_use]
    pub fn dimensions(&self) -> usize {
        if self.uses_openai() {
            OPENAI_DIMENSIONS
        } else {
            MINILM_DIMENSIONS
        }
    }

    /// Embeds each input in order. Failed inference falls back to hashing.
    #[must_use]
    pub fn embed(&self, texts: &[impl AsRef<str>]) -> Vec<Vec<f32>> {
        self.embed_with_mode(texts).vectors
    }

    /// Embeds each input and reports the actual provider used by the batch.
    #[must_use]
    pub fn embed_with_mode(&self, texts: &[impl AsRef<str>]) -> EmbeddingBatch {
        let texts: Vec<&str> = texts.iter().map(AsRef::as_ref).collect();
        let dimensions = self.dimensions();
        if texts.is_empty() {
            return EmbeddingBatch {
                vectors: Vec::new(),
                mode: self.selected_mode(),
            };
        }
        if self.uses_openai() {
            let result = openai(&texts, &self.inner.key);
            let mut state = self
                .inner
                .state
                .lock()
                .unwrap_or_else(PoisonError::into_inner);
            match result {
                Ok(vectors) => {
                    state.openai_error = None;
                    state.runtime_mode = Some(EmbeddingMode::OpenAi);
                    return EmbeddingBatch {
                        vectors,
                        mode: EmbeddingMode::OpenAi,
                    };
                }
                Err(error) => {
                    eprintln!("OpenAI embedding failed, falling back to hash@1536: {error}");
                    state.openai_error = Some(error);
                }
            }
        } else if self.inner.configured != "hash" {
            let mut state = self
                .inner
                .state
                .lock()
                .unwrap_or_else(PoisonError::into_inner);
            let session = state.session.get_or_insert_with(|| {
                if self.model_ready() {
                    MiniLm::load(&self.inner.directory)
                        .map(Box::new)
                        .map_err(|error| error.to_string())
                } else {
                    Err("ONNX model not downloaded".to_owned())
                }
            });
            let result = match session {
                Ok(model) => model.embed(&texts).map_err(|error| error.to_string()),
                Err(error) => Err(error.clone()),
            };
            if matches!(state.session, Some(Ok(_))) {
                state.last_used = Some(Instant::now());
                if !state.watching
                    && let Some(idle) = self.inner.idle
                {
                    let weak = Arc::downgrade(&self.inner);
                    state.watching = thread::Builder::new()
                        .name("embedding-idle".to_owned())
                        .spawn(move || unload_idle(&weak, idle))
                        .is_ok();
                }
            }
            match result {
                Ok(vectors) => {
                    state.onnx_error = None;
                    state.runtime_mode = Some(EmbeddingMode::Onnx);
                    return EmbeddingBatch {
                        vectors,
                        mode: EmbeddingMode::Onnx,
                    };
                }
                Err(error) => {
                    if state.onnx_error.as_deref() != Some(error.as_str()) {
                        eprintln!("ONNX embedding failed, falling back to hash@384: {error}");
                    }
                    state.onnx_error = Some(error);
                }
            }
        }
        let vectors = texts
            .into_iter()
            .map(|text| vector::hash_embedding(text, dimensions))
            .collect();
        self.inner
            .state
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .runtime_mode = Some(EmbeddingMode::Hash);
        EmbeddingBatch {
            vectors,
            mode: EmbeddingMode::Hash,
        }
    }

    /// Returns the selected provider and last runtime degradation without loading the model.
    #[must_use]
    pub fn status(&self) -> Value {
        let state = self
            .inner
            .state
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        self.describe(&state, self.selected_mode())
    }

    /// Describes the runtime after actual inference, or returns None while it is unloaded.
    #[must_use]
    pub fn runtime_status(&self) -> Option<Value> {
        let state = self
            .inner
            .state
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let mode = state.runtime_mode?;
        let selected = if mode == EmbeddingMode::Hash {
            self.selected_mode()
        } else {
            mode
        };
        let mut status = self.describe(&state, selected);
        status["mode"] = json!(mode.as_str());
        Some(status)
    }

    fn describe(&self, state: &State, mode: EmbeddingMode) -> Value {
        let mut status = json!({"status":"ok","configured_provider":self.inner.configured});
        match mode {
            EmbeddingMode::OpenAi => {
                status["active_provider"] = json!("openai");
                status["dims"] = json!(OPENAI_DIMENSIONS);
                status["model"] = json!(OPENAI_MODEL);
                status["mode"] = json!("openai");
                if let Some(error) = &state.openai_error {
                    status["model"] = json!("built-in hashing embedder (OpenAI unavailable)");
                    status["mode"] = json!("hashing");
                    status["degraded"] = json!(true);
                    status["openai_error"] = json!(error);
                }
            }
            EmbeddingMode::Onnx => {
                status["active_provider"] = json!("onnx");
                status["dims"] = json!(MINILM_DIMENSIONS);
                status["model_path"] = json!(self.inner.directory);
                status["model"] = json!(MINILM_MODEL);
                status["mode"] = json!("onnx");
                if let Some(error) = &state.onnx_error {
                    status["model"] = json!("built-in hashing embedder (ONNX unavailable)");
                    status["mode"] = json!("hashing");
                    status["degraded"] = json!(true);
                    status["onnx_error"] = json!(error);
                }
            }
            EmbeddingMode::Hash => {
                status["active_provider"] = json!("hash");
                status["model"] = json!("built-in hashing embedder");
                status["dims"] = json!(MINILM_DIMENSIONS);
                status["mode"] = json!("hashing");
                if let Some(error) = &state.onnx_error {
                    status["onnx_error"] = json!(error);
                } else if self.inner.configured != "hash" {
                    status["onnx_error"] = json!("ONNX model not downloaded");
                }
            }
        }
        status
    }

    fn uses_openai(&self) -> bool {
        self.inner.configured == "openai" && !self.inner.key.is_empty()
    }

    fn selected_mode(&self) -> EmbeddingMode {
        if self.uses_openai() {
            EmbeddingMode::OpenAi
        } else if self.inner.configured == "hash" || !self.model_ready() {
            EmbeddingMode::Hash
        } else {
            EmbeddingMode::Onnx
        }
    }
}

fn valid_model_dir(directory: &Path) -> bool {
    directory.join("model.onnx").is_file() && directory.join("tokenizer.json").is_file()
}

fn unload_idle(inner: &Weak<Inner>, idle: Duration) {
    loop {
        thread::sleep(idle.min(Duration::from_secs(60)));
        let Some(inner) = inner.upgrade() else { return };
        let mut state = inner.state.lock().unwrap_or_else(PoisonError::into_inner);
        let Some(last_used) = state.last_used else {
            state.watching = false;
            return;
        };
        if last_used.elapsed() >= idle {
            state.session = None;
            state.last_used = None;
            state.runtime_mode = None;
            state.watching = false;
            eprintln!(
                "ONNX embedding model unloaded after {} s idle",
                idle.as_secs_f64()
            );
            return;
        }
    }
}

fn openai(texts: &[&str], key: &str) -> Result<Vec<Vec<f32>>, String> {
    #[derive(Deserialize)]
    struct Response {
        data: Vec<Item>,
    }
    #[derive(Deserialize)]
    struct Item {
        index: usize,
        embedding: Vec<f32>,
    }
    let mut response = ureq::post("https://api.openai.com/v1/embeddings")
        .header("Authorization", format!("Bearer {key}"))
        .config()
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(30)))
        .build()
        .send_json(json!({"model":OPENAI_MODEL,"input":texts}))
        .map_err(|error| error.to_string())?;
    let response: Response = response
        .body_mut()
        .read_json()
        .map_err(|error| error.to_string())?;
    let mut data = response.data;
    data.sort_by_key(|item| item.index);
    if data.len() != texts.len()
        || data
            .iter()
            .enumerate()
            .any(|(index, item)| index != item.index)
    {
        return Err("OpenAI returned unexpected embedding indices".to_owned());
    }
    for item in &data {
        if item.embedding.len() != OPENAI_DIMENSIONS {
            return Err(format!(
                "OpenAI returned dim {}, expected {OPENAI_DIMENSIONS}",
                item.embedding.len()
            ));
        }
        if item.embedding.iter().any(|value| !value.is_finite()) {
            return Err("OpenAI returned non-finite embeddings".to_owned());
        }
    }
    Ok(data.into_iter().map(|item| item.embedding).collect())
}
