"""Figma's official careers journey: company page -> exact board API -> exact jobs.

The official ``https://www.figma.com/careers/`` page server-renders a stale
snapshot of its Job Openings list and then, in the real rendered UI, requests
the company-scoped Greenhouse board feed:

    https://boards-api.greenhouse.io/v1/boards/figma/jobs?content=true

That exact request was observed from the official page in headless Chromium,
returns ``meta.total`` plus every posting with its full HTML ``content``, and
its 145 IDs match the 145 rendered ``boards.greenhouse.io/figma/jobs/<id>``
anchors. The server-rendered anchors are therefore retained only as evidence:
a 2026-11-07 snapshot still contained an unpublished role that the rendered
feed had already removed. Membership authority is the page's own board feed,
not the stale SSR list.

Each posting's published URL is its ``absolute_url``
(``https://boards.greenhouse.io/figma/jobs/<id>?gh_jid=<id>``). The feed is
company-scoped to ``figma`` and has no search/filter/pagination parameters; all
145 rows return in one response. The embedded application form is never opened
or submitted.
"""
import hashlib
import html
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

ENTRY = "https://www.figma.com/careers/"
BOARD_API = "https://boards-api.greenhouse.io/v1/boards/figma/jobs?content=true"
GREENHOUSE_JOB = re.compile(
    r"^https://boards\.greenhouse\.io/figma/jobs/(\d+)\?gh_jid=(\d+)$"
)
JOBBOARDS_JOB = re.compile(
    r"^https://job-boards\.greenhouse\.io/figma/jobs/(\d+)\?gh_jid=(\d+)$"
)
ATTACHMENT = re.compile(r"\.(?:pdf|docx?|rtf|zip)(?:[?#]|$)", re.I)
SALARY_RANGE = re.compile(
    r"Annual Base Salary Range:\s*[\$€£]?\s*([\d,]+)\s*[-\u2013\u2014]\s*[\$€£]?\s*([\d,]+)\s*([A-Za-z]{3})?",
    re.I,
)
SALARY_LINE = re.compile(r"Annual Base Salary Range:[^\n]*", re.I)
PAY_START = "Pay Transparency Disclosure"
PAY_END_MARKERS = (
    "At Figma we celebrate",
    "Figma is proud to be an equal opportunity",
    "EEOC",
)
METHOD = "figma.official_careers_greenhouse_board_api.content"


def compact_body_hash(value):
    """NFC/whitespace-free body hash, for source-body identity checks."""
    text = value if isinstance(value, str) else text_html(value)
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[\s\ufeff]", "", text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def listing_entries(html_text):
    """Parse the official page's server-rendered anchors for evidence only.

    The rendered UI replaces this stale SSR list with the board API feed, so
    these entries must never be used as membership authority.
    """
    soup = BeautifulSoup(html_text, "lxml")
    entries, department, seen = [], None, set()
    for element in soup.find_all(["h2", "h3", "a"]):
        if element.name in ("h2", "h3"):
            department = element.get_text(" ", strip=True) or department
            continue
        href = (element.get("href") or "").strip()
        match = GREENHOUSE_JOB.match(href)
        if not match:
            continue
        if match.group(1) != match.group(2):
            raise ValueError(f"Figma careers anchor has mismatched gh_jid: {href}")
        identity = match.group(1)
        if identity in seen:
            raise ValueError(f"Figma careers listing repeats an exact job ID: {identity}")
        seen.add(identity)
        entries.append((identity, element.get_text(" ", strip=True), department, href))
    return entries


def decode_content(value):
    """Decode the API's HTML-escaped ``content`` field to its body inner HTML."""
    decoded = html.unescape(value or "")
    soup = BeautifulSoup(decoded, "lxml")
    if soup.body is not None:
        return soup.body.decode_contents()
    return decoded


