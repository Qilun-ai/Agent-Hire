"""Quadeye's own careers page, its company-scoped Zoho Recruit listing API, and details.

Journey (observed 2026-11-08):

* ``https://www.quadeye.com/careers/`` 308-redirects to
  ``https://www.quadeye.com/jobs``; that Next.js page renders a client-side
  ``Open Roles`` section whose rows come from
  ``GET https://www.quadeye.com/api/zoho-recruit/job-openings``.
* The exact request is baked into the page's own UI bundle
  (``/_next/static/chunks/2mju1wibq836k.js``:
  ``baseQuery("/api/zoho-recruit")`` + ``"/job-openings"``).  The endpoint
  returns every published opening in one response (no page/limit/offset
  parameters, they are ignored); the rendered count text and card count equal
  the array length.
* Cards link to ``/jobs/<zoho id>/<slug>?source=CareerSite`` where ``<slug>`` is
  the exact JS transform of the row's ``Posting_Title``.  Each detail page
  fetches ``GET /api/zoho-recruit/job-openings/<id>`` for the full HTML body;
  the list row already carries a plain-text rendering of the whole body, not a
  truncated preview.
* ``APPLY NOW`` on the detail page points at
  ``/job-apply?id=<zoho id>&title=<raw Posting_Title>`` on the same official
  site.  No external ATS board is enumerated.

The site's own display helper (``fixJobText``) rewrites the misspelling
``cypto`` to ``Crypto`` in visible titles/bodies.  One source row
(``ZR_23_JOB``) still stores ``Cypto`` in ``Posting_Title``; the visible title
and page H1 show ``Crypto`` while the generated URL/apply query keep the raw
spelling.  The normalized title is therefore the visible value with the raw
value retained next to it in ``raw``.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.quadeye.com/careers/"
LISTING_URL = "https://www.quadeye.com/jobs"
LISTING_API_URL = "https://www.quadeye.com/api/zoho-recruit/job-openings"
DETAIL_API_TEMPLATE = LISTING_API_URL + "/{job_id}"
DETAIL_PAGE_TEMPLATE = "https://www.quadeye.com/jobs/{job_id}/{slug}?source=CareerSite"
APPLY_PAGE_TEMPLATE = "https://www.quadeye.com/job-apply?id={job_id}&title={title}"
CANONICAL_DETAIL_TEMPLATE = "https://www.quadeye.com/jobs/{job_id}/{slug}"
EMPLOYER_NAME = "QE Securities LLP"
EMPLOYER_NAME_ALIASES = ("Quadeye", "Quadeye Securities", "QE Securities")
DISPLAY_TYPO = re.compile(r"\bcypto\b", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RESPONSIBILITY_HEADING = re.compile(
    r"what you.?ll do|what you will do|what you.?ll work on|responsibilit|your role",
    re.I,
)
BENEFITS_HEADING = re.compile(r"benefits|perks|what we offer", re.I)
QUALIFICATION_HEADING = re.compile(
    r"requirements|qualifications|eligibility|who you are|what you.?ll need|"
    r"what we.?re looking for|what we are looking for",
    re.I,
)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
ZERO_WIDTH = re.compile(r"[\u200b\ufeff]")


def compact(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value))).strip()


def display_title(value) -> str:
    return DISPLAY_TYPO.sub("Crypto", compact(value))


def title_slug(value) -> str:
    """Reproduce the exact slug expression shipped in Quadeye's UI bundle."""
    slug = compact(value).lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return re.sub(r"-+", "-", slug)


def detail_page_url(job_id, raw_title) -> str:
    return DETAIL_PAGE_TEMPLATE.format(
        job_id=quote(str(job_id), safe=""), slug=title_slug(raw_title)
    )


def apply_page_url(job_id, raw_title) -> str:
    return APPLY_PAGE_TEMPLATE.format(
        job_id=quote(str(job_id), safe=""), title=quote(compact(raw_title), safe="")
    )


def apply_page_path(job_id, raw_title) -> str:
    """The path form exactly as rendered in the detail page's APPLY NOW href."""
    return f"/job-apply?id={job_id}&title={quote(compact(raw_title), safe='')}"


