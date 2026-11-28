//! Borrowed posting fields used by the encoders.

/// A job posting to classify. Use empty strings for unavailable fields.
#[derive(Clone, Copy, Debug, Default)]
pub struct Job<'a> {
    /// The posting's title.
    pub title: &'a str,
    /// The posting's full description.
    pub description: &'a str,
    /// The employer's department label, which may be a JSON object.
    pub department: &'a str,
    /// The employer's name.
    pub company: &'a str,
}
