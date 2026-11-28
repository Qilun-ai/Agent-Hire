//! Countries of a job's location text, so a search for "India" also finds jobs listed only as
//! "Hyderabad". Uses a GeoNames gazetteer: country names and aliases first, then US state and
//! Canadian province codes after a comma, then cities and regions.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

const GAZETTEER: &str = include_str!("../data/geo_places.json");

struct Gazetteer {
    countries: HashMap<String, String>,
    names: HashMap<String, String>,
    places: HashMap<String, String>,
    us_states: HashSet<String>,
    ca_provinces: HashSet<String>,
}

fn string_map(value: &Value) -> HashMap<String, String> {
    value
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(key, value)| value.as_str().map(|text| (key.clone(), text.to_owned())))
        .collect()
}

fn string_set(value: &Value) -> HashSet<String> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect()
}

static DATA: LazyLock<Gazetteer> = LazyLock::new(|| {
    let parsed: Value = serde_json::from_str(GAZETTEER).unwrap_or(Value::Null);
    Gazetteer {
        countries: string_map(&parsed["countries"]),
        names: string_map(&parsed["names"]),
        places: string_map(&parsed["places"]),
        us_states: string_set(&parsed["us_states"]),
        ca_provinces: string_set(&parsed["ca_provinces"]),
    }
});

static SPLIT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\s*(?:;|\||/|•|·|\n| - | – |\bor\b|\band\b)\s*").expect("valid split pattern")
});
static PAREN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\([^)]*\)").expect("valid pattern"));

fn norm(text: &str) -> String {
    let trimmed = text.trim().trim_matches(|c| c == '.' || c == ',');
    trimmed
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

/// The ISO code when the search term names a country ("India", "USA").
pub fn country_of(term: &str) -> Option<String> {
    DATA.countries.get(&norm(term)).cloned()
}

/// A country's display name, or the code itself.
pub fn country_name(code: &str) -> String {
    DATA.names
        .get(code)
        .cloned()
        .unwrap_or_else(|| code.to_owned())
}

/// ISO country codes of the places in a location string; empty when nothing is recognisable.
pub fn countries(location_text: &str) -> BTreeSet<String> {
    let data = &*DATA;
    let mut found = BTreeSet::new();
    let without_parens = PAREN.replace_all(location_text, " ");
    for segment in SPLIT.split(&without_parens) {
        let parts: Vec<&str> = segment
            .split(',')
            .map(str::trim)
            .filter(|part| !part.is_empty())
            .collect();
        if parts.is_empty() {
            continue;
        }
        let mut hit: Option<String> = None;
        for (index, part) in parts.iter().rev().enumerate() {
            if let Some(code) = data.countries.get(&norm(part)) {
                hit = Some(code.clone());
                break;
            }
            let is_code = part.chars().count() == 2 && part.chars().all(|c| c.is_uppercase());
            if index == 0 && parts.len() > 1 && is_code {
                if data.us_states.contains(*part) {
                    hit = Some("US".to_owned());
                    break;
                }
                if data.ca_provinces.contains(*part) {
                    hit = Some("CA".to_owned());
                    break;
                }
            }
        }
        if hit.is_none() {
            hit = parts
                .iter()
                .find_map(|part| data.places.get(&norm(part)).cloned());
        }
        if let Some(code) = hit {
            found.insert(code);
        }
    }
    found
}

/// The stored `jobs.country_codes` value: `,IN,US,` (for `LIKE '%,IN,%'`), `""` when unknown.
pub fn codes_column(location_text: &str, locations_json: Option<&str>) -> String {
    let mut codes = countries(location_text);
    if let Some(raw) = locations_json
        && let Ok(Value::Array(items)) = serde_json::from_str::<Value>(raw)
    {
        for item in items {
            if let Some(text) = item.as_str() {
                codes.extend(countries(text));
            }
        }
    }
    if codes.is_empty() {
        return String::new();
    }
    format!(",{},", codes.into_iter().collect::<Vec<_>>().join(","))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn places_resolve_to_countries() {
        assert_eq!(country_of("India").as_deref(), Some("IN"));
        assert_eq!(codes_column("Hyderabad, Telangana, India", None), ",IN,");
        assert_eq!(codes_column("Austin, TX", None), ",US,");
        assert_eq!(codes_column("Remote", None), "");
    }
}
