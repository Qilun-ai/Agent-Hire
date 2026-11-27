"""Goldman Sachs' source-published Higher Results and Campus journeys."""
from __future__ import annotations

import re
import time
from copy import deepcopy

from bs4 import BeautifulSoup
from bs4.element import NavigableString

from .base import Adapter

CAREERS_URL = "https://www.goldmansachs.com/careers"
STUDENTS_URL = "https://www.goldmansachs.com/careers/students"
HIGHER_ORIGIN = "https://higher.gs.com"
API_URL = "https://api-higher.gs.com/gateway/api/v1/graphql"
RESULTS_ROUTE = "https://higher.gs.com/results?&page=1&sort=RELEVANCE"
CAMPUS_ROUTE = "https://higher.gs.com/campus?&page=1&sort=RELEVANCE"
PAGE_SIZE = 20
ORACLE_HOST = "https://hdpc.fa.us2.oraclecloud.com"
ORACLE_REQUISITION_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
ORACLE_SITE_NUMBER = "CX_3001"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": HIGHER_ORIGIN,
    "User-Agent": "CompanyCareersScraper/0.1 (public job listings)",
}
GET_ROLES_QUERY = """query GetRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    items {
      roleId
      corporateTitle
      jobTitle
      jobFunction
      locations { primary state country city }
      status
      division
      skills
      jobType { code description }
      externalSource { sourceId }
    }
  }
}"""
GET_CAMPUS_ROLES_QUERY = """query GetCampusRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    items {
      roleId
      corporateTitle
      jobTitle
      jobFunction
      locations { primary state country city }
      status
      division
      skills
      jobType { code description }
      externalSource { sourceId }
      educationLevel
      startDate
      gradDegreeStartDate
      gradDegreeEndDate
    }
  }
}"""
GET_ROLE_BY_ID_QUERY = """query GetRoleById($externalSourceId: String!, $externalSourceFetch: Boolean) {
  role(externalSourceId: $externalSourceId, externalSourceFetch: $externalSourceFetch) {
    roleId
    corporateTitle
    jobTitle
    jobFunction
    locations { primary state country city }
    division
    descriptionHtml
    jobType { code description }
    skillset
    compensation { minSalary maxSalary currency }
    applyActive
    status
    externalSource { externalApplicationUrl applyInExternalSource sourceId secondarySourceId }
  }
}"""
JOURNEYS = {
    "results": {
        "route": RESULTS_ROUTE,
        "official_parent": CAREERS_URL,
        "experiences": ["EARLY_CAREER", "PROFESSIONAL"],
        "operation": "GetRoles",
        "query": GET_ROLES_QUERY,
    },
    "campus": {
        "route": CAMPUS_ROUTE,
        "official_parent": STUDENTS_URL,
        "experiences": ["CAMPUS"],
        "operation": "GetCampusRoles",
        "query": GET_CAMPUS_ROLES_QUERY,
    },
}


def listing_payload(journey: str, page_number: int):
    config = JOURNEYS[journey]
    return {
        "operationName": config["operation"],
        "variables": {
            "searchQueryInput": {
                "page": {"pageSize": PAGE_SIZE, "pageNumber": page_number},
                "sort": {"sortStrategy": "RELEVANCE", "sortOrder": "DESC"},
                "filters": [],
                "experiences": list(config["experiences"]),
                "searchTerm": "",
            }
        },
        "query": config["query"],
    }


def role_url(source_id: str) -> str:
    """Build the exact public Higher role URL.

    The rendered results/campus cards publish `https://higher.gs.com/roles/<externalSource.sourceId>`
    (for example `/roles/187553`); the GraphQL `roleId` such as
    `187553_GS_NOTICE_OF_FILING_LCA` is not a valid public route.
    """
    return f"{HIGHER_ORIGIN}/roles/{source_id}"


def oracle_requisition_url(source_id: str) -> str:
    """The exact same-job Oracle requisition URL exposed by the official Higher Apply button."""
    return (
        f"{ORACLE_HOST}{ORACLE_REQUISITION_PATH}?expand=all&onlyData=true&finder=ById%3BId%3D%22"
        f"{source_id}%22%2CsiteNumber%3D{ORACLE_SITE_NUMBER}"
    )