def parse_salary(description_text):
    """Build the sourced salary object from the full post's pay disclosure."""
    text = description_text or ""
    start = text.find(PAY_START)
    if start < 0:
        return None
    end = len(text)
    for marker in PAY_END_MARKERS:
        position = text.find(marker, start)
        if position > start:
            end = min(end, position)
    source_text = text[start:end].strip()
    range_match = SALARY_RANGE.search(source_text)
    line_match = SALARY_LINE.search(source_text)
    if range_match:
        range_line = " ".join(range_match.group(0).split())
    elif line_match:
        range_line = line_match.group(0).strip()
    else:
        range_line = None
    return {
        "source_text": source_text,
        "source_field": "content.Pay Transparency Disclosure",
        "range_line": range_line,
        "range_raw": range_match.group(0) if range_match else None,
        "minimum": range_match.group(1) if range_match else None,
        "maximum": range_match.group(2) if range_match else None,
        "currency": range_match.group(3) if range_match else None,
        "semantics": (
            "Pay Transparency Disclosure and base-salary line exactly as published in "
            "the page-requested Greenhouse board feed; no conversion or inference."
        ),
    }


def attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if ATTACHMENT.search(href):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def parse_detail(html_text):
    """Extract a hosted detail page's body (used for independent cross-checks)."""
    soup = BeautifulSoup(html_text, "lxml")
    heading = soup.select_one("h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    location_el = soup.select_one(".job__location")
    location = location_el.get_text(" ", strip=True) if location_el else ""
    description_el = soup.select_one(".job__description.body")
    description_html = description_el.decode_contents() if description_el else ""
    pay_el = soup.select_one(".job__pay-ranges")
    pay_text = text_html(pay_el.decode_contents()) if pay_el else ""
    return title, location, description_html, pay_text


def parse_board_api(payload):
    """Validate the exact board feed and return (jobs, meta_total)."""
    if not isinstance(payload, dict):
        raise ValueError("Figma board API response is not a JSON object")
    jobs = payload.get("jobs")
    meta = payload.get("meta") or {}
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Figma board API returned no jobs")
    total = meta.get("total")
    if total is None:
        total = len(jobs)
    try:
        total = int(total)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Figma board API meta.total is not an integer: {total!r}") from exc
    if total != len(jobs):
        raise ValueError(
            f"Figma board API is incomplete: meta.total={total} but returned {len(jobs)} jobs")
    seen = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("Figma board API job is not an object")
        identity = str(job.get("id", ""))
        if not identity.isdigit():
            raise ValueError(f"Figma board API job has no numeric id: {identity!r}")
        if identity in seen:
            raise ValueError(f"Figma board API repeats a job ID: {identity}")
        seen.add(identity)
        absolute_url = job.get("absolute_url") or ""
        match = GREENHOUSE_JOB.match(absolute_url)
        if not match or match.group(1) != match.group(2) or match.group(1) != identity:
            raise ValueError(f"Figma board API job {identity} has a non-exact URL: {absolute_url}")
        if not str(job.get("title") or "").strip():
            raise ValueError(f"Figma board API job {identity} has no title")
    return jobs, total


class Figma(Adapter):
    """Collect exactly the jobs in Figma's official page-requested board feed."""

    VERIFICATION_REVISION = "figma.official-careers-greenhouse-board-api-content.2"

    @staticmethod
    def _assert_entry_url(url):
        parsed = urlsplit(str(url))
        if (parsed.scheme != "https" or parsed.hostname not in {"www.figma.com", "figma.com"}
                or parsed.path.rstrip("/") != "/careers"):
            raise ValueError(f"Figma dedicated flow must start from its official HTTPS /careers/ entry: {url}")
        return str(url)

    @staticmethod
    def _assert_api_url(url):
        if str(url) != BOARD_API:
            raise ValueError(f"Figma board feed must be the exact page-requested URL: {url}")
        return str(url)

    def _make_job(self, api_job, *, index, total):
        identity = str(api_job["id"])
        title = " ".join(str(api_job["title"]).split())
        absolute_url = api_job["absolute_url"]
        description_html = decode_content(api_job["content"])
        description_text = text_html(description_html)
        location = str((api_job.get("location") or {}).get("name") or "").strip()
        departments = [item for item in (api_job.get("departments") or []) if item.get("name")]
        department = departments[0]["name"] if departments else None
        salary = parse_salary(description_text)
        first_published = api_job.get("first_published")
        updated_at = api_job.get("updated_at")
        deadline = api_job.get("application_deadline")
        requisition_id = api_job.get("requisition_id")
        raw = {
            "api_listing": {
                "request_url": BOARD_API,
                "meta_total": total,
                "job_index": index,
            },
            "greenhouse_api_job": api_job,
            "source_ui": {
                "official_entry_url": ENTRY,
                "absolute_url": absolute_url,
                "detail_route": (
                    f"https://job-boards.greenhouse.io/figma/jobs/{identity}?gh_jid={identity}"
                ),
                "description_source_field": (
                    "content (HTML-escaped full job post in the page-requested board feed; "
                    "decoded into description_html)"
                ),
                "departments": departments,
                "offices": api_job.get("offices") or [],
                "first_published_raw": first_published,
                "updated_at_raw": updated_at,
                "application_deadline_raw": deadline,
                "data_compliance": api_job.get("data_compliance"),
                "ai_disclaimer": api_job.get("ai_disclaimer"),
                "ai_opt_out_request_url": api_job.get("ai_opt_out_request_url"),
                "attachment_links": attachment_links(description_html),
                "salary_from_content": salary,
            },
        }
        if not description_html.strip():
            self.result.errors.append(f"{identity}: Figma board feed content decoded to an empty body")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: Figma board feed content is implausibly short")
        job = self.job(
            raw,
            title=title,
            url=absolute_url,
            job_id=identity,
            requisition_id=str(requisition_id) if requisition_id not in (None, "") else None,
            apply_url=absolute_url,
            description=description_html,
            method=METHOD,
            description_url=BOARD_API,
            locations=[{"location": location}] if location else [],
            department=department,
            employment_type=None,
            workplace_type=None,
            date_posted=str(first_published) if first_published else None,
            date_posted_raw=first_published,
            date_updated=str(updated_at) if updated_at else None,
            valid_through=str(deadline) if deadline else None,
            salary=salary,
            employer=api_job.get("company_name"),
            language=api_job.get("language"),
        )
        job.description_text = description_text
        job.description_unavailable = not bool(description_html.strip())
        job.record_identity = f"figma:{identity}"
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self._assert_entry_url(self.company.careers_url)
        if not self.page_guard():
            return self.result

        entry_started = time.perf_counter()
        initial_entry = await self.http.get(entry_url)
        self._assert_entry_url(str(initial_entry.url))
        entry_seconds = time.perf_counter() - entry_started
        try:
            ssr_entries = listing_entries(initial_entry.text)
            ssr_error = None
        except Exception as exc:
            ssr_entries, ssr_error = [], f"{type(exc).__name__}: {exc}"

        api_started = time.perf_counter()
        self._assert_api_url(BOARD_API)
        initial_payload = await self.http.json(BOARD_API)
        initial_jobs, initial_total = parse_board_api(initial_payload)
        initial_api_seconds = time.perf_counter() - api_started
        initial_ids = [str(job["id"]) for job in initial_jobs]

        parse_started = time.perf_counter()
        jobs = [
            self._make_job(job, index=index, total=initial_total)
            for index, job in enumerate(initial_jobs)
        ]
        parse_seconds = time.perf_counter() - parse_started

        final_snapshot_verified = True
        final_entry_verified = True
        try:
            final_entry = await self.http.get(entry_url)
            self._assert_entry_url(str(final_entry.url))
        except Exception as exc:
            final_entry_verified = False
            self.result.warnings.append(
                f"official_entry_recheck_failed:{type(exc).__name__}")
        try:
            api_final_started = time.perf_counter()
            final_payload = await self.http.json(BOARD_API)
            final_jobs, final_total = parse_board_api(final_payload)
            final_api_seconds = time.perf_counter() - api_final_started
            final_ids = [str(job["id"]) for job in final_jobs]
        except Exception as exc:
            final_snapshot_verified = False
            final_jobs, final_total = initial_jobs, initial_total
            final_ids = initial_ids
            final_api_seconds = None
            self.result.errors.append(
                "Figma final official board feed snapshot failed; "
                f"initial usable entries retained: {type(exc).__name__}: {exc}"
            )

        initial_by_id = {entry[0]: entry for entry in ssr_entries}
        ssr_ids = list(initial_by_id)
        changed_membership = final_snapshot_verified and set(initial_ids) != set(final_ids)
        if changed_membership:
            added = sorted(set(final_ids) - set(initial_ids))
            removed = sorted(set(initial_ids) - set(final_ids))
            self.result.errors.append(
                "figma_official_board_membership_changed_between_initial_and_final_snapshots:"
                f" added={added} removed={removed}"
            )

        for job in jobs:
            self.result.validate_job(job)
            self.result.jobs.append(job)
            self.result.dirty_jobs.append(job)
        self.result.expected_total = final_total
        self.result.pages = 1
        self.result.listing_complete = final_snapshot_verified and not changed_membership

        department_counts = {}
        for job in jobs:
            department_counts[job.department] = department_counts.get(job.department, 0) + 1
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(initial_entry.url),
            "listing_api_url": BOARD_API,
            "membership_authority": (
                "The exact company-scoped board feed requested by the official /careers/ page in "
                "the rendered UI: boards-api.greenhouse.io/v1/boards/figma/jobs?content=true. "
                "meta.total and every returned posting define membership."
            ),
            "request_shape": {
                "method": "GET",
                "url": BOARD_API,
                "query": {"content": "true"},
                "authentication": None,
                "pagination": "none; one response has meta.total and all jobs",
                "scope": "figma company board only",
                "observed_from": "headless Chromium network log of the official /careers/ page",
            },
            "ui_pagination": (
                "None. The rendered #job-openings section shows every posting with no "
                "load-more/next/previous control; the page's board feed returns "
                "meta.total in one response."
            ),
            "official_entry_ssr": {
                "anchor_count": len(ssr_entries),
                "ids": ssr_ids,
                "differs_from_api_ids": ssr_ids != initial_ids,
                "evidence_only": (
                    "Server-rendered anchors are stale relative to the page's own board feed; "
                    "they are recorded but never used for membership."
                ),
                "parse_error": ssr_error,
            },
            "listing_api": {
                "initial_total": initial_total,
                "initial_ids": initial_ids,
                "final_total": final_total,
                "final_ids": final_ids,
                "initial_final_set_matches": set(initial_ids) == set(final_ids),
                "initial_final_order_matches": initial_ids == final_ids,
                "final_snapshot_verified": final_snapshot_verified,
                "final_entry_recheck_verified": final_entry_verified,
            },
            "department_counts": department_counts,
            "field_policy": {
                "job_id": "board feed id",
                "requisition_id": "board feed requisition_id",
                "date_posted": "board feed first_published (explicit publication timestamp)",
                "date_updated": "board feed updated_at",
                "valid_through": "board feed application_deadline",
                "employer": "board feed company_name",
                "language": "board feed language",
                "employment_type": "not exposed by this feed; stays null",
                "workplace_type": "not exposed by this feed; stays null",
                "salary": "Pay Transparency Disclosure text in the full content field",
                "locations": "board feed location.name; full offices retained in raw",
            },
            "description_source": (
                "Each posting's HTML-escaped content field in the exact page-requested board "
                "feed, decoded to its body inner HTML. Full sections, pay disclosure and legal "
                "text are retained; no detail-page application form is fetched."
            ),
            "detail_route_policy": (
                "absolute_url is the exact published boards.greenhouse.io/figma/jobs/<id> URL and "
                "is retained as job/apply URL. No per-job detail page is required for the body, "
                "and no application form is ever opened."
            ),
            "initial_and_final_api_requests": 2,
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
        self.result.timings["api_initial_seconds"] = round(initial_api_seconds, 6)
        self.result.timings["api_final_seconds"] = (
            round(final_api_seconds, 6) if final_api_seconds is not None else 0.0)
        self.result.timings["content_parse_seconds"] = round(parse_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result
