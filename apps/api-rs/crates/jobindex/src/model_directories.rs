//! Shared model paths for CPU inference and notebook deployments.

use std::env;
use std::path::{Path, PathBuf};

use crate::departments;

/// Locations of the two encoder installations.
#[derive(Clone, Debug)]
pub struct ModelDirectories {
    /// The level model folder.
    pub level: PathBuf,
    /// The department model folder.
    pub department: PathBuf,
}

impl ModelDirectories {
    /// Resolves explicit overrides, index-local models and packaged desktop assets in that order.
    #[must_use]
    pub fn from_jobs_dir(jobs_dir: &Path) -> Self {
        Self {
            level: model_path(jobs_dir, "AGENT_HIRE_LEVEL_MODEL_DIR", "level-model"),
            department: model_path(
                jobs_dir,
                "AGENT_HIRE_DEPARTMENT_MODEL_DIR",
                "department-model",
            ),
        }
    }
}

fn model_path(jobs_dir: &Path, variable: &str, default: &str) -> PathBuf {
    let value = env::var(variable).unwrap_or_default();
    match value.trim() {
        "" => {
            let local = jobs_dir.join(default);
            if !departments::model_available(&local)
                && let Some(resources) =
                    env::var_os("AGENT_HIRE_BUNDLED_MODELS_DIR").filter(|path| !path.is_empty())
            {
                let bundled = PathBuf::from(resources).join(default);
                if departments::model_available(&bundled) {
                    return bundled;
                }
            }
            local
        }
        "~" => env::var_os("HOME")
            .or_else(|| env::var_os("USERPROFILE"))
            .map_or_else(|| PathBuf::from("~"), PathBuf::from),
        path => {
            if let Some(tail) = path.strip_prefix("~/")
                && let Some(home) = env::var_os("HOME").or_else(|| env::var_os("USERPROFILE"))
            {
                return PathBuf::from(home).join(tail);
            }
            PathBuf::from(path)
        }
    }
}
