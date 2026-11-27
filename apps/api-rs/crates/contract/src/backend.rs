//! Starting and stopping one isolated backend instance.

use std::env;
use std::fs;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::sync::{Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const STARTUP_TIMEOUT: Duration = Duration::from_secs(180);
const DEFAULT_MAX_BACKENDS: usize = 4;

/// The shared secret the agent presents to `/api/v1/agent-internal/*` in contract runs.
pub(crate) const AGENT_SECRET: &str = "contract-agent-secret";

/// Which backend implementation the suite talks to.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Target {
    /// An explicitly selected Python reference installation.
    Python {
        /// The archived API directory containing its virtual environment.
        api_dir: PathBuf,
    },
    /// The Rust backend binary.
    Rust {
        /// Path to the server executable.
        binary: PathBuf,
    },
    /// An already-running backend; the suite neither starts nor stops it.
    External {
        /// For example `http://127.0.0.1:8110`.
        base_url: String,
    },
}

impl Target {
    /// Reads `CONTRACT_BASE_URL`, `CONTRACT_BACKEND`, `CONTRACT_RUST_BIN` and `CONTRACT_PYTHON_API_DIR`.
    ///
    /// # Panics
    ///
    /// Panics if the backend name is unknown or the Python target has no archive directory.
    pub fn from_env() -> Self {
        if let Ok(base_url) = env::var("CONTRACT_BASE_URL") {
            return Target::External {
                base_url: base_url.trim_end_matches('/').to_owned(),
            };
        }
        match env::var("CONTRACT_BACKEND").as_deref() {
            Ok("python") => {
                let directory = env::var_os("CONTRACT_PYTHON_API_DIR").filter(|value| !value.is_empty())
                    .expect("CONTRACT_BACKEND=python requires CONTRACT_PYTHON_API_DIR pointing to the archived API directory");
                let api_dir =
                    std::path::absolute(directory).expect("resolve CONTRACT_PYTHON_API_DIR");
                Target::Python { api_dir }
            }
            Err(_) | Ok("rust") => {
                let binary = env::var_os("CONTRACT_RUST_BIN")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| {
                        workspace_root().join(format!(
                            "target/debug/agent-hire-api{}",
                            env::consts::EXE_SUFFIX
                        ))
                    });
                Target::Rust { binary }
            }
            Ok(other) => panic!("CONTRACT_BACKEND must be `python` or `rust`, got {other:?}"),
        }
    }
}

/// One running backend with its own data directories, stopped when dropped.
pub struct Backend {
    base_url: String,
    data_root: PathBuf,
    child: Option<Child>,
    _slot: Option<SlotGuard>,
}

impl Backend {
    /// Starts `target` with all of its state under `data_root` and waits until `/health` answers.
    ///
    /// The process gets a cleared environment: a private `HOME`, XDG data dir and a minimal
    /// `PATH`, so nothing on this machine (real databases, CLI logins) leaks into the contract.
    ///
    /// # Panics
    ///
    /// Panics if the backend cannot be started or does not become healthy in time.
    pub fn start(target: &Target, data_root: &Path) -> Backend {
        Self::start_with_environment(target, data_root, &[])
    }

