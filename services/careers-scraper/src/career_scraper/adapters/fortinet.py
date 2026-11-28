"""Fortinet's own careers journey: official page -> published Oracle CX_2001 board.

The official entry page (https://www.fortinet.com/corporate/careers) publishes
"Careers at Fortinet" / "Explore Careers at Fortinet" CTAs that link to the
Oracle Candidate Experience board at
https://edel.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001.
That board's own public listing/detail requests (recruitingCEJobRequisitions /
recruitingCEJobRequisitionDetails) are the only membership/description authority
used here. No ATS board, guessed slug, or search outside the published board is
ever enumerated.

The rendered board initially shows 12 cards and a "Show More Results" control;
each click requests the same `findReqs` endpoint at the next 12-row offset.  The
flow below mirrors that request shape (page size 12, the same facet/expand
parameters), walks every offset, and then hydrates each exact listed ID.

The job page renders four public description sections in this order: Job
Description, Responsibilities, Qualifications, About Us.  The matching Oracle
fields are ExternalDescriptionStr, ExternalResponsibilitiesStr,
ExternalQualificationsStr and CorporateDescriptionStr.  All four are retained;
OrganizationDescriptionStr and ShortDescriptionStr remain in the raw row and are
not substituted for role content.

The entry page also links "Careers at Fortinet Federal"
(https://www.fortinetfederal.com/jobs/), a distinct legal entity
("Fortinet Federal, Inc.") hosted under a different CX site (CX_1).  It is
deliberately out of scope for the requested "Fortinet" company and is recorded
in the run metadata as an explicit exclusion, not silently merged in.
"""
from __future__ import annotations

import hashlib
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter, Options
from ..models import text_html

ENTRY_URL = "https://www.fortinet.com/corporate/careers"
CX_HOST = "edel.fa.us2.oraclecloud.com"
CX_ORIGIN = "https://edel.fa.us2.oraclecloud.com"
CX_SITE = "CX_2001"
CX_BOARD_URL = f"{CX_ORIGIN}/hcmUI/CandidateExperience/en/sites/{CX_SITE}"
LISTING_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAILS_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"

ROLE_FIELDS = (
    "ExternalDescriptionStr",
    "ExternalResponsibilitiesStr",
    "ExternalQualificationsStr",
)
# CorporateDescriptionStr is rendered by the board as the "About Us" section,
# including the accommodations/EEO text. It is part of the published job page,
# not the site footer. OrganizationDescriptionStr is not rendered on this board.
BODY_FIELDS = ROLE_FIELDS + ("CorporateDescriptionStr",)
BODY_LABELS = {
    "ExternalDescriptionStr": "JOB DESCRIPTION",
    "ExternalResponsibilitiesStr": "RESPONSIBILITIES",
    "ExternalQualificationsStr": "QUALIFICATIONS",
    "CorporateDescriptionStr": "ABOUT US",
}
FACETS = (
    "WORK_LOCATIONS;WORKPLACE_TYPES;TITLES;CATEGORIES;ORGANIZATIONS;"
    "POSTING_DATES;FLEX_FIELDS;LOCATIONS"
)
EXPAND = (
    "requisitionList.workLocation,requisitionList.otherWorkLocations,"
    "requisitionList.secondaryLocations,flexFieldsFacet.values,"
    "requisitionList.requisitionFlexFields"
)
PAGE_SIZE = 12  # the board's own "Show More Results" batch size


def detail_body_html(detail: dict) -> str:
    """Assemble the full published body from Fortinet's exact external fields.

    Each non-empty section becomes a labeled section in the order rendered by
    the board.  ShortDescriptionStr (listing teaser) and
    OrganizationDescriptionStr are preserved in the raw detail row but never
    used as a replacement for a role section.
    """
    parts = []
    for field in BODY_FIELDS:
        value = detail.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(f"<section><h2>{BODY_LABELS[field]}</h2>{value}</section>")
    return "\n".join(parts)


def role_fields_empty(detail: dict) -> bool:
    """True when all three role-bearing external sections are empty."""
    return not any(text_html(detail.get(field) or "") for field in ROLE_FIELDS)


def job_page_url(job_id: str) -> str:
    return f"{CX_BOARD_URL}/job/{job_id}"


def apply_page_url(job_id: str) -> str:
    """The route the board's APPLY NOW button opens for this exact job."""
    return f"{job_page_url(job_id)}/apply/email"


