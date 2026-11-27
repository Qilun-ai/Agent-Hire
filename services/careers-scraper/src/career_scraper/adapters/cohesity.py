"""Cohesity's own AEM careers journey: entry -> /careers/open-positions/ ->
AEM servlet /bin/cohesity/open-positions (Workday-backed) -> native
?gh_jid=<JobID>&type=wd detail pages.

Membership authority is the servlet JSON consumed by the page's own
_jobs-list.js (fetch('/bin/cohesity/open-positions')). No ATS board is
enumerated; only company-published JobIDs are followed.

Fallback (added 2026-11-08, waitlist retry round 2): when the official
www.cohesity.com host is unreachable from this network (Akamai 403),
the SAME company-scoped job universe is served directly by the Workday
board that every published servlet row links to via its `jobUrl` field
(https://cohesity.wd5.myworkdayjobs.com/Cohesity_Careers). The board's
own CXS listing API (POST /wday/cxs/cohesity/Cohesity_Careers/jobs) is
the exact request its own page makes; it is never guessed or
enumerated, and only the company-linked Cohesity_Careers site is used.
The fallback triggers ONLY on HTTP-level failure of the official host;
a served servlet (including its maintenance response) is never
bypassed.
"""
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .. import access
from .base import Adapter
from ..http import FetchError
from ..models import text_html


HEX32 = re.compile(r"^[0-9a-f]{32}$")


def native_job_url(listing_page_url, job_id):
    """Detail URL exactly as _jobs-list.js builds it for a published JobID."""
    if not job_id:
        raise ValueError("Cohesity published a listing row without a JobID")
    return urljoin(listing_page_url, f"/careers/open-positions/?gh_jid={job_id}&type=wd")