    /// Starts an isolated backend with additional per-test environment variables.
    pub(crate) fn start_with_environment(
        target: &Target,
        data_root: &Path,
        extra: &[(&str, &str)],
    ) -> Backend {
        if let Target::External { base_url } = target {
            return Backend {
                base_url: base_url.clone(),
                data_root: data_root.to_owned(),
                child: None,
                _slot: None,
            };
        }
        let slot = SlotGuard::acquire();
        if data_root.exists() {
            fs::remove_dir_all(data_root).expect("clearing the previous run's data directory");
        }
        let dirs = DataDirs::create(data_root);
        let mut command = match target {
            Target::Python { api_dir } => python_command(api_dir),
            Target::Rust { binary } => {
                let mut command = Command::new(binary);
                command.args(["--port", "0", "--no-token-stdout"]);
                command
            }
            Target::External { .. } => unreachable!("handled above"),
        };
        dirs.apply(&mut command);
        command.envs(extra.iter().copied());
        let log = File::create(data_root.join("backend.log")).expect("creating backend.log");
        command
            .stdout(Stdio::piped())
            .stderr(log)
            .stdin(Stdio::null());
        // Its own process group, so stopping it also stops any subprocesses it started.
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;

            command.process_group(0);
        }
        let mut child = command
            .spawn()
            .unwrap_or_else(|e| panic!("starting {target:?}: {e}"));
        let stdout = child.stdout.take().expect("stdout is piped");
        let Some(port) = read_port(stdout, data_root) else {
            // No `Backend` owns the child yet, so stop it here or it outlives the test binary.
            stop_group(&mut child);
            let _ = child.kill();
            let _ = child.wait();
            panic!(
                "backend never printed PORT:<n>; see {}/backend.log",
                data_root.display()
            );
        };
        let backend = Backend {
            base_url: format!("http://127.0.0.1:{port}"),
            data_root: data_root.to_owned(),
            child: Some(child),
            _slot: Some(slot),
        };
        backend.wait_healthy();
        backend
    }

    /// The backend's base URL, without a trailing slash.
    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    /// The directory holding this instance's databases, logs and temp files.
    pub fn data_root(&self) -> &Path {
        &self.data_root
    }

    fn wait_healthy(&self) {
        let deadline = Instant::now() + STARTUP_TIMEOUT;
        let url = format!("{}/health", self.base_url);
        while Instant::now() < deadline {
            if let Ok(response) = ureq::get(&url).call()
                && response.status().is_success()
            {
                return;
            }
            thread::sleep(Duration::from_millis(250));
        }
        panic!(
            "backend at {} never became healthy; see {}/backend.log",
            url,
            self.data_root.display()
        );
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        if let Some(child) = self.child.as_mut() {
            // Ignore errors: the process may already have exited.
            stop_group(child);
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// Asks the child's whole process group to terminate and waits a little for the leader to exit.
#[cfg(unix)]
fn stop_group(child: &mut Child) {
    let group = format!("-{}", child.id());
    let _ = Command::new("kill").args(["-TERM", "--", &group]).status();
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if let Ok(Some(_)) = child.try_wait() {
            break;
        }
        thread::sleep(Duration::from_millis(50));
    }
    let _ = Command::new("kill")
        .args(["-KILL", "--", &group])
        .stderr(Stdio::null())
        .status();
}

#[cfg(not(unix))]
fn stop_group(_child: &mut Child) {}

/// The per-instance directories every backend is pointed at.
struct DataDirs {
    root: PathBuf,
}

impl DataDirs {
    fn create(root: &Path) -> DataDirs {
        for sub in [
            "appdata/models",
            "jobs",
            "scrape-runs",
            "agent",
            "run",
            "home",
            "xdg",
        ] {
            fs::create_dir_all(root.join(sub)).expect("creating backend data directories");
        }
        link_embedding_model(&root.join("appdata/models"));
        DataDirs {
            root: root.to_owned(),
        }
    }

    fn apply(&self, command: &mut Command) {
        let path = |sub: &str| self.root.join(sub);
        command.env_clear();
        for keep in ["SYSTEMROOT", "TEMP", "TMP", "LANG"] {
            if let Some(value) = env::var_os(keep) {
                command.env(keep, value);
            }
        }
        let system_path = if cfg!(windows) {
            env::var("PATH").unwrap_or_default()
        } else {
            "/usr/local/bin:/usr/bin:/bin".to_owned()
        };
        command
            .env("PATH", system_path)
            .env("HOME", path("home"))
            .env("USERPROFILE", path("home"))
            .env("XDG_DATA_HOME", path("xdg"))
            .env("LOCALAPPDATA", path("xdg"))
            .env("AGENT_HIRE_DATA_DIR", path("appdata"))
            .env("AGENT_HIRE_JOBS_DIR", path("jobs"))
            .env("AGENT_HIRE_SCRAPE_RUNS_DIR", path("scrape-runs"))
            .env("AGENT_HIRE_AGENT_DIR", path("agent"))
            .env("AGENT_HIRE_RUN_DIR", path("run"))
            .env("AGENT_HIRE_AGENT_SECRET", AGENT_SECRET);
    }
}

/// Points the instance at the cached MiniLM ONNX model so no run downloads it.
///
/// The model is copied once from `CONTRACT_MODEL_SOURCE` (default: the installed app's copy) into
/// the workspace's `target/contract-cache`, then linked (or copied, where links are unavailable).
fn link_embedding_model(models_dir: &Path) {
    const MODEL: &str = "all-MiniLM-L6-v2";
    let cache = workspace_root().join("target/contract-cache").join(MODEL);
    if !cache.join("model.onnx").exists() {
        let Some(source) = env::var_os("CONTRACT_MODEL_SOURCE")
            .map(PathBuf::from)
            .or_else(|| {
                env::var_os("AGENT_HIRE_DATA_DIR")
                    .map(|d| PathBuf::from(d).join("models").join(MODEL))
            })
            .filter(|s| s.join("model.onnx").exists())
        else {
            // No cached model: the backend falls back to downloading it, which is slower but works.
            return;
        };
        fs::create_dir_all(&cache).expect("creating the model cache");
        for file in [
            "model.onnx",
            "tokenizer.json",
            "tokenizer_config.json",
            "config.json",
        ] {
            fs::copy(source.join(file), cache.join(file)).expect("copying the embedding model");
        }
    }
    let target = models_dir.join(MODEL);
    #[cfg(unix)]
    std::os::unix::fs::symlink(&cache, &target).expect("linking the embedding model");
    #[cfg(not(unix))]
    {
        fs::create_dir_all(&target).expect("creating the model directory");
        for entry in fs::read_dir(&cache).expect("reading the model cache") {
            let entry = entry.expect("reading a model cache entry");
            fs::copy(entry.path(), target.join(entry.file_name())).expect("copying the model");
        }
    }
}

fn python_command(api_dir: &Path) -> Command {
    let python = if cfg!(windows) {
        api_dir.join(".venv/Scripts/python.exe")
    } else {
        api_dir.join(".venv/bin/python")
    };
    assert!(
        python.is_file(),
        "No Python interpreter at {}. Set up the archived API virtual environment first.",
        python.display()
    );
    let mut command = Command::new(python);
    command
        .current_dir(api_dir)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONUNBUFFERED", "1")
        .args(["main.py", "--port", "0", "--no-token-stdout"]);
    command
}

/// Reads the `PORT:<n>` line the backend prints once it owns its socket, then keeps draining
/// stdout into `stdout.log` so the pipe never fills.
fn read_port(stdout: impl std::io::Read + Send + 'static, data_root: &Path) -> Option<u16> {
    let (sender, receiver) = mpsc::channel();
    let log_path = data_root.join("stdout.log");
    thread::spawn(move || {
        use std::io::Write;

        let mut log = File::create(log_path).ok();
        let mut sender = Some(sender);
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            if let Some(port) = line
                .strip_prefix("PORT:")
                .and_then(|p| p.trim().parse::<u16>().ok())
                && let Some(sender) = sender.take()
            {
                let _ = sender.send(port);
            }
            if let Some(log) = log.as_mut() {
                let _ = writeln!(log, "{line}");
            }
        }
    });
    receiver.recv_timeout(STARTUP_TIMEOUT).ok()
}

/// The workspace root (`apps/api-rs`).
pub(crate) fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// The repository root.
pub(crate) fn repo_root() -> PathBuf {
    workspace_root().join("../..")
}

/// Caps how many backends run at once: each Python instance holds ~650 MB.
struct SlotGuard;

static SLOTS: Mutex<usize> = Mutex::new(0);
static SLOT_FREED: Condvar = Condvar::new();

impl SlotGuard {
    fn acquire() -> SlotGuard {
        let max = env::var("CONTRACT_MAX_BACKENDS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(DEFAULT_MAX_BACKENDS);
        let mut used = SLOTS.lock().expect("backend slot counter poisoned");
        while *used >= max {
            used = SLOT_FREED
                .wait(used)
                .expect("backend slot counter poisoned");
        }
        *used += 1;
        SlotGuard
    }
}

impl Drop for SlotGuard {
    fn drop(&mut self) {
        if let Ok(mut used) = SLOTS.lock() {
            *used -= 1;
            SLOT_FREED.notify_one();
        }
    }
}
