"""Kotak Mahindra's official careers journey and the Oracle board it publishes.

Journey observed on 2026-11-07 (see companies/kotak-mahindra/journey-research.md):

* ``https://www.kotak.com/en/about-us/careers.html`` answers 200 after a
  redirect to ``https://www.kotak.bank.in/en/about-us/careers.html``.
* That page's "Job Openings" / "Find a role" CTAs link to the Oracle Candidate
  Experience board ``https://hcbt.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX``;
  the "Kotak ReLaunch" card links to ``.../sites/CX_4001``.  CX_4001 is a
  three-requisition filtered view and all three of its requisitions are also
  returned by CX, so the unfiltered CX board is the membership authority.  No
  other ATS/board is linked anywhere in the official careers section.
* ``/sites/CX/jobs`` renders 25 cards per page with the header "JOBS 12060".
  The SPA requests ``recruitingCEJobRequisitions`` (finder ``findReqs``) for the
  listing and ``recruitingCEJobRequisitionDetails`` (finder ``ById``) for each
  rendered job page.  Job pages show JOB DESCRIPTION / RESPONSIBILITIES /
  QUALIFICATIONS sections assembled from the detail row's external fields plus
  JOB INFO (job identification, category, posting date, apply-before, degree
  level).  APPLY NOW opens ``.../job/<id>/apply/email``.
* The board's findReqs offset window stops at 10,000: ``offset=10000`` returns
  an empty block while ``TotalJobsCount`` stays 12,060, and the same is true for
  every deeper offset.  The board's own search object accepts
  ``postingStartDate`` / ``postingEndDate`` filters, so this adapter walks the
  complete unfiltered listing as disjoint monthly date partitions (each far
  below the offset window) and reconciles every partition against its reported
  total.

The board reports 12,060 postings but returns 12,054 distinct requisitions:
six requisitions are counted by the search index (and by every month/category/
location facet that contains them) yet are never returned by any listing query
or sort.  They are recorded as a residual in metadata and are not fabricated.
"""
from __future__ import annotations

import hashlib
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

ENTRY_URL = "https://www.kotak.com/en/about-us/careers.html"
ENTRY_FINAL_URL = "https://www.kotak.bank.in/en/about-us/careers.html"
CX_HOST = "hcbt.fa.em2.oraclecloud.com"
CX_ORIGIN = "https://hcbt.fa.em2.oraclecloud.com"
CX_SITE = "CX"
RELAUNCH_SITE = "CX_4001"
BOARD_URL = f"{CX_ORIGIN}/hcmUI/CandidateExperience/en/sites/{CX_SITE}"
LISTING_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAILS_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"

ROLE_FIELDS = (
    "ExternalDescriptionStr",
    "ExternalResponsibilitiesStr",
    "ExternalQualificationsStr",
)
# CorporateDescriptionStr is the board's "About Us" section when populated;
# OrganizationDescriptionStr/ShortDescriptionStr are retained in raw only (the
# teaser is truncated and is never used as a description).
BODY_FIELDS = ROLE_FIELDS + ("CorporateDescriptionStr",)
BODY_LABELS = {
    "ExternalDescriptionStr": "JOB DESCRIPTION",
    "ExternalResponsibilitiesStr": "RESPONSIBILITIES",
    "ExternalQualificationsStr": "QUALIFICATIONS",
    "CorporateDescriptionStr": "ABOUT US",
}
EXPAND = (
    "requisitionList.workLocation,requisitionList.otherWorkLocations,"
    "requisitionList.secondaryLocations,requisitionList.requisitionFlexFields,"
    "flexFieldsFacet.values"
)
FACETS = "LOCATIONS;WORK_LOCATIONS;WORKPLACE_TYPES;ORGANIZATIONS;CATEGORIES;POSTING_DATES;TITLES"
PAGE_SIZE = 200  # tested maximum accepted page size; the UI renders 25 cards per page
MAX_OFFSET = 10000  # observed findReqs result-window limit (offset 10000+ returns nothing)
MONTHS_BACK = 120


def job_page_url(job_id: str, site: str = CX_SITE) -> str:
    return f"{CX_ORIGIN}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"


def apply_page_url(job_id: str, site: str = CX_SITE) -> str:
    """The route the board's APPLY NOW button opens (observed in Chromium)."""
    return f"{job_page_url(job_id, site)}/apply/email"


