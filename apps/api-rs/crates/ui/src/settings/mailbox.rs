//! Read-only email monitoring settings.

use maud::{Markup, html};
use serde_json::Value;

use crate::view;

pub(super) fn panel(email: &Value) -> Markup {
    let mailbox = &email["mailbox"];
    let host = view::text(mailbox, "host");
    let username = view::text(mailbox, "username");
    let configured = email["configured"] == true;
    let enabled = email["enabled"] == true;
    let presets = [
        ("imap.gmail.com", "Gmail"),
        ("outlook.office365.com", "Outlook / Microsoft 365"),
        ("imap.fastmail.com", "Fastmail"),
        ("", "Other"),
    ];
    let preset = if presets.iter().any(|&(key, _)| key == host) {
        host
    } else {
        ""
    };
    html! {
        section.card.setting-card {
            p { "Agent-Hire connects from this device using read-only IMAP. It never marks messages as read, sends mail, or uploads mailbox content." }
            div.notice {
                strong { @if enabled { "Monitoring is on" } @else if configured { "Configured but paused" } @else { "Not connected" } }
                p { @if username.is_empty() { "Connect the email account you use for job applications" }
                    @else { (username) }
                    " · " (view::number(view::count(email, "processed_count"))) " messages already checked"
                }
            }
            form method="post" data-action="/ui/actions/email/connect" data-settings="email" data-busy="Saving…" {
                div.field-grid {
                    (view::select("Email provider", "provider_preset", preset, &presets))
                    label.field { span { "Email address" } input type="email" name="username" autocomplete="email" value=(username) placeholder="you@example.com" required; }
                    label.field { span { "App password" }
                        input type="password" name="password" data-secret autocomplete="new-password"
                            placeholder=(if mailbox["password_set"] == true { "Saved — leave blank to keep it" } else { "Use a provider app password" })
                            required[mailbox["password_set"] != true];
                        small { "Create a revocable app password in your provider’s security settings." }
                    }
                    label.field { span { "IMAP server" } input name="host" value=(host) placeholder="imap.example.com" required; }
                    label.field { span { "Port" } input name="port" type="number" min="1" max="65535" value=(mailbox["port"].as_u64().unwrap_or(993)) required; }
                    (view::input("Folder", "mailbox", mailbox["mailbox"].as_str().unwrap_or("INBOX"), "INBOX"))
                    (view::select("How emails are read", "mode", email["mode"].as_str().unwrap_or("rules_and_model"), &[
                        ("rules_and_model", "Rules, then the AI model for unclear emails"),
                        ("rules", "Rules only (nothing sent to a model)"),
                    ]))
                }
                p.muted { "Agent-Hire examines only mail from companies you applied to or their hiring systems. With the model on, it receives that company list and an unclear email to identify a rejection, interview, assessment, offer or receipt." }
                button.primary { @if configured { "Save & enable" } @else { "Connect inbox" } }
                output aria-live="polite" {}
            }
            @if configured {
                div.inline.mail-actions {
                    (action("check", "Test connection", "Testing…", ""))
                    @if enabled { (action("poll", "Check now", "Checking…", "leads")) }
                    (action("disconnect", "Disconnect", "Disconnecting…", ""))
                }
            }
        }
    }
}

fn action(path: &str, label: &str, busy: &str, effects: &str) -> Markup {
    html! {
        form method="post" data-action=(format!("/ui/actions/email/{path}")) data-busy=(busy) data-effects=(effects) {
            button.secondary { (label) }
            output aria-live="polite" {}
        }
    }
}