def board_site_from_entry(html: str, entry_url: str) -> str:
    """Return the CX site number published by the official careers entry page.

    Raises ValueError unless the entry page links to the inspected Fortinet
    board (CX_2001) on the inspected Oracle host.
    """
    soup = BeautifulSoup(html, "lxml")
    found = set()
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        parsed = urlsplit(href)
        if parsed.hostname == CX_HOST and "/CandidateExperience/en/sites/" in parsed.path:
            site = parsed.path.rstrip("/").split("/sites/")[1].split("/")[0]
            found.add(site)
    if CX_SITE not in found:
        raise ValueError(
            f"Fortinet official entry does not publish the inspected CX board {CX_SITE} "
            f"(entry={entry_url}, boards_found={sorted(found)})"
        )
    if found - {CX_SITE}:
        raise ValueError(
            f"Fortinet official entry publishes unexpected additional boards "
            f"{sorted(found - {CX_SITE})} alongside {CX_SITE}; manual review required"
        )
    return CX_SITE


def _location_name(item) -> str | None:
    if isinstance(item, dict):
        value = (
            item.get("LocationName")
            or item.get("Name")
            or item.get("name")
            or item.get("TownOrCity")
        )
        return value if isinstance(value, str) and value.strip() else None
    if isinstance(item, str) and item.strip():
        return item
    return None


def location_values(detail: dict) -> list:
    """Public locations from the exact row (primary + secondary, then fallback)."""
    values: list = []
    primary = detail.get("PrimaryLocation")
    if isinstance(primary, str) and primary.strip():
        values.append(primary)
    for item in detail.get("secondaryLocations") or []:
        value = _location_name(item)
        if value:
            values.append(value)
        elif item:
            values.append(item)
    if not values:
        for key in ("workLocation", "otherWorkLocations"):
            for item in detail.get(key) or []:
                value = _location_name(item)
                if value:
                    values.append(value)
    # Preserve first-seen order while dropping exact duplicates.
    return list(dict.fromkeys(values))


def salary_fields(detail: dict):
    """Retain explicit salary/compensation flex fields if the board publishes any."""
    values = []
    for field in detail.get("requisitionFlexFields") or []:
        if not isinstance(field, dict):
            continue
        prompt = str(field.get("Prompt") or field.get("Name") or "")
        value = field.get("Value")
        if not value or not prompt:
            continue
        if any(word in prompt.casefold() for word in ("salary", "compensation", "pay range", "pay rate")):
            values.append({"label": prompt, "value": value})
    return values or None