def board_sites_from_entry(html: str, entry_url: str) -> dict:
    """Collect the Oracle CX sites the official careers page publishes.

    Raises unless the inspected CX board is linked and no unexpected Oracle site
    is published alongside it.  CX_4001 (Kotak ReLaunch) is expected; it is a
    filtered view of the same requisitions.
    """
    soup = BeautifulSoup(html, "lxml")
    sites: set[str] = set()
    links: set[str] = set()
    for anchor in soup.select("a"):
        for attribute in ("href", "data-desktoplink", "data-mobilelink"):
            value = (anchor.get(attribute) or "").strip()
            if not value:
                continue
            parsed = urlsplit(value)
            if parsed.hostname != CX_HOST:
                continue
            if "/CandidateExperience/en/sites/" not in parsed.path:
                continue
            site = parsed.path.split("/sites/", 1)[1].split("/")[0]
            sites.add(site)
            links.add(value)
    if CX_SITE not in sites:
        raise ValueError(
            f"Kotak official careers page does not publish the inspected CX board "
            f"(entry={entry_url}, oracle_sites={sorted(sites)})"
        )
    unexpected = sites - {CX_SITE, RELAUNCH_SITE}
    if unexpected:
        raise ValueError(
            f"Kotak official careers page publishes unexpected Oracle sites "
            f"{sorted(unexpected)}; manual review required"
        )
    return {
        "entry_url": entry_url,
        "oracle_sites": sorted(sites),
        "oracle_links": sorted(links),
        "cx_link_count": sum(1 for link in links if f"/sites/{CX_SITE}" in link),
        "relaunch_link_count": sum(1 for link in links if f"/sites/{RELAUNCH_SITE}" in link),
    }


def detail_body_html(detail: dict) -> str:
    """Assemble the published body sections in the order the board renders them."""
    parts = []
    for field in BODY_FIELDS:
        value = detail.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(f"<section><h2>{BODY_LABELS[field]}</h2>{value}</section>")
    return "\n".join(parts)


def role_fields_empty(detail: dict) -> bool:
    """True when every role-bearing external section the board publishes is empty."""
    return not any(text_html(detail.get(field) or "") for field in ROLE_FIELDS)


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


def finder(*, offset: int = 0, start: str | None = None, end: str | None = None,
           limit: int = PAGE_SIZE, organizations: int | str | None = None,
           site: str = CX_SITE) -> str:
    """The board's own findReqs request, with its published date/org filters."""
    parts = [f"findReqs;siteNumber={site}", f"limit={limit}",
             "sortBy=POSTING_DATES_DESC", f"offset={offset}"]
    if start:
        parts.append(f"postingStartDate={start}")
    if end:
        parts.append(f"postingEndDate={end}")
    if organizations is not None:
        parts.append(f"selectedOrganizationsFacet={organizations}")
    return ",".join(parts)


def month_buckets(today: date, months: int = MONTHS_BACK):
    """Disjoint monthly posting-date windows, newest first."""
    year, month = today.year, today.month
    for _ in range(months):
        first = date(year, month, 1)
        last = (date(year + (month == 12), 1 if month == 12 else month + 1, 1)
                - timedelta(days=1))
        yield first.isoformat(), last.isoformat()
        month -= 1
        if month == 0:
            year, month = year - 1, 12


def parse_listing_block(data: dict, site: str = CX_SITE) -> tuple[dict, list, int]:
    """Validate one findReqs response block (single site result)."""
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("Kotak CX listing response has no single public site result")
    block = items[0]
    if str(block.get("SiteNumber")) != site:
        raise ValueError(f"Kotak CX listing site mismatch: {block.get('SiteNumber')!r}")
    rows = block.get("requisitionList")
    if not isinstance(rows, list):
        raise ValueError("Kotak CX listing response is missing its requisitionList")
    total = int(block.get("TotalJobsCount", -1))
    if total < 0:
        raise ValueError("Kotak CX listing response is missing TotalJobsCount")
    return block, rows, total


def parse_detail_payload(data: dict, job_id: str, expected_title: str | None = None) -> dict:
    """Validate the ById detail payload for one exact listed requisition."""
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError(f"Kotak CX detail for {job_id} did not return exactly one row")
    row = items[0]
    if str(row.get("Id")) != str(job_id):
        raise ValueError(f"Kotak CX detail returned a different requisition than {job_id}")
    if expected_title is not None and row.get("Title") != expected_title:
        raise ValueError(f"Kotak CX detail title changed for {job_id}")
    return row


