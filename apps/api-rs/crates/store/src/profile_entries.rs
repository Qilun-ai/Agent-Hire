//! Normalizing education, certification and achievement entries.

use std::collections::HashSet;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

use crate::profile_normalization as normalize;
use crate::profile_skills;

pub(super) fn education(raw: &[Value]) -> Vec<Value> {
    let mut structured = Vec::new();
    let mut heuristic = Vec::new();
    let mut current = String::new();
    let mut pending = Vec::<String>::new();
    for item in raw {
        let title = entry_title(item);
        const STRUCTURED: &[&str] = &[
            "institution",
            "school",
            "college",
            "university",
            "degree",
            "studyType",
            "study_type",
            "area",
            "field",
            "major",
        ];
        if !title.is_empty()
            && STRUCTURED
                .iter()
                .any(|key| !normalize::text(&item[key]).is_empty())
        {
            structured.push(normalize::bounded(&normalize::inline(&title), 500));
            continue;
        }
        for line in split_education(&title)
            .into_iter()
            .map(normalize::inline)
            .filter(|line| !normalize::section_noise(line))
        {
            if education_anchor(&line) {
                if !current.is_empty() && same_education_entry(&current, &line) {
                    append_detail(&mut current, &line, " - ");
                    continue;
                }
                if !current.is_empty() {
                    heuristic.push(std::mem::take(&mut current));
                }
                current = line;
                for detail in pending.drain(..) {
                    append_detail(&mut current, &detail, ", ");
                }
            } else if education_detail(&line) {
                if current.is_empty() {
                    pending.push(line);
                } else {
                    append_detail(&mut current, &line, ", ");
                }
            } else if !current.is_empty() && line.split_whitespace().count() <= 8 {
                append_detail(&mut current, &line, " - ");
            }
        }
    }
    if !current.is_empty() {
        heuristic.push(current);
    }
    structured.extend(heuristic);
    unique_entries(structured)
}

static INSTITUTION: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\b(?:university|college|institute|school|academy|polytechnic)\b")
        .expect("valid school pattern")
});
static DEGREE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\b(?:b\.?\s?tech|bachelor|master|m\.?\s?tech|b\.?\s?e\.?|m\.?\s?e\.?|bsc|msc|bca|mca|mba|ph\.?d|degree|diploma)\b")
        .expect("valid degree pattern")
});
static DATE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)\b(?:(?:19|20)\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)\b",
    )
    .expect("valid education date pattern")
});

fn education_anchor(title: &str) -> bool {
    INSTITUTION.is_match(title) || DEGREE.is_match(title)
}

pub(super) fn education_detail(title: &str) -> bool {
    static GRADE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
            r"(?i)\b(?:cgpa|gpa|grade|percentage|marks?|score)\b|^\d+(?:\.\d+)?(?:\s*/\s*\d+)?$",
        )
        .expect("valid grade pattern")
    });
    const LOCATIONS: &[&str] = &[
        "andhra pradesh",
        "bengaluru",
        "bangalore",
        "chandigarh",
        "delhi",
        "haryana",
        "hyderabad",
        "india",
        "jalandhar",
        "karnataka",
        "maharashtra",
        "mumbai",
        "new delhi",
        "noida",
        "punjab",
        "remote",
        "uttar pradesh",
    ];
    let lower = title.trim_matches([' ', ',']).to_lowercase();
    LOCATIONS.contains(&lower.as_str())
        || GRADE.is_match(title)
        || (title.split_whitespace().count() <= 6 && DATE.is_match(title))
}

fn same_education_entry(previous: &str, incoming: &str) -> bool {
    normalize::key(previous).contains(&normalize::key(incoming))
        || (INSTITUTION.is_match(previous)
            && DEGREE.is_match(incoming)
            && !INSTITUTION.is_match(incoming))
        || (DEGREE.is_match(previous)
            && INSTITUTION.is_match(incoming)
            && !INSTITUTION.is_match(previous))
}

