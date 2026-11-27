//! Input cleanup and field cuts for the department encoder.

use std::borrow::Cow;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

use crate::Job;
use crate::metadata::Cuts;
use crate::text;

static MARKUP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"<[^>]+>|&nbsp;|[#*_`>|]+").expect("the department markup regex must compile")
});

pub(crate) fn build_text(job: &Job<'_>, cuts: Cuts, separator: &str) -> String {
    let department = clean_department(job.department);
    let description = MARKUP.replace_all(job.description, " ");
    let flat_description = text::collapse_whitespace(&description);
    let fields = [
        text::slice_to(job.title, cuts.title),
        text::slice_to(&department, cuts.department),
        text::slice_to(job.company, cuts.company),
        text::slice_to(flat_description.trim_matches(text::is_space), cuts.excerpt),
    ];
    text::join_fields(fields, separator)
}

fn clean_department(raw: &str) -> Cow<'_, str> {
    let raw = raw.trim_matches(text::is_space);
    if !raw.starts_with('{') {
        return Cow::Borrowed(raw);
    }
    let Ok(Value::Object(fields)) = serde_json::from_str(raw) else {
        return Cow::Borrowed("");
    };
    let name = ["en_name", "name"]
        .into_iter()
        .filter_map(|key| fields.get(key))
        .find(|value| is_truthy(value));
    match name {
        Some(Value::String(name)) => Cow::Owned(name.clone()),
        Some(name) => Cow::Owned(python_repr(name)),
        None => Cow::Borrowed(""),
    }
}

fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|number| number != 0.0),
        Value::String(string) => !string.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

fn python_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Number(number) if number.is_f64() => {
            let number = number.as_f64().expect("the JSON number is a finite float");
            let repr = format!("{number:?}");
            // Python writes a signed exponent with at least two digits.
            match repr.split_once('e') {
                Some((mantissa, exponent)) => {
                    let exponent: i32 = exponent.parse().expect("the float exponent is numeric");
                    format!("{mantissa}e{exponent:+03}")
                }
                None => repr,
            }
        }
        Value::Number(number) => number.to_string(),
        Value::String(string) => quoted_string(string),
        Value::Array(items) => {
            let items: Vec<String> = items.iter().map(python_repr).collect();
            format!("[{}]", items.join(", "))
        }
        Value::Object(fields) => {
            let fields: Vec<String> = fields
                .iter()
                .map(|(key, value)| format!("{}: {}", quoted_string(key), python_repr(value)))
                .collect();
            format!("{{{}}}", fields.join(", "))
        }
    }
}

fn quoted_string(string: &str) -> String {
    use std::fmt::Write as _;

    static NONPRINTABLE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"[\p{C}\p{Z}&&[^ ]]").expect("the nonprintable character regex must compile")
    });

    let quote = if string.contains('\'') && !string.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut result = String::from(quote);
    for character in string.chars() {
        match character {
            '\t' => result.push_str("\\t"),
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            character if character == quote || character == '\\' => {
                result.push('\\');
                result.push(character);
            }
            character if NONPRINTABLE.is_match(character.encode_utf8(&mut [0; 4])) => {
                let codepoint = u32::from(character);
                match codepoint {
                    0..=0xff => write!(result, "\\x{codepoint:02x}"),
                    0x100..=0xffff => write!(result, "\\u{codepoint:04x}"),
                    _ => write!(result, "\\U{codepoint:08x}"),
                }
                .expect("writing to a String cannot fail");
            }
            character => result.push(character),
        }
    }
    result.push(quote);
    result
}

#[cfg(test)]
mod tests {
    use crate::Job;
    use crate::metadata::Cuts;

    use super::build_text;

    #[test]
    fn cleans_markup_and_prefers_the_english_department_name() {
        let job = Job {
            title: " Engineer ",
            company: "Example",
            department: r#" {"name":"開発","en_name":"Engineering"} "#,
            description: "# Build **tools** &nbsp; <a href='URL'>with</a> _Rust_` and\u{1c}Python > |",
        };
        assert_eq!(
            build_text(&job, Cuts::default(), "<eos>"),
            " Engineer  <eos> Engineering <eos> Example <eos> Build tools with Rust and Python"
        );
    }

    #[test]
    fn omits_missing_fields_and_cuts_unicode_characters() {
        let job = Job {
            title: "東京駅",
            department: r#" {"name":"技術","en_name":""} "#,
            description: "\n<pre>😀😀😀</pre> extra",
            ..Job::default()
        };
        let cuts = Cuts {
            title: -1,
            department: 1,
            excerpt: 2,
            ..Cuts::default()
        };
        assert_eq!(build_text(&job, cuts, "|"), "東京 | 技 | 😀😀");
        let invalid = Job {
            department: "{",
            description: "<br>&nbsp;",
            ..Job::default()
        };
        assert_eq!(build_text(&invalid, Cuts::default(), "<eos>"), "");
    }

    #[test]
    fn formats_truthy_json_department_values() {
        let job = Job {
            department: r#"{"en_name":false,"name":{"g":[null,true,"z","\u2028","x'y",1e-5]}}"#,
            ..Job::default()
        };
        assert_eq!(
            build_text(&job, Cuts::default(), "|"),
            "{'g': [None, True, 'z', '\\u2028', \"x'y\", 1e-05]}"
        );
    }
}
