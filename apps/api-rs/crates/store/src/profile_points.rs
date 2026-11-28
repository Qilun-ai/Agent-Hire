//! Read-time point derivation for profile rows saved without point children.

use std::collections::HashSet;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};

use crate::profile_text;

static MARKER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:[-*•‣◦]|â€¢|\d+[.)])\s+").expect("valid bullet pattern"));
static STRIPPED_MARKER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:[-*•]|â€¢)\s+").expect("valid leading bullet pattern"));
static PROSE_BREAK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[.;]\s+|•").expect("valid prose break pattern"));

pub(super) fn ensure(profile: &mut Map<String, Value>) {
    for (key, description) in [("exp", "d"), ("projects", "impact")] {
        let Some(rows) = profile.get_mut(key).and_then(Value::as_array_mut) else {
            continue;
        };
        for row in rows.iter_mut().filter_map(Value::as_object_mut) {
            if row
                .get("points")
                .and_then(Value::as_array)
                .is_some_and(|points| !points.is_empty())
            {
                continue;
            }
            let Some(parent_id) = row
                .get("id")
                .and_then(Value::as_str)
                .filter(|id| !id.is_empty())
            else {
                continue;
            };
            let text = row
                .get(description)
                .and_then(Value::as_str)
                .unwrap_or_default();
            let points: Vec<Value> = split(text)
                .into_iter()
                .map(|text| {
                    let normalized = profile_text::collapse(&text).to_lowercase();
                    let id = profile_text::id(&format!("{parent_id}|{normalized}"));
                    json!({ "id": id, "text": text })
                })
                .collect();
            row.insert("points".to_owned(), json!(points));
        }
    }
}

pub(super) fn split(text: &str) -> Vec<String> {
    let lines = preprocess(text);
    let mut points = Vec::new();
    if lines.len() > 1 || lines.first().is_some_and(|line| line.marked) {
        for line in lines {
            if usable(&line.text)
                && (line.marked || (!line.boundary && line.text.chars().count() >= 4))
            {
                points.push(line.text);
            }
        }
    } else if let Some(line) = lines.first().filter(|line| !line.boundary) {
        for part in PROSE_BREAK.split(&line.text).map(profile_text::trim) {
            if part.chars().count() >= 16 && usable(part) && !is_boundary(part) {
                points.push(part.to_owned());
            }
        }
        if points.is_empty() && line.text.chars().count() >= 12 && usable(&line.text) {
            points.push(line.text.clone());
        }
    }

    let mut seen = HashSet::new();
    points
        .into_iter()
        .map(|point| point.chars().take(500).collect::<String>())
        .filter(|point| seen.insert(point_key(point)))
        .take(12)
        .collect()
}

fn usable(text: &str) -> bool {
    !text.trim_matches([' ', '-', '*', '_']).is_empty()
        && !matches!(
            text.to_lowercase().as_str(),
            "certificate link" | "view certificate" | "credential link"
        )
}

fn point_key(text: &str) -> String {
    // Numbered bullets remain distinct here. Ingest uses a separate key for exact matches.
    text.to_lowercase()
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect()
}

struct Line {
    text: String,
    marked: bool,
    boundary: bool,
}

fn preprocess(text: &str) -> Vec<Line> {
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    let mut lines: Vec<Line> = Vec::new();
    let mut separated = true;
    for raw in text.split('\n') {
        let trimmed = profile_text::trim(raw);
        if trimmed.is_empty() {
            separated = true;
            continue;
        }
        let repaired = repair_spacing(trimmed);
        let marked = MARKER.is_match(&repaired);
        let text = strip_markup(&repaired);
        let boundary = !marked && is_boundary(&text);
        let previous = lines.last_mut().filter(|line| !line.boundary);
        if let Some(previous) = previous
            && !separated
            && !marked
            && !boundary
            && ((previous.marked && raw.starts_with(profile_text::whitespace))
                || continues(&previous.text, &text))
        {
            if previous.text.ends_with('-') && text.starts_with(char::is_lowercase) {
                previous.text.pop();
            } else {
                previous.text.push(' ');
            }
            previous.text.push_str(&text);
        } else {
            lines.push(Line {
                text,
                marked,
                boundary,
            });
        }
        separated = false;
    }
    lines
}