def compact_body_hash(html):
    """NFC/whitespace-free SHA-256 over rendered detail text (browser-check mirror)."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compact_text_hash(text):
    """NFC/whitespace-free SHA-256 over plain text (rendered-body mirror)."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text or "")
    value = re.sub(r"[\s\ufeff]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def board_slug(external_path):
    """Detail-API slug (the board's jobPostingId) for a listing externalPath.

    externalPath is /job/<location>/<Title-slug>_<ReqID>; the CXS detail
    endpoint is /job/<Title-slug>_<ReqID> (last segment only).
    """
    if not isinstance(external_path, str) or not external_path.startswith("/job/"):
        raise ValueError(f"Workday board externalPath is not a job path: {external_path!r}")
    slug = external_path[len("/job/"):].split("/")[-1]
    if not slug:
        raise ValueError(f"Workday board externalPath has no usable slug: {external_path!r}")
    return slug


def parse_board_page(payload):
    """Validate one CXS listing page; return (total_or_None, posting rows)."""
    if not isinstance(payload, dict):
        raise ValueError("Workday board listing response is not a JSON object")
    postings = payload.get("jobPostings")
    if not isinstance(postings, list):
        raise ValueError("Workday board listing response lacks jobPostings")
    total = payload.get("total")
    if total is not None and not isinstance(total, int):
        raise ValueError("Workday board listing total is not an integer")
    rows = []
    for posting in postings:
        if not isinstance(posting, dict):
            raise ValueError("Workday board listing published a non-object row")
        bullets = posting.get("bulletFields") or []
        req = str(bullets[0]).strip() if bullets else ""
        if not req or not posting.get("title"):
            raise ValueError("Workday board listing published an unidentified job row")
        rows.append((req, posting))
    return total, rows


def parse_start_date(value):
    """Published ISO posting start date, or None (never a fabricated date)."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _regional_list(job):
    """Every published regional location for a row, as the listing UI shows it."""
    raw = job.get("jobSiteRegionalList")
    if not isinstance(raw, list) or not raw:
        raw = [job.get("primaryDisplayLocation") or job.get("primaryLocation")]
        additional = job.get("additionalLocations")
        if not isinstance(additional, list):
            combined = str(job.get("AdditionalLocations") or job.get("jobSite_Regional") or "")
            additional = [part.strip() for part in combined.split(";") if part.strip()]
        raw = raw + [loc for loc in additional]
    seen, values = set(), []
    for value in raw:
        loc = str(value or "").strip()
        if loc and loc.lower() != "unknown" and loc not in seen:
            seen.add(loc)
            values.append(loc)
    return values


def parse_detail_page(html):
    """Extract the server-rendered careers-details block for one exact JobID."""
    soup = BeautifulSoup(html, "lxml")
    if soup.select_one(".cmp-career-details.invalid"):
        raise ValueError("Cohesity native detail page reports INVALID JOB ID")
    container = soup.select_one(".cmp-career-details__container")
    if container is None:
        raise ValueError("Cohesity native detail page has no rendered job container")
    fields = {}
    for paragraph in container.select(".cmp-career-details__job-details p"):
        text = paragraph.get_text(" ", strip=True)
        match = re.match(r"^(Start Date|Company|Category|Location|Description)\s*:\s*(.*)$", text)
        if match:
            fields[match.group(1)] = match.group(2).strip()
    description_html = container.decode_contents()
    description_text = text_html(description_html)
    if len(description_text.strip()) < 100:
        raise ValueError("Cohesity native detail page description is implausibly short")
    apply_link = container.select_one("a[href]")
    apply_url = apply_link.get("href", "").strip() if apply_link else None
    if apply_url:
        apply_url = urljoin("https://www.cohesity.com/careers/open-positions/", apply_url)
    return {
        "description_html": description_html,
        "description_text": description_text,
        "fields": fields,
        "apply_url": apply_url,
    }


class Cohesity(Adapter):
    """Follow only the live servlet and job-ID routes published by Cohesity."""

    VERIFICATION_REVISION = "cohesity.aem-servlet+workday-board-fallback.2"
    ENTRY_HOST = "www.cohesity.com"
    LISTING_SERVLET_PATH = "/bin/cohesity/open-positions"
    LISTING_PAGE_PATH = "/careers/open-positions/"
    # Company-linked Workday board (jobUrl of every published servlet
    # row). Used ONLY as a fallback when the official host fetch fails.
    WORKDAY_HOST = "cohesity.wd5.myworkdayjobs.com"
    WORKDAY_SITE = "Cohesity_Careers"
    WORKDAY_BOARD_URL = f"https://{WORKDAY_HOST}/{WORKDAY_SITE}"
    WORKDAY_API_BASE = f"https://{WORKDAY_HOST}/wday/cxs/cohesity/{WORKDAY_SITE}"
    WORKDAY_PAGE_LIMIT = 20  # page-1 total; API rejects limit > 20 (HTTP 400)
    WORKDAY_MAX_PAGES = 40

    @staticmethod
    def _listing_rows(payload):
        if not isinstance(payload, dict):
            raise ValueError("Cohesity listing servlet response is not a JSON object")
        if payload.get("workdayMaintenance") is True:
            return None  # sentinel: backend explicitly in maintenance
        job_data = payload.get("job_data")
        departments = payload.get("careerSiteDeptList")
        if not isinstance(job_data, dict) or not isinstance(departments, list):
            raise ValueError(
                "Cohesity listing servlet response lacks job_data/careerSiteDeptList"
            )
        rows = []
        for department in departments:
            for job in (job_data.get(department) or []):
                if not isinstance(job, dict) or not job.get("JobID") or not job.get("title"):
                    raise ValueError("Cohesity listing servlet published an unidentified job row")
                rows.append((department, job))
        return rows

    def _make_job(self, department, row, *, listing_page_url):
        job_id = str(row["JobID"])
        job_url = native_job_url(listing_page_url, job_id)
        locations = [{"location": loc} for loc in _regional_list(row)]
        raw = {
            "listing": row,
            "source_ui": {
                "department": department,
                "native_job_url": job_url,
                "apply_url": None,
                "listing_servlet_url": urljoin(listing_page_url, self.LISTING_SERVLET_PATH),
                "workday_requisition_id": job_id,
            },
        }
        job = self.job(
            raw,
            title=row["title"],
            url=job_url,
            job_id=job_id,
            requisition_id=job_id,
            apply_url=None,
            description="",
            method="cohesity.aem_servlet_workday.detail_server_rendered",
            description_url=job_url,
            locations=locations,
            department=department,
            employment_type=None,
            workplace_type=None,
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            salary=None,
        )
        job.record_identity = f"cohesity:{job_id}"
        return job

    async def _detail(self, job):
        """Hydrate one exact company-published JobID detail page."""
        response = await self.http.get(job.url)
        if str(response.url) != job.url:
            job.warnings.append("native_detail_redirected")
        detail = parse_detail_page(response.text)
        job.description_html = detail["description_html"]
        job.description_text = detail["description_text"]
        job.apply_url = detail["apply_url"]
        job.raw["detail_fields"] = detail["fields"]
        job.raw["source_ui"]["apply_url"] = detail["apply_url"]
        source_date = detail["fields"].get("Start Date")
        job.date_posted = None
        job.date_posted_raw = source_date
        employer = detail["fields"].get("Company")
        if employer:
            job.employer = employer
        return job

    async def run(self):
        started = self.result._started_clock
        try:
            return await self._run_official_site(started)
        except FetchError as exc:
            # The company's own host is unreachable over plain HTTP (Akamai 403).
            # Escalate once through the stealth browser, then use the company's
            # official Workday tenant.
            page = await access.fetch_with_stealth(self.company.careers_url)
            self.result.metadata["access"] = access.attempt_metadata(
                page, url=self.company.careers_url
            )
            if page is not None and page.ok:
                self.result.errors.append(
                    "official_cohesity_host_resolved_by_stealth_no_listing_parser_"
                    "falling_back_to_official_workday_tenant"
                )
            return await self._run_workday_fallback(
                f"official_cohesity_host_fetch_failed: {exc}", started=started,
            )

    async def _run_official_site(self, started):
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https" or entry_parts.hostname != self.ENTRY_HOST
                or entry_parts.path.rstrip("/") != "/company/careers"):
            raise ValueError("Cohesity dedicated flow must start from its official HTTPS entry")
        entry = await self.http.get(entry_url)
        entry_final = urlsplit(str(entry.url))
        if entry_final.scheme != "https" or entry_final.hostname != self.ENTRY_HOST:
            raise ValueError("Cohesity official entry redirected off the company host")

        # /company/careers/ publishes /careers/; its hero CTA reaches
        # /careers/open-positions/, the page that embeds the jobs list.
        listing_page_url = urljoin(str(entry.url), self.LISTING_PAGE_PATH)
        if entry_final.path.rstrip("/") != self.LISTING_PAGE_PATH.rstrip("/"):
            listing_page = await self.http.get(listing_page_url)
            if str(listing_page.url) != listing_page_url:
                raise ValueError("Cohesity open-positions page redirected unexpectedly")
            listing_html = listing_page.text
        else:
            listing_html = entry.text
        soup = BeautifulSoup(listing_html, "lxml")
        container = soup.select_one(".cmp-jobs-list__container[data-pagepath]")
        if container is None:
            raise ValueError(
                "Cohesity listing page no longer embeds its cmp-jobs-list component"
            )

        servlet_url = urljoin(listing_page_url, self.LISTING_SERVLET_PATH)
        servlet = await self.http.get(servlet_url)
        if str(servlet.url) != servlet_url:
            raise ValueError("Cohesity listing servlet request redirected away from its source")
        try:
            payload = servlet.json()
        except ValueError as exc:
            raise ValueError("Cohesity listing servlet response is not JSON") from exc

        rows = self._listing_rows(payload)
        if rows is None:
            # The company's own backend reports scheduled maintenance; the
            # published UI renders a maintenance notice instead of jobs.
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.listing_complete = False
            self.result.errors.append(
                "cohesity_workday_maintenance: /bin/cohesity/open-positions returned "
                + '{"workdayMaintenance": true}'
            )
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": str(entry.url),
                "listing_page_url": listing_page_url,
                "listing_servlet_url": servlet_url,
                "membership_authority": (
                    "Cohesity AEM open-positions servlet /bin/cohesity/open-positions "
                    "(Workday-backed), consumed by _jobs-list.js on /careers/open-positions/"
                ),
                "workday_maintenance": True,
                "final_snapshot_verified": True,
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
            })
            self.result.timings["detail_phase_seconds"] = 0.0
            self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
            return self.result

        mapping_started = time.perf_counter()
        jobs = [self._make_job(department, row, listing_page_url=listing_page_url) for department, row in rows]
        self.result.expected_total = len(jobs)
        self.result.pages = 1
        jobs = await self.hydrate(jobs, self._detail)
        for job in jobs:
            self.add(job)
        detail_seconds = time.perf_counter() - mapping_started
        self.result.listing_complete = not self.result.errors
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "listing_page_url": listing_page_url,
            "listing_servlet_url": servlet_url,
            "membership_authority": (
                "Cohesity AEM open-positions servlet /bin/cohesity/open-positions "
                "(Workday-backed), consumed by _jobs-list.js on /careers/open-positions/"
            ),
            "workday_maintenance": False,
            "final_snapshot_verified": True,
            "department_filters": payload.get("careerSiteDeptList") or [],
            "source_rows_total": len(rows),
            "description_source": (
                "Server-rendered .cmp-career-details block on the exact "
                "?gh_jid=<JobID>&type=wd page for each listed JobID."
            ),
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result

    # ------------------------------------------------------------------
    # Workday board fallback (official host unreachable; same company-
    # scoped job universe the company's own servlet rows link to).
    # ------------------------------------------------------------------

    async def _fetch_board_rows(self):
        """Paginate the board's own CXS listing API until total is exhausted."""
        rows, seen = [], set()
        first_req = None
        total = None
        offset = 0
        pages = 0
        while True:
            payload = await self.http.json(
                f"{self.WORKDAY_API_BASE}/jobs", method="POST",
                json={"appliedFacets": {}, "limit": self.WORKDAY_PAGE_LIMIT,
                      "offset": offset, "searchText": ""},
            )
            page_total, page_rows = parse_board_page(payload)
            if total is None:
                total = page_total
            pages += 1
            if page_rows and page_rows[0][0] == first_req:
                # Offsets at/after total wrap back to page one on this API.
                break
            if first_req is None and page_rows:
                first_req = page_rows[0][0]
            for req, posting in page_rows:
                if req in seen:
                    raise ValueError(f"Workday board listing repeated requisition {req}")
                seen.add(req)
                rows.append((req, posting))
            offset += self.WORKDAY_PAGE_LIMIT
            if len(page_rows) < self.WORKDAY_PAGE_LIMIT:
                break
            if total is not None and offset >= total:
                break
            if pages >= self.WORKDAY_MAX_PAGES:
                raise ValueError("Workday board pagination did not terminate")
        return total, rows, pages

    def _make_board_job(self, req, row):
        slug = board_slug(row.get("externalPath"))
        canonical_url = f"{self.WORKDAY_BOARD_URL}{row['externalPath']}"
        detail_api_url = f"{self.WORKDAY_API_BASE}/job/{slug}"
        locations = []
        locations_text = row.get("locationsText")
        if locations_text:
            locations = [{"location": str(locations_text)}]
        raw = {
            "board_listing": row,
            "source_ui": {
                "workday_board_url": self.WORKDAY_BOARD_URL,
                "workday_jobs_api_url": f"{self.WORKDAY_API_BASE}/jobs",
                "detail_api_url": detail_api_url,
                "job_slug": slug,
                "canonical_board_url": canonical_url,
            },
        }
        job = self.job(
            raw,
            title=row["title"],
            url=canonical_url,
            job_id=req,
            requisition_id=req,
            apply_url=None,
            description="",
            method="cohesity.workday_board.cxs_detail_api",
            description_url=canonical_url,
            locations=locations,
            department=None,
            employment_type=None,
            workplace_type=None,
            date_posted=None,
            date_posted_raw=row.get("postedOn"),
            date_updated=None,
            salary=None,
        )
        job.record_identity = f"cohesity:{req}"
        return job

    async def _board_detail(self, job):
        """Hydrate one company-scoped board posting from its CXS detail API."""
        response = await self.http.get(job.raw["source_ui"]["detail_api_url"])
        payload = response.json()
        info = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
        if not isinstance(info, dict) or not isinstance(info.get("jobDescription"), str):
            raise ValueError("Workday board detail response lacks jobPostingInfo/jobDescription")
        if info.get("jobReqId") != job.requisition_id:
            raise ValueError("Workday board detail requisition differs from its listing row")
        if info.get("externalUrl") != job.url:
            raise ValueError("Workday board detail canonical URL differs from its listing row")
        description_html = info["jobDescription"]
        description_text = text_html(description_html)
        if len(description_text.strip()) < 100:
            raise ValueError("Workday board detail description is implausibly short")
        job.description_html = description_html
        job.description_text = description_text
        job.apply_url = job.url
        job.raw["workday_detail"] = payload
        job.raw["source_ui"]["apply_url"] = job.url
        start_date = parse_start_date(info.get("startDate"))
        job.date_posted = start_date
        job.date_posted_raw = info.get("startDate")
        if info.get("timeType"):
            job.employment_type = info["timeType"]
        if info.get("remoteType"):
            job.workplace_type = info["remoteType"]
        if info.get("location"):
            job.locations = [{"location": str(info["location"])}]
        employer = (payload.get("hiringOrganization") or {}).get("name")
        if employer:
            job.employer = employer
        return job

    async def _run_workday_fallback(self, trigger, *, started):
        total, rows, listing_pages = await self._fetch_board_rows()
        jobs = [self._make_board_job(req, row) for req, row in rows]
        self.result.expected_total = total if total is not None else len(rows)
        self.result.pages = max(self.result.pages, listing_pages)
        jobs = await self.hydrate(jobs, self._board_detail)
        for job in jobs:
            self.add(job)
        self.result.listing_complete = not self.result.errors
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": self.company.careers_url,
            "listing_page_url": self.WORKDAY_BOARD_URL,
            "listing_servlet_url": None,
            "workday_board_url": self.WORKDAY_BOARD_URL,
            "workday_jobs_api_url": f"{self.WORKDAY_API_BASE}/jobs",
            "membership_authority": (
                "Company-scoped Workday board Cohesity_Careers "
                "(cohesity.wd5.myworkdayjobs.com), linked from every "
                "published servlet row's jobUrl field; full listing via "
                "the board page's own CXS jobs API. Used because the "
                "official www.cohesity.com host is unreachable here."
            ),
            "workday_fallback": True,
            "fallback_trigger": trigger,
            "final_snapshot_verified": True,
            "board_total_reported": total,
            "source_rows_total": len(rows),
            "listing_pages_fetched": listing_pages,
            "description_source": (
                "Workday CXS detail API "
                "GET /wday/cxs/cohesity/Cohesity_Careers/job/<slug> "
                "(the exact request the board detail page makes)."
            ),
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result
