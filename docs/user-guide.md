# User guide

## Home and Find jobs

Home stores the role and location preferences used by discovery. Enter a natural-language role such as `backend engineer` and optionally set a country and city. Choose **Find jobs** to start a fresh collection from the enabled sources.

On a new installation, every currently selectable portal starts enabled. Use **Sources** to turn individual portals off; retired or unsupported catalog entries are not selectable. A dashboard Find action returns only jobs observed by that collection. If the scraper cannot start or finish, Agent-Hire shows the failure instead of substituting older stored jobs.

Open **Find jobs** to review the result tabs:

- **All jobs** shows the current saved set.
- **Best matches** prioritizes stronger profile matches.
- **New** shows newly discovered roles.
- **Reviewed** shows roles that have been scored.
- **Documents ready** shows roles with generated material.
- **Applications** shows roles you have marked as applied or moved into a later outcome.
- **Hidden** keeps jobs you discarded or marked as not relevant.

Use the search, source, location, seniority, and sort controls to narrow the current results. A new Find action refreshes discovery; it deduplicates new results and protects application history.

When the standalone scraper backend is enabled, choose role families/functions,
the four experience categories (**No prior experience**, **Entry level**, **Mid
level**, **Senior level**), and optional required skills. **Include unresolved**
starts on so missing evidence does not silently remove jobs. Role chips,
experience reasons, Required/Preferred/Mentioned lists and evidence explain each
posting; an `or` group represents alternatives. Refresh enrichment to update
pending classifications. Resume a partial import from its saved checkpoint;
a scraper reset requires a new search and preserves your imported leads.

Deleting a lead affects your pipeline and suppresses future re-imports through
local tombstones. Application reset clears your personal workspace and leaves
scraped jobs and other users' workspaces intact. Scraper reset is an operator
action described in the runbook.

### How a search works, and how Find jobs filters it

A search collects fresh postings from your enabled job boards, keeps only titles that match what you asked for, and reads each job's **experience level**, **skills** and **requirements** once. Nothing about a search is narrowed by level or skill — that happens in **Find jobs**, where the *Experience level* menu (No prior experience, Entry level, Mid-level, Senior level) and the skill filter narrow the jobs you have already saved instantly, without searching again.

A job whose posting does not make its level or skills clear is shown as *unresolved* and kept by default; switch off *Include unresolved and pending jobs* to see only certain matches. Skills and requirements list what the posting requires (languages, frameworks, tools, years of experience, education, certifications) — preferred and nice-to-have items are left out.

Each run keeps a durable identity with its inputs frozen. Stopping a run cancels it and delivers nothing. Run evidence reports the sources and what each basic filter removed: failed or partial sources are shown as such, never as empty successes. See Job discovery for the full contract and [Troubleshooting](troubleshooting.md) if a search returns nothing.

## Build your profile

Open **Add experience** to upload a PDF, DOCX, text, or Markdown résumé. You can also add skills, roles, projects, education, certifications, achievements, or raw notes manually.

Résumé parsing runs in the background. The progress card remains visible when you move to another screen. When the parser finds similar points, open the review dialog and choose the wording you want to keep. Nothing is removed until you confirm a decision.

### Tags and shared experience

Assign an upload to a profile tag with **Assign uploaded file to**. The document-library filter is separate: using **Any tag** to browse files does not make the next upload untagged.

You can tag individual skills, projects, and experience points. In a job's document builder:

- A selected tag shows only points belonging to that track.
- **Any** shows track columns side by side.
- A point shared by multiple tracks can be visible in more than one column but is exported once.
- **General** is reserved for genuinely untagged source material.
- A shared role or project can show multiple source titles. Choose one title in its heading; the choice is saved for that job and tag.

## Tailor a document

Add a job to the **Cart** (or use **Add a job** for one you found yourself), then open **Cart** and pick the job. **Tailor résumé with AI** builds a PDF for that job from your profile.

To choose the content yourself, open **Pick the points yourself**: select the exact skills, experience points, project points and title variants to include, or use **Let AI pre-select points** and review its picks and wording suggestions. **Make PDF from these picks** builds the résumé from exactly that selection. Cover letters are generated separately and can use your uploaded cover letter as a rewrite base.

Generated content is a draft. Check dates, companies, metrics, links, and formatting before sending it.

## Apply with the extension

From a prepared application, open the target job site and use the extension's fill action. It maps the prepared profile to common form fields and can assist with file uploads. Move through multi-page forms yourself and press Submit yourself.

## Track outcomes

- **Applications** keeps status and notes for submitted roles.
- **Settings → Email updates** can monitor a configured mailbox and help update application outcomes.
- **Settings → General → Open activity log** explains searches, parsing stages, document generation, and failures.
- **Analytics** summarizes your funnel and outcomes.

The top bar has six pages (Home, Find jobs, Cart, Applications, Analytics, Profile) plus **Settings**, which has tabs for the AI model, email updates, job sites and general options.

## Export and backups

Use **Export profile** from the profile screen when you want a portable copy of your profile data. Keep backups of your local application-data directory if you need to preserve settings, documents, and generated files.
