//! Locating the checkout for bundled backend data sources.

use std::env;
use std::path::{Path, PathBuf};

/// Where the checkout is, if present.
#[derive(Clone, Debug, Default)]
pub struct RepoPaths {
    /// The checkout root, when the server runs from one.
    pub root: Option<PathBuf>,
}

impl RepoPaths {
    /// Searches upwards from the executable, then from the working directory, for the checkout.
    pub fn discover() -> RepoPaths {
        let mut starts: Vec<PathBuf> = Vec::new();
        if let Ok(exe) = env::current_exe() {
            starts.push(exe);
        }
        if let Ok(dir) = env::current_dir() {
            starts.push(dir);
        }
        let root = starts.iter().find_map(|start| find_root(start));
        RepoPaths { root }
    }
}

fn find_root(start: &Path) -> Option<PathBuf> {
    start
        .ancestors()
        .find(|dir| dir.join("apps").join("api-rs").is_dir())
        .map(Path::to_path_buf)
}