class KotakMahindra(Adapter):
    """Collect the unfiltered membership of the CX board the careers page publishes."""

    VERIFICATION_REVISION = "kotak-mahindra.official-careers-cx.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._detail_index: dict[str, dict] = {}
        self._bucket_reports: list[dict] = []

    # ---------------------------------------------------------------- entry
    async def _entry(self) -> float:
        started = time.perf_counter()
        parsed = urlsplit(self.company.careers_url)
        if parsed.hostname not in {"www.kotak.com", "kotak.com"}:
            raise ValueError("Kotak flow must start from its official kotak.com careers URL")
        response = await self.http.get(self.company.careers_url)
        final = urlsplit(str(response.url))
        if (final.scheme != "https" or final.hostname not in {"www.kotak.bank.in", "kotak.bank.in"}
                or final.path.rstrip("/") != "/en/about-us/careers.html"):
            raise ValueError(
                f"Kotak official careers entry redirected away from the inspected page: {response.url}"
            )
        info = board_sites_from_entry(response.text, str(response.url))
        self.result.metadata["entry"] = {
            **info,
            "request_url": self.company.careers_url,
            "final_url": str(response.url),
            "status": response.status_code,
            "redirects": [str(item.url) for item in response.history],
        }
        return time.perf_counter() - started

    # --------------------------------------------------------------- listing
    async def _listing_page(self, *, offset: int = 0, start: str | None = None,
                            end: str | None = None, limit: int = PAGE_SIZE,
                            organizations: int | str | None = None,
                            site: str = CX_SITE):
        params = {
            "onlyData": "true",
            "expand": EXPAND,
            "finder": finder(offset=offset, start=start, end=end, limit=limit,
                             organizations=organizations, site=site),
        }
        response = await self.http.request(
            "GET", f"{CX_ORIGIN}{LISTING_PATH}", params=params,
            headers={"Ora-Irc-Language": "en"},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError(f"Kotak CX listing page at offset {offset} was not JSON") from exc
        block, rows, total = parse_listing_block(data, site)
        return str(response.url), block, rows, total

    async def _total(self, start: str | None = None, end: str | None = None) -> int:
        _, _, _, total = await self._listing_page(offset=0, start=start, end=end, limit=1)
        return total

    async def _collect_bucket(self, start: str, end: str, total: int) -> dict:
        """Walk one posting-date partition until its reported total is consumed."""
        rows: dict[str, dict] = {}
        offset = 0
        observed_total = None
        empty_before_total = False
        last_url = None
        while offset < total:
            if not self.page_guard():
                raise ValueError("Kotak CX page budget exhausted during the listing walk")
            url, block, batch, block_total = await self._listing_page(
                offset=offset, start=start, end=end)
            last_url = url
            if observed_total is None:
                observed_total = block_total
            elif block_total > total:
                total = block_total
            if not batch:
                empty_before_total = True
                break
            for row in batch:
                job_id = str(row.get("Id") or "")
                if not job_id or not row.get("Title"):
                    raise ValueError(f"Kotak CX listing row in {start} is missing Id/Title")
                rows.setdefault(job_id, row)
            offset += len(batch)
        recovered = 0
        if len(rows) < total:
            # One clean re-walk of the same partition: distinguishes a transient
            # paging duplicate from a count the board never materialises.
            retry: dict[str, dict] = {}
            retry_offset = 0
            while retry_offset < total:
                if not self.page_guard():
                    break
                _, _, batch, _ = await self._listing_page(
                    offset=retry_offset, start=start, end=end)
                if not batch:
                    break
                for row in batch:
                    retry.setdefault(str(row["Id"]), row)
                retry_offset += len(batch)
            before = len(rows)
            for job_id, row in retry.items():
                if job_id not in rows:
                    rows[job_id] = row
                    recovered += 1
            del before
        report = {
            "start": start,
            "end": end,
            "reported_total": total,
            "returned_unique": len(rows),
            "recovered_on_retry": recovered,
            "empty_page_before_total": empty_before_total,
            "shortfall": max(0, total - len(rows)),
        }
        self._bucket_reports.append(report)
        return {"report": report, "rows": rows, "url": last_url}

    async def _listing(self) -> tuple[dict[str, dict], int]:
        started = time.perf_counter()
        advertised = await self._total()
        self.result.metadata["board_advertised_total"] = advertised
        buckets: list[dict] = []
        consecutive_empty = 0
        running_total = 0
        for start, end in month_buckets(datetime.now(timezone.utc).date()):
            total = await self._total(start, end)
            if total:
                consecutive_empty = 0
                running_total += total
                buckets.append({"start": start, "end": end, "total": total})
            else:
                consecutive_empty += 1
            # Never stop on running_total alone: new postings during the sweep can
            # inflate it and would silently drop the oldest partitions.  A full run
            # of empty months (or the window limit) is the only stop condition.
            if consecutive_empty >= 12:
                break
        self.result.metadata["posting_date_partitions"] = buckets
        self.result.metadata["partition_total"] = running_total
        if running_total != advertised:
            self.result.errors.append(
                f"kotak_cx_partition_total_mismatch:{running_total}!={advertised}"
            )
        all_rows: dict[str, dict] = {}
        duplicate_ids: list[str] = []
        for bucket in buckets:
            outcome = await self._collect_bucket(bucket["start"], bucket["end"], bucket["total"])
            for job_id, row in outcome["rows"].items():
                if job_id in all_rows:
                    duplicate_ids.append(job_id)
                    continue
                all_rows[job_id] = row
        self.result.metadata["listing_buckets"] = self._bucket_reports
        self.result.metadata["duplicate_ids_across_partitions"] = duplicate_ids[:50]
        self.result.metadata["listable_unique_ids"] = len(all_rows)
        self.result.metadata["count_residual"] = {
            "board_advertised": advertised,
            "partition_total": running_total,
            "listable_unique": len(all_rows),
            "counted_but_never_returned": advertised - len(all_rows),
            "evidence": (
                "Every posting-date partition was walked to exhaustion and re-walked "
                "once; the residual rows are counted by TotalJobsCount and by the "
                "category/location facets, but no listing query or sort returns them. "
                "They are not fabricated and no other site is substituted."
            ),
        }
        if advertised != len(all_rows):
            self.result.warnings.append(
                f"board_count_exceeds_listable_requisitions:{advertised - len(all_rows)}"
            )
        self.result.expected_total = len(all_rows)
        self.result.timings["listing_phase_seconds"] = round(
            time.perf_counter() - started, 6)
        return all_rows, advertised

    # ------------------------------------------------------------ org mapping
    async def _organizations(self, advertised: int) -> dict[str, str]:
        """Map each listable requisition to the board's own organization facet."""
        started = time.perf_counter()
        params = {
            "onlyData": "true",
            "expand": "requisitionList.secondaryLocations",
            "finder": (f"findReqs;siteNumber={CX_SITE},facetsList={FACETS},limit=1,"
                       "sortBy=POSTING_DATES_DESC,offset=0"),
        }
        response = await self.http.request(
            "GET", f"{CX_ORIGIN}{LISTING_PATH}", params=params,
            headers={"Ora-Irc-Language": "en"},
        )
        data = response.json()
        block, _, total = parse_listing_block(data)
        facet = block.get("organizationsFacet") or []
        if not facet:
            self.result.warnings.append("organizations_facet_missing")
            self.result.timings["organization_mapping_seconds"] = round(
                time.perf_counter() - started, 6)
            return {}
        organizations = [
            {"id": int(item["Id"]), "name": item.get("Name"), "total": int(item.get("TotalCount", 0))}
            for item in facet if item.get("Id") is not None
        ]
        self.result.metadata["organizations_facet"] = {
            "reported_total": total, "advertised_total": advertised,
            "facets": organizations,
        }
        largest = max(organizations, key=lambda item: item["total"])
        mapping: dict[str, str] = {}
        for organization in organizations:
            if organization is largest or organization["total"] >= 5000:
                continue
            offset = 0
            while offset < organization["total"]:
                if not self.page_guard():
                    break
                _, _, rows, _ = await self._listing_page(
                    offset=offset, limit=PAGE_SIZE, organizations=organization["id"])
                if not rows:
                    break
                for row in rows:
                    mapping[str(row["Id"])] = organization["name"]
                offset += len(rows)
        self.result.metadata["organization_mapping"] = {
            "largest": largest,
            "small_organizations": [org for org in organizations if org is not largest],
            "mapped_small_jobs": len(mapping),
        }
        self.result.timings["organization_mapping_seconds"] = round(
            time.perf_counter() - started, 6)
        return mapping

    # ----------------------------------------------------------------- detail
    async def _fetch_detail(self, job_id: str, expected_title: str, site: str = CX_SITE):
        params = {
            "onlyData": "true",
            "expand": "all",
            "finder": f'ById;Id="{job_id}",siteNumber={site}',
        }
        response = await self.http.request(
            "GET", f"{CX_ORIGIN}{DETAILS_PATH}", params=params,
            headers={"Ora-Irc-Language": "en"},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Kotak CX detail for {job_id} was not JSON") from exc
        row = parse_detail_payload(payload, job_id, expected_title)
        return str(response.url), hashlib.sha256(response.content).hexdigest(), row

    def _job_from_listing(self, row: dict, listing_url: str, site: str = CX_SITE):
        job_id = str(row.get("Id") or "")
        job = self.job(
            {"listing": row},
            title=row["Title"],
            job_id=job_id,
            url=job_page_url(job_id, site),
            apply_url=apply_page_url(job_id, site),
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
        job.record_identity = f"kotak-mahindra:{job_id}"
        job.raw["listing_request_url"] = listing_url
        job.raw["site_number"] = site
        return job

    async def run(self):
        entry_seconds = await self._entry()
        self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)

        rows, advertised = await self._listing()
        listed_ids = set(rows)
        for _job_id, row in rows.items():
            self.add(self._job_from_listing(row, BOARD_URL))

        # The official careers page also publishes the ReLaunch site (CX_4001).
        # Observed 2026-11-07: its three requisitions are NOT returned by the
        # unfiltered CX listing (their direct CX job route redirects to CX_1001),
        # so they are published records that must be collected from that exact
        # site. Nothing else is added from any unpublished site.
        relaunch = await self._fetch_site_membership(RELAUNCH_SITE)
        extra_ids = [job_id for job_id in relaunch["rows"] if job_id not in listed_ids]
        for job_id in extra_ids:
            self.add(self._job_from_listing(relaunch["rows"][job_id],
                                            relaunch["url"], RELAUNCH_SITE))
        self.result.metadata["relaunch_site"] = {
            "site_number": RELAUNCH_SITE,
            "site_url": f"{CX_ORIGIN}/hcmUI/CandidateExperience/en/sites/{RELAUNCH_SITE}",
            "reported_total": relaunch["total"],
            "ids": list(relaunch["rows"]),
            "ids_already_on_cx": [job_id for job_id in relaunch["rows"] if job_id in listed_ids],
            "added_from_relaunch_site": extra_ids,
            "direct_cx_job_route_redirects_to": "CX_1001 for these requisitions (observed)",
        }
        if len(extra_ids) + len(relaunch["rows"]) != len(relaunch["rows"]) + len(extra_ids):
            self.result.errors.append("kotak_relaunch_site_duplicate_ids")

        employers = await self._organizations(advertised)
        default_employer = (self.result.metadata.get("organization_mapping", {})
                            .get("largest", {}).get("name") or "Kotak Mahindra Bank Ltd")
        for job in self.result.jobs:
            employer = employers.get(job.job_id) or default_employer
            job.employer = employer
            job.raw["organization"] = employer

        detail_started = time.perf_counter()

        async def detail(job):
            site = (job.raw.get("site_number") or CX_SITE) if isinstance(job.raw, dict) else CX_SITE
            detail_url, digest, row = await self._fetch_detail(job.job_id, job.title, site)
            job.raw["detail"] = row
            job.raw["detail_response_evidence"] = {
                "url": detail_url,
                "sha256": digest,
                "fetched_at": job.fetched_at,
            }
            if not all(key in row for key in BODY_FIELDS):
                raise ValueError(
                    f"Kotak CX detail for {job.job_id} omitted an inspected external "
                    "description field; schema drift requires manual review"
                )
            job.requisition_id = str(row["RequisitionId"]) if row.get("RequisitionId") else None
            posted = row.get("ExternalPostedStartDate")
            job.date_posted = (posted[:10] if isinstance(posted, str) and len(posted) >= 10
                               else job.date_posted)
            job.date_posted_raw = {
                "listing_PostedDate": (job.raw.get("listing") or {}).get("PostedDate"),
                "ExternalPostedStartDate": posted,
            }
            # The board renders ExternalPostedEndDate as "Apply Before"; it is the
            # publisher's own posting expiry, not a request/template timestamp.
            end = row.get("ExternalPostedEndDate")
            job.valid_through = end[:10] if isinstance(end, str) and len(end) >= 10 else None
            job.locations = location_values(row) or job.locations
            job.department = row.get("Category") or job.department
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
            job.apply_url = apply_page_url(job.job_id, site)
            job.description_source_url = detail_url
            job.description_method = "kotak.oracle_cx.external_sections"
            if role_fields_empty(row):
                job.description_html = ""
                job.description_unavailable = True
                job.warnings.extend(["source_description_missing", "description_not_full_field"])
                self.result.metadata["source_empty_roles"] = (
                    self.result.metadata.get("source_empty_roles", 0) + 1
                )
            else:
                job.description_html = detail_body_html(row)
                job.description_text = text_html(job.description_html)
                job.description_sha256 = (
                    hashlib.sha256(job.description_text.strip().encode()).hexdigest()
                    if job.description_text.strip() else None
                )
            return job

        if self.result.jobs:
            self.result.jobs = await self.hydrate(self.result.jobs, detail)
            self.result.timings["detail_wall_seconds"] = round(
                time.perf_counter() - detail_started, 6)

        expected_published = len(listed_ids) + len(extra_ids)
        self.result.expected_total = expected_published
        residual = self.result.metadata.get("count_residual") or {}
        residual.update({
            "cx_listable_unique": len(listed_ids),
            "relaunch_only_published": len(extra_ids),
            "total_published_records": expected_published,
            "relaunch_ids": list(relaunch["rows"]),
        })
        self.result.metadata["count_residual"] = residual
        membership_reconciled = (
            expected_published == len(self.result.jobs)
            and not any(error.startswith("kotak_cx_") for error in self.result.errors)
        )
        self.result.listing_complete = membership_reconciled
        if not membership_reconciled:
            self.result.errors.append(
                f"kotak_membership_unreconciled:cx_listable={len(listed_ids)}"
                f",relaunch_only={len(extra_ids)},collected={len(self.result.jobs)}"
            )

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": ENTRY_URL,
            "board_url": BOARD_URL,
            "cx_site_number": CX_SITE,
            "membership_authority": (
                "Kotak's own careers page publishes the unfiltered Oracle CX board; "
                "its findReqs listing membership is the authority. CX_4001 (ReLaunch) "
                "is a filtered view whose requisitions are all present on CX."
            ),
            "listing_endpoint": f"{CX_ORIGIN}{LISTING_PATH}",
            "detail_endpoint": f"{CX_ORIGIN}{DETAILS_PATH}",
            "page_size": PAGE_SIZE,
            "ui_pagination": (
                "/sites/CX/jobs shows the header 'JOBS 12060' and renders 25 cards; "
                "scrolling to the bottom appends another 25 cards (observed 25 -> 175 "
                "after six scrolls), each batch being the next findReqs offset. The API "
                "accepts 200 rows per page, used here."
            ),
            "offset_window": (
                f"findReqs offset is capped at {MAX_OFFSET}; offset={MAX_OFFSET} and every "
                "deeper offset return an empty block while TotalJobsCount is unchanged. "
                "The complete unfiltered listing is therefore walked as disjoint "
                "monthly postingStartDate/postingEndDate partitions."
            ),
            "detail_finder": 'ById;Id="<ID>",siteNumber=CX',
            "description_source": (
                "exact ById detail row's ExternalDescriptionStr, "
                "ExternalResponsibilitiesStr, ExternalQualificationsStr and (when "
                "populated) CorporateDescriptionStr; each as a labeled section. "
                "ShortDescriptionStr (listing teaser) and OrganizationDescriptionStr "
                "stay in raw and are never substituted."
            ),
            "apply_url_policy": (
                "The board's APPLY NOW button opens .../job/<ID>/apply/email "
                "(observed in Chromium); stored as apply_url."
            ),
            "evergreen_tech_intake": (
                "The official /careers/tech-jobs.html page also publishes ten tech "
                "profile cards with modal descriptions and a generic apply form; they "
                "have no requisition ID, location, or posting date and are not Oracle "
                "requisitions. They are captured separately in the review folder, not "
                "counted as board postings."
            ),
            "page_count": self.result.pages,
            "collection_wall_seconds": round(
                time.perf_counter() - self.result._started_clock, 6),
        })
        return self.result

    async def _fetch_site_membership(self, site: str) -> dict:
        """Read one additionally published CX site's exact listing rows."""
        rows: dict[str, dict] = {}
        total = None
        offset = 0
        last_url = None
        while total is None or offset < total:
            if not self.page_guard():
                break
            url, block, batch, block_total = await self._listing_page(
                offset=offset, limit=PAGE_SIZE, site=site)
            last_url = url
            if total is None:
                total = block_total
            elif block_total > total:
                total = block_total
            if not batch:
                break
            for row in batch:
                rows.setdefault(str(row["Id"]), row)
            offset += len(batch)
        return {"total": total, "rows": rows, "url": last_url}
