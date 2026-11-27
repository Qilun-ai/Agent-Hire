//! Shared HTML controls and display formatting.

use maud::{Markup, html};
use serde_json::Value;
use url::Url;

pub(crate) fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or_default()
}

pub(crate) fn list<'a>(value: &'a Value, key: &str) -> &'a [Value] {
    value[key].as_array().map_or(&[], Vec::as_slice)
}

pub(crate) fn count(value: &Value, key: &str) -> u64 {
    value[key].as_u64().unwrap_or_default()
}

pub(crate) fn safe_url(raw: &str) -> Option<&str> {
    let url = Url::parse(raw).ok()?;
    (matches!(url.scheme(), "http" | "https") && url.has_host()).then_some(raw)
}

pub(crate) fn external(label: &str, raw: &str) -> Markup {
    html! {
        @if let Some(url) = safe_url(raw) {
            a href=(url) target="_blank" rel="noreferrer noopener" { (label) }
        } @else { (label) }
    }
}

pub(crate) fn segment(raw: &str) -> String {
    url::form_urlencoded::byte_serialize(raw.as_bytes())
        .collect::<String>()
        .replace('+', "%20")
}

pub(crate) fn number(n: u64) -> String {
    let digits = n.to_string();
    let mut output = String::new();
    for (index, digit) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            output.push(',');
        }
        output.push(digit);
    }
    output
}

pub(crate) fn time(value: &str, fallback: &str) -> Markup {
    html! { @if value.is_empty() { (if fallback == "never" { fallback } else { "—" }) } @else { time datetime=(value) data-relative=(fallback) { (value.get(..10).unwrap_or(value)) } } }
}

pub(crate) fn progress(done: u64, total: u64, label: &str) -> Markup {
    html! { progress value=(done) max=(total.max(1)) aria-label=(label) {} }
}

pub(crate) fn icon(name: &str) -> Markup {
    html! {
        svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" {
            use href=(format!("#icon-{name}")) {}
        }
    }
}

pub(crate) fn input(label: &str, name: &str, value: &str, placeholder: &str) -> Markup {
    html! { label.field { span { (label) } input name=(name) value=(value) placeholder=(placeholder); } }
}

pub(crate) fn select(label: &str, name: &str, current: &str, options: &[(&str, &str)]) -> Markup {
    html! { label.field { span { (label) } select name=(name) { @for &(key, title) in options { option value=(key) selected[key == current] { (title) } } } } }
}
