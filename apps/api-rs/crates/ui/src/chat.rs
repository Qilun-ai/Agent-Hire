//! Chat shell, history and conversation fragments.

mod tool_card;

use maud::{Markup, html};
use serde_json::Value;

use crate::markdown;
use crate::shell;
use crate::view;

const SUGGESTIONS: [&str; 4] = [
    "Here's my résumé — I'm a fresher looking for software engineering roles.",
    "Find software engineer jobs posted in the last 24 hours that match my skills.",
    "Scrape new jobs and check back every 3 hours with anything that fits me.",
    "How are my applications doing?",
];

/// Renders the browser chat surface and its initial welcome view.
pub fn page() -> Markup {
    html! {
        div.chat-layout {
            aside.chat-history id="chat-history" aria-label="Chat history" {
                button.primary #chat-new type="button" { (view::icon("plus")) "New chat" }
                input #chat-filter type="search" aria-label="Search chats" placeholder="Search chats…";
                div #chat-sessions { p.muted { "Your chats will appear here" } }
            }
            section.chat-main aria-label="Agent chat" {
                header.chat-heading {
                    button.icon #chat-history-toggle type="button" aria-label="Toggle chat history" aria-controls="chat-history" aria-expanded="false" { (view::icon("history")) }
                    h1 #chat-title { "Agent chat" }
                    span #chat-connection role="status" {}
                }
                div #chat-banner .notice hidden {
                    span #chat-reason { "Add an AI provider key to start chatting." }
                    a.button.secondary href="/settings/ai" { "AI model settings" }
                }
                div #chat-scroll role="log" aria-label="Chat messages" aria-live="off" tabindex="0" {
                    div #chat-thread {}
                    div #chat-starting hidden { (shell::starting()) }
                    (welcome())
                    div #chat-typing .typing hidden role="status" { (typing()) }
                }
                div.chat-bottom {
                    div #chat-schedules .chips aria-label="Scheduled check-ins" {}
                    form #chat-form {
                        output #chat-error aria-live="polite" {}
                        div #chat-files .chips {}
                        div.composer {
                            button.icon #chat-attach type="button" aria-label="Attach files" { (view::icon("attach")) }
                            input type="file" id="chat-upload" accept=".pdf,.docx,.doc,.txt,.md,.tex,.rtf" multiple hidden;
                            textarea #chat-message aria-label="Message Agent-Hire" placeholder="Message Agent-Hire…" rows="1" maxlength="20000" {}
                            button.primary #chat-send type="submit" disabled { "Send" }
                        }
                        p #chat-model .muted {}
                    }
                }
            }
        }
    }
}

fn welcome() -> Markup {
    html! {
        section #chat-welcome {
            h2 { "What can I find for you?" }
            p { "Search for roles, tailor your résumé and keep track of your applications." }
            div.suggestions {
                @for (index, &suggestion) in SUGGESTIONS.iter().enumerate() {
                    button type="button" class="card suggestion" data-suggestion=(suggestion) data-attach[index == 0] {
                        span { (suggestion) }
                        (view::icon(if index == 0 { "attach" } else { "arrow" }))
                    }
                }
            }
            noscript { p.muted { "Enable JavaScript to send messages and see your chat history." } }
        }
    }
}

/// Renders chat fragments using the same markup for stored and streamed items.
pub fn render(kind: &str, data: &Value) -> Markup {
    match kind {
        "thread" => html! { @for entry in data.as_array().into_iter().flatten() { (item(entry)) } },
        "item" => item(data),
        "history" => history(data),
        "schedules" => schedules(data),
        "starting" => shell::starting(),
        _ => html! {},
    }
}

fn typing() -> Markup {
    html! { span.spinner aria-hidden="true" {} "Agent is working…" }
}

