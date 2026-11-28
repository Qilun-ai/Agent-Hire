# Extension end-to-end check

Loads the built extension (`dist/`) into Chromium, opens a test page, starts a fill exactly as the side panel's **Fill this page** button does, and records every
status line plus the final value of each field (and a full-page screenshot next to the JSON).

```bash
npm run build                                   # in apps/extension
python3 -m http.server 8765 --bind 127.0.0.1 -d e2e &
PLAYWRIGHT_BROWSERS_PATH=/path/to/ms-playwright PW_FROM=/path/to/a/project/with/playwright/package.json \
  node e2e/run.mjs "$PWD/dist" http://127.0.0.1:8765/apply.html <job_id> /tmp/out.json decision
```

- Needs Agent-Hire running on :8000 and Playwright's own Chromium (`npx playwright install chromium`): branded
  Google Chrome no longer accepts `--load-extension`.
- `decision` uses the decision model if one is reachable (else the main model); `agent` runs the AI page agent.
- `test-applicant.json` is a fictional applicant (`PUT /api/v1/fill/facts`) for checking every field; save your own
  answers again afterwards (or delete the `application_facts` / `application_defaults` settings if you never saved any).
- What the page checks: label styles (for=, wrapping, aria-labelledby, placeholder-only, legend), selects and radio
  groups, a framework-controlled input that re-renders from its own state on blur, a field the applicant already
  typed (must be kept), a captcha question and a consent box (both must be left alone), file inputs, and no submit.

Measured 2026-11-19 (Nimble Q8 decision model on a T4): fictional applicant 26/29 filled, all correct; the 3 left were the
captcha, the consent box and the pre-typed field. ~60 s per form, most of it the decision model (~1,000-token prompt
per field, two passes per request on the server).

Pages:
- `apply.html`: a Greenhouse-style form (labels of every style, selects, radio groups, controlled input, captcha, consent).
- `widgets.html`: custom widgets (React-Select-style combobox, Ashby-style Yes/No buttons, checkbox group, split phone
  with a country-code dropdown, date/number inputs, a hidden file input behind a drop zone).
- `embed.html`: a careers page embedding `apply.html` from another origin in an iframe (serve the folder on :8765 and
  :8766); the extension must pick the iframe. `RESUME_URL=/api/v1/documents/<id>/file` tests the résumé attach.

2026-11-19, test applicant: apply 26/29 · widgets 9/9 · embed 25/29 + résumé attached (left: captcha, consent, a
pre-typed field). The AI agent mode (`agent`) filled 7/33 in ~2-3 min: it is the slow fallback for odd forms.
