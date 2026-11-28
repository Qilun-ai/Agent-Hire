//! The per-process API bearer token.

use std::fmt;

/// A random 64-character lowercase hex token, regenerated on every start and never stored.
#[derive(Clone, PartialEq, Eq)]
pub struct Token(String);

impl Token {
    /// Makes a token from 32 bytes of OS randomness.
    ///
    /// # Panics
    ///
    /// Panics if the OS random source fails, which leaves no safe way to authenticate clients.
    pub fn generate() -> Token {
        let mut bytes = [0_u8; 32];
        getrandom::fill(&mut bytes).expect("the OS random source must be available");
        Token(hex(&bytes))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Compares in time independent of where the first difference is.
    pub fn matches(&self, candidate: &str) -> bool {
        constant_time_eq(self.0.as_bytes(), candidate.as_bytes())
    }
}

impl fmt::Debug for Token {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Token(<redacted>)")
    }
}

/// Lowercase hex encoding.
pub fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(char::from(DIGITS[usize::from(byte >> 4)]));
        out.push(char::from(DIGITS[usize::from(byte & 0x0f)]));
    }
    out
}

/// Equality that does not stop at the first differing byte.
pub fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut difference = 0_u8;
    for (a, b) in left.iter().zip(right) {
        difference |= a ^ b;
    }
    difference == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_tokens_are_hex_and_distinct() {
        let first = Token::generate();
        let second = Token::generate();
        assert_eq!(first.as_str().len(), 64);
        assert!(
            first
                .as_str()
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        );
        assert_ne!(first, second);
        assert!(first.matches(first.as_str()));
        assert!(!first.matches(second.as_str()));
        assert!(!first.matches(""));
    }
}
