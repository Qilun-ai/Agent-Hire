"""Zynga's own careers entry and its exact Greenhouse board.

Membership authority (inspected 2026-11-08):

- Official entry ``https://www.zynga.com/jobs/`` 301-redirects (CloudFront) to
  ``https://job-boards.greenhouse.io/zyngacareers`` — the board titled "Jobs at Zynga".
  No other board is queried; a guessed or parent-wide board is not used.
- The board SSR renders every card on one page: visible text "48 jobs" plus 48 anchors
  ``/zyngacareers/jobs/<id>``. Filters (Search, Office, Disciplines) are client-side only;
  there is no pagination.
- The board's public feed ``https://api.greenhouse.io/v1/boards/zyngacareers/jobs?content=true``
  returns the same 48 IDs with full HTML-escaped ``content`` per row. SSR link IDs, API IDs and
  the advertised count must reconcile exactly, both at first load and on a final recheck.
- Each exact job page ``https://job-boards.greenhouse.io/zyngacareers/jobs/<id>`` renders the
  same full description and hosts its application form (``action="/zyngacareers/jobs/<id>"``),
  so ``apply_url`` is the job URL itself.
"""
from __future__ import annotations

from hashlib import sha256
import html as _html
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.zynga.com/jobs/"
LISTING_URL = "https://job-boards.greenhouse.io/zyngacareers"
BOARD = "zyngacareers"
API_URL = f"https://api.greenhouse.io/v1/boards/{BOARD}/jobs?content=true"
JOB_PATH = re.compile(r"^/zyngacareers/jobs/(\d+)$")
ADVERTISED_COUNT = re.compile(r"(\d+)\s+jobs\b")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
RESPONSIBILITY_TOKENS = ("responsibilit", "what you will do", "what you'll do", "the role", "position overview")
QUALIFICATION_TOKENS = ("qualif", "requirement", "what we're looking for", "what we’re looking for",
                        "what you will need", "what you'll need", "who you are", "required skills",
                        "what we are looking for")


def normalized_body_text(html: str | None) -> str:
    """One-line NFC text of HTML, for hash comparison with rendered pages."""
    value = text_html(html or "")
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def decode_content(row: dict) -> str:
    raw = row.get("content")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"Zynga job {row.get('id')} has no Greenhouse content field")
    # Greenhouse serves description HTML escaped ("&lt;div..."); decode once, as the board does.
    return _html.unescape(raw)


def metadata_map(row: dict) -> dict:
    values = {}
    for meta in row.get("metadata") or []:
        if isinstance(meta, dict) and meta.get("name"):
            values[str(meta["name"])] = meta.get("value")
    return values


