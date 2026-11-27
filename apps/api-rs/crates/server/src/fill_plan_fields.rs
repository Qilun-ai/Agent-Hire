//! Saved values and deterministic matches for application form fields.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};

use crate::fill_job_data::{self, JOB_FIELDS};
use crate::fill_routes;
use crate::python_text;
use crate::validation::ModelReader;

/// A field found on the application page.
pub(super) struct PageField {
    pub(super) id: String,
    pub(super) kind: String,
    pub(super) label: String,
    pub(super) name: String,
    pub(super) placeholder: String,
    pub(super) aria: String,
    pub(super) autocomplete: String,
    pub(super) options: Vec<String>,
    pub(super) context: String,
}

impl PageField {
    pub(super) fn read(reader: &mut ModelReader<'_>) -> PageField {
        let id = reader.string("id", Some(80), true).unwrap_or_default();
        let kind = reader
            .string("kind", Some(20), false)
            .unwrap_or_else(|| "text".to_owned());
        let label = reader.string("label", Some(600), false).unwrap_or_default();
        let name = reader.string("name", Some(200), false).unwrap_or_default();
        let placeholder = reader
            .string("placeholder", Some(300), false)
            .unwrap_or_default();
        let aria = reader.string("aria", Some(300), false).unwrap_or_default();
        let autocomplete = reader
            .string("autocomplete", Some(80), false)
            .unwrap_or_default();
        let options = reader.string_list("options", Some(200)).unwrap_or_default();
        reader.boolean("required");
        let context = reader
            .string("context", Some(600), false)
            .unwrap_or_default();
        PageField {
            id,
            kind,
            label,
            name,
            placeholder,
            aria,
            autocomplete,
            options,
            context,
        }
    }

    pub(super) fn question(&self) -> &str {
        [&self.label, &self.placeholder, &self.name]
            .into_iter()
            .find(|text| !text.is_empty())
            .map_or("", String::as_str)
    }

    pub(super) fn kind(&self) -> &str {
        if self.kind.is_empty() {
            "text"
        } else {
            &self.kind
        }
    }

    fn text(&self) -> String {
        [&self.label, &self.name, &self.placeholder, &self.aria]
            .map(String::as_str)
            .join(" ")
            .trim()
            .to_owned()
    }

    pub(super) fn reason_to_skip(&self) -> Option<&'static str> {
        static CONSENT: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(r"(?i)\b(agree|consent|terms|privacy|certify|acknowledge|accept|attest|authori[sz]e .*(contact|process))\b")
                .expect("valid consent pattern")
        });
        static CHALLENGE: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(r"(?i)captcha|are you (a )?(human|robot)|prove (that )?you|\bsolve\b|what is \d+ ?[-+x*×] ?\d+")
                .expect("valid human check pattern")
        });
        let text = self.text();
        if self.kind == "checkbox" && CONSENT.is_match(&format!("{text} {}", self.context)) {
            Some("consent: tick it yourself")
        } else if CHALLENGE.is_match(&text) {
            Some("human check: answer it yourself")
        } else {
            None
        }
    }

    pub(super) fn asks_for_phone_code(&self) -> bool {
        static CODE: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(r"(?i)country\s*code|dial(l?ing)?\s*code|calling\s*code|phone\s*(country\s*)?code|^code$")
                .expect("valid calling code pattern")
        });
        matches!(self.kind.as_str(), "select" | "combobox" | "radio") && CODE.is_match(&self.text())
    }
}

