//! Where the app keeps its data.

use std::env;
use std::path::PathBuf;

/// The directory name under the platform data directory.
const APP_DIR_NAME: &str = "Agent-Hire";

/// The app data directory and the files in it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AppPaths {
    /// The root of everything the app stores.
    pub data_dir: PathBuf,
}

impl AppPaths {
    /// Resolves the data directory: `AGENT_HIRE_DATA_DIR` as is, else `AGENT_HIRE_DATA_BASE_DIR` plus
    /// the app name, else the platform data directory plus the app name (`LOCALAPPDATA` on
    /// Windows, `~/Library/Application Support` on macOS, `XDG_DATA_HOME` or `~/.local/share`
    /// elsewhere).
    pub fn from_env() -> AppPaths {
        let data_dir = if let Some(dir) = non_empty_var("AGENT_HIRE_DATA_DIR") {
            expand_home(&dir)
        } else if let Some(base) = non_empty_var("AGENT_HIRE_DATA_BASE_DIR") {
            expand_home(&base).join(APP_DIR_NAME)
        } else {
            platform_data_dir().join(APP_DIR_NAME)
        };
        AppPaths { data_dir }
    }

    /// The app database, `<data dir>/crm.db`.
    pub fn database(&self) -> PathBuf {
        self.data_dir.join("crm.db")
    }

    /// Generated documents, `<data dir>/assets`.
    pub fn assets_dir(&self) -> PathBuf {
        self.data_dir.join("assets")
    }

    /// Embedding and classifier models, `<data dir>/models`.
    pub fn models_dir(&self) -> PathBuf {
        self.data_dir.join("models")
    }
}

fn non_empty_var(name: &str) -> Option<String> {
    env::var(name).ok().filter(|value| !value.is_empty())
}

fn home_dir() -> PathBuf {
    non_empty_var("HOME")
        .or_else(|| non_empty_var("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Expands a leading `~` to the home directory.
fn expand_home(path: &str) -> PathBuf {
    if path == "~" {
        return home_dir();
    }
    if let Some(rest) = path.strip_prefix("~/").or_else(|| path.strip_prefix("~\\")) {
        return home_dir().join(rest);
    }
    PathBuf::from(path)
}

fn platform_data_dir() -> PathBuf {
    if cfg!(windows) {
        non_empty_var("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| home_dir().join("AppData").join("Local"))
    } else if cfg!(target_os = "macos") {
        home_dir().join("Library").join("Application Support")
    } else {
        non_empty_var("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home_dir().join(".local").join("share"))
    }
}
