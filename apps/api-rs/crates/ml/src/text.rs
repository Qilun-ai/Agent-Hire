//! Character slicing, whitespace normalization, and field separation shared by the encoders.

use std::borrow::Cow;
use std::sync::LazyLock;

use regex::Regex;

static WHITESPACE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"[\s\x1c-\x1f]+").expect("the Python whitespace regex must compile")
});

pub(crate) fn is_space(character: char) -> bool {
    // Python's str.isspace also includes these four ASCII information separators.
    character.is_whitespace() || matches!(character, '\x1c'..='\x1f')
}

pub(crate) fn collapse_whitespace(text: &str) -> Cow<'_, str> {
    WHITESPACE.replace_all(text, " ")
}

pub(crate) fn prefix(text: &str, count: usize) -> &str {
    match text.char_indices().nth(count) {
        Some((end, _)) => &text[..end],
        None => text,
    }
}

pub(crate) fn slice_to(text: &str, end: isize) -> &str {
    if end >= 0 {
        return prefix(text, end as usize);
    }
    match text.char_indices().rev().nth(end.unsigned_abs() - 1) {
        Some((end, _)) => &text[..end],
        None => "",
    }
}

pub(crate) fn join_fields<const N: usize>(fields: [&str; N], separator: &str) -> String {
    let nonempty: Vec<&str> = fields
        .into_iter()
        .filter(|field| !field.is_empty())
        .collect();
    nonempty.join(&format!(" {separator} "))
}