def oracle_description(data):
    """Compose the exact published body from an Oracle requisition response.

    Goldman's campus Higher bodies are the corporate/program + organization/division
    sections (ExternalDescriptionStr is only ``<br>`` for those postings); lateral
    Higher bodies are the external section. Use the external section whenever it has
    visible text, otherwise concatenate ``CorporateDescriptionStr`` then
    ``OrganizationDescriptionStr`` in that order.
    """
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("Oracle requisition response must contain exactly one requisition")
    item = items[0]
    external = item.get("ExternalDescriptionStr") or ""
    corporate = item.get("CorporateDescriptionStr") or ""
    organization = item.get("OrganizationDescriptionStr") or ""
    if role_inner_text(external):
        html = external
        method = "oracle.recruitingCEJobRequisitionDetails.ExternalDescriptionStr"
        used_fields = ["ExternalDescriptionStr"]
    else:
        html = "".join(part for part in (corporate, organization) if part)
        method = (
            "oracle.recruitingCEJobRequisitionDetails."
            "CorporateDescriptionStr+OrganizationDescriptionStr"
        )
        used_fields = [
            name for name, value in (
                ("CorporateDescriptionStr", corporate),
                ("OrganizationDescriptionStr", organization),
            ) if value
        ]
    text = role_inner_text(html)
    if len(text) < 100:
        raise ValueError("Oracle requisition does not publish a full description body")
    return {
        "description_html": html,
        "description_text": text,
        "description_method": method,
        "used_fields": used_fields,
        "external_description_chars": len(external),
        "corporate_description_chars": len(corporate),
        "organization_description_chars": len(organization),
    }


_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}


