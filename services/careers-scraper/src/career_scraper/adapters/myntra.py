"""Myntra's first-party Flutter careers portal and its public Spire iExchange API.

The official careers page (careers.myntra.com) publishes every "Read More" and
"Explore Careers" link to jobs.myntra.com/home.  That portal is a Flutter web
app whose listing and detail views request the company-scoped, public endpoints
below.  This adapter follows the observed requests only:

* GET /ies/v1/p/workspaceId?domain=jobs.myntra.com   -> MYNTRA-93as3
* GET /ies/v1/p/requisition/_count                   -> 50 public postings
* GET /ies/v1/p/requisition/_search?page=N&size=6     -> the rendered 6-card pages
* GET /ies/v1/p/requisition/aggregation/_search       -> the rendered filter facets
* GET /ies/v1/p/requisition/displayId/<public id>     -> the rendered detail body

The public Job ID shown on the page is ``displayId``; the internal API record id
is kept as the requisition id.  Dates are converted from the API's epoch
milliseconds.  The employer-authored ``jobDescription`` is retained verbatim as
HTML and rendered to text with ordered-list numbers preserved.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString

from .base import Adapter
from ..models import Job


OFFICIAL_ENTRY = "https://careers.myntra.com/"
JOBS_HOME = "https://jobs.myntra.com/home"
JOBS_ORIGIN = "https://jobs.myntra.com"
API_ORIGIN = "https://io.spire2grow.com"
API_BASE = API_ORIGIN + "/ies/v1/p"
WORKSPACE_DOMAIN = "jobs.myntra.com"
WORKSPACE_PREFIX = "MYNTRA-"
PAGE_SIZE = 6
SORT_FIELD = "postedOn"
SORT_ORDER = "desc"
EMPLOYER_NAME = "Myntra"
DISPLAY_ID = re.compile(r"^\d{4,12}$")
WORKSPACE_ID = re.compile(r"^MYNTRA-[A-Za-z0-9]{2,40}$")
INTERNAL_ID = re.compile(r"^r_[0-9a-f]{16,64}$")
DETAIL_PATH = "/jobs/{display_id}"


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def iso_from_ms(value: Any, field: str) -> str | None:
    """Convert a public epoch-millisecond field to an ISO-8601 UTC timestamp."""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Myntra {field} is not an integer epoch-millisecond value")
    # Reject obvious request-time/template values outside a plausible posting range.
    if not (946684800000 <= value <= 4102444800000):
        raise ValueError(f"Myntra {field} is outside the plausible posting-date range: {value}")
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="seconds")


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Validate the first-party careers page and its company jobs-home links."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "careers.myntra.com" or parts.path not in ("", "/"):
        raise ValueError("Myntra official careers entry redirected away from careers.myntra.com")
    soup = BeautifulSoup(html, "lxml")
    links = [urljoin(response_url, a.get("href", "")) for a in soup.select("a[href]")]
    job_links = []
    for link in links:
        target = urlsplit(link)
        if (target.scheme == "https" and target.netloc == "jobs.myntra.com"
                and target.path.rstrip("/") == "/home"):
            job_links.append(link)
    if not job_links:
        raise ValueError("Myntra official careers page no longer links to jobs.myntra.com/home")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    body = normalized(soup.get_text(" ", strip=True))
    if "Myntra" not in body:
        raise ValueError("Myntra official careers page no longer identifies the company")
    return {
        "url": response_url,
        "title": title,
        "jobs_home_url": JOBS_HOME,
        "jobs_home_link_count": len(job_links),
        "jobs_home_links": list(dict.fromkeys(job_links)),
        "company_name_present": "Myntra" in body,
    }


def parse_workspace_id(value: Any, response_url: str) -> str:
    """Read the public workspace id the portal uses for every request header."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "io.spire2grow.com"
            or parts.path != "/ies/v1/p/workspaceId"
            or parse_qs(parts.query) != {"domain": [WORKSPACE_DOMAIN]}):
        raise ValueError("Myntra workspace lookup came from an unexpected route")
    if isinstance(value, dict):
        value = value.get("workspaceId")
    if not isinstance(value, str):
        raise ValueError("Myntra workspace lookup did not return a string")
    workspace = value.strip()
    if not WORKSPACE_ID.fullmatch(workspace):
        raise ValueError(f"Myntra workspace id changed or is not company-scoped: {workspace!r}")
    return workspace


