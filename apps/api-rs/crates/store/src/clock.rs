//! UTC wall-clock formatting without a date library.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// A broken-down UTC time.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct UtcTime {
    /// The year, for example 2026.
    pub year: i64,
    /// 1 to 12.
    pub month: u32,
    /// 1 to 31.
    pub day: u32,
    /// 0 to 23.
    pub hour: u32,
    /// 0 to 59.
    pub minute: u32,
    /// 0 to 59.
    pub second: u32,
    /// 0 to 999 999.
    pub microsecond: u32,
}

impl UtcTime {
    /// The current time.
    pub fn now() -> UtcTime {
        let since_epoch = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or(Duration::ZERO);
        UtcTime::from_unix(since_epoch)
    }

    /// This time plus `seconds` (which may be negative).
    pub fn plus_seconds(self, seconds: i64) -> UtcTime {
        let base = self.unix_seconds() + seconds;
        let mut shifted =
            UtcTime::from_unix(Duration::from_secs(u64::try_from(base.max(0)).unwrap_or(0)));
        shifted.microsecond = self.microsecond;
        shifted
    }

    /// Seconds since the Unix epoch.
    pub fn unix_seconds(self) -> i64 {
        days_from_civil(self.year, self.month, self.day) * 86_400
            + i64::from(self.hour) * 3600
            + i64::from(self.minute) * 60
            + i64::from(self.second)
    }

    /// `2026-11-26 06:30:24`, the format of SQLite's `datetime('now')`.
    pub fn to_sqlite(self) -> String {
        format!(
            "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
            self.year, self.month, self.day, self.hour, self.minute, self.second
        )
    }

    /// Converts a time since the Unix epoch.
    pub fn from_unix(since_epoch: Duration) -> UtcTime {
        let seconds = i64::try_from(since_epoch.as_secs()).unwrap_or(i64::MAX);
        let (year, month, day) = civil_from_days(seconds.div_euclid(86_400));
        let second_of_day = u32::try_from(seconds.rem_euclid(86_400)).unwrap_or(0);
        UtcTime {
            year,
            month,
            day,
            hour: second_of_day / 3600,
            minute: second_of_day / 60 % 60,
            second: second_of_day % 60,
            microsecond: since_epoch.subsec_micros(),
        }
    }

    /// `2026-11-26T06:30:24Z`.
    pub fn to_iso_seconds_z(self) -> String {
        format!(
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
            self.year, self.month, self.day, self.hour, self.minute, self.second
        )
    }

    /// `2026-11-26T06:30:24.123456+00:00`, as Python's `datetime.isoformat()` writes UTC times
    /// (the fraction is left out when it is zero).
    pub fn to_iso_micros(self) -> String {
        let fraction = if self.microsecond == 0 {
            String::new()
        } else {
            format!(".{:06}", self.microsecond)
        };
        format!(
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}{fraction}+00:00",
            self.year, self.month, self.day, self.hour, self.minute, self.second
        )
    }
}

/// (year, month, day) to days since 1970-01-01.
pub fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let year_of_era = year.rem_euclid(400);
    let month = i64::from(month);
    let day_of_year =
        (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + i64::from(day) - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

/// Parses the date-time formats SQLite's date functions accept (`YYYY-MM-DD`, optionally with
/// `T` or a space and `HH:MM[:SS[.fff]]`, optionally ending in `Z` or `±HH:MM`) to Unix seconds.
pub fn parse_sqlite_datetime(text: &str) -> Option<i64> {
    let text = text.trim();
    let bytes = text.as_bytes();
    if bytes.len() < 10 || bytes[4] != b'-' || bytes[7] != b'-' {
        return None;
    }
    let year: i64 = text.get(0..4)?.parse().ok()?;
    let month: u32 = text.get(5..7)?.parse().ok()?;
    let day: u32 = text.get(8..10)?.parse().ok()?;
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    let mut seconds = days_from_civil(year, month, day) * 86_400;
    let mut rest = &text[10..];
    if rest.is_empty() {
        return Some(seconds);
    }
    rest = rest.strip_prefix('T').or_else(|| rest.strip_prefix(' '))?;
    let hour: i64 = rest.get(0..2)?.parse().ok()?;
    if rest.as_bytes().get(2) != Some(&b':') {
        return None;
    }
    let minute: i64 = rest.get(3..5)?.parse().ok()?;
    seconds += hour * 3600 + minute * 60;
    rest = &rest[5..];
    if let Some(after) = rest.strip_prefix(':') {
        let second: i64 = after.get(0..2)?.parse().ok()?;
        seconds += second;
        rest = &after[2..];
        if let Some(fraction) = rest.strip_prefix('.') {
            let digits = fraction.bytes().take_while(u8::is_ascii_digit).count();
            rest = &fraction[digits..];
        }
    }
    let rest = rest.trim();
    if rest.is_empty() || rest.eq_ignore_ascii_case("z") {
        return Some(seconds);
    }
    let sign = match rest.as_bytes()[0] {
        b'+' => 1,
        b'-' => -1,
        _ => return None,
    };
    let offset_hours: i64 = rest.get(1..3)?.parse().ok()?;
    let offset_minutes: i64 = rest.get(4..6).and_then(|m| m.parse().ok()).unwrap_or(0);
    Some(seconds - sign * (offset_hours * 3600 + offset_minutes * 60))
}

/// Days since 1970-01-01 to (year, month, day) in the proleptic Gregorian calendar.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    // Shift the epoch to 0000-03-01 so leap days fall at the end of a 400-year era.
    let days = days + 719_468;
    let era = days.div_euclid(146_097);
    let day_of_era = days.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = u32::try_from(day_of_year - (153 * month_index + 2) / 5 + 1).unwrap_or(1);
    let month = u32::try_from(if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    })
    .unwrap_or(1);
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_sqlite_formats() {
        assert_eq!(parse_sqlite_datetime("1970-01-02"), Some(86_400));
        assert_eq!(
            parse_sqlite_datetime("2026-11-26 06:26:48"),
            Some(1_795_674_408)
        );
        assert_eq!(
            parse_sqlite_datetime("2026-11-26T06:26:48.123+00:00"),
            Some(1_795_674_408)
        );
        assert_eq!(
            parse_sqlite_datetime("2026-11-26T08:26:48+02:00"),
            Some(1_795_674_408)
        );
        assert_eq!(parse_sqlite_datetime("26/11/2026"), None);
        let time = UtcTime::from_unix(Duration::from_secs(1_795_674_408));
        assert_eq!(time.unix_seconds(), 1_795_674_408);
        assert_eq!(time.plus_seconds(86_400).to_sqlite(), "2026-11-27 06:26:48");
    }

    #[test]
    fn formats_known_instants() {
        let time = UtcTime::from_unix(Duration::new(1_795_674_408, 123_456_000));
        assert_eq!(time.to_iso_seconds_z(), "2026-11-26T06:26:48Z");
        assert_eq!(time.to_iso_micros(), "2026-11-26T06:26:48.123456+00:00");
        assert_eq!(
            UtcTime::from_unix(Duration::ZERO).to_iso_micros(),
            "1970-01-01T00:00:00+00:00"
        );
        // A leap day.
        assert_eq!(
            UtcTime::from_unix(Duration::from_secs(951_782_400)).to_iso_seconds_z(),
            "2000-02-29T00:00:00Z"
        );
    }
}
