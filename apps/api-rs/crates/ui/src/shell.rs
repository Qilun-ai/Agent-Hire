//! The document shell, navigation and shared drawers.

use maud::{DOCTYPE, Markup, html};

use crate::assets;
use crate::view;

/// The primary page shown in the document.
#[derive(Clone, Copy, Eq, PartialEq)]
pub enum Tab {
    /// Agent chat.
    Chat,
    /// Job index and shortlist.
    Jobs,
    /// Application pipeline.
    Applications,
    /// Local preferences.
    Settings,
}

impl Tab {
    fn label(self) -> &'static str {
        match self {
            Self::Chat => "Agent chat",
            Self::Jobs => "Jobs",
            Self::Applications => "Applications",
            Self::Settings => "Settings",
        }
    }

    fn href(self) -> &'static str {
        match self {
            Self::Chat => "/",
            Self::Jobs => "/jobs",
            Self::Applications => "/applications",
            Self::Settings => "/settings/ai",
        }
    }
}

/// Current counts on the two pipeline tabs.
#[derive(Default)]
pub struct Badges {
    /// Jobs in the extension queue.
    pub cart: u64,
    /// Applications in all active and completed stages.
    pub applied: u64,
}

/// Renders the two independently updated counts and the navigation.
pub fn navigation(active: Tab, badges: &Badges) -> Markup {
    html! {
        nav #primary-nav aria-label="Main navigation" {
            @for (tab, count) in [(Tab::Chat, 0), (Tab::Jobs, badges.cart), (Tab::Applications, badges.applied), (Tab::Settings, 0)] {
                a href=(tab.href()) aria-current=[(tab == active).then_some("page")] {
                    (tab.label())
                    @if count > 0 { " " span.badge { (view::number(count)) } }
                }
            }
        }
    }
}

/// Wraps a complete view in the application chrome.
pub fn page(
    tab: Tab,
    token: &str,
    live: &str,
    badges: &Badges,
    status: Markup,
    content: Markup,
) -> Markup {
    html! {
        (DOCTYPE)
        html lang="en" {
            head {
                meta charset="utf-8";
                meta name="viewport" content="width=device-width,initial-scale=1";
                meta name="agent-token" content=(token);
                title { (tab.label()) " · Agent-Hire" }
                link rel="icon" href=(assets::path("logo.svg")) type="image/svg+xml";
                script src=(assets::path("theme.js")) {}
                link rel="stylesheet" href=(assets::path("ui.css"));
            }
            body data-live=(live) data-datastar=(assets::path("datastar-core.js")) data-patch=(assets::path("datastar-patch.js")) data-chat=[(tab == Tab::Chat).then(|| assets::path("chat.js"))] {
                (maud::PreEscaped(include_str!("../static/icons.svg")))
                a.skip href="#main" { "Skip to content" }
                header.topbar {
                    a.brand href="/" { img src=(assets::path("logo.svg")) alt="" width="32" height="32"; "Agent-Hire" }
                    (navigation(tab, badges))
                    div.top-actions {
                        (status)
                        button.quiet type="button" data-open="/ui/companies" { "Companies" }
                        button.quiet type="button" data-open="/ui/profile" { "Profile" }
                    }
                }
                main #main { (content) }
                (dialog("side-panel", "drawer"))
                (dialog("popover", "popover"))
                div #toast role="status" aria-live="polite" {}
                span #profile-refresh hidden {}
                span #companies-refresh hidden {}
                script type="module" src=(assets::path("ui.js")) {}
            }
        }
    }
}

fn dialog(id: &str, class: &str) -> Markup {
    html! {
        dialog id=(id) class=(class) aria-label="Details" {
            form method="dialog" .dismiss { button.icon aria-label="Close" { (view::icon("close")) } }
            div data-dialog-body {}
        }
    }
}

/// Displays the startup state while the agent or backend becomes ready.
pub fn starting() -> Markup {
    html! { div.starting role="status" { img src=(assets::path("logo.svg")) alt="Agent-Hire" width="42" height="42"; span.spinner aria-hidden="true" {} "Starting…" } }
}

#[cfg(test)]
mod tests {
    use maud::html;

    use super::{Badges, Tab, page};

    #[test]
    fn shell_escapes_embedded_credentials_and_marks_the_active_tab() {
        let rendered = page(
            Tab::Jobs,
            "\"><script>",
            "/ui/live?tab=jobs",
            &Badges {
                cart: 2,
                applied: 4,
            },
            html! {},
            html! {},
        )
        .into_string();
        assert!(rendered.contains("content=\"&quot;&gt;&lt;script&gt;\""));
        assert!(rendered.contains("href=\"/jobs\" aria-current=\"page\""));
        assert!(rendered.contains(">4</span>"));
    }
}