fn item(data: &Value) -> Markup {
    let text = view::text(data, "text");
    match view::text(data, "kind") {
        "user" if data["source"] == "schedule" => {
            let instruction = text
                .strip_prefix("[Scheduled check-in")
                .and_then(|line| line.split_once(']'))
                .map_or(text, |(_, line)| line.trim());
            html! { div.message.system { "Scheduled check-in — " (instruction) } }
        }
        "user" => user(text),
        "assistant" if data["streaming"] == true && text.is_empty() => {
            html! { div.message.typing { (typing()) } }
        }
        "assistant" => {
            let error = view::text(data, "error");
            if text.is_empty() && error.is_empty() {
                return html! {};
            }
            html! {
                div.message.assistant {
                    div.markdown { (markdown::render(text)) }
                    @if !error.is_empty() { (error_message(error)) }
                }
            }
        }
        "tool" => {
            html! { div data-tool-id=(view::text(data, "id")) data-args=(data["args"].to_string()) { (tool_card::render(data)) } }
        }
        "notice" => html! { div.message.notice-message { (text) } },
        "error" => html! { div.message.assistant { (error_message(text)) } },
        _ => html! {},
    }
}

fn error_message(error: &str) -> Markup {
    let lower = error.to_ascii_lowercase();
    html! {
        p.bad role="alert" { (error) }
        @if ["key", "provider", "settings"].iter().any(|word| lower.contains(word)) {
            a.button.secondary href="/settings/ai" { "AI model settings" }
        }
    }
}

fn user(text: &str) -> Markup {
    let mut files = Vec::new();
    let mut lines = Vec::new();
    for line in text.lines() {
        if let Some((name, _)) = line
            .strip_prefix("[Attached file: ")
            .and_then(|s| s.strip_suffix(']'))
            .and_then(|s| s.rsplit_once(" ("))
        {
            files.push(name);
        } else {
            lines.push(line);
        }
    }
    html! {
        div.message.user-message {
            @if !lines.is_empty() { div.prose-line { (lines.join("\n")) } }
            @if !files.is_empty() {
                div.chips { @for name in files { span.chip { (view::icon("attach")) (name) } } }
            }
        }
    }
}

fn history(data: &Value) -> Markup {
    let items = view::list(data, "sessions");
    html! {
        @if items.is_empty() { p.muted { "Your chats will appear here" } }
        @for entry in items {
            @let id = view::text(entry, "id");
            @let title = view::text(entry, "title");
            div.session {
                button.session-link type="button" data-session=(id) aria-pressed=(data["active"] == id) title=(title) {
                    @if entry["running"] == true { span.dot.live aria-label="Running" {} }
                    span { (title) }
                }
                button.icon type="button" data-delete=(id) aria-label=(format!("Delete {title}")) { (view::icon("trash")) }
            }
        }
    }
}

fn schedules(data: &Value) -> Markup {
    html! {
        @for schedule in view::list(data, "schedules") {
            span.chip title=(view::text(schedule, "instruction")) {
                (repeat(schedule)) " · next " (view::time(view::text(schedule, "nextRunAt"), "clock"))
                button.icon type="button" aria-label="Cancel check-in" data-cancel=(view::text(schedule, "id")) { (view::icon("close")) }
            }
        }
    }
}

pub(super) fn repeat(schedule: &Value) -> String {
    let minutes = view::count(schedule, "repeatEveryMinutes");
    if minutes == 0 {
        "once".to_owned()
    } else if minutes.is_multiple_of(60) {
        format!("every {}h", minutes / 60)
    } else {
        format!("every {minutes}m")
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{page, render};

    #[test]
    fn welcome_renders_without_the_agent_and_messages_escape_files() {
        let page = page().into_string();
        assert!(page.contains("What can I find for you?"));
        assert_eq!(page.matches("data-suggestion=").count(), 4);
        let message = render("item", &json!({"kind":"user","text":"<b>hello</b>\n[Attached file: <img> (1 KB, upload_id=secret)]"})).into_string();
        assert!(message.contains("&lt;b&gt;hello&lt;/b&gt;"));
        assert!(message.contains("&lt;img&gt;"));
        assert!(!message.contains("upload_id"));
    }
}
