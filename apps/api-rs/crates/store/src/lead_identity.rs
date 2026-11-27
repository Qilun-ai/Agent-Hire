//! How a lead is recognised again after it is deleted: its canonical id and normalised URLs.

use serde_json::{Map, Value};
use url::Url;

/// Query parameters that only track where a click came from.
fn is_tracking_parameter(key: &str) -> bool {
    let key = key.to_lowercase();
    matches!(
        key.as_str(),
        "fbclid" | "gclid" | "mc_cid" | "mc_eid" | "ref" | "referrer" | "source" | "src"
    ) || key.starts_with("utm_")
}

/// A comparison key for a source URL: lower-cased scheme and host without `www.`, no default
/// port, no trailing slash, tracking parameters dropped, the rest sorted, no fragment.
pub fn normalize_source_url(raw: &str) -> String {
    let text = raw.trim();
    if text.is_empty() {
        return String::new();
    }
    let parsed = match Url::parse(text) {
        Ok(parsed) if parsed.host_str().is_some() => parsed,
        _ => return text.trim_end_matches('/').to_lowercase(),
    };
    let scheme = parsed.scheme().to_lowercase();
    let host = parsed.host_str().unwrap_or("").to_lowercase();
    let host = host.strip_prefix("www.").unwrap_or(&host).to_owned();
    let port = parsed
        .port()
        .filter(|port| !((scheme == "http" && *port == 80) || (scheme == "https" && *port == 443)))
        .map(|port| format!(":{port}"))
        .unwrap_or_default();
    let credentials = if parsed.username().is_empty() {
        String::new()
    } else {
        format!("{}:{}@", parsed.username(), parsed.password().unwrap_or(""))
    };
    let mut path = parsed.path().trim_end_matches('/').to_owned();
    if path.is_empty() {
        path.push('/');
    }
    let mut pairs: Vec<(String, String)> = parsed
        .query_pairs()
        .filter(|(key, _)| !is_tracking_parameter(key))
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect();
    pairs.sort();
    let query = if pairs.is_empty() {
        String::new()
    } else {
        let encoded: String = url::form_urlencoded::Serializer::new(String::new())
            .extend_pairs(pairs)
            .finish();
        format!("?{encoded}")
    };
    format!("{scheme}://{credentials}{host}{port}{path}{query}")
}

/// A canonical id as compared: trimmed and lower-cased.
pub fn canonical_identity(value: &str) -> String {
    value.trim().to_lowercase()
}

/// Every identity of a job object: canonical id, primary URL, URL and source URLs, without
/// duplicates and in that order. Kinds are `canonical` and `url`.
pub fn identity_bundle(job: &Map<String, Value>) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    let mut push = |kind: &str, value: String| {
        if !value.is_empty() && !out.iter().any(|(k, v)| k == kind && *v == value) {
            out.push((kind.to_owned(), value));
        }
    };
    let text = |key: &str| -> String {
        match job.get(key) {
            Some(Value::String(value)) => value.clone(),
            Some(Value::Null) | None => String::new(),
            Some(other) => other.to_string(),
        }
    };
    let canonical = {
        let id = text("job_id");
        if id.trim().is_empty() {
            text("canonical_job_id")
        } else {
            id
        }
    };
    push("canonical", canonical_identity(&canonical));
    push("url", normalize_source_url(&text("primary_url")));
    push("url", normalize_source_url(&text("url")));
    if let Some(sources) = job.get("sources").and_then(Value::as_array) {
        for source in sources {
            if let Some(url) = source.get("url").and_then(Value::as_str) {
                push("url", normalize_source_url(url));
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_normalisation() {
        assert_eq!(
            normalize_source_url("HTTP://www.Example.com:80/jobs/1/?utm_source=x&b=2&a=1#top"),
            "http://example.com/jobs/1?a=1&b=2"
        );
        assert_eq!(normalize_source_url("https://h.io"), "https://h.io/");
        assert_eq!(normalize_source_url("not a url/"), "not a url");
        assert_eq!(normalize_source_url("  "), "");
    }
}
