//! Content-addressed static files embedded in the executable.

use std::sync::LazyLock;

use sha2::{Digest, Sha256};

/// An embedded file and its public address.
pub struct Asset {
    name: &'static str,
    /// The immutable public path.
    pub path: String,
    /// The HTTP media type.
    pub mime: &'static str,
    /// The uncompressed file.
    pub bytes: &'static [u8],
}

const FILES: &[(&str, &str, &[u8])] = &[
    (
        "theme.js",
        "text/javascript; charset=utf-8",
        include_str!("../static/theme.js").as_bytes(),
    ),
    (
        "ui.js",
        "text/javascript; charset=utf-8",
        include_str!("../static/ui.js").as_bytes(),
    ),
    (
        "chat.js",
        "text/javascript; charset=utf-8",
        include_str!("../static/chat.js").as_bytes(),
    ),
    (
        "datastar-core.js",
        "text/javascript; charset=utf-8",
        include_str!("../static/vendor/datastar-core.js").as_bytes(),
    ),
    (
        "datastar-patch.js",
        "text/javascript; charset=utf-8",
        include_str!("../static/vendor/datastar-patch.js").as_bytes(),
    ),
    (
        "ui.css",
        "text/css; charset=utf-8",
        include_str!("../static/ui.css").as_bytes(),
    ),
    (
        "logo.svg",
        "image/svg+xml",
        include_bytes!("../static/logo.svg"),
    ),
];

static ASSETS: LazyLock<Vec<Asset>> = LazyLock::new(|| {
    FILES
        .iter()
        .map(|&(name, mime, bytes)| {
            let hash = format!("{:x}", Sha256::digest(bytes));
            Asset {
                name,
                path: format!("/static/{hash}/{name}"),
                mime,
                bytes,
            }
        })
        .collect()
});

/// Returns the versioned path for a known file.
pub fn path(name: &str) -> &str {
    &ASSETS
        .iter()
        .find(|asset| asset.name == name)
        .expect("template references an embedded asset")
        .path
}

/// Looks up an exact content-addressed request path.
pub fn get(path: &str) -> Option<&'static Asset> {
    ASSETS.iter().find(|asset| asset.path == path)
}

#[cfg(test)]
mod tests {
    use super::{ASSETS, get, path};

    #[test]
    fn embedded_assets_fit_the_uncompressed_budget() {
        let size = |ext: &str| {
            ASSETS
                .iter()
                .filter(|asset| asset.name.ends_with(ext))
                .map(|asset| asset.bytes.len())
                .sum::<usize>()
        };
        assert!(size(".js") <= 50_000, "JavaScript: {} bytes", size(".js"));
        assert!(size(".css") <= 40_000, "CSS: {} bytes", size(".css"));
        assert!(get(path("ui.js")).is_some());
        assert!(get("/static/old/ui.js").is_none());
    }
}