fn split_education(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    for line in text.lines() {
        let mut start = 0;
        for (offset, _) in line.match_indices(" - ") {
            let next = &line[offset + 3..];
            let lower = next.to_lowercase();
            if next.starts_with(|c: char| c.is_ascii_digit())
                || ["cgpa", "gpa", "grade"]
                    .iter()
                    .any(|prefix| lower.starts_with(prefix))
            {
                lines.push(&line[start..offset]);
                start = offset + 3;
            }
        }
        lines.push(&line[start..]);
    }
    lines
}

pub(super) fn entries(raw: &[Value], cert: bool) -> Vec<Value> {
    static CREDENTIAL_NOTE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)\b(?:(?:credential|certificate) link|(?:view|verify|open) (?:credential|certificate))\b").expect("valid certification note")
    });
    static MONTH: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)((?:19|20)\d{2})\b")
            .expect("valid certification month")
    });
    static SEPARATOR: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\s*[-|]{2,}\s*").expect("valid credential separator"));
    let mut out = Vec::<String>::new();
    for item in raw {
        let mut title = normalize::inline(&entry_title(item));
        if cert {
            let cleaned = CREDENTIAL_NOTE.replace_all(&title, "");
            let cleaned = normalize::URL.replace_all(&cleaned, "");
            let cleaned = MONTH.replace_all(&cleaned, "$1 $2");
            let cleaned = SEPARATOR.replace_all(&cleaned, " - ");
            title = normalize::inline(&cleaned)
                .trim_matches([' ', '-', ':', '|'])
                .to_owned();
            let lower = title.to_lowercase();
            if matches!(
                lower.as_str(),
                "certificate"
                    | "certification"
                    | "certifications"
                    | "credential"
                    | "credentials"
                    | "link"
            ) {
                continue;
            }
            if let Some(previous) = out.last_mut() {
                let issuer = [
                    "nptel",
                    "coursera",
                    "udemy",
                    "edx",
                    "aws",
                    "google",
                    "microsoft",
                    "oracle",
                    "meta",
                    "ibm",
                    "linkedin learning",
                ]
                .contains(&lower.as_str());
                if issuer || certification_date(&title) {
                    append_detail(previous, &title, " - ");
                    continue;
                }
            }
        }
        if normalize::section_noise(&title)
            || (!cert && education_detail(&title))
            || (title.split_whitespace().count() == 1
                && profile_skills::canonical_skill(&title).is_some())
        {
            continue;
        }
        out.push(title);
    }
    unique_entries(out)
}

fn certification_date(title: &str) -> bool {
    static CERT_DATE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s*'?\.?\s*\d{2,4}\b|\b(?:19|20)\d{2}\b")
            .expect("valid certification date pattern")
    });
    if title.split_whitespace().count() > 7 || !CERT_DATE.is_match(title) {
        return false;
    }
    !CERT_DATE
        .replace_all(title, "")
        .split(|c: char| !c.is_alphabetic())
        .any(|word| word.chars().count() >= 4)
}

fn entry_title(item: &Value) -> String {
    if !item.is_object() {
        return normalize::text(item);
    }
    let direct = normalize::text(normalize::first(item, &["title", "name", "n"]));
    if !direct.is_empty() {
        return direct;
    }
    let study = normalize::text(normalize::first(item, &["studyType", "degree"]));
    let area = normalize::text(normalize::first(item, &["area", "field", "major"]));
    let school = normalize::text(normalize::first(
        item,
        &["institution", "school", "college"],
    ));
    let study = format!("{study} {area}");
    let study = study.trim();
    match (study.is_empty(), school.is_empty()) {
        (true, _) => school,
        (false, true) => study.to_owned(),
        (false, false) => format!("{study} at {school}"),
    }
}

fn append_detail(base: &mut String, detail: &str, separator: &str) {
    if !detail.is_empty() && !base.to_lowercase().contains(&detail.to_lowercase()) {
        base.push_str(separator);
        base.push_str(detail);
    }
}

fn unique_entries(entries: Vec<String>) -> Vec<Value> {
    let mut seen = HashSet::new();
    entries
        .into_iter()
        .filter(|entry| !entry.is_empty() && seen.insert(entry.to_lowercase()))
        .map(Value::String)
        .collect()
}