fn continues(previous: &str, next: &str) -> bool {
    const CONNECTORS: [&str; 14] = [
        "a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "to", "with",
    ];
    let last_word = previous
        .rsplit(' ')
        .next()
        .unwrap_or_default()
        .to_lowercase();
    CONNECTORS.contains(&last_word.as_str())
        || (next.starts_with(char::is_lowercase) && !previous.ends_with(['.', '!', '?', ':']))
        || (previous.len() > 1
            && previous.split(' ').all(|word| {
                (1..=3).contains(&word.chars().count()) && word.chars().all(char::is_uppercase)
            })
            && next.starts_with(char::is_alphabetic))
}

pub(super) fn clean_inline(text: &str) -> String {
    strip_markup(&repair_spacing(profile_text::trim(text)))
}

fn strip_markup(text: &str) -> String {
    static LINK: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\[([^\]]*)\]\([^)]*\)").expect("valid link pattern"));
    static BOLD: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\*\*([^*]+)\*\*").expect("valid bold pattern"));
    static ITALIC: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\*([^*]+)\*").expect("valid italic pattern"));

    let mut text = text;
    while let Some(marker) = STRIPPED_MARKER.find(text) {
        text = &text[marker.end()..];
    }
    let text = LINK.replace_all(text, "$1");
    let text = BOLD.replace_all(&text, "$1");
    let text = ITALIC.replace_all(&text, "$1");
    profile_text::collapse(&text.replace('`', "").replace('→', "->").replace('·', "-"))
}

fn repair_spacing(text: &str) -> String {
    const TECHNOLOGIES: &str = "OpenSearch|OpenBao|PostgreSQL|MongoDB|FastAPI|Kubernetes|Kafka|Redis|Docker|Python|React|Linux|k3s|AWS|GCP";
    static KAFKA: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?i)\bkaf ka\b").expect("valid Kafka spacing pattern"));
    static TWO_INITIALS: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\b([A-Z]\.[A-Z])\s+([a-z]{2,})\b").expect("valid initials pattern")
    });
    static ONE_INITIAL: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\b([A-Z]\.)\s+([A-Za-z]{2,})\b").expect("valid initial pattern")
    });
    static SPLIT_WORD: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\b([BFV])\s+([a-z]{2,})\b").expect("valid split word pattern")
    });
    static METRIC: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"([a-z])([0-9]+[kKmMgG])\b").expect("valid metric spacing pattern")
    });
    static PUNCTUATION: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"([,;)])(\p{L})").expect("valid spacing pattern"));
    static TECH_SUFFIX: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(&format!(r"\b({TECHNOLOGIES})(?i:(and|with|for))\b"))
            .expect("valid technology suffix pattern")
    });
    static TECH_PREFIX: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(&format!(r"\b(with|using)({TECHNOLOGIES})\b"))
            .expect("valid technology prefix pattern")
    });
    let text = KAFKA.replace_all(text, "Kafka");
    let text = TWO_INITIALS.replace_all(&text, "$1$2");
    let text = ONE_INITIAL.replace_all(&text, "$1$2");
    let text = SPLIT_WORD.replace_all(&text, "$1$2");
    let text = METRIC.replace_all(&text, "$1 $2");
    let text = PUNCTUATION.replace_all(&text, "$1 $2");
    let text = TECH_PREFIX.replace_all(&text, "$1 $2");
    TECH_SUFFIX.replace_all(&text, "$1 $2").into_owned()
}