pub(super) fn catalog(
    profile: &Value,
    facts: Map<String, Value>,
    defaults: Map<String, Value>,
    record: &Value,
) -> Map<String, Value> {
    let exp = fill_job_data::experiences(profile)
        .iter()
        .find(|exp| exp.is_object())
        .unwrap_or(&Value::Null);
    let education = fill_job_data::items(&profile["education"])
        .first()
        .unwrap_or(&Value::Null);
    let skills = fill_job_data::skills(profile);
    let resume = [
        (
            "current_title",
            fill_job_data::first(exp, &["role", "title"]),
        ),
        (
            "current_company",
            fill_job_data::first(exp, &["co", "company"]),
        ),
        ("summary", fill_job_data::text(profile, "s")),
        ("key_skills", skills.as_str()),
        (
            "highest_education",
            fill_job_data::first(education, &["text", "title"]),
        ),
    ];
    let description = |key: &str| {
        let fact = fill_routes::FACT_FIELDS
            .iter()
            .chain(fill_routes::DEFAULT_FIELDS)
            .find(|field| field.key == key);
        fact.map(|field| field.description)
            .or_else(|| job_field(key))
    };
    let mut values = Map::new();
    let mut put = |key: &str, value: &str, source: &str, label: Option<&str>| {
        let value = value.trim();
        if !value.is_empty() {
            let label = label.or_else(|| description(key)).unwrap_or(key);
            values.insert(
                key.to_owned(),
                json!({"description": label, "value": value, "source": source}),
            );
        }
    };
    for (key, value) in resume {
        put(key, value, "resume", job_field(key));
    }
    for (key, value) in defaults {
        put(&key, &python_text::str_of(&value), "defaults", None);
    }
    if matches!(record["source"].as_str(), Some("generate" | "reuse")) {
        for (key, value) in record["data"].as_object().into_iter().flatten() {
            if let Some(label) = job_field(key) {
                put(key, &python_text::str_of(value), "job_data", Some(label));
            }
        }
    }
    for (key, value) in facts {
        put(&key, &python_text::str_of(&value), "facts", None);
    }
    values
}

fn job_field(key: &str) -> Option<&'static str> {
    JOB_FIELDS
        .iter()
        .find(|(field, _)| key == *field)
        .map(|(_, label)| *label)
}

pub(super) fn rule_key(field: &PageField, values: &Map<String, Value>) -> Option<&'static str> {
    for token in field.autocomplete.to_lowercase().split_whitespace().rev() {
        let key = match token {
            "email" => "email",
            "tel" | "tel-national" => "phone",
            "tel-country-code" => "phone_country_code",
            "name" => "full_name",
            "given-name" => "first_name",
            "family-name" => "last_name",
            "postal-code" => "postal_code",
            "street-address" | "address-line1" => "address_line1",
            "address-level2" => "city",
            "address-level1" => "state",
            "country" | "country-name" => "country",
            "url" => "portfolio_url",
            "organization" => "current_company",
            "organization-title" => "current_title",
            _ => continue,
        };
        if values.contains_key(key) {
            return Some(key);
        }
    }
    for (kind, key) in [("email", "email"), ("tel", "phone")] {
        if field.kind == kind && values.contains_key(key) {
            return Some(key);
        }
    }
    let label = [&field.label, &field.placeholder, &field.aria]
        .into_iter()
        .find(|label| !label.is_empty())
        .map_or("", |label| label.trim());
    if label.is_empty() || label.chars().count() > 60 {
        return None;
    }
    static RULES: LazyLock<Vec<(Regex, &'static str)>> = LazyLock::new(|| {
        [
            (r"linked\s*in", "linkedin_url"),
            (r"git\s*hub", "github_url"),
            (r"portfolio|personal (web)?site|^website\b", "portfolio_url"),
            (r"\bpreferred (first )?name\b", "preferred_name"),
            (r"\b(first|given) name\b|^first$|\bfname\b", "first_name"),
            (
                r"\b(last|family) name\b|\bsurname\b|^last$|\blname\b",
                "last_name",
            ),
            (r"^\W*(full |legal |your )?name\W*$", "full_name"),
            (r"\be-?mail\b", "email"),
            (r"\b(phone|mobile|telephone|cell|contact number)\b", "phone"),
            (r"\b(zip|postal|post) ?code\b|^zip$", "postal_code"),
            (r"^\W*(city|town)\W*$", "city"),
            (r"^\W*(state|province|region)\W*$", "state"),
            (r"^\W*country( of residence)?\W*$", "country"),
            (r"^\W*(street )?address( line 1)?\W*$", "address_line1"),
        ]
        .into_iter()
        .map(|(pattern, key)| {
            (
                Regex::new(&format!("(?i){pattern}")).expect("valid field pattern"),
                key,
            )
        })
        .collect()
    });
    RULES
        .iter()
        .find(|(pattern, key)| values.contains_key(*key) && pattern.is_match(label))
        .map(|(_, key)| *key)
}