class Fortinet(Adapter):
    """Collect only the membership its official page publishes on CX_2001."""

    VERIFICATION_REVISION = "fortinet.official-careers-cx2001.2"
    PAGE_SIZE = PAGE_SIZE

    def __init__(self, http, company, source, options: Options):
        super().__init__(http, company, source, options)
        self._entry_url = ENTRY_URL
        self._board_url = CX_BOARD_URL

    # ---------------------------------------------------------------- entry
    async def _entry_board(self) -> float:
        started = time.perf_counter()
        entry_parts = urlsplit(self.company.careers_url)
        if (
            entry_parts.scheme != "https"
            or entry_parts.hostname not in {"www.fortinet.com", "fortinet.com"}
            or entry_parts.path.rstrip("/") != "/corporate/careers"
        ):
            raise ValueError(
                "Fortinet dedicated flow must start from its official "
                "https://www.fortinet.com/corporate/careers entry"
            )
        entry = await self.http.get(self.company.careers_url)
        if str(entry.url).rstrip("/") != self.company.careers_url.rstrip("/"):
            raise ValueError(
                f"Fortinet official entry redirected: {self.company.careers_url} -> {entry.url}"
            )
        site = board_site_from_entry(entry.text, str(entry.url))
        if site != CX_SITE:
            raise ValueError(f"Fortinet entry board changed from inspected site: {site}")
        return time.perf_counter() - started

    # --------------------------------------------------------------- listing
    def _finder(self, *, offset: int | None = None, facets: str | None = None) -> str:
        parts = [f"findReqs;siteNumber={CX_SITE}"]
        if facets:
            parts.append(f"facetsList={facets}")
        parts.append(f"limit={self.PAGE_SIZE}")
        if offset:
            parts.append(f"offset={offset}")
        parts.append("sortBy=POSTING_DATES_DESC")
        return ",".join(parts)

    async def _listing_page(self, offset: int):
        params = {
            "onlyData": "true",
            "expand": EXPAND,
            "finder": self._finder(offset=offset or None, facets=FACETS),
        }
        response = await self.http.request("GET", f"{CX_ORIGIN}{LISTING_PATH}", params=params)
        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError(f"Fortinet CX listing page at offset {offset} was not JSON") from exc
        items = data.get("items") or []
        if len(items) != 1:
            raise ValueError(f"Fortinet CX listing page shape invalid at offset {offset}")
        block = items[0]
        if str(block.get("SiteNumber")) != CX_SITE:
            raise ValueError(f"Fortinet CX listing site mismatch at offset {offset}")
        rows = block.get("requisitionList")
        if not isinstance(rows, list):
            raise ValueError(f"Fortinet CX requisitionList missing at offset {offset}")
        return str(response.url), block, rows

    # ---------------------------------------------------------------- detail
    async def _fetch_detail(self, job_id: str):
        params = {
            "onlyData": "true",
            "expand": "all",
            "finder": f'ById;Id="{job_id}",siteNumber={CX_SITE}',
        }
        response = await self.http.request("GET", f"{CX_ORIGIN}{DETAILS_PATH}", params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Fortinet CX detail for exact ID {job_id} was not JSON") from exc
        rows = payload.get("items") or []
        if len(rows) != 1 or str(rows[0].get("Id")) != str(job_id):
            raise ValueError(f"Fortinet CX detail did not return the exact published posting {job_id}")
        row = rows[0]
        return str(response.url), response.content, row

    def _job_from_listing(self, row: dict, listing_url: str):
        job_id = str(row.get("Id") or "")
        if not job_id or not row.get("Title"):
            raise ValueError("Fortinet CX listing row is missing its exact ID or title")
        job = self.job(
            {"listing": row},
            title=row["Title"],
            job_id=job_id,
            url=job_page_url(job_id),
            apply_url=apply_page_url(job_id),
            date_posted=row.get("PostedDate"),
            date_posted_raw=row.get("PostedDate"),
            locations=location_values(row),
            department=row.get("Department") or row.get("Category"),
            team=row.get("JobFunction") or row.get("JobFamily"),
            employment_type=row.get("JobSchedule") or row.get("JobType"),
            workplace_type=row.get("WorkplaceType") or row.get("WorkplaceTypeCode") or None,
            salary=salary_fields(row),
            language=row.get("Language") or row.get("ContentLocale"),
        )
        job.record_identity = f"fortinet:{job_id}"
        job.raw["listing_request_url"] = listing_url
        job.raw["site_number"] = CX_SITE
        return job

    # ---------------------------------------------------------------- run
    async def run(self):
        entry_seconds = await self._entry_board()
        self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
        listing_started = time.perf_counter()

        pages = []
        seen_ids: set[str] = set()
        offset = 0
        expected_total = None
        while self.page_guard():
            listing_url, block, rows = await self._listing_page(offset)
            observed_total = int(block.get("TotalJobsCount", -1))
            if expected_total is None:
                expected_total = observed_total
                self.result.expected_total = observed_total
            pages.append({
                "offset": offset,
                "rows": len(rows),
                "first_id": str(rows[0]["Id"]) if rows else None,
                "last_id": str(rows[-1]["Id"]) if rows else None,
                "source_total": observed_total,
                "url": listing_url,
            })
            if observed_total != expected_total:
                self.result.errors.append(
                    f"fortinet_cx_total_changed_during_listing:{expected_total}->{observed_total}"
                )
                break
            if not rows:
                break
            before = len(self.result.jobs)
            for row in rows:
                job_id = str(row.get("Id") or "")
                if job_id in seen_ids:
                    self.result.errors.append(f"fortinet_cx_duplicate_public_id:{job_id}")
                    continue
                seen_ids.add(job_id)
                self.add(self._job_from_listing(row, listing_url))
            if len(self.result.jobs) == before:
                self.result.errors.append(f"fortinet_cx_no_new_ids_at_offset_{offset}")
                break
            offset += len(rows)

        listing_seconds = time.perf_counter() - listing_started
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.metadata["listing_pages"] = pages
        self.result.metadata["source_total_at_first_page"] = expected_total
        self.result.metadata["source_total_reported"] = expected_total
        self.result.metadata["visible_ids_collected"] = len(seen_ids)

        if self.result.jobs:
            detail_started = time.perf_counter()

            async def detail(job):
                detail_url, content, row = await self._fetch_detail(job.job_id)
                if row.get("Title") != job.title:
                    raise ValueError(
                        f"Fortinet CX detail title does not match the exact listing ID {job.job_id}"
                    )
                job.raw["detail"] = row
                job.raw["detail_response_evidence"] = {
                    "url": detail_url,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "fetched_at": job.fetched_at,
                }
                if not all(key in row for key in BODY_FIELDS):
                    raise ValueError(
                        f"Fortinet CX detail for {job.job_id} omitted an inspected external "
                        "description field; schema drift requires manual review, not an "
                        "unavailable verdict"
                    )
                job.title = row.get("Title") or job.title
                job.requisition_id = str(row["RequisitionId"]) if row.get("RequisitionId") else None
                job.date_posted = row.get("ExternalPostedStartDate") or job.date_posted
                job.date_posted_raw = {
                    "listing_PostedDate": (job.raw.get("listing") or {}).get("PostedDate"),
                    "ExternalPostedStartDate": row.get("ExternalPostedStartDate"),
                }
                job.valid_through = row.get("ExternalPostedEndDate")
                job.locations = location_values(row) or job.locations
                job.department = row.get("Department") or row.get("Category") or job.department
                job.team = row.get("JobFunction") or row.get("JobFamily") or job.team
                job.employment_type = row.get("JobSchedule") or row.get("JobType") or job.employment_type
                job.workplace_type = (
                    row.get("WorkplaceType") or row.get("WorkplaceTypeCode") or job.workplace_type
                )
                job.salary = salary_fields(row) or job.salary
                job.qualifications = row.get("ExternalQualificationsStr") or None
                job.responsibilities = row.get("ExternalResponsibilitiesStr") or None
                job.education = row.get("StudyLevel")
                job.language = row.get("ContentLocale") or job.language
                job.employer = row.get("LegalEmployer")
                job.apply_url = apply_page_url(job.job_id)
                job.description_source_url = detail_url
                job.description_method = "fortinet.oracle_cx.external_sections"
                if role_fields_empty(row):
                    job.description_html = ""
                    job.description_unavailable = True
                    job.warnings.extend(["source_description_missing", "description_not_full_field"])
                    self.result.metadata["source_empty_roles"] = (
                        self.result.metadata.get("source_empty_roles", 0) + 1
                    )
                else:
                    job.description_html = detail_body_html(row)
                    body_text = text_html(job.description_html)
                    job.description_text = body_text
                    job.description_sha256 = (
                        hashlib.sha256(body_text.strip().encode()).hexdigest()
                        if body_text.strip() else None
                    )
                return job

            self.result.jobs = await self.hydrate(self.result.jobs, detail)
            self.result.timings["detail_wall_seconds"] = round(
                time.perf_counter() - detail_started, 6
            )

        membership_reconciled = (
            len(seen_ids) == expected_total
            and not any(error.startswith("fortinet_cx_") for error in self.result.errors)
        )
        self.result.listing_complete = membership_reconciled
        if not membership_reconciled and not any(
            error.startswith("fortinet_cx_total")
            or error.startswith("fortinet_cx_membership")
            for error in self.result.errors
        ):
            self.result.errors.append(
                f"fortinet_cx_membership_unreconciled:sourcereported={expected_total}"
                f",visible={len(seen_ids)}"
            )
            self.result.listing_complete = False

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": ENTRY_URL,
            "cx_board_url": CX_BOARD_URL,
            "cx_site_number": CX_SITE,
            "membership_authority": (
                "Fortinet official /corporate/careers page's published "
                '"Careers at Fortinet"/"Explore Careers at Fortinet" CTA to Oracle CX site '
                "CX_2001; its own public listing membership is the only authority"
            ),
            "listing_endpoint": f"{CX_ORIGIN}{LISTING_PATH}",
            "detail_endpoint": f"{CX_ORIGIN}{DETAILS_PATH}",
            "page_size": self.PAGE_SIZE,
            "ui_pagination": (
                "Board's /jobs page shows 12 cards with a 'Show More Results' button; "
                "each click requests findReqs at the next 12-row offset (12, 24, ...). "
                "The same offsets are walked here until the source-reported total is "
                "exhausted and the first empty page is returned."
            ),
            "page_end_condition": "first empty listing page after the reported total",
            "detail_finder": 'ById;Id="<ID>",siteNumber=CX_2001',
            "description_source": (
                "exact ById detail row's ExternalDescriptionStr, "
                "ExternalResponsibilitiesStr, ExternalQualificationsStr and "
                "CorporateDescriptionStr ('About Us'), as labeled sections; "
                "Short/Organization descriptions stay in raw only"
            ),
            "description_sections_rendered_by_board": [
                "JOB DESCRIPTION", "RESPONSIBILITIES", "QUALIFICATIONS", "ABOUT US"
            ],
            "federal_board_excluded": (
                "Entry also links 'Careers at Fortinet Federal' "
                "(https://www.fortinetfederal.com/jobs/#en/sites/CX_1), a distinct legal "
                "entity (Fortinet Federal, Inc.); not collected; its board was never queried"
            ),
            "apply_url_policy": (
                "The board's APPLY NOW button opens the exact job's "
                "…/job/<ID>/apply/email route; that public route is stored as apply_url. "
                "The job page …/job/<ID> remains the job URL."
            ),
            "page_count": len(pages),
            "collection_wall_seconds": round(
                time.perf_counter() - self.result._started_clock, 6
            ),
        })
        return self.result