def parse_listing_html(html: str, response_url: str = LISTING_URL) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "job-boards.greenhouse.io" or parsed.path.rstrip("/") != f"/{BOARD}":
        raise ValueError("Zynga listing response came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if title != "Jobs at Zynga":
        raise ValueError(f"Zynga listing page title changed: {title!r}")
    text = soup.get_text(" ", strip=True)
    match = ADVERTISED_COUNT.search(text)
    advertised = int(match.group(1)) if match else None
    if advertised is None:
        raise ValueError("Zynga listing page no longer advertises a job count")

    rows, seen, issues = [], set(), []
    for anchor in soup.select('a[href*="/zyngacareers/jobs/"]'):
        href = anchor.get("href", "")
        parts = urlsplit(href)
        m = JOB_PATH.fullmatch(parts.path)
        if m is None or parts.netloc != "job-boards.greenhouse.io":
            continue
        identity = m.group(1)
        if identity in seen:
            issues.append(f"duplicate_listing_link:{identity}")
            continue
        seen.add(identity)
        rows.append({"id": identity, "url": href})
    if advertised != len(rows):
        issues.append(f"advertised_count_mismatch:advertised={advertised},links={len(rows)}")
    if not rows:
        issues.append("zynga_listing_has_no_exposed_job_rows")
    labels = [node.get_text(" ", strip=True) for node in soup.select("select, button, input[placeholder]")]
    return {
        "rows": rows, "job_ids": [r["id"] for r in rows], "count": len(rows),
        "advertised_jobs": advertised, "page_title": title, "issues": issues,
        "filter_controls": labels,
    }


def parse_api_payload(payload: dict, origin: str = "initial") -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError(f"Zynga {origin} board API response has no jobs array")
    rows, seen, issues = [], set(), []
    for row in payload["jobs"]:
        if not isinstance(row, dict):
            issues.append("zynga_api_row_not_object")
            continue
        identity = row.get("id")
        if not isinstance(identity, int) or not row.get("title"):
            issues.append("zynga_api_row_missing_id_or_title")
            continue
        sid = str(identity)
        if sid in seen:
            issues.append(f"zynga_api_duplicate_id:{sid}")
            continue
        expected = f"https://job-boards.greenhouse.io/{BOARD}/jobs/{sid}"
        if row.get("absolute_url") != expected:
            issues.append(f"zynga_api_unexpected_absolute_url:{sid}")
            continue
        try:
            decoded = decode_content(row)
        except ValueError as exc:
            issues.append(str(exc))
            continue
        if len(normalized_body_text(decoded)) < 100:
            issues.append(f"zynga_api_description_empty_or_truncated:{sid}")
        seen.add(sid)
        rows.append(row)
    return {"rows": rows, "job_ids": [str(r["id"]) for r in rows],
            "count": len(rows), "issues": issues}


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-zynga-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-zynga-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = block.get_text(" ", strip=True)
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = re.sub(r"\s+", " ", block.get_text(" ", strip=True)).strip()
            strong_text = re.sub(r"\s+", " ", strong.get_text(" ", strip=True)).strip() if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 140:
                heading = full_text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    return sections


def _attachment_links(decoded_html: str) -> list[dict]:
    soup = BeautifulSoup(decoded_html or "", "lxml")
    return [
        {"text": a.get_text(" ", strip=True), "url": a.get("href")}
        for a in soup.select("a[href]") if ATTACHMENT_SUFFIX.search(a.get("href") or "")
    ]


def detail_page_reference(html: str, expected_row: dict, expected_url: str) -> dict:
    """Check an exact job page against the API ``content`` for the same ID."""
    parts = urlsplit(expected_url)
    m = JOB_PATH.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.netloc != "job-boards.greenhouse.io"
            or m is None or m.group(1) != str(expected_row.get("id"))):
        raise ValueError("Zynga representative detail URL is not an exact exposed job link")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    expected_title = f"Job Application for {expected_row.get('title', '')} at Zynga"
    if title != expected_title:
        raise ValueError(f"Zynga exact detail title changed: {title!r} != {expected_title!r}")
    decoded = decode_content(expected_row)
    expected_text = normalized_body_text(decoded)
    rendered = normalized_body_text(soup.get_text(" ", strip=True))
    if expected_text not in rendered:
        raise ValueError("Zynga exact detail page does not render the full API description")
    form = soup.select_one("form[action]")
    action = form.get("action") if form else None
    return {
        "url": expected_url,
        "title": title,
        "description_normalized_chars": len(expected_text),
        "description_fnv1a64": fnv1a64(expected_text),
        "api_text_rendered_on_detail_page": True,
        "application_form_action": action,
    }


