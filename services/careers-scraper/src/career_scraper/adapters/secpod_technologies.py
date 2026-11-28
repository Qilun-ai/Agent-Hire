"""SecPod Technologies' official careers page and its exact Keka jobs widget.

The company page publishes a Keka embedded-jobs widget in ``#khembedjobs``.
The widget configuration (observed in the page's Next.js chunk) identifies the
SecPod Keka account ``8010166a-b48d-4997-9d93-64a354522150`` and portal
``https://secpod.keka.com/careers/``.  The widget fetches every active job from
one company-scoped endpoint and renders all of them without pagination.  This
adapter reads only that endpoint and then checks each exact Keka detail page.
"""
from __future__ import annotations

from hashlib import sha256
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.secpod.com/careers/"
KEKA_PORTAL = "https://secpod.keka.com/careers/"
KEKA_IDENTIFIER = "8010166a-b48d-4997-9d93-64a354522150"
KEKA_ACTIVE_JOBS_API = KEKA_PORTAL + "api/embedjobs/default/active/" + KEKA_IDENTIFIER
KEKA_CAREERPORTAL_INFO_API = KEKA_PORTAL + "api/organization/default/careerportalinfo"
KEKA_DEPARTMENTS_API = KEKA_PORTAL + "api/embedjobs/departments/" + KEKA_IDENTIFIER
KEKA_GLOBAL_FILTERS_API = KEKA_PORTAL + "api/embedjobs/globalfiltergroups/" + KEKA_IDENTIFIER
KEKA_EMBED_SCRIPT = KEKA_PORTAL + "api/embedjobs/js/" + KEKA_IDENTIFIER

