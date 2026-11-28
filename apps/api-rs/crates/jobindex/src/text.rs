//! Character-offset helpers: the rules were written against Python strings, which index by
//! character, while Rust regex matches report byte offsets.

/// The byte offset `n` characters before `byte` (clamped to the start).
pub fn back(text: &str, byte: usize, n: usize) -> usize {
    let mut index = byte.min(text.len());
    for _ in 0..n {
        match text[..index].char_indices().next_back() {
            Some((start, _)) => index = start,
            None => break,
        }
    }
    index
}

/// The byte offset `n` characters after `byte` (clamped to the end).
pub fn forward(text: &str, byte: usize, n: usize) -> usize {
    let start = byte.min(text.len());
    match text[start..].char_indices().nth(n) {
        Some((offset, _)) => start + offset,
        None => text.len(),
    }
}

/// The first `n` characters.
pub fn prefix(text: &str, n: usize) -> &str {
    &text[..forward(text, 0, n)]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn offsets_count_characters() {
        let text = "aé年b";
        assert_eq!(back(text, text.len(), 2), 3);
        assert_eq!(forward(text, 0, 2), 3);
        assert_eq!(prefix(text, 3), "aé年");
        assert_eq!(forward(text, 0, 99), text.len());
        assert_eq!(back(text, 0, 5), 0);
    }
}