def parse_count(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Myntra requisition count is not an object")
    total, hot = value.get("totalCount"), value.get("hotJobCount")
    if (not isinstance(total, int) or isinstance(total, bool) or total < 0
            or not isinstance(hot, int) or isinstance(hot, bool) or hot < 0):
        raise ValueError("Myntra requisition count is missing totalCount/hotJobCount")
    if total == 0:
        raise ValueError("Myntra requisition count is zero; refusing a zero-job replacement")
    return {"totalCount": total, "hotJobCount": hot}


def _validate_row(row: Any, workspace: str, context: str) -> list[str]:
    issues: list[str] = []
    if not isinstance(row, dict):
        return [f"{context}:not_an_object"]
    identity = row.get("displayId")
    if not isinstance(identity, str) or not DISPLAY_ID.fullmatch(identity):
        issues.append(f"{context}:missing_or_invalid_display_id")
    if not isinstance(row.get("id"), str) or not INTERNAL_ID.fullmatch(row["id"]):
        issues.append(f"{context}:missing_or_invalid_internal_id")
    if row.get("workspaceId") != workspace:
        issues.append(f"{context}:workspace_mismatch")
    if not normalized(row.get("jobTitle")):
        issues.append(f"{context}:missing_job_title")
    if not isinstance(row.get("jobDescription"), str) or len(normalized(row["jobDescription"])) < 100:
        issues.append(f"{context}:empty_or_short_job_description")
    if not isinstance(row.get("aboutCompany"), str) or not normalized(row["aboutCompany"]):
        issues.append(f"{context}:missing_about_company")
    if not isinstance(row.get("jobLocation"), list) or not row["jobLocation"]:
        issues.append(f"{context}:missing_job_location")
    if not isinstance(row.get("requiredEducation"), list):
        issues.append(f"{context}:missing_required_education")
    if not isinstance(row.get("requiredExperienceInMonths"), dict):
        issues.append(f"{context}:missing_required_experience")
    if not isinstance(row.get("skills"), list):
        issues.append(f"{context}:missing_skills")
    if not normalized(row.get("departmentName")):
        issues.append(f"{context}:missing_department_name")
    if not normalized(row.get("employmentType")):
        issues.append(f"{context}:missing_employment_type")
    posting = row.get("jobPosting")
    if not isinstance(posting, dict):
        issues.append(f"{context}:missing_job_posting")
    else:
        if posting.get("status") != "ACTIVE":
            issues.append(f"{context}:job_posting_not_active")
        try:
            iso_from_ms(posting.get("startDate"), f"{context}.jobPosting.startDate")
            iso_from_ms(posting.get("endDate"), f"{context}.jobPosting.endDate")
        except ValueError as exc:
            issues.append(str(exc))
    for field in ("createdOn", "updatedOn"):
        try:
            iso_from_ms(row.get(field), f"{context}.{field}")
        except ValueError as exc:
            issues.append(str(exc))
    status = row.get("jobStatus")
    if not isinstance(status, dict) or status.get("statusCode") != "OPEN":
        issues.append(f"{context}:job_status_not_open")
    return issues


def parse_listing_page(value: Any, requested_page: int, workspace: str,
                       page_size: int = PAGE_SIZE) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Myntra requisition search page is not an object")
    entities, total = value.get("entities"), value.get("total")
    if not isinstance(entities, list):
        raise ValueError("Myntra requisition search page is missing entities")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError("Myntra requisition search page is missing total")
    expected_rows = min(page_size, max(0, total - (requested_page - 1) * page_size))
    issues: list[str] = []
    if len(entities) != expected_rows:
        issues.append(
            f"page_{requested_page}:row_count_{len(entities)}_does_not_match_expected_{expected_rows}"
        )
    rows, ids = [], []
    for row in entities:
        row_issues = _validate_row(row, workspace, f"page_{requested_page}")
        issues.extend(row_issues)
        rows.append(row)
        if isinstance(row, dict) and isinstance(row.get("displayId"), str):
            ids.append(row["displayId"])
    if len(ids) != len(set(ids)):
        issues.append(f"page_{requested_page}:duplicate_display_id_within_page")
    return {"rows": rows, "total": total, "ids": ids, "issues": issues,
            "expected_rows": expected_rows, "page": requested_page}


def parse_job_detail(value: Any, expected_display_id: str, workspace: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"Myntra detail {expected_display_id} is not an object")
    if value.get("displayId") != expected_display_id:
        raise ValueError(f"Myntra detail {expected_display_id} returned a different display id")
    issues = _validate_row(value, workspace, f"detail_{expected_display_id}")
    if issues:
        raise ValueError("Myntra detail validation failed: " + "; ".join(issues))
    return {"entity": value, "issues": []}


def parse_aggregation(value: Any, expected_total: int) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Myntra aggregation response is not an object")
    if value.get("total") != expected_total:
        raise ValueError("Myntra aggregation total does not match the requisition count")
    entities = value.get("entities")
    aggregations = value.get("aggregations")
    if entities != [] or not isinstance(aggregations, list) or not aggregations:
        raise ValueError("Myntra aggregation response shape changed")
    facets: dict[str, list[dict]] = {}
    for facet in aggregations:
        if not isinstance(facet, dict) or not isinstance(facet.get("name"), str):
            raise ValueError("Myntra aggregation contains an unnamed facet")
        values = facet.get("values")
        if not isinstance(values, list):
            raise ValueError(f"Myntra aggregation {facet['name']} has no values")
        for item in values:
            if (not isinstance(item, dict) or not isinstance(item.get("key"), str)
                    or not isinstance(item.get("count"), int) or item["count"] < 0):
                raise ValueError(f"Myntra aggregation {facet['name']} contains an invalid value")
        facets[facet["name"]] = values
    groups = facets.get("jobGroup")
    if not groups:
        raise ValueError("Myntra aggregation no longer exposes the rendered jobGroup facet")
    group_total = sum(item["count"] for item in groups)
    if group_total != expected_total:
        raise ValueError("Myntra jobGroup facet counts do not reconcile with the listing total")
    return {"total": expected_total, "facets": facets,
            "job_group_counts": {item["displayKey"]: item["count"] for item in groups}}


def render_description_text(html: str) -> str:
    """Render employer HTML to text, preserving ordered/unordered list markers."""
    soup = BeautifulSoup(f'<div data-myntra-description="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-myntra-description]")
    if root is None:
        return ""
    for node in root(["script", "style", "noscript"]):
        node.decompose()
    for ordered, tag in ((True, "ol"), (False, "ul")):
        for list_node in root.find_all(tag):
            for index, item in enumerate(list_node.find_all("li", recursive=False), start=1):
                marker = f"{index}. " if ordered else "- "
                item.insert(0, NavigableString(marker))
    lines = [normalized(line) for line in root.get_text("\n").splitlines()]
    lines = [line for line in lines if line]
    merged: list[str] = []
    for line in lines:
        if merged and re.fullmatch(r"(?:\d+\.|-)", merged[-1]):
            merged[-1] = f"{merged[-1]} {line}"
        else:
            merged.append(line)
    return "\n".join(merged)


def skill_names(rows: Any) -> list[str]:
    if not isinstance(rows, list):
        return []
    values = []
    for row in rows:
        if isinstance(row, dict):
            value = normalized(row.get("skill"))
            if value and value not in values:
                values.append(value)
    return values


def degree_names(rows: Any) -> list[str]:
    if not isinstance(rows, list):
        return []
    values = []
    for row in rows:
        if isinstance(row, dict):
            value = normalized(row.get("degreeName"))
            if value and value not in values:
                values.append(value)
    return values


def location_names(rows: Any) -> list[str]:
    if not isinstance(rows, list):
        return []
    values = []
    for row in rows:
        if isinstance(row, dict):
            value = normalized(row.get("fqLocationName"))
            if value and value not in values:
                values.append(value)
    return values


def experience_label(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    start, end = value.get("from"), value.get("to")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    def years(months: int) -> str:
        whole, remainder = divmod(max(0, months), 12)
        return f"{whole}Y" if not remainder else f"{whole}Y {remainder}M"
    return f"{years(start)} - {years(end)}"


def detail_page_url(display_id: str) -> str:
    return JOBS_ORIGIN + DETAIL_PATH.format(display_id=display_id)


def detail_api_url(display_id: str) -> str:
    return f"{API_BASE}/requisition/displayId/{display_id}"


def build_job(detail: dict, listing_row: dict, *, workspace: str,
              listing_page: dict) -> Job:
    """Map the exact public detail entity onto the shared Job record."""
    identity = detail["displayId"]
    posting = detail.get("jobPosting") or {}
    description_html = detail.get("jobDescription") or ""
    description_text = render_description_text(description_html)
    job = Job(
        company=EMPLOYER_NAME,
        source="myntra",
        title=normalized(detail.get("jobTitle")),
        url=detail_page_url(identity),
        job_id=identity,
        requisition_id=detail.get("id"),
        apply_url=None,
        description_html=description_html,
        description_text=description_text,
        locations=detail.get("jobLocation") or [],
        department=detail.get("departmentName"),
        employment_type=detail.get("employmentType"),
        workplace_type=detail.get("jobType"),
        date_posted=iso_from_ms(posting.get("startDate"), "jobPosting.startDate"),
        date_posted_raw={
            "jobPosting.startDate": posting.get("startDate"),
            "createdOn": detail.get("createdOn"),
            "updatedOn": detail.get("updatedOn"),
        },
        date_updated=iso_from_ms(detail.get("updatedOn"), "updatedOn"),
        valid_through=iso_from_ms(posting.get("endDate"), "jobPosting.endDate"),
        salary=None,
        qualifications=None,
        responsibilities=None,
        experience=detail.get("requiredExperienceInMonths"),
        education=detail.get("requiredEducation") or [],
        employer=EMPLOYER_NAME,
        language="en",
        description_source_url=detail_api_url(identity),
        description_method="company_job_api.displayId.detail.jobDescription",
        raw={
            "workspace_id": workspace,
            "listing_row": listing_row,
            "listing_page": listing_page,
            "detail_entity": detail,
            "job_group_facets": None,
            "about_company_html": detail.get("aboutCompany"),
            "about_company_text": render_description_text(detail.get("aboutCompany") or ""),
            "skills": detail.get("skills") or [],
            "skill_names": skill_names(detail.get("skills")),
            "required_education": detail.get("requiredEducation") or [],
            "degree_names": degree_names(detail.get("requiredEducation")),
            "location_names": location_names(detail.get("jobLocation")),
            "experience_label": experience_label(detail.get("requiredExperienceInMonths")),
            "job_status": detail.get("jobStatus"),
            "job_posting": posting,
            "hot_job": detail.get("hotJob"),
            "is_fresher_job": detail.get("isFresherJob"),
            "recruiter": detail.get("recruiter"),
            "fcpr": detail.get("fcpr"),
            "created_on_ms": detail.get("createdOn"),
            "updated_on_ms": detail.get("updatedOn"),
            "date_behavior": "date_posted=jobPosting.startDate; date_updated=updatedOn; "
                             "valid_through=jobPosting.endDate; createdOn retained raw",
            "apply_url_behavior": "Apply control opens the portal application flow; no distinct "
                                  "public apply link is exposed without starting an application",
        },
    )
    return job


class Myntra(Adapter):
    """Collect every public Myntra role from its own careers portal."""

    VERIFICATION_REVISION = "myntra.spire-iexchange-company-portal.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        if source.url != JOBS_HOME:
            raise ValueError("Myntra source URL must be the inspected jobs.myntra.com/home page")
        self.workspace: str | None = None
        self.api_headers: dict[str, str] = {}

    def _request_meta(self, response, seconds: float, page: int | None = None) -> dict:
        return {
            "request_url": str(response.request.url),
            "final_url": str(response.url),
            "status": response.status_code,
            "request_seconds": round(max(0.0, seconds), 6),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "page": page,
        }

    async def _official_entry(self) -> dict:
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = time.perf_counter() - started
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(self._request_meta(response, seconds))
        return parsed

    async def _workspace(self) -> dict:
        started = time.perf_counter()
        response = await self.http.get(
            f"{API_BASE}/workspaceId", params={"domain": WORKSPACE_DOMAIN}
        )
        seconds = time.perf_counter() - started
        workspace = parse_workspace_id(response.text, str(response.url))
        self.workspace = workspace
        self.api_headers = {
            "workspaceid": workspace,
            "language": "en",
            "Referer": JOBS_HOME,
        }
        return {"workspace_id": workspace, **self._request_meta(response, seconds)}

    async def _get_api_json(self, path: str, *, params: dict | None = None,
                            page: int | None = None) -> tuple[Any, dict]:
        started = time.perf_counter()
        response = await self.http.request(
            "GET", f"{API_BASE}{path}", params=params, headers=self.api_headers
        )
        seconds = time.perf_counter() - started
        target = urlsplit(str(response.url))
        if target.scheme != "https" or target.netloc != "io.spire2grow.com" or not target.path.startswith("/ies/v1/p/"):
            raise ValueError("Myntra public API redirected outside its company endpoint")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Myntra public API returned non-JSON: {response.url}") from exc
        return payload, self._request_meta(response, seconds, page)

    async def _count(self) -> dict:
        payload, meta = await self._get_api_json("/requisition/_count")
        return {**parse_count(payload), **meta}

    async def _aggregation(self, expected_total: int) -> dict:
        payload, meta = await self._get_api_json(
            "/requisition/aggregation/_search",
            params={"selectedSortOrder": SORT_ORDER, "selectedSortField": SORT_FIELD},
        )
        return {**parse_aggregation(payload, expected_total), **meta}

    async def _listing(self, total: int) -> dict:
        pages = []
        rows = []
        seen: set[str] = set()
        issues: list[str] = []
        total_pages = math.ceil(total / PAGE_SIZE) if total else 0
        if total_pages < 1:
            raise ValueError("Myntra listing has no page budget")
        started = time.perf_counter()
        for page in range(1, total_pages + 1):
            if not self.page_guard():
                issues.append("myntra_listing_page_budget_exhausted")
                break
            payload, meta = await self._get_api_json(
                "/requisition/_search",
                params={"page": page, "size": PAGE_SIZE,
                        "selectedSortOrder": SORT_ORDER, "selectedSortField": SORT_FIELD},
                page=page,
            )
            parsed = parse_listing_page(payload, page, self.workspace or "", PAGE_SIZE)
            if parsed["total"] != total:
                issues.append(f"page_{page}:listing_total_changed_{parsed['total']}_to_{total}")
            for row in parsed["rows"]:
                identity = str(row.get("displayId", ""))
                if identity in seen:
                    issues.append(f"page_{page}:duplicate_display_id_across_pages:{identity}")
                    continue
                seen.add(identity)
                rows.append(row)
            issues.extend(parsed["issues"])
            pages.append({
                "page": page,
                "ids": parsed["ids"],
                "rows": len(parsed["rows"]),
                "total": parsed["total"],
                "issues": parsed["issues"],
                **meta,
            })
        return {
            "rows": rows,
            "ids": [row["displayId"] for row in rows],
            "pages": pages,
            "total_pages": total_pages,
            "issues": issues,
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def _detail(self, job: Job) -> Job:
        display_id = str(job.job_id)
        payload, meta = await self._get_api_json(f"/requisition/displayId/{display_id}")
        parsed = parse_job_detail(payload, display_id, self.workspace or "")
        detail = parsed["entity"]
        listing_row = job.raw["listing_row"]
        critical = ("id", "displayId", "jobTitle", "jobDescription", "aboutCompany",
                    "jobPosting", "createdOn", "updatedOn", "departmentName",
                    "employmentType", "jobLocation", "skills", "requiredEducation")
        mismatches = [field for field in critical if detail.get(field) != listing_row.get(field)]
        if mismatches:
            job.warnings.append("detail_listing_field_mismatch:" + ",".join(mismatches))
        replacement = build_job(detail, listing_row, workspace=self.workspace or "",
                                listing_page=job.raw["listing_page"])
        replacement.raw["detail_request"] = meta
        replacement.raw["detail_listing_mismatches"] = mismatches
        replacement.record_identity = job.record_identity
        return replacement

    async def _hydrate_detail(self, job: Job) -> Job:
        return await self._detail(job)

    async def run(self):
        started = time.perf_counter()
        entry = await self._official_entry()
        workspace = await self._workspace()
        count = await self._count()
        total = count["totalCount"]
        aggregation = await self._aggregation(total)
        listing = await self._listing(total)
        collection_seconds = max(0.0, time.perf_counter() - started)
        self.result.expected_total = total
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry": entry,
            "workspace": workspace,
            "count": count,
            "aggregation": aggregation,
            "listing": {
                "url": JOBS_HOME,
                "api_path": "/requisition/_search",
                "page_size": PAGE_SIZE,
                "pages": listing["pages"],
                "total_pages": listing["total_pages"],
                "total": total,
                "ids": listing["ids"],
                "issues": listing["issues"],
            },
            "detail_request_shape": {
                "path": "/requisition/displayId/{displayId}",
                "url_template": detail_api_url("{displayId}"),
                "page_url_template": JOBS_ORIGIN + DETAIL_PATH,
            },
            "scope_reason": "careers.myntra.com publishes jobs.myntra.com/home; the portal's "
                            "public workspace count and all listing pages are company-scoped Myntra roles.",
        })
        self.result.timings.update({
            "source_collection_seconds": round(collection_seconds, 6),
            "official_entry_seconds": entry["request_seconds"],
            "workspace_seconds": workspace["request_seconds"],
            "count_seconds": count["request_seconds"],
            "aggregation_seconds": aggregation["request_seconds"],
            "listing_pages_seconds": round(listing["seconds"], 6),
        })
        if listing["issues"]:
            self.result.errors.extend(listing["issues"])
        if len(listing["rows"]) != total:
            self.result.errors.append(
                f"myntra_listing_membership_mismatch:{len(listing['rows'])}_of_{total}"
            )
            self.result.metadata["listing"]["row_count"] = len(listing["rows"])
            return self.result

        jobs = []
        for row in listing["rows"]:
            page_number = next(
                (page["page"] for page in listing["pages"] if row["displayId"] in page["ids"]), None
            )
            job = self.job(
                {"listing_row": row, "listing_page": page_number,
                 "listing_total": total, "workspace_id": self.workspace},
                title=normalized(row.get("jobTitle")),
                url=detail_page_url(row["displayId"]),
                job_id=row["displayId"],
                description="",
                method=None,
                description_url=None,
            )
            job.requisition_id = row.get("id")
            job.raw["job_group_facets"] = aggregation["job_group_counts"]
            jobs.append(job)
            self.add(job)
        if len(jobs) != total:
            self.result.errors.append("myntra_listing_placeholder_count_mismatch")
            return self.result

        hydrated = await self.hydrate(jobs, self._hydrate_detail)
        self.result.jobs = list(hydrated)
        self.result.listing_complete = True
        if self.result.errors:
            self.result.listing_complete = False
        if any(not job.description_verified for job in self.result.jobs):
            self.result.errors.append("myntra_detail_verification_incomplete")
            self.result.listing_complete = False
        self.result.metadata["details_fetched"] = len(self.result.jobs)
        self.result.metadata["listing"]["row_count"] = len(self.result.jobs)
        self.result.metadata["listing"]["collected_ids"] = [job.job_id for job in self.result.jobs]
        self.result.timings["source_collection_seconds"] = round(collection_seconds, 6)
        return self.result
