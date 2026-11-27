# Privacy and security

Agent-Hire is designed to run locally, but it can send selected data to services you configure. Treat résumé text, contact details, job descriptions, email content, and API credentials as sensitive.

## What stays on your machine

The local API stores profile data, uploaded documents, generated assets, settings, activity, and application records in its application-data area. On Linux the default location is under `~/.local/share/Agent-Hire`; Windows and macOS use their normal per-user application-data locations. You can override the location with `AGENT_HIRE_DATA_DIR`.

The browser UI talks to the local API. The API protects application routes with a local bearer token and binds the normal development services to loopback addresses.

## What can leave your machine

- Résumé/profile text and job context sent to the AI provider selected in Settings.
- Search requests sent to the enabled job-source connectors.
- Optional email content sent through the mailbox connection you configure.
- Optional provider or job-enrichment requests enabled in your local settings.

Read the terms and privacy policy of every provider you connect. Ollama and subscription CLI providers can keep model calls local to the tools you run, but their own integrations and logs still apply.

## Credentials

Keep `.env`, API keys, cookies, bearer tokens, and mailbox credentials out of Git. The repository ignores local `.env` files and runtime databases. Agent-Hire masks stored secrets in the settings UI, but local filesystem access remains powerful: protect your operating-system account and application-data directory.

### Discovery run evidence

Job-discovery runs record internal material the product needs for resume and audit — frozen config/plan/dataset snapshots, per-attempt worker tokens, checkpoint journals, and process identity. Public surfaces (the UI, API responses, run reports, logs) receive an allowlisted projection only: attempt tokens, raw worker/process identity, frozen secrets, checkpoint journals, cookies, and auth headers never appear there. The worker receives its attempt token through the environment, never the command line, so it does not surface in process listings. Provider credentials that individual sources require are read from the environment or a local `.env` at fetch time; they are never stored in managed discovery config or run evidence — see Job discovery.

If a key is exposed, revoke or rotate it immediately and replace it in Settings. Do not upload credentials in screenshots or bug reports.

## Browser extension

The extension is intentionally fill-only. It may read and write fields on a supported career-site page when you invoke it, and it can attach the generated résumé where the page exposes a file input. It does not press Submit automatically. Review the browser's extension permissions and remove the extension when you no longer need it.

## Backups and deletion

Application reset affects only that app's SQLite, graph, vectors and personal
files. It returns `scraper_untouched: true`; neither reset mode calls a scraper
purge or history-clear endpoint. Other app instances and collected jobs remain
unchanged. A local full reset can also reset its shared source configuration;
remote source configuration stays operator-managed. Individual lead deletion
records local tombstones, with explicit restore available.

The standalone scraper has a separate database/role and `SCRAPER_DATA_DIR`.
Only search inputs and an optional candidate country cross that boundary.
The scraper's explicit operator reset rotates its generation, preserves all
app databases and credentials, and has no HTTP endpoint. See the
operator runbook. The reset command was not executed
during implementation.

Back up the local application-data directory if you need to preserve profile data, documents, generated assets, and settings. Deleting that data is local and irreversible unless you have a backup. Deleting a document does not necessarily remove profile points that were already imported from it; review the profile separately.

Before sharing diagnostics, remove résumé text, contact details, job URLs containing private information, provider responses, and all tokens. Use the Activity log's high-level messages instead of raw request bodies.
