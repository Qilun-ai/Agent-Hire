//! A date-time with a UTC offset, parsed and printed the way Python's `datetime` does for the
//! formats job sites publish.

use std::time::{SystemTime, UNIX_EPOCH};

use agent_hire_store::clock::days_from_civil;

const MONTHS: [&str; 12] = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
];

/// A local date-time plus its offset from UTC.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DateTime {
    /// Microseconds since the Unix epoch (UTC).
    pub micros: i64,
    /// The offset from UTC in seconds, kept for printing.
    pub offset: i32,
}

impl DateTime {
    /// Now, in UTC.
    pub fn now_utc() -> DateTime {
        let since = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default();
        DateTime {
            micros: i64::try_from(since.as_micros()).unwrap_or(i64::MAX),
            offset: 0,
        }
    }

    /// This moment shifted by `seconds`.
    pub fn minus_seconds(self, seconds: i64) -> DateTime {
        DateTime {
            micros: self.micros - seconds * 1_000_000,
            offset: self.offset,
        }
    }

    /// `YYYY-MM-DDTHH:MM:SS[.ffffff]+HH:MM`, as `datetime.isoformat()` prints an aware value.
    pub fn isoformat(self) -> String {
        self.format(true)
    }

    /// The same without the fraction (`isoformat(timespec="seconds")`).
    pub fn isoformat_seconds(self) -> String {
        self.format(false)
    }

    fn format(self, fraction: bool) -> String {
        let local = self.micros + i64::from(self.offset) * 1_000_000;
        let seconds = local.div_euclid(1_000_000);
        let micro = local.rem_euclid(1_000_000);
        let (year, month, day) = civil(seconds.div_euclid(86_400));
        let second_of_day = seconds.rem_euclid(86_400);
        let mut out = format!(
            "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}",
            second_of_day / 3600,
            second_of_day / 60 % 60,
            second_of_day % 60
        );
        if fraction && micro != 0 {
            out.push_str(&format!(".{micro:06}"));
        }
        let sign = if self.offset < 0 { '-' } else { '+' };
        let offset = self.offset.unsigned_abs();
        out.push_str(&format!(
            "{sign}{:02}:{:02}",
            offset / 3600,
            offset / 60 % 60
        ));
        if !offset.is_multiple_of(60) {
            out.push_str(&format!(":{:02}", offset % 60));
        }
        out
    }

    /// Parses an absolute date as the scraper rules accept it: epoch seconds or milliseconds,
    /// ISO 8601 (naive values are UTC), or one of the common written formats.
    pub fn parse_absolute(text: &str) -> Option<DateTime> {
        let text = text.trim();
        if text.is_empty() {
            return None;
        }
        if (10..=13).contains(&text.len()) && text.bytes().all(|b| b.is_ascii_digit()) {
            let number: i64 = text.parse().ok()?;
            let micros = if text.len() == 13 {
                number * 1000
            } else {
                number * 1_000_000
            };
            return Some(DateTime { micros, offset: 0 });
        }
        parse_iso(text).or_else(|| parse_written(text))
    }

    fn from_parts(
        date: (i64, u32, u32),
        time: (i64, i64, i64, i64),
        offset: i32,
    ) -> Option<DateTime> {
        let (year, month, day) = date;
        if !(1..=12).contains(&month) || day == 0 || day > days_in_month(year, month) {
            return None;
        }
        let (hour, minute, second, micro) = time;
        if hour > 23 || minute > 59 || second > 59 {
            return None;
        }
        let local = days_from_civil(year, month, day) * 86_400 + hour * 3600 + minute * 60 + second;
        Some(DateTime {
            micros: (local - i64::from(offset)) * 1_000_000 + micro,
            offset,
        })
    }
}