EMPLOYER_NAME = "SecPod Technologies"
PORTAL_NAME = "SecPod Careers"
PORTAL_DOMAIN = "secpod.keka.com"
JOB_TYPE_LABELS = {1: "Part Time", 2: "Full Time"}
JOB_DETAIL_PATH = re.compile(r"^/careers/jobdetails/(\d+)$")
JOB_APPLY_PATH = re.compile(r"^/careers/applyjob/(\d+)$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    """Match the visible browser body shape for Keka's description container."""
    soup = BeautifulSoup(f'<div data-secpod-body="true">{html or ""}</div>', "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True).replace("\u00a0", " ")
    # BeautifulSoup inserts a separator between inline tags; remove the
    # resulting space before sentence punctuation (including a period, which
    # the browser does not render as a separate visible space).
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def description_sections(html: str) -> list[dict]:
    """Keep employer-authored heading blocks and the body beneath each one."""
    soup = BeautifulSoup(f'<div data-secpod-sections="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-secpod-sections]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections: list[dict] = []
    current: dict | None = None
    for block in blocks:
        heading = ""
        whole = normalized_title(block.get_text(" ", strip=True))
        if block.name in HEADING_TAGS:
            heading = whole
        elif block.name in {"div", "p"}:
            strongs = block.find_all(["strong", "b"], recursive=False)
            if len(strongs) == 1:
                strong_text = normalized_title(strongs[0].get_text(" ", strip=True))
                if strong_text and strong_text == whole and len(whole) <= 160:
                    heading = strong_text
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
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return [section for section in sections if section["html"].strip() or section["heading"]]


def _sections_matching(sections: list[dict], tokens: tuple[str, ...]) -> list[dict] | None:
    matched = [
        section for section in sections
        if section.get("heading") and any(token in section["heading"].lower() for token in tokens)
    ]
    return matched or None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "www.secpod.com"
            or parts.path.rstrip("/") != "/careers"):
        raise ValueError("SecPod official careers URL redirected away from its inspected route")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "SecPod Careers" not in title:
        raise ValueError("SecPod official careers page identity changed")
    containers = soup.select("div#khembedjobs")
    if len(containers) != 1:
        raise ValueError("SecPod careers page no longer publishes exactly one Keka jobs container")
    headings = [normalized_title(node.get_text(" ", strip=True)) for node in soup.select("h2")]
    if not any(value == "Current Job Openings" for value in headings):
        raise ValueError("SecPod careers page no longer publishes the Current Job Openings section")
    return {
        "url": response_url,
        "title": title,
        "embed_container_id": "khembedjobs",
        "section_heading": "Current Job Openings",
        "keka_portal": KEKA_PORTAL,
        "keka_identifier": KEKA_IDENTIFIER,
    }


def parse_portal_info(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("SecPod Keka careerportalinfo response is not an object")
    setting = value.get("jobListingSetting")
    filters = setting.get("filters") if isinstance(setting, dict) else None
    if value.get("name") != PORTAL_NAME or value.get("careersPortalDomain") != PORTAL_DOMAIN:
        raise ValueError("SecPod Keka portal identity changed")
    if not isinstance(filters, list) or set(filters) != {"department", "location"}:
        raise ValueError("SecPod Keka listing filters changed")
    if setting.get("groupBy") != "department" or setting.get("jobListingFormat") != 2:
        raise ValueError("SecPod Keka listing layout changed")
    return {
        "name": value.get("name"),
        "short_name": value.get("shortName"),
        "domain": value.get("careersPortalDomain"),
        "filters": filters,
        "group_by": setting.get("groupBy"),
        "listing_format": setting.get("jobListingFormat"),
        "job_fields": setting.get("jobFields"),
    }


def parse_active_jobs(value) -> dict:
    if not isinstance(value, list) or not value:
        raise ValueError("SecPod Keka active-jobs response is not a non-empty list")
    rows: list[dict] = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("SecPod Keka active-jobs response contains a non-object row")
        identity = row.get("id")
        if not isinstance(identity, int) or identity < 1:
            raise ValueError("SecPod Keka row has no numeric id")
        job_id = str(identity)
        if job_id in seen:
            raise ValueError(f"SecPod Keka duplicate job id: {job_id}")
        if not normalized_title(row.get("title")):
            raise ValueError(f"SecPod Keka row {job_id} has no title")
        description = row.get("description")
        if not isinstance(description, str) or len(normalized_body_text(description)) < 100:
            raise ValueError(f"SecPod Keka row {job_id} has an empty or truncated description")
        if not isinstance(row.get("departmentName"), str) or not row["departmentName"].strip():
            raise ValueError(f"SecPod Keka row {job_id} has no department")
        if row.get("jobType") not in JOB_TYPE_LABELS:
            raise ValueError(f"SecPod Keka row {job_id} has an unmapped jobType")
        if not isinstance(row.get("jobNumber"), str) or not row["jobNumber"].strip():
            raise ValueError(f"SecPod Keka row {job_id} has no public jobNumber")
        published = row.get("publishedOn")
        if not isinstance(published, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T", published):
            raise ValueError(f"SecPod Keka row {job_id} has no usable publishedOn")
        locations = row.get("jobLocations")
        if not isinstance(locations, list) or not locations:
            raise ValueError(f"SecPod Keka row {job_id} has no job location")
        for location in locations:
            if not isinstance(location, dict) or not normalized_title(location.get("name")):
                raise ValueError(f"SecPod Keka row {job_id} has an unidentified location")
        if not isinstance(row.get("skillNames", []), list):
            raise ValueError(f"SecPod Keka row {job_id} has malformed skillNames")
        seen.add(job_id)
        rows.append(row)
    return {"rows": rows, "job_ids": [str(row["id"]) for row in rows], "count": len(rows)}


def parse_detail_page(html: str, expected_row: dict, detail_url: str) -> dict:
    parts = urlsplit(detail_url)
    match = JOB_DETAIL_PATH.fullmatch(parts.path)
    expected_id = str(expected_row.get("id"))
    if (parts.scheme != "https" or parts.netloc != PORTAL_DOMAIN or match is None
            or match.group(1) != expected_id):
        raise ValueError("SecPod representative detail URL is not an exact Keka job route")
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.select_one("h1")
    actual_title = normalized_title(title_node.get_text(" ", strip=True)) if title_node else ""
    expected_title = normalized_title(expected_row.get("title"))
    if not title_node or actual_title != expected_title:
        raise ValueError(f"SecPod detail {expected_id} title no longer matches its listing row")
    container = soup.select_one(".job-description-container")
    if container is None:
        raise ValueError(f"SecPod detail {expected_id} has no description container")
    description_html = container.decode_contents()
    description_text = normalized_body_text(description_html)
    api_text = normalized_body_text(expected_row.get("description"))
    if not description_text:
        raise ValueError(f"SecPod detail {expected_id} has an empty description")
    if description_text != api_text:
        raise ValueError(
            f"SecPod detail {expected_id} description does not match the listing field "
            f"({len(description_text)} vs {len(api_text)} chars)"
        )
    apply_button = soup.select_one("#apply-job")
    if apply_button is None or normalized_title(apply_button.get_text(" ", strip=True)) != "Apply for this job":
        raise ValueError(f"SecPod detail {expected_id} has no current apply control")
    apply_url = KEKA_PORTAL + "applyjob/" + expected_id
    if JOB_APPLY_PATH.fullmatch(urlsplit(apply_url).path) is None:
        raise ValueError(f"SecPod detail {expected_id} apply route changed")
    sections = description_sections(description_html)
    links = [
        {"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
        for anchor in BeautifulSoup(description_html, "lxml").select("a[href]")
    ]
    attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
    return {
        "job_id": expected_id,
        "title": actual_title,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(description_text),
        "description_fnv1a64": fnv1a64(description_text),
        "description_sha256": sha256(description_text.encode()).hexdigest(),
        "description_sections": sections,
        "description_links": links,
        "role_attachments": attachments,
        "apply_url": apply_url,
        "apply_control": normalized_title(apply_button.get_text(" ", strip=True)),
        "matches_api_description_text": True,
    }


class SecPod(Adapter):
    """Read only SecPod's own Keka active-jobs feed and its exact job pages."""

    VERIFICATION_REVISION = "secpod.technologies.keka-embed.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update({
            "status": response.status_code,
            "seconds": seconds,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
        })
        return parsed

    async def _portal_info(self):
        started = time.perf_counter()
        response = await self.http.get(KEKA_CAREERPORTAL_INFO_API)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != KEKA_CAREERPORTAL_INFO_API:
            raise ValueError("SecPod Keka careerportalinfo redirected away from its company route")
        parsed = parse_portal_info(response.json())
        parsed.update({
            "url": str(response.url),
            "status": response.status_code,
            "seconds": seconds,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
        })
        return parsed

    async def _api_snapshot(self):
        started = time.perf_counter()
        response = await self.http.get(KEKA_ACTIVE_JOBS_API)
        request_seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != KEKA_ACTIVE_JOBS_API:
            raise ValueError("SecPod Keka active-jobs API redirected away from its company route")
        parsed = parse_active_jobs(response.json())
        parsed.update({
            "url": str(response.url),
            "status": response.status_code,
            "request_seconds": request_seconds,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
        })
        parsed["row_fields"] = sorted(parsed["rows"][0].keys())
        return parsed

    def _make_job(self, row: dict, index: int) -> object:
        identity = str(row["id"])
        detail_url = KEKA_PORTAL + "jobdetails/" + identity
        sections = description_sections(row["description"])
        responsibilities = _sections_matching(
            sections, ("responsibilit", "what you will do", "what you'll do", "your opportunity", "key duties")
        )
        qualifications = _sections_matching(
            sections, ("qualif", "requirement", "must-have", "must have", "what you'll bring",
                       "what we're looking for", "preferred", "education")
        )
        locations = [normalized_title(location["name"]) for location in row["jobLocations"]]
        job = self.job(
            {
                "api_row": dict(row),
                "source_row_index": index,
                "api_description_sections": sections,
                "portal_name": PORTAL_NAME,
                "portal_domain": PORTAL_DOMAIN,
                "salary_note": (
                    "Source publishes salaryRange/salaryRangeFormat; the current values "
                    "(0.00-11.00 INR) look like a portal placeholder and are retained verbatim, not interpreted."
                ),
            },
            title=normalized_title(row["title"]),
            url=detail_url,
            job_id=identity,
            requisition_id=row["jobNumber"],
            apply_url=KEKA_PORTAL + "applyjob/" + identity,
            description=row["description"],
            method="secpod.keka.active_jobs.description",
            description_url=KEKA_ACTIVE_JOBS_API,
            locations=locations,
            department=row["departmentName"],
            employment_type=JOB_TYPE_LABELS[row["jobType"]],
            workplace_type=None,
            date_posted=row["publishedOn"],
            date_posted_raw=row["publishedOn"],
            date_updated=None,
            valid_through=None,
            salary=row.get("salaryRange"),
            employer=EMPLOYER_NAME,
            language="en",
            qualifications=qualifications,
            responsibilities=responsibilities,
        )
        job.description_text = normalized_body_text(row["description"])
        job.raw["listing"] = dict(row)
        job.raw["detail"] = None
        return job

    async def detail(self, job):
        row = job.raw["api_row"]
        request_started = time.perf_counter()
        response = await self.http.get(job.url)
        request_seconds = max(0.0, time.perf_counter() - request_started)
        if str(response.url) != job.url or response.history:
            raise ValueError(f"SecPod detail {job.job_id} redirected away from its exact Keka route")
        parse_started = time.perf_counter()
        parsed = parse_detail_page(response.text, row, job.url)
        parse_seconds = max(0.0, time.perf_counter() - parse_started)
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "secpod.keka.jobdetails.job-description-container"
        job.description_source_url = job.url
        job.apply_url = parsed["apply_url"]
        job.raw["detail"] = {
            "url": job.url,
            "status": response.status_code,
            "final_url": str(response.url),
            "request_seconds": request_seconds,
            "parse_seconds": parse_seconds,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "description_normalized_chars": parsed["description_normalized_chars"],
            "description_fnv1a64": parsed["description_fnv1a64"],
            "description_sha256": parsed["description_sha256"],
            "description_sections": parsed["description_sections"],
            "description_links": parsed["description_links"],
            "role_attachments": parsed["role_attachments"],
            "apply_control": parsed["apply_control"],
            "apply_url": parsed["apply_url"],
            "matches_api_description_text": parsed["matches_api_description_text"],
        }
        self.result.timings["detail_request_seconds_sum"] = self.result.timings.get("detail_request_seconds_sum", 0.0) + request_seconds
        self.result.timings["detail_parse_seconds_sum"] = self.result.timings.get("detail_parse_seconds_sum", 0.0) + parse_seconds
        return job

    @staticmethod
    def _stable_row(row: dict) -> dict:
        # publishedSinceDays is a derived day counter, not membership; it can
        # change at a date boundary during a run without changing the job.
        return {key: value for key, value in row.items() if key != "publishedSinceDays"}

    async def run(self):
        source_started = time.perf_counter()
        entry = await self._official_entry()
        portal = await self._portal_info()
        if not self.page_guard():
            raise ValueError("SecPod listing page budget exhausted")
        initial = await self._api_snapshot()
        source_collection_seconds = max(0.0, time.perf_counter() - source_started)
        if not initial["rows"]:
            raise ValueError("SecPod Keka active-jobs response contains no jobs; verify the widget before certifying")
        self.result.expected_total = initial["count"]
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": KEKA_ACTIVE_JOBS_API,
            "portal_url": KEKA_PORTAL,
            "keka_identifier": KEKA_IDENTIFIER,
            "listing_authority": (
                "SecPod's official careers page publishes a Keka embed in #khembedjobs. "
                "The page's Next.js configuration identifies this exact SecPod Keka account; "
                "the active-jobs endpoint is the single listing request used by that widget."
            ),
            "listing_membership_request": {
                "method": "GET",
                "url": KEKA_ACTIVE_JOBS_API,
                "query_shape": None,
                "filters_applied": False,
                "pagination_parameter": None,
                "scope": "SecPod's Keka identifier only; no other Keka company or board is queried.",
            },
            "listing_ui": {
                "container": "#khembedjobs",
                "filters": ["department", "location", "search"],
                "pagination_controls": [],
                "group_by": "department",
                "job_listing_format": 2,
            },
            "detail_method": "Exact Keka jobdetails/{id} page, .job-description-container.",
            "detail_page_count": 0,
            "source_row_fields": initial["row_fields"],
            "employer": EMPLOYER_NAME,
            "salary_note": (
                "salaryRange/salaryRangeFormat are retained verbatim. The observed 0.00-11.00 INR "
                "values appear to be a portal placeholder, not a verified compensation range."
            ),
            "date_mapping": {
                "date_posted": "publishedOn (source posting timestamp)",
                "date_posted_raw": "publishedOn",
                "date_updated": None,
                "valid_through": None,
                "publishedSinceDays": "retained raw only; derived day counter is not a date",
            },
        })
        for index, row in enumerate(initial["rows"]):
            self.add(self._make_job(row, index))
        if len(self.result.jobs) != initial["count"]:
            self.result.errors.append("secpod_listing_row_count_after_deduplication_mismatch")
        await self.hydrate(self.result.jobs, self.detail)
        self.result.metadata["detail_page_count"] = sum(
            1 for job in self.result.jobs if job.raw.get("detail") is not None
        )

        final_started = time.perf_counter()
        final_entry = await self._official_entry()
        final_portal = await self._portal_info()
        final = await self._api_snapshot()
        final_seconds = max(0.0, time.perf_counter() - final_started)

        initial_ids = initial["job_ids"]
        final_ids = final["job_ids"]
        initial_stable = {str(row["id"]): self._stable_row(row) for row in initial["rows"]}
        final_stable = {str(row["id"]): self._stable_row(row) for row in final["rows"]}
        changed_ids = sorted(
            key for key in initial_stable.keys() & final_stable.keys()
            if initial_stable[key] != final_stable[key]
        )
        new_ids = sorted(final_stable.keys() - initial_stable.keys())
        removed_ids = sorted(initial_stable.keys() - final_stable.keys())
        ids_match = initial_ids == final_ids
        stable_rows_match = not changed_ids and not new_ids and not removed_ids
        entry_matches = (entry["response_sha256"] == final_entry["response_sha256"]
                         and portal["response_sha256"] == final_portal["response_sha256"])
        if not ids_match:
            self.result.errors.append("secpod_final_listing_ids_changed")
        if not stable_rows_match:
            self.result.errors.append("secpod_final_listing_rows_changed")
        if not entry_matches:
            self.result.errors.append("secpod_official_entry_or_portal_changed_during_run")
        self.result.metadata["membership_reconciliation"] = {
            "initial_count": initial["count"],
            "final_count": final["count"],
            "expected_count": self.result.expected_total,
            "initial_ids": initial_ids,
            "final_ids": final_ids,
            "initial_final_ids_match": ids_match,
            "initial_final_stable_rows_match": stable_rows_match,
            "changed_ids": changed_ids,
            "new_ids": new_ids,
            "removed_ids": removed_ids,
            "official_entry_and_portal_unchanged": entry_matches,
            "pages_observed": 1,
            "pagination_controls": [],
            "filters_applied": False,
            "ui_visible_count": None,
            "ui_visible_ids": None,
        }
        self.result.metadata["final_recheck"] = {
            "initial_api_seconds": initial["request_seconds"],
            "final_recheck_seconds": final_seconds,
            "final_api_seconds": final["request_seconds"],
            "entry_sha256": entry["response_sha256"],
            "final_entry_sha256": final_entry["response_sha256"],
            "portal_sha256": portal["response_sha256"],
            "final_portal_sha256": final_portal["response_sha256"],
            "initial_api_sha256": initial["response_sha256"],
            "final_api_sha256": final["response_sha256"],
        }
        self.result.timings["source_collection_seconds"] = source_collection_seconds
        self.result.timings["final_recheck_seconds"] = final_seconds
        self.result.timings["official_entry_seconds"] = entry["seconds"]
        self.result.timings["portal_info_seconds"] = portal["seconds"]
        self.result.timings["initial_api_seconds"] = initial["request_seconds"]
        self.result.listing_complete = bool(
            ids_match and stable_rows_match and entry_matches
            and self.result.expected_total == len(self.result.jobs)
        )
        self.result.finish()
        return self.result