class Zynga(Adapter):
    """Follow only Zynga's official redirect target board (zyngacareers)."""

    VERIFICATION_REVISION = "zynga.official-redirect-greenhouse-board.1"

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        final = str(response.url).rstrip("/")
        if final != LISTING_URL:
            raise ValueError(f"Zynga official entry redirected to an unexpected URL: {response.url}")
        parsed = parse_listing_html(response.text, LISTING_URL)
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content),
                      request_url=OFFICIAL_ENTRY, final_url=str(response.url),
                      redirects=[str(r.url) for r in response.history])
        return parsed

    async def _api_snapshot(self, label: str):
        started = time.perf_counter()
        response = await self.http.get(API_URL)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != API_URL:
            raise ValueError(f"Zynga {label} board API redirected to {response.url}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Zynga {label} board API response is not JSON") from exc
        parsed = parse_api_payload(payload, label)
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content),
                      request_url=API_URL, final_url=str(response.url))
        return parsed

    def _make_job(self, row, *, initial_row=None, membership_changed=False):
        identity = str(row["id"])
        decoded = decode_content(row)
        description_text = text_html(decoded)
        meta = metadata_map(row)
        disciplines = meta.get("Disciplines") or []
        department = ", ".join(disciplines) if isinstance(disciplines, list) else disciplines or None
        company_meta = meta.get("Company")
        sections = description_sections(decoded)
        resp = [s for s in sections if s["heading"] and any(t in s["heading"].lower() for t in RESPONSIBILITY_TOKENS)]
        qual = [s for s in sections if s["heading"] and any(t in s["heading"].lower() for t in QUALIFICATION_TOKENS)]
        locations = []
        loc_name = (row.get("location") or {}).get("name")
        if loc_name:
            locations.append(loc_name)
        job = self.job(
            {"listing": row, "metadata": meta,
             **({"initial_listing": initial_row} if initial_row is not None and initial_row != row else {})},
            title=row["title"],
            url=row["absolute_url"],
            job_id=identity,
            requisition_id=row.get("requisition_id") or None,
            apply_url=row["absolute_url"],
            description=decoded,
            method="zynga.official_redirect.greenhouse_board_content",
            description_url=API_URL,
            locations=locations,
            department=department or None,
            employment_type=meta.get("Time Type") or None,
            workplace_type=(meta.get("Connected Workplace Options") or [None])[0]
            if isinstance(meta.get("Connected Workplace Options"), list) else meta.get("Connected Workplace Options"),
            date_posted=row.get("first_published"),
            date_posted_raw=row.get("first_published"),
            date_updated=row.get("updated_at"),
            valid_through=row.get("application_deadline"),
            salary=None,
            employer=(row.get("company_name") or "").strip() or None,
            language=row.get("language"),
            responsibilities=resp or None,
            qualifications=qual or None,
        )
        job.description_text = description_text
        normalized = normalized_body_text(decoded)
        job.raw["detail"] = {
            "source_field": "content (html-unescaped once)",
            "description_sha256": sha256(decoded.encode()).hexdigest(),
            "description_normalized_chars": len(normalized),
            "description_fnv1a64": fnv1a64(normalized),
            "description_sections": sections,
            "role_attachments": _attachment_links(decoded),
            "legal_employer_metadata_company": company_meta,
            "worker_type": meta.get("Worker Type"),
            "early_careers_job": meta.get("Early Careers Job?"),
            "application_route": {"job_page_url": row["absolute_url"],
                                  "apply_form_action": f"/{BOARD}/jobs/{identity}"},
        }
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_authority": "The exact Greenhouse board (zyngacareers) that Zynga's own /jobs/ entry 301-redirects to.",
            "listing_membership_request": {
                "method": "GET", "path": "/v1/boards/zyngacareers/jobs", "query": {"content": "true"},
                "filters_applied": False, "scope": "zyngacareers board only; no other board is queried.",
            },
            "detail_method": "Full decoded content is returned inline by the same board feed; each exact job page renders it and hosts the application form.",
            "request_scope": "Only the zyngacareers board published by Zynga's official entry and its exact exposed job IDs.",
        })
        try:
            initial_listing = await self._official_entry()
            initial_api = await self._api_snapshot("initial")
        except Exception as exc:
            self.result.errors.append(f"Zynga source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.expected_total = initial_listing["advertised_jobs"]
        self.result.metadata["initial_listing"] = {k: v for k, v in initial_listing.items() if k != "rows"}
        self.result.metadata["initial_listing_job_ids"] = initial_listing["job_ids"]
        initial_by_id = {str(r["id"]): r for r in initial_api["rows"]}
        self.result.metadata["initial_api_listing"] = {k: v for k, v in initial_api.items() if k != "rows"}
        self.result.errors.extend(initial_listing["issues"])
        self.result.errors.extend(initial_api["issues"])
        # SSR order is the board UI's display sort; the API feed uses its own order.
        # Membership reconciles as a set; jobs are exported in visible SSR order.
        if set(initial_listing["job_ids"]) != set(initial_api["job_ids"]):
            self.result.errors.append("zynga_ssr_and_api_membership_mismatch")
        self.result.metadata["ssr_order_equals_api_feed_order"] = (
            initial_listing["job_ids"] == initial_api["job_ids"])
        if initial_listing["advertised_jobs"] != initial_api["count"]:
            self.result.errors.append("zynga_advertised_count_and_api_count_mismatch")

        # Final recheck: membership must be unchanged before certifying completeness.
        final_listing = final_api = None
        final_by_id: dict = {}
        final_matches = False
        try:
            final_listing = await self._official_entry()
            final_api = await self._api_snapshot("final")
        except Exception as exc:
            self.result.errors.append(f"Zynga final source recheck failed: {type(exc).__name__}: {exc}")
        if final_listing is not None and final_api is not None:
            final_by_id = {str(r["id"]): r for r in final_api["rows"]}
            changed = sorted(i for i in initial_by_id if i in final_by_id and initial_by_id[i] != final_by_id[i])
            same_ids = initial_api["job_ids"] == final_api["job_ids"]
            same_ssr = initial_listing["job_ids"] == final_listing["job_ids"]
            final_matches = bool(same_ids and same_ssr and not changed
                                 and not final_api["issues"] and not final_listing["issues"]
                                 and final_listing["advertised_jobs"] == final_api["count"])
            self.result.metadata["final_listing"] = {k: v for k, v in final_listing.items() if k != "rows"}
            self.result.metadata["final_listing_job_ids"] = final_listing["job_ids"]
            self.result.metadata["final_api_listing"] = {k: v for k, v in final_api.items() if k != "rows"}
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial_api["job_ids"], "final_ids": final_api["job_ids"],
                "initial_ssr_ids": initial_listing["job_ids"], "final_ssr_ids": final_listing["job_ids"],
                "initial_advertised": initial_listing["advertised_jobs"],
                "final_advertised": final_listing["advertised_jobs"],
                "same_ids_and_order": same_ids, "same_ssr_ids_and_order": same_ssr,
                "all_row_fields_match": not changed, "changed_fields_by_job_id": changed,
                "new_job_ids": [i for i in final_api["job_ids"] if i not in initial_by_id],
                "removed_job_ids": [i for i in initial_by_id if i not in final_by_id],
                "complete": final_matches,
            }
            if not same_ids or not same_ssr:
                self.result.errors.append("zynga_listing_membership_changed_during_collection")
            if changed:
                self.result.errors.append("zynga_source_fields_changed_during_collection")
            self.result.errors.extend(final_api["issues"])
            self.result.errors.extend(final_listing["issues"])

        # Retain the union in SSR order; rows present only in the initial snapshot are kept and flagged.
        order = list(dict.fromkeys(initial_listing["job_ids"] + (final_listing["job_ids"] if final_listing else [])))
        jobs, parse_seconds = [], 0.0
        for identity in order:
            row = final_by_id.get(identity) if final_api else None
            row = row if row is not None else initial_by_id.get(identity)
            if row is None:
                self.result.errors.append(f"zynga_listed_job_without_api_row:{identity}")
                continue
            started = time.perf_counter()
            job = self._make_job(row, initial_row=initial_by_id.get(identity),
                                 membership_changed=bool(final_api) and identity not in final_by_id)
            parse_seconds += max(0.0, time.perf_counter() - started)
            if job.raw["detail"]["role_attachments"]:
                self.result.errors.append(f"zynga_role_attachments_require_exact_review:{identity}")
            jobs.append(job)
        self.result.jobs = jobs
        for job in jobs:
            self.result.validate_job(job)
        self.result.timings["inline_detail_parse_seconds_sum"] = parse_seconds
        self.result.timings["detail_phase_seconds"] = parse_seconds
        self.result.metadata["detail_hydration_mode"] = "Full decoded content is inline in the exact company-scoped board feed; no detail fetch per row."
        self.result.metadata["job_ids"] = [str(j.job_id) for j in jobs]
        self.result.metadata["current_native_membership_reconciled"] = final_matches
        self.result.pages = 2 if final_matches else 1
        self.result.listing_complete = bool(final_matches and len(jobs) == initial_api["count"]
                                            and not self.result.errors)
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
