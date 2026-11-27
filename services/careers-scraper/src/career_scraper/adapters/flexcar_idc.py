"""Flexcar IDC — Flexcar's own JazzHR-hosted careers board and detail/apply pages.

Owned module for the ledger slug ``flexcar-idc`` (review-resume revision). It
keeps the parsing and collection contract of the earlier ``flexcar.py``
revision byte-for-byte; only the module/class name follows the ledger-owned
path given in the assignment.

Observed official journey (2026-11-07):

* The requested ``https://www.flexcar.com/careers`` returns HTTP 404. The
  homepage ``https://www.flexcar.com/`` publishes ``https://careers.flexcar.com/``
  as its careers link, which serves the Flexcar Career Page (canonical
  ``https://careers.flexcar.com/apply``) under the JazzHR "Resumator" skin.
* The listing is a single server-rendered page: ``<ul class="list-group">``
  under "Current Openings", one ``<h3 class="list-group-item-heading">``
  anchor per posting with the location in the sibling
  ``ul.list-inline.list-group-item-text``. There is no pagination, no
  department filter bar and no load-more control in the shipped HTML.
* Each exact listing anchor is a job page
  ``https://careers.flexcar.com/apply/<10-char-code>/<Title-Slug>``. The
  page contains the full role body in ``#job-description``, a schema.org
  ``JobPosting`` JSON-LD block (``datePosted``, ``validThrough``,
  ``employmentType``, ``experienceRequirements``, ``uniqueJobCode``,
  ``jobLocation``), the location/employment/experience chips in
  ``.job-header`` and the application form (POST to the same URL). The job
  page *is* the apply page; no separate board or ATS account was searched.
* The JSON-LD ``description`` and the ``#job-description`` div carry the
  identical full role text (verified on two representative bodies, codes
  dzOqjXPf8C and 36UmEpLd4b). Corporate footers, cookie text and
  application questions live outside ``#job-description`` and are not
  retained as descriptions.

Only the exact codes/URLs this listing page publishes are fetched. The
board host is pinned to ``careers.flexcar.com`` so a guessed foreign board
can never be enumerated.
"""
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

CODE = re.compile(r"^[A-Za-z0-9]{10}$")
JOB_PATH = re.compile(r"^/apply/([A-Za-z0-9]{10})/([^/]+)$")
ANCHOR_PATH = re.compile(r"^/apply/([A-Za-z0-9]{10})(?:/([^/]*))?$")


