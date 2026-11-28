//! Dated professional experience and year requirements.

use std::sync::LazyLock;

use agent_hire_jobindex::DateTime;
use regex::Regex;

static PRESENT: LazyLock<Regex> = LazyLock::new(|| pattern(r"\b(?:present|current|now|today)\b"));
static PERIOD: LazyLock<Regex> = LazyLock::new(|| {
    pattern(r"\b(?:([a-z]{3,4})\s+)?(\d{4})\s*(?:to\b|->|[-–—→])\s*(?:([a-z]{3,4})\s+)?(\d{4})\b")
});
static SINGLE_YEARS: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"\b([0-9]{1,2})\s*(?:[+]\s*)?(?:years?|yrs?|yoe)\b"));
static REQUIRED_YEARS: LazyLock<Regex> = LazyLock::new(|| {
    pattern(
        r"\b([0-9]{1,2}(?:\.[0-9]+)?)(?:\s*(?:[-–—]|to)\s*([0-9]{1,2}(?:\.[0-9]+)?))?\s*(?:[+]\s*)?(?:years?|yrs?|yoe)\b",
    )
});
const MONTHS: [&str; 12] = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
];

fn pattern(pattern: &str) -> Regex {
    Regex::new(pattern).expect("valid experience duration pattern")
}

pub(crate) fn today_month() -> String {
    let date = DateTime::now_utc().isoformat_seconds();
    let month: usize = date[5..7].parse().expect("UTC date has a month");
    format!("{} {}", MONTHS[month - 1], &date[..4])
}

fn month(name: Option<&str>, default: i64) -> i64 {
    name.and_then(|name| MONTHS.iter().position(|month| name.starts_with(month)))
        .map_or(default, |index| index as i64 + 1)
}

pub(crate) fn months(period: &str, today: &str) -> i64 {
    let lower = period.to_lowercase();
    let current = PRESENT.replace_all(&lower, today);
    let mut ranges = PERIOD.captures_iter(&current).peekable();
    if ranges.peek().is_none() {
        return SINGLE_YEARS
            .captures(&current)
            .map_or(0, |year| parse(&year[1]) * 12);
    }
    let mut total = 0;
    for range in ranges {
        let start = month(range.get(1).map(|part| part.as_str()), 1);
        let end = month(range.get(3).map(|part| part.as_str()), 12);
        total += ((parse(&range[4]) - parse(&range[2])) * 12 + (end - start) + 1).clamp(0, 600);
    }
    total
}

pub(crate) fn stated_months(text: &str) -> i64 {
    SINGLE_YEARS
        .captures_iter(&text.to_lowercase())
        .map(|year| parse(&year[1]) * 12)
        .max()
        .unwrap_or(0)
}

pub(crate) fn required_years(text: &str) -> f64 {
    let mut years: f64 = 0.0;
    for range in REQUIRED_YEARS.captures_iter(text) {
        years = years.max(range[1].parse::<f64>().unwrap_or_default());
        if let Some(end) = range.get(2) {
            years = years.max(end.as_str().parse::<f64>().unwrap_or_default());
        }
    }
    years
}

fn parse(text: &str) -> i64 {
    text.parse().unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::{months, required_years, stated_months};

    #[test]
    fn counts_inclusive_periods_and_caps_ranges() {
        assert_eq!(months("Mar 2023 to Present", "jun 2026"), 40);
        assert_eq!(months("2024–2024; 2025 -> 2025", "jun 2026"), 24);
        assert_eq!(months("Jan 1950 - Dec 2024", "jun 2026"), 600);
        assert_eq!(months("Dec 2025 - Jan 2025", "jun 2026"), 0);
        assert_eq!(months("3 YOE", "jun 2026"), 36);
        assert_eq!(stated_months("3 years, 5 yrs, 100 years"), 60);
        assert_eq!(
            required_years("3–5 years and 7+ YOE".to_lowercase().as_str()),
            7.0
        );
    }
}
