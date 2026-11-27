//! Safe Markdown shared by chat history and streaming replies.

use std::sync::LazyLock;

use maud::{Markup, PreEscaped, html};
use pulldown_cmark::{CowStr, Event, Options, Parser, Tag, TagEnd};
use regex::Regex;

use crate::view;

static BARE_URL: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"https?://[^\s<>"']+"#).expect("bare URL pattern is valid"));

/// Renders Markdown with escaped HTML and HTTP(S)-only links.
pub fn render(source: &str) -> Markup {
    let options = Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH;
    let mut events = Vec::new();
    let mut link_end = None;
    let mut in_code = false;
    let mut images = 0;
    for event in Parser::new_ext(source, options) {
        match event {
            Event::Start(Tag::Image { .. }) => images += 1,
            Event::End(TagEnd::Image) => images -= 1,
            Event::Start(Tag::Link { dest_url, .. }) => {
                if images == 0 {
                    let (open, close) = if let Some(url) = view::safe_url(&dest_url) {
                        let tag =
                            html! { a href=(url) target="_blank" rel="noreferrer noopener" {} };
                        (
                            tag.into_string().trim_end_matches("</a>").to_owned(),
                            "</a>",
                        )
                    } else {
                        ("<span>".to_owned(), "</span>")
                    };
                    events.push(Event::InlineHtml(CowStr::from(open)));
                    link_end = Some(close);
                }
            }
            Event::End(TagEnd::Link) if images == 0 => {
                if let Some(close) = link_end.take() {
                    events.push(Event::InlineHtml(CowStr::from(close)));
                }
            }
            Event::Html(raw) | Event::InlineHtml(raw) => events.push(Event::Text(raw)),
            Event::Start(Tag::CodeBlock(_)) => {
                in_code = true;
                events.push(event);
            }
            Event::End(TagEnd::CodeBlock) => {
                in_code = false;
                events.push(event);
            }
            Event::Text(raw) if link_end.is_none() && !in_code && images == 0 => {
                let mut end = 0;
                for matched in BARE_URL.find_iter(&raw) {
                    let url = matched
                        .as_str()
                        .trim_end_matches(['.', ',', '!', '?', ';', ':', ')', ']', '}']);
                    if view::safe_url(url).is_none() {
                        continue;
                    }
                    events.push(Event::Text(CowStr::from(
                        raw[end..matched.start()].to_owned(),
                    )));
                    let link = view::external(url, url);
                    events.push(Event::InlineHtml(CowStr::from(link.into_string())));
                    end = matched.start() + url.len();
                }
                events.push(Event::Text(CowStr::from(raw[end..].to_owned())));
            }
            Event::Text(_) | Event::Code(_) if images > 0 => events.push(event),
            _ if images > 0 => {}
            other => events.push(other),
        }
    }
    let mut output = String::new();
    pulldown_cmark::html::push_html(&mut output, events.into_iter());
    PreEscaped(output)
}

#[cfg(test)]
mod tests {
    use super::render;

    #[test]
    fn renders_supported_blocks() {
        let output = render("# Hello\n\n1. **one**\n2. *two*\n\n| A | B |\n|---|---|\n| x | y |\n\n```js\na < b\n```").into_string();
        for expected in [
            "<h1>Hello</h1>",
            "<ol>",
            "<strong>one</strong>",
            "<em>two</em>",
            "<table>",
            "a &lt; b",
        ] {
            assert!(output.contains(expected), "missing {expected}");
        }
    }

    #[test]
    fn escapes_markup_and_filters_destinations() {
        let output = render("<script>alert(1)</script>\n\n[x](javascript:alert) [y](//host) [z](https://example.org) https://example.net. `http://code.example` ![alt](http://image.example)").into_string();
        assert!(output.contains("&lt;script&gt;"));
        assert!(!output.contains("href=\"javascript"));
        assert!(!output.contains("href=\"//"));
        assert!(!output.contains("<img"));
        assert!(!output.contains("href=\"http://code.example"));
        assert_eq!(output.matches("rel=\"noreferrer noopener\"").count(), 2);
        assert!(output.contains("https://example.net</a>."));
    }
}