def normalized_text(value) -> str:
    """Browser-reference normalization: NFC, no zero-width, collapsed whitespace.

    The site's HTML uses ``<br/>`` inside running sentences, so the DOM text
    has no space before punctuation while ``text_html`` inserts one.  Removing
    that inserted space reproduces the browser's ``innerText`` exactly.
    """
    text = unicodedata.normalize("NFC", str(value or ""))
    text = ZERO_WIDTH.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,;:.!?])", r"\1", text)


def normalized_body_text(html) -> str:
    return normalized_text(text_html(html))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def description_fingerprint(html) -> dict:
    text = normalized_body_text(html)
    return {
        "normalized_chars": len(text),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "fnv1a64": fnv1a64(text),
    }


def display_html(value) -> str:
    """Apply the site's exact visible-text fix without touching other markup."""
    return DISPLAY_TYPO.sub("Crypto", str(value or ""))


def json_ld_nodes(soup) -> list[dict]:
    found = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        found.extend(node for node in nodes if isinstance(node, dict))
    return found


def parse_official_entry(html: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "www.quadeye.com" or parsed.path.rstrip("/") != "/jobs":
        raise ValueError(f"Quadeye official careers entry left its inspected destination: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    title = compact(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Quadeye Careers" not in title:
        raise ValueError(f"Quadeye official jobs page title changed: {title!r}")
    app_name = soup.select_one('meta[name="application-name"]')
    if not app_name or compact(app_name.get("content")) != "Quadeye":
        raise ValueError("Quadeye official jobs page no longer identifies the Quadeye application")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = compact(canonical.get("href")) if canonical else ""
    if canonical_url.rstrip("/") != LISTING_URL:
        raise ValueError(f"Quadeye official jobs canonical URL changed: {canonical_url!r}")
    if not soup.select_one("#jobs_secA"):
        raise ValueError("Quadeye official jobs page no longer contains the observed jobs section")
    if not any(compact(h.get_text(" ", strip=True)) == "Open Roles" for h in soup.find_all(["h1", "h2", "h3"])):
        raise ValueError("Quadeye official jobs page no longer exposes the Open Roles heading")
    organisation = next(
        (node for node in json_ld_nodes(soup) if node.get("@type") == "Organization"), {}
    )
    legal_name = compact(organisation.get("legalName"))
    if legal_name != EMPLOYER_NAME:
        raise ValueError(f"Quadeye legal employer identity changed: {legal_name!r}")
    if not soup.select_one(".card-grid"):
        raise ValueError("Quadeye official jobs page no longer contains the observed role card grid")
    return {
        "url": response_url,
        "title": title,
        "canonical_url": canonical_url,
        "application_name": compact(app_name.get("content")),
        "legal_name": legal_name,
        "organisation_aliases": organisation.get("alternateName") or [],
        "contact_emails": [
            node.get("href", "").removeprefix("mailto:")
            for node in soup.select('a[href^="mailto:"]')
        ],
        "jobs_section_id": "jobs_secA",
        "open_roles_heading": "Open Roles",
        "initial_count_text": (
            compact(soup.select_one(".roles-count").get_text(" ", strip=True))
            if soup.select_one(".roles-count") else ""
        ),
        "api_endpoint_observed_in_bundle": LISTING_API_URL,
        "api_client_bundle": "https://www.quadeye.com/_next/static/chunks/2mju1wibq836k.js",
    }


def parse_listing_api(payload: dict, response_url: str = LISTING_API_URL) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Quadeye listing API response is not an object")
    if payload.get("success") is not True:
        raise ValueError(f"Quadeye listing API did not report success: {payload.get('success')!r}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("Quadeye listing API response has no data array")

    rows, ids, issues = [], [], []
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            issues.append(f"listing row {index} is not an object")
            continue
        identity = row.get("id")
        identity = str(identity).strip() if identity is not None else ""
        title = compact(row.get("Posting_Title"))
        if not re.fullmatch(r"\d+", identity):
            issues.append(f"listing row {index} has no numeric company job id")
            continue
        if not title:
            issues.append(f"listing row {identity} has no published title")
            continue
        if identity in ids:
            issues.append(f"listing row {identity} is repeated")
            continue
        if row.get("Publish") is not True:
            issues.append(f"listing row {identity} is not published")
        if row.get("$approved") is not True:
            issues.append(f"listing row {identity} is not approved")
        if not compact(row.get("Job_Opening_Status")):
            issues.append(f"listing row {identity} has no opening status")
        locations = row.get("Job_Location")
        if not isinstance(locations, list) or not any(compact(item) for item in locations):
            issues.append(f"listing row {identity} has no published location list")
        if not isinstance(row.get("Job_Description"), str) or not compact(row.get("Job_Description")):
            issues.append(f"listing row {identity} has no list description text")
        opened = row.get("Date_Opened")
        if opened is not None and not DATE_RE.fullmatch(str(opened)):
            issues.append(f"listing row {identity} has an unusual Date_Opened: {opened!r}")
        ids.append(identity)
        rows.append(dict(row))

    if not rows:
        raise ValueError("Quadeye listing API returned no published openings")
    fields = sorted({key for row in data if isinstance(row, dict) for key in row})
    return {
        "url": response_url,
        "rows": rows,
        "count": len(rows),
        "ids": ids,
        "fields": fields,
        "issues": issues,
        "cached": payload.get("cached"),
    }


def parse_detail_api(payload: dict, expected_row: dict, response_url: str) -> dict:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError(f"Quadeye detail API failed for {expected_row.get('id')}: {str(payload)[:200]!r}")
    detail = payload.get("data")
    if not isinstance(detail, dict):
        raise ValueError(f"Quadeye detail API returned no data object for {expected_row.get('id')}")
    identity = str(detail.get("id") or "")
    if identity != str(expected_row.get("id")):
        raise ValueError(f"Quadeye detail id mismatch: {identity!r} != {expected_row.get('id')!r}")
    raw_title = compact(expected_row.get("Posting_Title"))
    if compact(detail.get("Posting_Title")) != raw_title:
        raise ValueError(f"Quadeye detail title changed for {identity}: {detail.get('Posting_Title')!r}")
    detail_location = [compact(item) for item in detail.get("Job_Location") or [] if compact(item)]
    listing_location = [compact(item) for item in expected_row.get("Job_Location") or [] if compact(item)]
    if detail_location != listing_location:
        raise ValueError(f"Quadeye detail locations changed for {identity}: {detail_location} != {listing_location}")
    body = detail.get("Job_Description")
    if not isinstance(body, str) or not compact(body):
        raise ValueError(f"Quadeye detail description is missing for {identity}")
    text = normalized_body_text(body)
    if len(text) < 200:
        raise ValueError(f"Quadeye detail description is too short for {identity}: {len(text)} chars")
    return {
        "url": response_url,
        "data": detail,
        "id": identity,
        "raw_title": raw_title,
        "description_html": body,
        "description_text": text_html(body),
        "description_normalized": normalized_text(text),
        "description_fingerprint": description_fingerprint(body),
        "fields": sorted(detail),
    }


def block_heading(block: Tag) -> str:
    """Return the authored heading a block carries, if it is heading-only.

    Quadeye publishes headings as ``<h3>``, as ``<p><b>...</b></p>``, and (in
    the Trade Monitoring template) as ``<div><b>...</b></div>`` nested inside
    styled spans.  A block only counts when its whole visible text is the bold
    text (zero-width padding ignored).
    """
    if block.name in HEADING_TAGS:
        full = compact(block.get_text(" ", strip=True))
        if not ZERO_WIDTH.sub("", full).strip() or len(full) > 140:
            return ""
        return full
    if block.name not in ("p", "div"):
        return ""
    bolds = block.find_all(["b", "strong"])
    if not bolds:
        return ""
    full = compact(block.get_text(" ", strip=True))
    bold_text = compact(" ".join(bold.get_text(" ", strip=True) for bold in bolds))
    if not ZERO_WIDTH.sub("", full).strip() or not ZERO_WIDTH.sub("", bold_text).strip():
        return ""
    if len(full) > 140:
        return ""
    if re.sub(r"\s+", "", full) == re.sub(r"\s+", "", bold_text):
        return full
    return ""


def description_sections(html: str) -> list[dict]:
    """Split an authored block into heading + exact body sections.

    Bullet content is often in ``<div>`` elements, so all direct element
    children are preserved verbatim.
    """
    soup = BeautifulSoup(f'<div data-quadeye-root="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-quadeye-root]")
    blocks = [node for node in (root.contents if root else []) if isinstance(node, Tag)]
    sections, current = [], None
    for block in blocks:
        heading = block_heading(block)
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        sections.append(current)
    for section in sections:
        section["text"] = normalized_text(text_html(section["html"]))
    return [section for section in sections if section["heading"] is not None or section["text"]]


def parse_description(html: str) -> dict:
    """Extract the three Zoho-authored wrappers from the full description HTML."""
    soup = BeautifulSoup(html or "", "lxml")
    description = soup.select_one("#spandesc")
    requirements = soup.select_one("#spanreq")
    benefits = soup.select_one("#spanben")
    if description is None:
        raise ValueError("Quadeye detail no longer contains the #spandesc authored body")
    description_sections_list = description_sections(description.decode_contents())
    responsibilities = [
        {"heading": section["heading"], "text": section["text"]}
        for section in description_sections_list
        if section["heading"] and RESPONSIBILITY_HEADING.search(section["heading"]) and section["text"]
    ]
    description_qualification_sections = [
        {"heading": section["heading"], "text": section["text"]}
        for section in description_sections_list
        if section["heading"] and QUALIFICATION_HEADING.search(section["heading"]) and section["text"]
    ]
    benefit_sections = []
    if benefits is not None:
        benefit_sections = [
            {"heading": section["heading"], "text": section["text"]}
            for section in description_sections(benefits.decode_contents())
            if section["text"]
            and (section["heading"] is None or BENEFITS_HEADING.search(section["heading"]))
        ]
    return {
        "description_sections": description_sections_list,
        "responsibilities": responsibilities or None,
        "qualifications": (
            {
                "heading": "Requirements",
                "html": str(requirements),
                "text": normalized_text(text_html(str(requirements))),
                "sections": description_sections(requirements.decode_contents()),
                "description_sections": description_qualification_sections,
            }
            if requirements is not None
            else (
                {
                    "heading": description_qualification_sections[0]["heading"],
                    "html": None,
                    "text": "\n\n".join(
                        f"{item['heading']}: {item['text']}"
                        if item["heading"] else item["text"]
                        for item in description_qualification_sections
                    ),
                    "sections": description_qualification_sections,
                    "description_sections": description_qualification_sections,
                }
                if description_qualification_sections
                else None
            )
        ),
        "benefits": (
            {
                "heading": "Benefits",
                "html": str(benefits),
                "text": normalized_text(text_html(str(benefits))),
                "sections": benefit_sections,
            }
            if benefits is not None
            else None
        ),
        "wrappers": {
            "description": "#spandesc" if description is not None else None,
            "requirements": "#spanreq" if requirements is not None else None,
            "benefits": "#spanben" if benefits is not None else None,
        },
    }


def detail_page_check(html: str, job_id: str, raw_title: str, location_text: str) -> dict:
    """Validate the server-rendered shell of the exact company detail route."""
    soup = BeautifulSoup(html, "lxml")
    canonical = soup.select_one('link[rel="canonical"]')
    expected_canonical = CANONICAL_DETAIL_TEMPLATE.format(
        job_id=job_id, slug=title_slug(raw_title)
    )
    canonical_url = compact(canonical.get("href")) if canonical else ""
    if canonical_url.rstrip("/") != expected_canonical:
        raise ValueError(f"Quadeye detail page canonical mismatch for {job_id}: {canonical_url!r}")
    title = compact(soup.title.get_text(" ", strip=True) if soup.title else "")
    expected_title = f"{display_title(raw_title)} \u2013 {location_text} | Quadeye"
    if title != expected_title:
        raise ValueError(f"Quadeye detail page title mismatch for {job_id}: {title!r}")
    if not soup.select_one("#job_details_secA .content_details"):
        raise ValueError(f"Quadeye detail page no longer contains the job details content area for {job_id}")
    return {
        "url": expected_canonical,
        "http_title": title,
        "canonical_url": canonical_url,
        "job_details_section": "job_details_secA",
        "content_details_present": True,
        "server_rendered_h1_empty": not compact(
            (soup.find("h1") or {}).get_text(" ", strip=True) if soup.find("h1") else ""
        ),
        "server_rendered_apply_placeholder": [
            anchor.get("href") for anchor in soup.select('a[href*="job-apply"]')
        ],
    }


class Quadeye(Adapter):
    """Collect Quadeye's own published roles from its company-scoped API."""

    VERIFICATION_REVISION = "quadeye.qe-securities.zoho-proxy-listing-and-detail.1"
    DETAIL_CONCURRENCY = 2

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.entry = None
        self.listing = None
        self.final_listing = None
        self.representative_pages: dict[str, dict] = {}

    def _stub(self, row: dict):
        raw_title = compact(row.get("Posting_Title"))
        identity = str(row["id"])
        locations = [compact(item) for item in row.get("Job_Location") or [] if compact(item)]
        job = self.job(
            dict(row),
            title=display_title(raw_title),
            url=detail_page_url(identity, raw_title),
            job_id=identity,
            method="quadeye.company_scoped_zoho_detail_api",
            description_url=DETAIL_API_TEMPLATE.format(job_id=identity),
            requisition_id=compact(row.get("Job_Opening_ID")) or None,
            apply_url=apply_page_url(identity, raw_title),
            locations=locations,
            department=None,
            employment_type=compact(row.get("Job_Type")) or None,
            experience=compact(row.get("Work_Experience")) or None,
            date_posted=compact(row.get("Date_Opened")) or None,
            date_posted_raw=row.get("Date_Opened"),
            salary=compact(row.get("Salary")) or None,
            employer=EMPLOYER_NAME,
            language="en",
        )
        job.raw.update({
            "listing_row": row,
            "source_title": raw_title,
            "source_apply_title": raw_title,
            "title_display_transform": (
                {"rule": "site fixJobText: \\bcypto\\b -> Crypto", "applied": raw_title != display_title(raw_title)}
            ),
            "slug": title_slug(raw_title),
            "detail_page_url": job.url,
            "apply_page_path": apply_page_path(identity, raw_title),
            "listing_api_url": LISTING_API_URL,
            "detail_api_url": DETAIL_API_TEMPLATE.format(job_id=identity),
            "source_status": compact(row.get("Job_Opening_Status")) or None,
            "source_positions": compact(row.get("Number_of_Positions")) or None,
            "source_designation": compact(row.get("Designation")) or None,
            "source_industry": compact(row.get("Industry")) or None,
            "source_client": (row.get("Client_Name") or {}).get("name")
            if isinstance(row.get("Client_Name"), dict) else None,
        })
        if job.raw["source_status"] == "On-Hold":
            job.warnings.append("source_status_on_hold")
        if not compact(row.get("Required_Skills")):
            job.warnings.append("source_required_skills_empty")
        return job

    async def _fetch_listing(self, label: str):
        started = time.perf_counter()
        response = await self.http.get(LISTING_API_URL)
        payload = response.json()
        parsed = parse_listing_api(payload, str(response.url))
        parsed["http_status"] = response.status_code
        parsed["response_sha256"] = hashlib.sha256(response.content).hexdigest()
        parsed["response_bytes"] = len(response.content)
        parsed["request_seconds"] = max(0.0, time.perf_counter() - started)
        parsed["label"] = label
        return parsed

    async def detail(self, job):
        identity = str(job.job_id)
        row = job.raw["listing_row"]
        started = time.perf_counter()
        response = await self.http.get(DETAIL_API_TEMPLATE.format(job_id=identity))
        payload = response.json()
        parsed = parse_detail_api(payload, row, str(response.url))
        parsed["http_status"] = response.status_code
        parsed["response_sha256"] = hashlib.sha256(response.content).hexdigest()
        parsed["response_bytes"] = len(response.content)
        parsed["request_seconds"] = max(0.0, time.perf_counter() - started)
        parsed["fetched_at"] = None
        sections = parse_description(parsed["description_html"])

        job.description_html = display_html(parsed["description_html"])
        job.description_text = text_html(job.description_html)
        job.description_method = "quadeye.company_scoped_zoho_detail_api"
        job.description_source_url = str(response.url)
        job.qualifications = sections["qualifications"]
        job.responsibilities = sections["responsibilities"]
        if job.date_posted is None and DATE_RE.fullmatch(str(row.get("Date_Opened") or "")):
            job.date_posted = str(row["Date_Opened"])
        job.raw["detail_row"] = parsed["data"]
        job.raw["detail"] = {
            "api_url": str(response.url),
            "http_status": response.status_code,
            "response_sha256": parsed["response_sha256"],
            "response_bytes": parsed["response_bytes"],
            "request_seconds": parsed["request_seconds"],
            "fields": parsed["fields"],
            "description_fingerprint": parsed["description_fingerprint"],
        }
        job.raw["description_sections"] = sections["description_sections"]
        job.raw["benefits"] = sections["benefits"]
        job.raw["description_wrappers"] = sections["wrappers"]
        job.raw["company_detail_page_url"] = job.url
        job.raw["apply_url"] = job.apply_url
        job.raw["detail_page_observation"] = self.representative_pages.get(identity)
        job.validate()
        return job

    async def _representative_detail_pages(self, jobs):
        """Fetch the first and last listed detail routes exactly as the site links them."""
        if not jobs:
            return
        for job in (jobs[0], jobs[-1]):
            started = time.perf_counter()
            response = await self.http.get(job.url)
            location_text = ", ".join(job.locations)
            observed = detail_page_check(response.text, job.job_id, job.raw["source_title"], location_text)
            observed.update({
                "http_status": response.status_code,
                "request_seconds": max(0.0, time.perf_counter() - started),
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
                "response_bytes": len(response.content),
            })
            self.representative_pages[str(job.job_id)] = observed
            job.raw["detail_page_observation"] = observed
            job.raw["detail_page"] = observed

    async def run(self):
        started = time.perf_counter()
        source_started = time.perf_counter()
        entry_response = await self.http.get(OFFICIAL_ENTRY)
        self.entry = parse_official_entry(entry_response.text, str(entry_response.url))
        self.entry.update({
            "http_status": entry_response.status_code,
            "redirects": [str(item.url) for item in entry_response.history],
            "request_seconds": max(0.0, time.perf_counter() - source_started),
            "response_sha256": hashlib.sha256(entry_response.content).hexdigest(),
        })
        self.result.metadata["official_entry"] = self.entry

        listing_started = time.perf_counter()
        self.listing = await self._fetch_listing("initial")
        self.result.timings["listing_collection_seconds"] = max(
            0.0, time.perf_counter() - listing_started
        )
        self.result.timings["source_collection_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        self.result.metadata["listing"] = {
            key: value for key, value in self.listing.items() if key != "rows"
        }
        self.result.metadata["listing_response_sha256"] = self.listing["response_sha256"]
        self.result.pages = 1
        self.result.expected_total = self.listing["count"]
        for issue in self.listing["issues"]:
            self.result.errors.append(f"listing: {issue}")

        jobs = []
        for row in self.listing["rows"]:
            try:
                job = self._stub(row)
                job.raw["listing_position"] = len(jobs) + 1
                job.raw["listing_response_sha256"] = self.listing["response_sha256"]
                self.add(job)
                jobs.append(job)
            except ValueError as exc:
                self.result.errors.append(f"listing row {row.get('id')}: {exc}")
        self.result.metadata["listing_rows_created"] = len(jobs)
        self.result.listing_complete = not self.result.errors and len(jobs) == self.listing["count"]
        if len(jobs) != self.listing["count"]:
            self.result.errors.append("listing_stub_count_mismatch")

        await self.hydrate(jobs, self.detail)
        jobs = list(self.result.jobs)

        # Representative detail pages are checked only after the exact API details.
        detail_page_started = time.perf_counter()
        try:
            await self._representative_detail_pages(jobs)
            self.result.timings["representative_detail_pages_seconds"] = max(
                0.0, time.perf_counter() - detail_page_started
            )
        except Exception as exc:
            self.result.errors.append(f"representative detail page: {type(exc).__name__}: {exc}")
            self.result.timings["representative_detail_pages_seconds"] = max(
                0.0, time.perf_counter() - detail_page_started
            )

        final_started = time.perf_counter()
        try:
            self.final_listing = await self._fetch_listing("final")
            self.result.timings["final_listing_recheck_seconds"] = max(
                0.0, time.perf_counter() - final_started
            )
            self._reconcile_final_listing(expected_ids=self.listing["ids"])
        except Exception as exc:
            self.result.errors.append(f"final listing recheck: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["final_listing_recheck_seconds"] = max(
                0.0, time.perf_counter() - final_started
            )

        self.result.metadata["detail_pages_checked"] = [
            str(job.job_id) for job in jobs if str(job.job_id) in self.representative_pages
        ]
        self.result.metadata["source_fields_per_listing_row"] = self.listing["fields"]
        self.result.metadata["employer"] = {
            "legal_name": EMPLOYER_NAME,
            "evidence": "official /jobs Organization JSON-LD",
        }
        self.result.metadata["display_transform"] = {
            "rule": "site fixJobText rewrites the misspelling cypto -> Crypto",
            "applied_to_source_titles": [
                str(job.job_id) for job in jobs if job.raw.get("title_display_transform", {}).get("applied")
            ],
        }
        self.result.metadata["detail_route"] = {
            "pattern": DETAIL_PAGE_TEMPLATE,
            "slug_rule": "raw Posting_Title lowercased, non [a-z0-9\\s-] removed, whitespace -> '-', '-' runs collapsed",
        }
        self.result.metadata["apply_route"] = {
            "pattern": APPLY_PAGE_TEMPLATE,
            "source": "exact APPLY NOW href on each detail page (client-rendered)",
        }
        self.result.listing_complete = bool(
            self.result.listing_complete
            and not self.result.errors
            and len(jobs) == self.listing["count"]
            and len(self.representative_pages) == 2
        )
        self.result.finish()
        self.result.timings["collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
        return self.result

    def _reconcile_final_listing(self, expected_ids):
        final = self.final_listing
        if final is None:
            raise ValueError("no final listing response")
        self.result.metadata["final_listing"] = {
            key: value for key, value in final.items() if key != "rows"
        }
        if final["ids"] != expected_ids:
            self.result.errors.append("listing_changed_during_run")
            self.result.listing_complete = False
            self.result.metadata["membership_change"] = {
                "initial": expected_ids,
                "final": final["ids"],
                "added": [item for item in final["ids"] if item not in expected_ids],
                "removed": [item for item in expected_ids if item not in final["ids"]],
            }
            return
        changed = []
        final_by_id = {str(row["id"]): row for row in final["rows"]}
        for job in self.result.jobs:
            identity = str(job.job_id)
            before = job.raw.get("listing_row") or {}
            after = final_by_id.get(identity)
            if after is None:
                changed.append({"id": identity, "reason": "missing_from_final"})
                continue
            for field in sorted(set(before) | set(after)):
                if field == "Job_Description":
                    if normalized_text(before.get(field)) != normalized_text(after.get(field)):
                        changed.append({"id": identity, "field": field})
                elif before.get(field) != after.get(field):
                    changed.append({"id": identity, "field": field})
        self.result.metadata["membership_change"] = {
            "initial": expected_ids,
            "final": final["ids"],
            "added": [],
            "removed": [],
            "changed_fields": changed,
        }
        if changed:
            self.result.errors.append("listing_rows_changed_during_run")
            self.result.listing_complete = False
