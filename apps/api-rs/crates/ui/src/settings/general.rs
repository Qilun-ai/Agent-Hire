//! Appearance, activity, license and the explicit reset confirmation.

use maud::{Markup, html};
use serde_json::Value;

use crate::view;

pub(super) fn panel(activity: &Value) -> Markup {
    let rows = activity.as_array().map_or(&[][..], Vec::as_slice);
    html! {
        section.card.setting-card {
            h3 { "Appearance" }
            fieldset.chips #theme-settings {
                legend.sr-only { "Theme" }
                @for (key, label) in [("light", "Light"), ("dark", "Dark"), ("system", "System")] {
                    label.choice { input type="radio" name="theme" value=(key) checked[key == "system"]; (label) }
                }
            }
        }
        section.card.setting-card {
            h3 { "Activity log" }
            @if rows.is_empty() { p.muted { "Background activity will appear here." } }
            @else {
                div.table-scroll {
                    table {
                        thead { tr { th { "Time" } th { "Kind" } th { "Message" } } }
                        tbody {
                            @for row in rows {
                                @let action = view::text(row, "action");
                                @let (kind, message) = action.split_once(": ").unwrap_or(("Activity", action));
                                tr { td { (view::text(row, "ts").replace('T', " ")) } td { (kind) } td { (message) } }
                            }
                        }
                    }
                }
            }
        }
        section.card.setting-card {
            h3 { "About & license" }
            p {
                "Agent-Hire, copyright (C) 2026 noobed-max and Agent-Hire contributors. Licensed under the "
                (view::external("GNU AGPL v3.0 only", "https://www.gnu.org/licenses/agpl-3.0.en.html"))
                ", with no warranty. Source code: "
                (view::external("github.com/Qilun-ai/Agent-Hire", "https://github.com/Qilun-ai/Agent-Hire")) "."
            }
        }
        section.card.setting-card.danger-zone {
            h3 { "Danger zone" }
            p { "Delete saved leads, your profile and generated documents from this device. Settings, provider keys and job sources are kept unless you choose a full reset." }
            details #reset-confirmation {
                summary.button.danger { "Delete all data" }
                form method="post" data-action="/ui/actions/data/reset" data-busy="Deleting…" {
                    label.check { input type="checkbox" name="clear_settings" data-boolean;
                        "Also reset your settings, provider keys, preferences and local job source selections — no backup is kept"
                    }
                    p data-reset-copy { "A backup of the local database is written first." }
                    label.field { span { "Type DELETE to confirm" } input name="confirm" autocomplete="off" required; }
                    div.inline {
                        button.danger data-reset-submit disabled { "Delete all data" }
                        button.secondary type="button" data-cancel-reset { "Cancel" }
                    }
                    output aria-live="polite" {}
                    a.button.secondary href="/settings/general" data-reload-app hidden { "Reload app" }
                }
            }
        }
    }
}
