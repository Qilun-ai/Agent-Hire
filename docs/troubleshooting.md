# Troubleshooting

## Windows: startup problems

Run the installer from a normal PowerShell; it uses winget to install Git, Node.js, Rust and uv. If a tool is not found afterwards, open a new terminal so the updated PATH is loaded. Stop the app processes with `powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Stop`; logs are in `.run\`.

## The page is blank or returns 502

Open the app on `APP_PORT` (8000 by default), not on the API's internal port. The agent there forwards page requests to the API, which renders the web UI; a 502 means the API is not up yet or failed to start. Check `.run/api.log`, then restart with `make up` (or `./scripts/start.sh`).

## The API will not start

Check `.run/api.log` first. The usual causes:

- the port is already in use: stop the previous Agent-Hire process (`make down`) or choose a different `APP_PORT` / `API_INTERNAL_PORT`;
- the data directory is not writable: check `AGENT_HIRE_DATA_DIR` and `AGENT_HIRE_JOBS_DIR` in `.env`;
- a job scrape fails to start: the careers scraper needs `uv` on `PATH` (the installer provides it).

## Find jobs returns no useful results

Confirm that a role is saved on Home, the enabled source portals are not all disabled, and the scraper service is running. Use the activity log (Settings → General) to distinguish an empty source from a scraper or network failure. Search again to start a fresh collection.

If Activity says npm could not be found, install Node.js/npm or set `SCRAPER_NPM` to the absolute npm executable before starting Agent-Hire. The runner detects common nvm, Volta, mise, and asdf installations automatically, but a custom service environment may still need the explicit path.

Some sources do not publish a job inside every time window. Try a broader role or location, then remove overly narrow filters in Find jobs.

## Every job is unresolved, or none match my search

- **Unresolved levels and skills.** The decision model is not configured or has not been probed, so no model verdict exists. Configure it (see Job discovery), run `uv run agent-hire decision probe --dry-run` then `--yes`, and search again. Experience still resolves from explicit evidence in the posting; skills need the extraction model for anything beyond the built-in dictionary.
- **Few or no titles survive.** The decision model judges each title against what you typed. A very narrow role phrase, or a model that is down, gives few matches. Try a broader phrase and check the run's title counts in the progress card.
- **No descriptions.** Some boards publish titles only. Level then comes from the title and the model, and skills stay unresolved rather than guessed.

## Imported discovery config warns about search queries or rejects keys

`search_queries` entries are preserved through import/export but never executed — the warning is expected, not data loss. Keys that would run code (`parser`, `command`, `executable`, `plugin`) or carry credentials (`api_key`, `token`, `secret`, `password`, `cookie`, `authorization`) are rejected at load; remove them or set provider credentials in the environment instead. A `config hash mismatch` on save means the config changed since you loaded it — reload and reapply.

## Résumé parsing is slow or appears stuck

Open **Profile → Add résumé or experience** and check the ingestion progress card. Parsing continues when you change screens. Use the activity log (**Settings → General**) for stage details and errors. Check the AI provider with **Test AI connection**, confirm the model name, and verify that the uploaded file is readable and within your provider's context limits.

If parsing fails after a provider timeout, retry after checking the provider's status and rate limits. Large or image-only PDFs may need a provider with document/image support; a text-extraction fallback is used when possible.

## OpenCode Go says `x-opencode-session` is missing

Open Settings and select the separate **OpenCode** provider. Use the default OpenCode Go endpoint and **Responses** format, then save the settings and restart Agent-Hire. Older installations may need their configuration migration to run once. Do not use the generic OpenAI-compatible panel for OpenCode Go.

Rotate any key that was exposed in a terminal, screenshot, issue, or chat history.

## Duplicate review is empty or cannot be completed

Refresh the page after the ingestion task reaches review state. The dialog reads staged pairs from the active task and requires a decision for each pair. If the task completed without reviewable pairs, the message is informational. If it says review is required but shows no pairs, inspect Activity for the ingestion task error and retry the upload.

## The wrong points appear for a tag

Open **Add experience**, check the document's upload tag and then inspect the profile-point tag controls. A tag on a document is the starting provenance; individual points can have their own tags. In **Any**, shared points appear in multiple columns but remain one export item. General should contain only untagged material.

## The extension does not fill a page

Build and reload the extension:

```bash
make ext-build
```

On the browser's extension page, reload the unpacked extension and refresh the career-site tab. Make sure the site is supported and that the extension has permission for that site. Fill page by page; some fields are hidden until earlier answers are entered.

The extension never submits. If a field is wrong, correct it on the page before submitting yourself.

## A provider returns 401, 403, 404, or 429

- `401`/`403`: the key is invalid, expired, scoped incorrectly, or not accepted by that endpoint.
- `404`: the base URL, request format, or model identifier is wrong.
- `429`: the provider rate limit or account quota was reached; wait or use a permitted model.
- `5xx`: the provider is unavailable; retry later and check its status page.

Use **Check API** after correcting settings. Do not include the full key or private provider response in bug reports.
