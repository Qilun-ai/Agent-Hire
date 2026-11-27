"""Zerodha's first-party careers SPA and its public ``GET /api/jobs`` listing.

Observed on 2026-11-08 (see ``companies/zerodha/journey-research.md``):

- ``https://zerodha.com/careers`` answers HTTP 301 with
  ``Location: https://careers.zerodha.com``.
- ``https://careers.zerodha.com/`` is a Vite single-page app (974-byte shell,
  ``<div id="app">``, ``/assets/index-*.js``).  The app's only listing request
  is ``GET https://careers.zerodha.com/api/jobs`` with no query/filter/paging
  parameters, and it renders ``data`` as the complete job list.
- That public response is ``{"count": 0, "data": [], "success": true}`` on the
  observation date, and the rendered page says "There are no job openings
  currently."  Zero is the site's authoritative live membership, not an
  inaccessible listing.
- When rows exist, the UI bundle displays ``job_title``, ``location``,
  ``location_type`` and injects ``description`` as the expanded full body;
  ``name`` is the posting identifier passed to the application modal as
  ``jobId``.  The modal posts to ``/api/applications``; there is no separate
  server-rendered per-job page.

The adapter follows only the official entry and the SPA's own jobs request.  It
does not guess an ATS slug, enumerate a parent/group board, or fabricate dates,
requisitions, salaries or detail URLs.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import Job


OFFICIAL_ENTRY = "https://careers.zerodha.com/"
JOBS_API = "https://careers.zerodha.com/api/jobs"
APPLICATIONS_API = "https://careers.zerodha.com/api/applications"
EXPECTED_TITLE = "Careers - Zerodha"
EXPECTED_EMPLOYER = "Zerodha Broking Ltd."
ZERO_VACANCY_TEXT = "There are no job openings currently."
APP_SHELL_MARKER = re.compile(r'<div\s+id="app"\s*>\s*</div>', re.I)
ASSET_SCRIPT = re.compile(r"/assets/index-[A-Za-z0-9_-]+\.js")
JS_JOB_FIELDS = ("name", "job_title", "location", "location_type", "description")


def normalized_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return normalized_space(soup.get_text(" "))


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "careers.zerodha.com"
            or parsed.path not in ("", "/")):
        raise ValueError("Zerodha official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ")) if soup.title else ""
    if title != EXPECTED_TITLE:
        raise ValueError("Zerodha official careers page title changed")
    if soup.select_one('meta[name="viewport"]') is None:
        raise ValueError("Zerodha official careers page is no longer the inspected responsive SPA")
    if not APP_SHELL_MARKER.search(html) or soup.select_one("div#app") is None:
        raise ValueError("Zerodha official careers page no longer serves the inspected SPA shell")
    scripts = [
        str(script.get("src")) for script in soup.select("script[src]")
        if ASSET_SCRIPT.fullmatch(str(script.get("src") or ""))
    ]
    if not scripts:
        raise ValueError("Zerodha official careers page has no index JavaScript asset")
    favicon = [
        link.get("href") for link in soup.select('link[rel="icon"]')
        if "zerodha.com/static/images/favicon.png" in (link.get("href") or "")
    ]
    if not favicon:
        raise ValueError("Zerodha official careers page no longer identifies the Zerodha favicon")
    return {
        "url": response_url,
        "title": title,
        "app_shell_present": True,
        "index_assets": scripts,
        "favicon": favicon,
        "employer_marker_in_shell": EXPECTED_EMPLOYER in html,
        "listing_request_embedded_in_page": "/api/jobs" in html,
    }


def parse_jobs_payload(payload, response_url: str = JOBS_API) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "careers.zerodha.com"
            or parsed.path != "/api/jobs" or parsed.query):
        raise ValueError("Zerodha jobs response came from an unexpected API route")
    if not isinstance(payload, dict):
        raise ValueError("Zerodha jobs response is not a JSON object")
    if payload.get("success") is not True:
        raise ValueError("Zerodha jobs response no longer reports success=true")
    count = payload.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("Zerodha jobs response has no non-negative integer count")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("Zerodha jobs response has no data list")
    if len(data) != count:
        raise ValueError(
            f"Zerodha jobs response count {count} does not match its {len(data)} data rows"
        )
    rows, names = [], set()
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(f"Zerodha jobs row {index} is not an object")
        name = row.get("name")
        title = row.get("job_title")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Zerodha jobs row {index} has no posting name/id")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"Zerodha jobs row {index} has no job_title")
        identity = name.strip()
        if identity in names:
            raise ValueError(f"Zerodha jobs response repeats posting name {identity!r}")
        names.add(identity)
        location = row.get("location")
        location_type = row.get("location_type")
        description = row.get("description")
        for field, value in (("location", location), ("location_type", location_type),
                             ("description", description)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Zerodha jobs row {index} has a non-string {field}")
        rows.append({
            "id": identity,
            "title": normalized_space(title),
            "location": normalized_space(location) or None,
            "location_type": normalized_space(location_type) or None,
            "description": description or "",
            "record": row,
        })
    return {
        "url": response_url,
        "success": True,
        "count": count,
        "data_length": len(data),
        "rows": rows,
        "observed_row_fields": sorted({key for row in data for key in row}),
        "zero_vacancy": count == 0,
        "application_endpoint": APPLICATIONS_API,
        "filters_or_paging_parameters": None,
    }


def build_job(adapter: Adapter, row: dict, listing: dict) -> Job:
    """Create one Job from an observed API row without inventing source fields."""
    record = row["record"]
    job = adapter.job(
        {"jobs_api_record": record, "jobs_api": listing, "application_route": APPLICATIONS_API,
         "application_job_id": row["id"]},
        title=row["title"],
        url=OFFICIAL_ENTRY,
        job_id=row["id"],
        description=row["description"],
        method="official_jobs_api_inline_description",
        description_url=JOBS_API,
        locations=[row["location"]] if row["location"] else [],
        workplace_type=row["location_type"],
        employer=EXPECTED_EMPLOYER,
    )
    if not row["description"].strip():
        job.description_unavailable = True
        job.warnings.append("description_empty_in_official_jobs_api")
    job.raw["description_html_sha256"] = sha256(row["description"].encode()).hexdigest()
    job.raw["description_normalized_chars"] = len(normalized_body_text(row["description"]))
    job.raw["description_fnv1a64"] = fnv1a64(normalized_body_text(row["description"]))
    return job


class Zerodha(Adapter):
    VERIFICATION_REVISION = "zerodha.official-spa-jobs-api.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": JOBS_API,
            "legal_employer": EXPECTED_EMPLOYER,
            "listing_authority": (
                "The official careers SPA's own unfiltered GET /api/jobs request. "
                "No query parameters, filters or pagination are published."
            ),
            "detail_method": (
                "The jobs API returns the full description HTML inline; the SPA expands it "
                "under the job card. There is no separate detail route. Job identity comes "
                "from the row's name field, which the apply modal also posts as jobId."
            ),
        })
        self.result.metadata["journey"] = []
        self.result.metadata["failed_sources"] = []

    def _record(self, url: str, response, seconds: float, *, method: str = "GET"):
        self.result.metadata["journey"].append({
            "url": url,
            "method": method,
            "status": response.status_code,
            "final_url": str(response.url),
            "seconds": round(seconds, 6),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "content_type": response.headers.get("content-type"),
        })
        return response

    def _failed_source(self, url: str, reason: str):
        self.result.metadata["failed_sources"].append({"url": url, "reason": reason})

    async def run(self):
        source_started = time.perf_counter()
        if self.company.careers_url != OFFICIAL_ENTRY:
            raise ValueError("Zerodha dedicated flow must start from its official HTTPS careers entry")

        entry_started = time.perf_counter()
        try:
            entry_response = await self.http.get(OFFICIAL_ENTRY, headers={
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            })
        except Exception as exc:
            seconds = max(0.0, time.perf_counter() - entry_started)
            self.result.errors.append(f"zerodha_official_entry_unavailable: {type(exc).__name__}: {exc}")
            self.result.metadata.setdefault("journey", []).append({
                "url": OFFICIAL_ENTRY, "method": "GET", "status": None,
                "seconds": round(seconds, 6), "error": f"{type(exc).__name__}: {exc}",
            })
            self._failed_source(OFFICIAL_ENTRY, f"{type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            return self.result
        entry_seconds = max(0.0, time.perf_counter() - entry_started)
        self._record(OFFICIAL_ENTRY, entry_response, entry_seconds)
        try:
            entry = parse_official_entry(entry_response.text, str(entry_response.url))
        except ValueError as exc:
            self.result.errors.append(f"zerodha_official_entry_changed: {exc}")
            self.result.listing_complete = False
            return self.result
        self.result.metadata["official_entry"] = entry

        api_started = time.perf_counter()
        try:
            jobs_response = await self.http.get(JOBS_API, headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": OFFICIAL_ENTRY,
                "Origin": "https://careers.zerodha.com",
            })
        except Exception as exc:
            seconds = max(0.0, time.perf_counter() - api_started)
            self.result.errors.append(f"zerodha_jobs_api_unavailable: {type(exc).__name__}: {exc}")
            self.result.metadata.setdefault("journey", []).append({
                "url": JOBS_API, "method": "GET", "status": None,
                "seconds": round(seconds, 6), "error": f"{type(exc).__name__}: {exc}",
            })
            self._failed_source(JOBS_API, f"{type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            return self.result
        api_seconds = max(0.0, time.perf_counter() - api_started)
        self._record(JOBS_API, jobs_response, api_seconds)
        try:
            payload = jobs_response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            self.result.errors.append(f"zerodha_jobs_api_not_json: {exc}")
            self.result.listing_complete = False
            return self.result
        try:
            listing = parse_jobs_payload(payload, str(jobs_response.url))
        except ValueError as exc:
            self.result.errors.append(f"zerodha_jobs_api_changed: {exc}")
            self.result.listing_complete = False
            return self.result

        self.result.metadata["jobs_api"] = listing
        self.result.expected_total = listing["count"]
        self.result.pages = 1
        for row in listing["rows"]:
            self.add(build_job(self, row, listing))
        self.result.listing_complete = True
        self.result.metadata["membership_reconciliation"] = {
            "listing_api_count": listing["count"],
            "data_rows": listing["data_length"],
            "count_matches_data": listing["count"] == listing["data_length"],
            "jobs_added": len(self.result.jobs),
            "complete": listing["count"] == len(self.result.jobs) and not self.result.errors,
            "pagination": "None published: one unfiltered GET /api/jobs returns count and every data row.",
            "filters": [],
            "zero_vacancy": listing["zero_vacancy"],
            "detail_route": None,
            "description_source": "inline jobs API description field rendered by the SPA",
        }
        if listing["zero_vacancy"]:
            self.result.metadata["zero_vacancy"] = {
                "listing_api_returned_zero_total": True,
                "rendered_empty_state": ZERO_VACANCY_TEXT,
                "count": 0,
                "data_rows": 0,
                "detail_pages_available": False,
            }
        self.result.timings.update({
            "official_entry_seconds": round(entry_seconds, 6),
            "jobs_api_seconds": round(api_seconds, 6),
            "source_collection_seconds": round(max(0.0, time.perf_counter() - source_started), 6),
            "detail_seconds": 0.0,
        })
        return self.result


__all__ = [
    "OFFICIAL_ENTRY",
    "JOBS_API",
    "APPLICATIONS_API",
    "EXPECTED_TITLE",
    "EXPECTED_EMPLOYER",
    "ZERO_VACANCY_TEXT",
    "JS_JOB_FIELDS",
    "Zerodha",
    "build_job",
    "fnv1a64",
    "normalized_body_text",
    "normalized_space",
    "parse_jobs_payload",
    "parse_official_entry",
]
