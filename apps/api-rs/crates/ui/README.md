# Agent-Hire UI

Maud templates render complete pages and addressable fragments from view models. The Rust server
loads the data in process from the existing stores and services. Each page has a single UI SSE
subscription to the server event bus. Datastar applies the resulting HTML patches. The chat module
reads the agent's own session and host streams and uses the same Rust Markdown renderer for
history and streaming. The standard forms and native dialogs share a small browser controller.

The binary embeds every asset and serves it at a SHA-256 versioned URL. Datastar v1.0.4 is pinned
under `static/vendor` with its upstream MIT license. The uncompressed budgets are 50,000 bytes
of JavaScript (including Datastar) and 40,000 bytes of CSS. The asset unit test enforces both.

The vendor assets are the official minified core and the same release's DOM morphing watcher.
The watcher's entry point is limited to outer HTML patches keyed by ID, which are the only
patches the server emits. The core's watcher interface handles dispatch without compiling
expressions or requiring an inline script, and the adapter never executes scripts in patches.