fn normalize_option(value: &str) -> String {
    value
        .to_lowercase()
        .split(|c: char| !(c.is_ascii_alphanumeric() || c == '+'))
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

pub(super) fn match_option<'a>(value: &str, options: &'a [String]) -> Option<&'a str> {
    const YES: &[&str] = &[
        "yes",
        "y",
        "true",
        "1",
        "i am",
        "i do",
        "authorized",
        "authorised",
    ];
    const NO: &[&str] = &["no", "n", "false", "0", "i am not", "i do not", "none"];
    let value = normalize_option(value);
    let options = options
        .iter()
        .map(|option| (normalize_option(option), option.as_str()))
        .collect::<Vec<_>>();
    let find = |matches: &dyn Fn(&str) -> bool| {
        options
            .iter()
            .find(|(normalized, _)| matches(normalized))
            .map(|(_, option)| *option)
    };
    if let Some(found) = find(&|option| option == value) {
        return Some(found);
    }
    if YES.contains(&value.as_str()) || value.starts_with("yes") {
        return find(&|option| YES.contains(&option) || option.starts_with("yes"));
    }
    if NO.contains(&value.as_str()) || value.starts_with("no") {
        return find(&|option| NO.contains(&option) || option.starts_with("no "));
    }
    if ["wish to answer", "prefer not", "decline"]
        .iter()
        .any(|phrase| value.contains(phrase))
    {
        return find(&|option| {
            ["not wish", "prefer not", "decline", "don t wish", "not to"]
                .iter()
                .any(|phrase| option.contains(phrase))
        });
    }
    let mut prefixes = options.iter().filter(|(option, _)| {
        !value.is_empty()
            && option.len() > 1
            && (option.starts_with(&value) || value.starts_with(option))
    });
    let first = prefixes.next()?;
    prefixes.next().is_none().then_some(first.1)
}

pub(super) fn match_many(value: &str, options: &[String]) -> String {
    static SEPARATORS: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\s*(?:[,;/\n]|\band\b)\s*").expect("valid list separators"));
    let mut matched = match_option(value, options)
        .filter(|option| !option.is_empty())
        .into_iter()
        .collect::<Vec<_>>();
    for part in SEPARATORS
        .split(value)
        .filter(|part| !part.trim().is_empty())
    {
        if let Some(option) = match_option(part, options)
            && !option.is_empty()
            && !matched.contains(&option)
        {
            matched.push(option);
        }
    }
    matched.join("\n")
}

pub(super) fn without_code(phone: &str, code: &str) -> String {
    let digits = code
        .chars()
        .filter(|character| character.is_numeric())
        .collect::<String>();
    if !digits.is_empty() {
        let prefix =
            Regex::new(&format!(r"^\s*(\+|00)?{digits}[\s.-]*")).expect("valid number prefix");
        if let Some(prefix) = prefix.find(phone) {
            let number = &phone[prefix.end()..];
            if number.chars().filter(|c| c.is_numeric()).count() >= 6 {
                return number.to_owned();
            }
        }
    }
    static PLUS: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^\s*\+\d{1,3}[\s.-]+").expect("valid calling code prefix"));
    PLUS.replace(phone, "").into_owned()
}

/// Returns `value` with `https://` in front when it is a bare web address such as
/// `linkedin.com/in/someone`, which browsers reject in `type="url"` inputs. Anything that already has
/// a scheme, or does not look like an address, comes back trimmed but otherwise unchanged.
pub(super) fn with_scheme(value: &str) -> String {
    let value = value.trim();
    let looks_like_address = value.contains('.') && !value.contains(char::is_whitespace);
    if !looks_like_address || value.contains("://") || value.starts_with("mailto:") {
        value.to_owned()
    } else if let Some(rest) = value.strip_prefix("//") {
        format!("https://{rest}")
    } else {
        format!("https://{value}")
    }
}

#[cfg(test)]
mod tests {
    use super::with_scheme;

    #[test]
    fn bare_addresses_get_https() {
        assert_eq!(
            with_scheme("linkedin.com/in/ana"),
            "https://linkedin.com/in/ana"
        );
        assert_eq!(
            with_scheme(" www.github.com/ana "),
            "https://www.github.com/ana"
        );
        assert_eq!(with_scheme("//ana.dev"), "https://ana.dev");
    }

    #[test]
    fn other_values_are_left_alone() {
        assert_eq!(with_scheme("https://ana.dev"), "https://ana.dev");
        assert_eq!(with_scheme("http://ana.dev"), "http://ana.dev");
        assert_eq!(
            with_scheme("mailto:ana@example.com"),
            "mailto:ana@example.com"
        );
        assert_eq!(with_scheme("not applicable"), "not applicable");
        assert_eq!(with_scheme(""), "");
    }
}
