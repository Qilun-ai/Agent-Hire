//! Raw text from documents and web pages, for the agent to read itself (no AI pipeline).

mod extract;
mod html;

pub use extract::{MAX_BYTES, extract, tidy};
pub use html::{html_to_markdown, page_text, unescape};