fn is_boundary(text: &str) -> bool {
    const SECTIONS: &[&str] = &[
        "summary",
        "profile",
        "objective",
        "skills",
        "technical skills",
        "experience",
        "work experience",
        "employment",
        "projects",
        "personal projects",
        "selected work",
        "portfolio",
        "education",
        "certifications",
        "certificates",
        "achievements",
        "awards",
        "research publications",
    ];
    static EMAIL: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\b[\w.+-]+@[\w.-]+\.\w+\b").expect("valid email pattern"));
    static PHONE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^\+?[\d(). -]{7,}$").expect("valid phone pattern"));
    static DATE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^(?:[a-z]{3,9}\.?\s+)?(?:19|20)\d{2}(?:\s*[-–—/]\s*(?:(?:[a-z]{3,9}\.?\s+)?(?:19|20)\d{2}|present|current))?$")
            .expect("valid date pattern")
    });
    static ROLE_COMPANY: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^\p{Lu}[\w &+./-]{0,40} at \p{Lu}[\w &,./-]*$")
            .expect("valid role and company pattern")
    });

    let lower = text.to_lowercase();
    let heading = profile_text::trim(lower.trim_matches(['#', ' ', ':']));
    text.is_empty()
        || SECTIONS.contains(&heading)
        || ["http", "www.", "linkedin.com/", "github.com/"]
            .iter()
            .any(|part| lower.contains(part))
        || EMAIL.is_match(text)
        || PHONE.is_match(text)
        || DATE.is_match(&lower)
        || is_label(text)
        || (ROLE_COMPANY.is_match(text) && !text.split(' ').any(|word| word == "and"))
        || (text.matches('|').count() >= 2
            && lower
                .split('|')
                .any(|part| DATE.is_match(profile_text::trim(part))))
}

fn is_label(text: &str) -> bool {
    const ACTIONS: &[&str] = &[
        "built",
        "created",
        "developed",
        "designed",
        "engineered",
        "implemented",
        "integrated",
        "launched",
        "shipped",
        "automated",
        "managed",
        "drove",
        "wrote",
        "improved",
        "reduced",
        "increased",
        "optimized",
        "trained",
        "fine-tuned",
        "distributed",
        "cut",
        "held",
        "achieved",
        "deployed",
        "led",
        "mentored",
        "benchmarked",
    ];
    let Some((label, _)) = text.split_once(':') else {
        return false;
    };
    if label.chars().count() > 40
        || !label.starts_with(char::is_alphabetic)
        || !label
            .chars()
            .all(|c| c.is_alphanumeric() || " +&#/._-".contains(c))
    {
        return false;
    }
    let first = label.split(' ').next().unwrap_or_default().to_lowercase();
    let title_case = label
        .split(' ')
        .all(|word| word.starts_with(char::is_uppercase));
    title_case || !ACTIONS.contains(&first.as_str())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{ensure, split};

    #[test]
    fn keeps_wrapped_bullets_and_filters_boundaries() {
        let text = "Summary\r\n- Implemented an extensible\r\n  transactional platform\r\nProjects\r\n• A\r\n- View certificate";
        assert_eq!(
            split(text),
            ["Implemented an extensible transactional platform", "A"]
        );
        assert_eq!(split("1. Foo\n2. Foo\nFoo"), ["1. Foo", "2. Foo"]);
        assert_eq!(split("I\nBuilt a thing"), ["Built a thing"]);
        assert_eq!(
            split("- Cachingapi, shipped100m to scale300k"),
            ["Cachingapi, shipped 100m to scale 300k"]
        );
        assert_eq!(
            split("One sufficiently long sentence; Second sufficiently long sentence."),
            [
                "One sufficiently long sentence",
                "Second sufficiently long sentence."
            ]
        );
        assert!(split("3M tx/day").is_empty());
    }

    #[test]
    fn derives_parent_scoped_ids_and_preserves_imported_points() {
        let mut profile = json!({
            "exp": [{"id": "point-probe", "d": "- Implemented an extensible\n  transactional platform\n- Led the team."}],
            "projects": [{"id": "project", "points": [{"id": "imported", "text": "A", "tag_ids": ["tag"]}]}],
        });
        ensure(profile.as_object_mut().expect("profile is an object"));
        assert_eq!(profile["exp"][0]["points"][0]["id"], "5046e6e52ca9");
        assert_eq!(profile["exp"][0]["points"][1]["id"], "78e123c5cc7b");
        assert_eq!(
            profile["projects"][0]["points"],
            json!([{"id": "imported", "text": "A", "tag_ids": ["tag"]}])
        );
    }
}