def role_inner_text(markup):
    """Normalize the full role HTML like rendered text without spacing inline nodes."""
    soup = BeautifulSoup(markup or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    chunks = []

    def visit(node):
        if isinstance(node, NavigableString):
            chunks.append(str(node))
            return
        name = getattr(node, "name", None)
        if name in {"script", "style", "noscript"}:
            return
        is_block = name in _BLOCK_TAGS or name == "br"
        if is_block and chunks and not chunks[-1].endswith(("\n", "\r", "\t", " ")):
            chunks.append("\n")
        for child in getattr(node, "children", ()):
            visit(child)
        if is_block:
            chunks.append("\n")

    visit(soup)
    return re.sub(r"\s+", " ", "".join(chunks)).strip()


def role_classification(role_id: str, status: str | None) -> str:
    if "_GS_NOTICE_OF_FILING_LCA" in str(role_id):
        return "lca_notice_linked_posting"
    if status == "SCHEDULED":
        return "scheduled_future_role"
    return "published_role"


class GoldmanSachs(Adapter):
    """Collect exact results/campus pages and hydrate only their returned source IDs."""

    VERIFICATION_REVISION = "2026-11-07.2"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._rows_by_url: dict[str, dict] = {}
        self._invalidated_cache_ids: list[str] = []

    def add(self, job):
        row = job.raw.get("goldman_listing") or {}
        self._rows_by_url[job.url] = row
        cached = self.options.cached_jobs.get(job.url)
        if cached and cached.description_unavailable:
            # The exact same-job Oracle requisition now provides a documented body for
            # Higher-null postings, so a previously unavailable record must be retried.
            self.options.cached_jobs.pop(job.url, None)
            self._invalidated_cache_ids.append(str(cached.job_id or job.url))
        elif cached:
            old = cached.raw.get("goldman_listing") or {}
            fields = ("roleId", "jobTitle", "corporateTitle", "status", "division", "locations", "externalSource")
            if any(old.get(field) != row.get(field) for field in fields):
                self.options.cached_jobs.pop(job.url, None)
                self._invalidated_cache_ids.append(str(cached.job_id or job.url))
            else:
                # Exact official role route and unfiltered listing row still identify this post.
                cached.record_identity = None
        super().add(job)

    async def _fetch_journey(self, journey: str, *, count_pages: bool = True):
        total = None
        pages = []
        seen_ids = set()
        seen_source_ids = set()
        page_number = 0
        while True:
            if count_pages and not self.page_guard():
                return {"journey": journey, "pages": pages, "roles": [], "complete": False}
            request = listing_payload(journey, page_number)
            try:
                payload = await self.http.json(API_URL, method="POST", json=request, headers=HEADERS)
            except Exception as exc:
                self.result.errors.append(f"goldman_{journey}_listing_page_{page_number}_failed:{type(exc).__name__}:{exc}")
                return {"journey": journey, "pages": pages, "roles": [], "complete": False}
            errors = payload.get("errors") or []
            if errors:
                self.result.errors.append(f"goldman_{journey}_graphql_errors_page_{page_number}:{errors}")
                return {"journey": journey, "pages": pages, "roles": [], "complete": False}
            listing = (payload.get("data") or {}).get("roleSearch") or {}
            page_total = listing.get("totalCount")
            rows = listing.get("items") or []
            if total is None:
                total = int(page_total or 0)
            elif page_total != total:
                self.result.errors.append(f"goldman_{journey}_listing_total_changed:{total}->{page_total}:page{page_number}")
                return {"journey": journey, "pages": pages, "roles": [], "complete": False}
            expected = min(PAGE_SIZE, max(0, total - page_number * PAGE_SIZE))
            if len(rows) != expected:
                self.result.errors.append(f"goldman_{journey}_page_count_mismatch:page{page_number}:{len(rows)}!={expected}")
                return {"journey": journey, "pages": pages, "roles": [], "complete": False}
            page_ids = [row.get("roleId") for row in rows]
            page_source_ids = [str((row.get("externalSource") or {}).get("sourceId") or "") for row in rows]
            if any(not role_id for role_id in page_ids) or len(page_ids) != len(set(page_ids)):
                self.result.errors.append(f"goldman_{journey}_invalid_or_duplicate_role_id:page{page_number}")
                return {"journey": journey, "pages": pages, "roles": [], "complete": False}
            if any(not source_id for source_id in page_source_ids) or len(page_source_ids) != len(set(page_source_ids)):
                self.result.errors.append(f"goldman_{journey}_invalid_or_duplicate_external_source_id:page{page_number}")
                return {"journey": journey, "pages": pages, "roles": [], "complete": False}
            duplicate = seen_ids.intersection(page_ids)
            if duplicate:
                self.result.errors.append(f"goldman_{journey}_role_ids_repeated_across_pages:{sorted(duplicate)}")
                return {"journey": journey, "pages": pages, "roles": [], "complete": False}
            duplicate_sources = seen_source_ids.intersection(page_source_ids)
            if duplicate_sources:
                self.result.errors.append(f"goldman_{journey}_source_ids_repeated_across_pages:{sorted(duplicate_sources)}")
                return {"journey": journey, "pages": pages, "roles": [], "complete": False}
            seen_ids.update(page_ids)
            seen_source_ids.update(page_source_ids)
            pages.append({"page_number": page_number, "count": len(rows), "role_ids": page_ids,
                          "total_count": total, "operation_name": request["operationName"],
                          "variables": deepcopy(request["variables"])})
            for item in rows:
                item = deepcopy(item)
                item["goldman_journey"] = journey
                item["goldman_parent_route"] = JOURNEYS[journey]["official_parent"]
                item["listing_page_number"] = page_number
                url = role_url(str((item.get("externalSource") or {}).get("sourceId")))
                if url in self._rows_by_url:
                    self.result.errors.append(f"goldman_role_url_duplicate_across_journeys:{item['roleId']}")
                    return {"journey": journey, "pages": pages, "roles": [], "complete": False}
                self._rows_by_url[url] = item
            if (page_number + 1) * PAGE_SIZE >= total:
                break
            page_number += 1
        return {"journey": journey, "pages": pages, "role_ids": sorted(seen_ids),
                "source_ids": sorted(seen_source_ids), "total_count": total,
                "complete": len(seen_ids) == total and len(seen_source_ids) == total}

    async def _list_all_pages(self, *, count_pages: bool = True):
        self._rows_by_url = {}
        journeys = {}
        for journey in ("results", "campus"):
            result = await self._fetch_journey(journey, count_pages=count_pages)
            journeys[journey] = result
            if not result.get("complete"):
                return journeys, []
        roles = list(self._rows_by_url.values())
        return journeys, roles

    def _job_from_listing(self, row):
        external = row.get("externalSource") or {}
        role_id = str(row["roleId"])
        source_id = str(external.get("sourceId") or "")
        if not source_id:
            raise ValueError(f"Exact listed role has no externalSource.sourceId: {role_id}")
        url = role_url(source_id)
        job_type = row.get("jobType") or {}
        raw = {
            "goldman_listing": row,
            "journey": row.get("goldman_journey"),
            "role_type_classification": role_classification(role_id, row.get("status")),
            "official_higher_route": JOURNEYS[row["goldman_journey"]]["route"],
            "goldman_public_role_url": url,
            "goldman_source_id": source_id,
        }
        return self.job(
            raw,
            title=row.get("jobTitle") or "",
            url=url,
            job_id=role_id,
            requisition_id=source_id,
            locations=row.get("locations") or [],
            department=row.get("division"),
            team=row.get("jobFunction"),
            employment_type=job_type.get("description") if isinstance(job_type, dict) else job_type,
            education=row.get("educationLevel"),
        )

    async def _apply_detail(self, job, role):
        listing = job.raw.get("goldman_listing") or {}
        role_id = str(listing.get("roleId"))
        if not role or str(role.get("roleId")) != role_id:
            raise ValueError(f"Higher detail roleId does not match listing: expected {role_id}, got {None if not role else role.get('roleId')}")
        listing_source_id = str((listing.get("externalSource") or {}).get("sourceId"))
        external = role.get("externalSource") or {}
        detail_source_id = str(external.get("sourceId")) if external.get("sourceId") is not None else None
        if listing_source_id != detail_source_id:
            raise ValueError(f"Higher externalSource.sourceId mismatch: listing {listing_source_id}, detail {detail_source_id}")
        detail_title = role.get("jobTitle") or job.title
        if detail_title != job.title:
            job.warnings.append("goldman_listing_detail_title_conflict")
            job.raw["title_conflict"] = {"listing_title": job.title, "detail_title": detail_title}
        job.title = detail_title
        job.job_id = role_id
        job.requisition_id = detail_source_id
        job.apply_url = external.get("externalApplicationUrl")
        job.description_html = role.get("descriptionHtml") or ""
        job.description_text = role_inner_text(job.description_html)
        job.description_method = "higher.GetRoleById.role.descriptionHtml"
        job.description_source_url = API_URL
        job.department = role.get("division") or job.department
        job.team = role.get("jobFunction") or job.team
        job.locations = role.get("locations") or job.locations
        job_type = role.get("jobType") or {}
        job.employment_type = job_type.get("description") if isinstance(job_type, dict) else job_type
        compensation = role.get("compensation") or {}
        job.salary = compensation if any(value is not None for value in compensation.values()) else None
        job.date_posted = None
        job.date_posted_raw = None
        job.date_updated = None
        job.valid_through = None
        job.qualifications = None
        job.responsibilities = None
        job.raw["goldman_detail"] = role
        job.raw["goldman_application"] = {
            "apply_active": role.get("applyActive"),
            "status": role.get("status"),
            "external_application_url": external.get("externalApplicationUrl"),
            "apply_in_external_source": external.get("applyInExternalSource"),
            "source_id": external.get("sourceId"),
            "secondary_source_id": external.get("secondarySourceId"),
        }
        job.raw["role_type_classification"] = role_classification(role_id, role.get("status"))
        if not job.description_text:
            await self._recover_empty_detail(job, role)
        if not job.description_text:
            job.description_unavailable = True
            job.raw["description_unavailable_reason"] = (
                "higher.GetRoleById.role.descriptionHtml is null/empty and the exact same-job "
                "Oracle requisition exposed by the Apply button did not yield a published body"
            )
            job.warnings.append("description_not_full_field")
        return job

    async def _recover_empty_detail(self, job, role):
        """Use the exact same-job Oracle requisition linked by the Higher Apply button.

        Higher campus bodies are composed from Oracle CorporateDescriptionStr +
        OrganizationDescriptionStr; the ExternalDescriptionStr is only ``<br>``.
        Only the exact requisition ID from the public detail/apply URL is requested.
        """
        source_id = str((role.get("externalSource") or {}).get("sourceId") or "")
        requisition_url = oracle_requisition_url(source_id) if source_id else None
        recovery = {
            "attempted": True,
            "requisition_url": requisition_url,
            "higher_description_html": role.get("descriptionHtml"),
            "higher_description_was_null": not role.get("descriptionHtml"),
            "status": "failed",
        }
        job.raw["description_recovery"] = recovery
        if not source_id:
            recovery["error"] = "detail has no externalSource.sourceId for the exact Oracle requisition"
            self.result.errors.append(f"goldman_oracle_recovery_failed:{job.job_id}:missing_source_id")
            return
        try:
            data = await self.http.json(requisition_url)
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list) or len(items) != 1:
                raise ValueError("Oracle requisition response must contain exactly one requisition")
            item = items[0]
            if str(item.get("Id")) != source_id:
                raise ValueError(
                    f"Oracle requisition Id {item.get('Id')!r} does not match source id {source_id}"
                )
            oracle_title = re.sub(r"\s+", " ", str(item.get("Title") or "")).strip()
            expected_title = re.sub(r"\s+", " ", job.title or "").strip()
            if oracle_title and expected_title and oracle_title != expected_title:
                raise ValueError(
                    f"Oracle requisition Title {oracle_title!r} does not match listing title {expected_title!r}"
                )
            composed = oracle_description(data)
        except Exception as exc:
            recovery["error"] = f"{type(exc).__name__}: {exc}"
            job.raw["goldman_oracle_requisition"] = {
                "requisition_url": requisition_url,
                "status": "failed",
                "error": recovery["error"],
            }
            self.result.errors.append(
                f"goldman_oracle_recovery_failed:{source_id}:{type(exc).__name__}:{exc}"
            )
            return
        job.description_html = composed["description_html"]
        job.description_text = composed["description_text"]
        job.description_method = composed["description_method"]
        job.description_source_url = requisition_url
        job.description_unavailable = False
        recovery.update({
            "status": "recovered",
            "used_fields": composed["used_fields"],
            "description_method": composed["description_method"],
            "description_source_url": requisition_url,
            "description_chars": len(composed["description_text"]),
        })
        job.raw["goldman_oracle_requisition"] = {
            "requisition_url": requisition_url,
            "status": "ok",
            "Id": item.get("Id"),
            "Title": item.get("Title"),
            "RequisitionType": item.get("RequisitionType"),
            "PrimaryLocation": item.get("PrimaryLocation"),
            "ExternalDescriptionStr": item.get("ExternalDescriptionStr"),
            "CorporateDescriptionStr": item.get("CorporateDescriptionStr"),
            "OrganizationDescriptionStr": item.get("OrganizationDescriptionStr"),
            "external_description_chars": composed["external_description_chars"],
            "corporate_description_chars": composed["corporate_description_chars"],
            "organization_description_chars": composed["organization_description_chars"],
            "used_fields": composed["used_fields"],
            "note": (
                "Exact same-job Oracle requisition exposed by the Higher Apply button; "
                "Higher descriptionHtml is null."
            ),
        }

    async def run(self):
        if self.company.slug != "goldman-sachs":
            raise ValueError("Goldman Sachs adapter requires the exact Goldman Sachs company")
        if self.source.url != CAREERS_URL or self.source.evidence_url != CAREERS_URL:
            raise ValueError(f"Goldman Sachs source must be the official careers entry {CAREERS_URL}")

        collection_started = time.perf_counter()
        initial_started = time.perf_counter()
        journeys, rows = await self._list_all_pages()
        initial_seconds = max(0.0, time.perf_counter() - initial_started)
        if not rows or len(self.result.errors):
            self.result.expected_total = len(rows)
            self.result.listing_complete = False
            self.result.metadata["journeys"] = journeys
            return self.result

        role_ids = [str(row["roleId"]) for row in rows]
        if len(role_ids) != len(set(role_ids)):
            self.result.errors.append("goldman_role_id_overlap_across_official_journeys")
            self.result.listing_complete = False
            return self.result
        self.result.expected_total = len(rows)
        self.result.metadata["journeys"] = journeys
        self.result.metadata["official_membership"] = {
            "official_entry_url": CAREERS_URL,
            "higher_results_route": RESULTS_ROUTE,
            "official_students_route": STUDENTS_URL,
            "higher_campus_route": CAMPUS_ROUTE,
            "api_endpoint": API_URL,
            "total_roles": len(rows),
            "unique_role_ids": len(set(role_ids)),
            "journey_totals": {name: item.get("total_count") for name, item in journeys.items()},
            "publisher_embeds_or_links_generic_ats_for_membership": False,
            "query_shape": {name: listing_payload(name, 0) for name in JOURNEYS},
            "note": "Results and Campus are separate official Higher experiences published by Goldman Sachs; both are included without text, location, or other filters.",
        }
        for row in rows:
            self.add(self._job_from_listing(row))
            if self.limit():
                return self.result
        self.result.listing_complete = len(self.result.jobs) == len(rows)

        async def detail(job):
            listing = job.raw["goldman_listing"]
            source_id = (listing.get("externalSource") or {}).get("sourceId")
            if source_id is None:
                raise ValueError(f"Exact listed role has no externalSource.sourceId: {listing.get('roleId')}")
            payload = await self.http.json(API_URL, method="POST", json={
                "operationName": "GetRoleById",
                "variables": {"externalSourceId": str(source_id), "externalSourceFetch": True},
                "query": GET_ROLE_BY_ID_QUERY,
            }, headers={**HEADERS, "Referer": job.url})
            if payload.get("errors"):
                raise ValueError(f"GetRoleById GraphQL errors for {listing.get('roleId')}: {payload['errors']}")
            role = (payload.get("data") or {}).get("role")
            return await self._apply_detail(job, role)

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        for job in self.result.jobs:
            if not job.apply_url and job.raw.get("goldman_application", {}).get("apply_active"):
                self.result.errors.append(f"goldman_apply_active_without_url:{job.job_id}")

        final_started = time.perf_counter()
        final_journeys, final_rows = await self._list_all_pages(count_pages=False)
        final_seconds = max(0.0, time.perf_counter() - final_started)
        initial_signatures = [{
            "roleId": row.get("roleId"),
            "jobTitle": row.get("jobTitle"),
            "status": row.get("status"),
            "division": row.get("division"),
            "locations": row.get("locations"),
            "sourceId": (row.get("externalSource") or {}).get("sourceId"),
            "journey": row.get("goldman_journey"),
        } for row in rows]
        final_signatures = [{
            "roleId": row.get("roleId"),
            "jobTitle": row.get("jobTitle"),
            "status": row.get("status"),
            "division": row.get("division"),
            "locations": row.get("locations"),
            "sourceId": (row.get("externalSource") or {}).get("sourceId"),
            "journey": row.get("goldman_journey"),
        } for row in final_rows]
        same = initial_signatures == final_signatures and len(final_rows) == len(rows)
        self.result.metadata["final_membership_recheck"] = {
            "status": "complete" if same else "mismatch",
            "initial_total": len(rows),
            "final_total": len(final_rows),
            "initial_journey_totals": {name: item.get("total_count") for name, item in journeys.items()},
            "final_journey_totals": {name: item.get("total_count") for name, item in final_journeys.items()},
            "initial_role_ids": role_ids,
            "final_role_ids": [str(row.get("roleId")) for row in final_rows],
            "initial_signatures": initial_signatures,
            "final_signatures": final_signatures,
            "exact_order_and_public_fields_match": same,
        }
        if not same or any(not item.get("complete") for item in final_journeys.values()):
            self.result.listing_complete = False
            self.result.errors.append("goldman_final_unfiltered_membership_recheck_mismatch")
        self.result.metadata["archive_cache_invalidations"] = self._invalidated_cache_ids
        total_seconds = max(0.0, time.perf_counter() - collection_started)
        self.result.timings["goldman_initial_listing_seconds"] = round(initial_seconds, 6)
        self.result.timings["goldman_final_listing_seconds"] = round(final_seconds, 6)
        self.result.timings["goldman_listing_total_seconds"] = round(initial_seconds + final_seconds, 6)
        self.result.timings["goldman_collection_wall_seconds"] = round(total_seconds, 6)
        return self.result