def compact_body_hash(html):
    """NFC/whitespace-free SHA-256 of a role body, for body comparisons."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s﻿]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _labelled_line(description_text, label):
    """Return the free-text value of a ``Label: value`` role-body line."""
    lines = [line.strip() for line in (description_text or "").splitlines()]
    for index, line in enumerate(lines):
        match = re.match(rf"^{re.escape(label)}\s*:\s*(.*)$", line, re.I)
        if not match:
            continue
        value = match.group(1).strip()
        if value:
            return value
        for following in lines[index + 1:]:
            if following:
                return following
    return None


def _salary_from_description(description_text):
    value = _labelled_line(description_text, "Compensation")
    if not value:
        return None
    return {"source_text": value, "source_field": "description"}


def _ld_block(soup):
    # Most pages carry a schema.org JobPosting block, but the source page
    # itself is the authority: xK8pKmA1Sj (Larchmont, NY) publishes only the
    # Organization block. Return None then; callers keep LD-only fields null.
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


def parse_listing(html):
    """Return exact listing rows in page order: code, URL, title, location."""
    soup = BeautifulSoup(html, "lxml")
    rows, seen = [], set()
    for anchor in soup.select("h3.list-group-item-heading a[href]"):
        href = anchor.get("href", "").strip()
        path = urlsplit(href).path
        match = JOB_PATH.fullmatch(path) or ANCHOR_PATH.fullmatch(path)
        if not match or not CODE.fullmatch(match.group(1)):
            raise ValueError(f"Flexcar listing job link changed from /apply/<code>/<slug>: {href!r}")
        code = match.group(1)
        if code in seen:
            raise ValueError(f"Flexcar listing repeats company-published job code {code}")
        seen.add(code)
        item = anchor.find_parent("li", class_="list-group-item")
        location = None
        if item is not None:
            loc_el = item.select_one("ul.list-inline.list-group-item-text li")
            if loc_el is not None:
                location = loc_el.get_text(" ", strip=True) or None
        rows.append({
            "code": code,
            "url": href,
            "title": anchor.get_text(" ", strip=True),
            "listing_location": location,
        })
    if not rows:
        raise ValueError("Flexcar official listing page exposes zero job anchors; empty state requires review")
    return rows


def parse_detail(html):
    """Parse one exact Flexcar job page into its public source fields."""
    soup = BeautifulSoup(html, "lxml")
    ld = _ld_block(soup)
    description_div = soup.find(id="job-description")
    if description_div is None:
        raise ValueError("Flexcar detail page has no #job-description role body")
    description_html = description_div.decode_contents()
    description_text = text_html(description_html)
    ld_text = text_html(ld.get("description") or "") if ld else None
    header = soup.select_one(".job-header")
    header_texts = [el.get_text(" ", strip=True) for el in header.select(".job-attributes-container > div")] if header else []
    return {
        "json_ld": ld,
        "description_html": description_html,
        "description_text": description_text,
        "ld_description_text": ld_text,
        "header_attributes": header_texts,
    }


class FlexcarIDC(Adapter):
    """Collect only the exact postings Flexcar's own board page publishes."""

    VERIFICATION_REVISION = "flexcar.jazzhr-single-page-list-and-detail-apply.1"
    BOARD_HOST = "careers.flexcar.com"

    @staticmethod
    def _assert_board_url(url, *, path_prefix=None):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname != FlexcarIDC.BOARD_HOST:
            raise ValueError(f"Flexcar company page left the official careers board: {url}")
        if path_prefix is not None and not parsed.path.startswith(path_prefix):
            raise ValueError(f"Flexcar company page has an unexpected path: {url}")
        return parsed

    async def _listing_rows(self, url, label):
        response = await self.http.get(url)
        self._assert_board_url(response.url)
        return parse_listing(response.text), response

    def _make_stub(self, row, index):
        parsed = urlsplit(row["url"])
        if not parsed.path.startswith("/apply/"):
            raise ValueError(f"Flexcar listed URL is not an exact job page: {row['url']}")
        raw = {
            "listing": dict(row, index=index),
            "source_ui": {
                "listing_index": index,
                "listing_location": row["listing_location"],
                "board_host": self.BOARD_HOST,
            },
        }
        job = self.job(
            raw,
            title=row["title"],
            url=row["url"],
            job_id=row["code"],
            requisition_id=None,
            apply_url=row["url"],
            description="",
            method=None,
            description_url=None,
            locations=[{"location": row["listing_location"]}] if row["listing_location"] else [],
        )
        job.record_identity = f"flexcar:{row['code']}"
        return job

    async def _hydrate_one(self, job):
        response = await self.http.get(job.url)
        self._assert_board_url(response.url)
        if urlsplit(str(response.url)).path != urlsplit(job.url).path:
            raise ValueError(f"Flexcar exact job page redirected to a different posting: {response.url}")
        detail = parse_detail(response.text)
        ld = detail["json_ld"] or {}
        if ld and ld.get("url") and ld["url"].rstrip("/") != job.url.rstrip("/"):
            raise ValueError(f"Flexcar JobPosting url differs from its exact published job page: {job.url}")
        description_html = detail["description_html"]
        description_text = detail["description_text"]
        if not description_html.strip():
            self.result.errors.append(f"{job.job_id}: Flexcar public detail #job-description is empty")
        elif detail["ld_description_text"] is not None and detail["ld_description_text"] != description_text:
            self.result.errors.append(f"{job.job_id}: Flexcar JSON-LD full description differs from #job-description body")
        if detail["json_ld"] is None:
            job.warnings.append("source_page_has_no_jobposting_jsonld_structured_fields_null")
        date_posted = ld.get("datePosted") if isinstance(ld.get("datePosted"), str) and ld["datePosted"].strip() else None
        job_address = ((ld.get("jobLocation") or {}).get("address") or {}) if isinstance(ld.get("jobLocation"), dict) else {}
        listing_location = job.raw["source_ui"]["listing_location"]
        header_attrs = detail["header_attributes"]
        header_location = header_attrs[0] if len(header_attrs) > 0 else None
        header_employment = header_attrs[1] if len(header_attrs) > 1 else None
        header_experience = header_attrs[2] if len(header_attrs) > 2 else None
        salary = _salary_from_description(description_text)
        description_location = _labelled_line(description_text, "Location")
        employment_from_body = _labelled_line(description_text, "Employment Type")
        job.locations = [{
            "location": listing_location,
            "job_location": {
                "locality": job_address.get("addressLocality") or None,
                "region": job_address.get("addressRegion") or None,
                "postal_code": job_address.get("postalCode") or None,
            },
            "header_location": header_location,
            "description_location_line": description_location,
        }]
        job.employment_type = header_employment or ld.get("employmentType")
        job.experience = ld.get("experienceRequirements") or header_experience
        job.employer = ((ld.get("hiringOrganization") or {}).get("name")) or "Flexcar"
        job.requisition_id = ld.get("uniqueJobCode")
        job.salary = salary
        job.date_posted = date_posted
        job.date_posted_raw = ld.get("datePosted")
        job.date_updated = None
        job.valid_through = ld.get("validThrough") if isinstance(ld.get("validThrough"), str) and ld["validThrough"].strip() else None
        job.description_html = description_html
        job.description_text = description_text
        job.description_method = "flexcar.official_careers_board.detail_job-description"
        job.description_source_url = job.url
        job.raw["json_ld"] = detail["json_ld"]
        job.raw["source_ui"].update({
            "header_attributes": header_attrs,
            "header_location": header_location,
            "header_employment_type": header_employment,
            "header_experience": header_experience,
            "description_location_line": description_location,
            "description_employment_type": employment_from_body,
            "json_ld_employment_type": ld.get("employmentType"),
            "json_ld_experience_requirements": ld.get("experienceRequirements"),
            "json_ld_date_posted": ld.get("datePosted"),
            "json_ld_valid_through": ld.get("validThrough"),
            "unique_job_code": ld.get("uniqueJobCode"),
            "hiring_organization": ld.get("hiringOrganization"),
            "salary_from_description": salary,
            "attachment_links": _job_attachment_links(description_html),
            "labeled_form_apply_url": job.url,
        })
        if job.raw["source_ui"]["attachment_links"]:
            self.result.warnings.append(f"{job.job_id}: role attachment links present in source body; links retained in raw")
        job.validate()
        self.result.validate_job(job)
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        self._assert_board_url(entry_url)

        initial_rows, entry_response = await self._listing_rows(entry_url, "initial")
        initial_by_id = {row["code"]: row for row in initial_rows}
        stubs = [self._make_stub(row, index) for index, row in enumerate(initial_rows)]
        self.result.expected_total = len(initial_rows)
        self.result.pages = 1

        # Hydrate every exact published job page. The shared helper records
        # detail timings, provenance and failures per posting.
        jobs = await self.hydrate(stubs, self._hydrate_one)
        for job in jobs:
            self.add(job)

        final_snapshot_verified = True
        try:
            final_rows, _ = await self._listing_rows(entry_url, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_rows = initial_rows
            self.result.errors.append(
                f"Flexcar final company-published listing snapshot failed; initial usable rows retained: {type(exc).__name__}: {exc}"
            )
        final_by_id = {row["code"]: row for row in final_rows}
        changed_membership = final_snapshot_verified and set(final_by_id) != set(initial_by_id)
        if changed_membership:
            self.result.errors.append("flexcar_official_listing_membership_changed_between_initial_and_final_snapshots")
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors
        existing = {job.job_id for job in self.result.jobs}
        for row in final_rows:
            if row["code"] not in existing:
                self.result.errors.append(f"{row['code']}: posting appeared in final listing but has no exact detail page")
        for job in self.result.jobs:
            if job.job_id not in final_by_id and final_snapshot_verified:
                job.warnings.append("posting_absent_from_final_listing_snapshot")

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry_response.url),
            "membership_authority": "Flexcar's own published careers board listing (Current Openings list-group) at the official careers host; every exact /apply/<code>/<slug> anchor on that page, in page order",
            "listing_pagination": "None: the listing is a single server-rendered page with no pagination, filter bar or load-more control in the shipped HTML",
            "departments": [],
            "listing_request_count": 2,
            "initial_codes": [row["code"] for row in initial_rows],
            "final_codes": [row["code"] for row in final_rows],
            "final_snapshot_verified": final_snapshot_verified,
            "description_source": "Each exact published job page's #job-description body (byte-identical in plain text to its schema.org JobPosting JSON-LD description); the page is also the application page (form POSTs to the same URL)",
            "apply_page_policy": "The exact job page URL is both the posting URL and the labeled application URL; no application form was opened or submitted",
            "job_attachment_links": {
                job.job_id: job.raw["source_ui"]["attachment_links"]
                for job in jobs if job.raw.get("source_ui", {}).get("attachment_links")
            },
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result


# Compatibility alias for callers written against the earlier revision.
Flexcar = FlexcarIDC
