# Decision model settings

The Settings panel is opt-in: set `DECISION_CONFIG_UI_ENABLED=true` on both the
app API and scraper service. Its default is false. The standalone scraper API
also needs its existing `SCRAPER_API_ENABLED` flag and configured authentication.
The saved configuration lives in the scraper's SQLite file (`decision_config`); no
migration step exists. The decision model decides title relevance, occupational roles and experience
level for every job a search collects — see Job discovery.

The app proxies `/api/v1/decision-config` to the scraper's admin config routes.
It uses `SCRAPER_API_ADMIN_KEY` when provided, otherwise the existing server-side
`SCRAPER_API_KEY`. That caller must be in `SCRAPER_API_ADMIN_CLIENTS`. The browser
uses the existing app authentication; it never receives the scraper admin key.
Anyone with authenticated app access can configure the shared scraper when the
flag is enabled. This is the policy explicitly authorized by the settings task.

Only one provider configuration exists: Disabled or SystemOne-compatible.
Custom deployments must implement `model/state/questions → model/answers/usage`.
Paste the full evaluation URL; it is used verbatim without appending `/v1`.
Accepted served models are response-validation names or trailing-star prefixes,
not a fallback list. OpenAI chat classifiers are not supported.

Configuration is a single row in the scraper's SQLite file, layered over env bootstrap.
Credentials are immutable files under `SCRAPER_DATA_DIR/decision-secrets`, with
0700 directory and 0600 file permissions. SQLite stores the path only. File
permissions are checked before writing any secret. A missing/unsafe namespace
refuses the save. **The checkout's filesystem ignores chmod and reports 0755;
it cannot be used as this credential namespace.** The guide's operator-owned file
alternative is available through the existing `DECISION_API_KEY_FILE`: point it
at a service-owned 0600 bootstrap file on a POSIX filesystem. UI-managed keys
then live in a private `decision-secrets` directory beside that file, while
scraper caches and objects keep their existing data directory. The bootstrap
file is preserved; key keep/replace/clear remains controlled by the single DB row.
No encryption dependency or plaintext DB credential is added. Symlinks, foreign
ownership and non-private permissions are refused. In the isolated local profile,
an empty bootstrap file under the user's private Linux data directory supplies
this secure namespace; the user must enter/save the actual key themselves.

The API key is write-only: omitted means keep, null or an empty string means
clear, and a string means replace. GET exposes `api_key_set` only. The app never
stores this key in its SQLite settings. It also rejects `decision_*` fields at
the ordinary `/settings` save endpoint. Stored endpoint paths/queries are never
retrieved: the URL input stays blank when editing an existing configuration,
and a blank input preserves it. The panel clears newly entered key/URL values
after a successful save, and clears the key after any save attempt.

Save never calls the provider. Test connection requires an explicit quota
confirmation and uses the existing synthetic probe. The panel displays capability
results, served model, timestamp, circuit state, and plain-language incompatible
reasons. The existing caller limiter still applies (5 requests/s, burst 60).

API processes and workers load saved revisions; the dispatcher loads the selected model configuration at the start of each run.
An in-flight run may finish on its previous configuration.
The circuit is in-process, as specified, and resets with a new revision.
With the default `DECISION_REQUIRE_PROBE=true`, saves require probe evidence
newer than the saved revision, including key/limit changes which preserve the
fingerprint. Probe enforcement remains operator-only; config PUT refuses
`limits.require_probe`. The existing operator override `DECISION_REQUIRE_PROBE=false` is
preserved and identified in the panel. The CLI probe also uses the saved config
when this feature is enabled.

Disabling the flag restores env bootstrap without deleting the saved row.
No saved row is read when the flag is off. Re-enabling resumes the saved row.
Retired immutable credential files are deliberately retained so concurrent
readers cannot lose their file during a save; operator cleanup of retired files
is not automated. Failed DB commits can similarly leave private orphan files.

Offline coverage uses generated ephemeral keys, MockTransport/fake providers,
isolated temporary SQLite, and the Rust test suites. Real endpoint/credential compatibility,
provider usage and deployed multi-process reload remain unverified. The separate
read-only migration review records inherited adapter issues; they were not
silently changed by this feature.