fn days_in_month(year: i64, month: u32) -> u32 {
    match month {
        2 if (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

fn civil(days: i64) -> (i64, u32, u32) {
    let days = days + 719_468;
    let era = days.div_euclid(146_097);
    let doe = days.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = u32::try_from(doy - (153 * mp + 2) / 5 + 1).unwrap_or(1);
    let month = u32::try_from(if mp < 10 { mp + 3 } else { mp - 9 }).unwrap_or(1);
    (yoe + era * 400 + i64::from(month <= 2), month, day)
}

fn digits(text: &str, start: usize, count: usize) -> Option<i64> {
    let slice = text.get(start..start + count)?;
    slice
        .bytes()
        .all(|b| b.is_ascii_digit())
        .then(|| slice.parse().ok())
        .flatten()
}

/// ISO 8601 as `datetime.fromisoformat` reads it (with `Z` accepted).
fn parse_iso(text: &str) -> Option<DateTime> {
    let bytes = text.as_bytes();
    let year = digits(text, 0, 4)?;
    let (month, day, mut rest) = if bytes.get(4) == Some(&b'-') {
        (
            u32::try_from(digits(text, 5, 2)?).ok()?,
            u32::try_from(digits(text, 8, 2)?).ok()?,
            &text[10.min(text.len())..],
        )
    } else {
        (
            u32::try_from(digits(text, 4, 2)?).ok()?,
            u32::try_from(digits(text, 6, 2)?).ok()?,
            &text[8.min(text.len())..],
        )
    };
    if bytes.get(4) == Some(&b'-') && (bytes.get(7) != Some(&b'-') || text.len() < 10) {
        return None;
    }
    let mut time = (0, 0, 0, 0);
    let mut offset = 0;
    if !rest.is_empty() {
        rest = rest.strip_prefix('T').or_else(|| rest.strip_prefix(' '))?;
        let hour = digits(rest, 0, 2)?;
        let mut minute = 0;
        let mut second = 0;
        let mut micro = 0;
        let mut index = 2;
        if rest.as_bytes().get(index) == Some(&b':') {
            minute = digits(rest, 3, 2)?;
            index = 5;
            if rest.as_bytes().get(index) == Some(&b':') {
                second = digits(rest, 6, 2)?;
                index = 8;
                if matches!(rest.as_bytes().get(index), Some(b'.' | b',')) {
                    let fraction: String = rest[index + 1..]
                        .chars()
                        .take_while(char::is_ascii_digit)
                        .collect();
                    if fraction.is_empty() {
                        return None;
                    }
                    index += 1 + fraction.len();
                    let padded: String = format!("{fraction:0<6}").chars().take(6).collect();
                    micro = padded.parse().ok()?;
                }
            }
        }
        time = (hour, minute, second, micro);
        let zone = rest[index..].trim();
        if zone.eq_ignore_ascii_case("z") {
            offset = 0;
        } else if !zone.is_empty() {
            let sign = match zone.as_bytes()[0] {
                b'+' => 1,
                b'-' => -1,
                _ => return None,
            };
            let body = zone[1..].replace(':', "");
            let hours: i32 = body.get(0..2)?.parse().ok()?;
            let minutes: i32 = body.get(2..4).map_or(Some(0), |m| m.parse().ok())?;
            offset = sign * (hours * 3600 + minutes * 60);
        }
    }
    DateTime::from_parts((year, month, day), time, offset)
}

fn month_number(name: &str, abbreviated: bool) -> Option<u32> {
    let lowered = name.to_lowercase();
    MONTHS
        .iter()
        .position(|month| {
            if abbreviated {
                month[..3] == lowered
            } else {
                *month == lowered
            }
        })
        .map(|index| index as u32 + 1)
}

/// The written formats: `26 November 2026`, `November 26, 2026`, `Nov 26, 2026`, `26 Nov 2026`,
/// `11/26/2026` (month first, then day first), RFC 2822 with a zone name, `2026/11/26`.
fn parse_written(text: &str) -> Option<DateTime> {
    let midnight = (0, 0, 0, 0);
    let words: Vec<&str> = text.split_whitespace().collect();
    let number = |word: &str| word.trim_end_matches(',').parse::<i64>().ok();
    if words.len() == 3 {
        let day_first = number(words[0]).zip(number(words[2]));
        if let Some((day, year)) = day_first
            && !words[0].ends_with(',')
        {
            for abbreviated in [false, true] {
                if let Some(month) = month_number(words[1], abbreviated) {
                    return DateTime::from_parts(
                        (year, month, u32::try_from(day).ok()?),
                        midnight,
                        0,
                    );
                }
            }
        }
        if words[1].ends_with(',')
            && let (Some(day), Some(year)) = (number(words[1]), number(words[2]))
        {
            for abbreviated in [false, true] {
                if let Some(month) = month_number(words[0], abbreviated) {
                    return DateTime::from_parts(
                        (year, month, u32::try_from(day).ok()?),
                        midnight,
                        0,
                    );
                }
            }
        }
    }
    let slashed: Vec<&str> = text.split('/').collect();
    if slashed.len() == 3
        && slashed
            .iter()
            .all(|part| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()))
    {
        let parts: Vec<i64> = slashed
            .iter()
            .filter_map(|part| part.parse().ok())
            .collect();
        if slashed[0].len() == 4 {
            return DateTime::from_parts(
                (
                    parts[0],
                    u32::try_from(parts[1]).ok()?,
                    u32::try_from(parts[2]).ok()?,
                ),
                midnight,
                0,
            );
        }
        if slashed[2].len() == 4 {
            let month_first = DateTime::from_parts(
                (
                    parts[2],
                    u32::try_from(parts[0]).ok()?,
                    u32::try_from(parts[1]).ok()?,
                ),
                midnight,
                0,
            );
            return month_first.or_else(|| {
                DateTime::from_parts(
                    (
                        parts[2],
                        u32::try_from(parts[1]).ok()?,
                        u32::try_from(parts[0]).ok()?,
                    ),
                    midnight,
                    0,
                )
            });
        }
    }
    // "Wed, 26 Nov 2026 06:30:24 GMT"
    if words.len() == 6 && words[0].ends_with(',') {
        let day = u32::try_from(number(words[1])?).ok()?;
        let month = month_number(words[2], true)?;
        let year = number(words[3])?;
        let clock: Vec<i64> = words[4]
            .split(':')
            .filter_map(|part| part.parse().ok())
            .collect();
        if clock.len() == 3 && words[5].chars().all(char::is_alphabetic) {
            return DateTime::from_parts((year, month, day), (clock[0], clock[1], clock[2], 0), 0);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_round_trips_like_python() {
        let parsed = DateTime::parse_absolute("2026-11-20").expect("date");
        assert_eq!(parsed.isoformat(), "2026-11-20T00:00:00+00:00");
        let parsed = DateTime::parse_absolute("2026-11-20T10:15:00.5+05:30").expect("aware");
        assert_eq!(parsed.isoformat(), "2026-11-20T10:15:00.500000+05:30");
        assert_eq!(
            DateTime::parse_absolute("2026-11-20T10:15:00Z")
                .expect("z")
                .isoformat(),
            "2026-11-20T10:15:00+00:00"
        );
        assert_eq!(
            DateTime::parse_absolute("1795674408")
                .expect("epoch")
                .isoformat(),
            "2026-11-26T06:26:48+00:00"
        );
    }

    #[test]
    fn written_formats() {
        for text in [
            "26 November 2026",
            "November 26, 2026",
            "Nov 26, 2026",
            "26 Nov 2026",
            "11/26/2026",
            "2026/11/26",
        ] {
            assert_eq!(
                DateTime::parse_absolute(text)
                    .map(DateTime::isoformat)
                    .as_deref(),
                Some("2026-11-26T00:00:00+00:00"),
                "{text}"
            );
        }
        assert_eq!(
            DateTime::parse_absolute("26/11/2026")
                .map(DateTime::isoformat)
                .as_deref(),
            Some("2026-11-26T00:00:00+00:00")
        );
        assert!(DateTime::parse_absolute("30+ days ago").is_none());
    }
}
