"""Jubilant FoodWorks' official careers journey: company page -> Oracle CX_2005.

The current official entry is the company's own
``https://www.jubilantfoodworks.com/careers/work-with-us`` page (the older
``/careers`` URL in the ledger currently returns the CMS 404 template).  That
page publishes exactly one jobs CTA, "View All Jobs", to the Oracle Candidate
Experience board ``hcmUI/CandidateExperience/en/sites/jubilant`` (site number
``CX_2005``).  The board's own unfiltered ``findReqs`` listing is the membership
authority; its ``organizationsFacet`` is exhausted by eight Jubilant FoodWorks
brands (Jubilant FoodWorks, Domino's Pizza India/Sri Lanka, Popeyes, Dunkin,
Hong's Kitchen, Integrated Supply Chain, Corporate), so this is the
company-scoped listing the official page publishes.

The rendered board shows 25 cards and loads 25 more on each scroll to the
bottom.  The flow mirrors that exact 25-row offset pagination and hydrates every
listed ``Id`` through the board's exact ``ById`` detail request.

The job page renders JOB DESCRIPTION, RESPONSIBILITIES, QUALIFICATIONS,
ABOUT US (only when the source field is non-empty) and JOB INFO.  The matching
Oracle fields are ExternalDescriptionStr, ExternalResponsibilitiesStr,
ExternalQualificationsStr and CorporateDescriptionStr.  A large number of this
board's high-volume store postings publish no role description at all; those
records keep the public listing fields and the published ABOUT US raw evidence
but are marked ``description_unavailable`` rather than substituting corporate
boilerplate for a missing role description.
"""
from __future__ import annotations

import gzip
from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..http import FetchError, HTTP
from ..models import now


LEDGER_ENTRY = "https://www.jubilantfoodworks.com/careers"
OFFICIAL_ENTRY = "https://www.jubilantfoodworks.com/careers/work-with-us"
ORACLE_HOST = "fa-exph-saasfaprod1.fa.ocs.oraclecloud.com"
ORACLE_ORIGIN = "https://fa-exph-saasfaprod1.fa.ocs.oraclecloud.com"
SITE_NUMBER = "CX_2005"
BOARD_PATH = "/hcmUI/CandidateExperience/en/sites/jubilant"
BOARD_URL = ORACLE_ORIGIN + BOARD_PATH
LISTING_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAILS_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
SETTINGS_PATH = f"/hcmRestApi/CandidateExperience/en/siteSettings/{SITE_NUMBER}"
PAGE_SIZE = 25  # the board's own infinite-scroll batch size
FACETS = ("WORK_LOCATIONS;WORKPLACE_TYPES;TITLES;CATEGORIES;ORGANIZATIONS;"
          "POSTING_DATES;FLEX_FIELDS;LOCATIONS")
EXPAND = ("requisitionList.workLocation,requisitionList.otherWorkLocations,"
          "requisitionList.secondaryLocations,flexFieldsFacet.values,"
          "requisitionList.requisitionFlexFields")
ROLE_FIELDS = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
               "ExternalQualificationsStr")
BODY_FIELDS = ROLE_FIELDS + ("CorporateDescriptionStr",)
BODY_LABELS = {
    "ExternalDescriptionStr": "JOB DESCRIPTION",
    "ExternalResponsibilitiesStr": "RESPONSIBILITIES",
    "ExternalQualificationsStr": "QUALIFICATIONS",
    "CorporateDescriptionStr": "ABOUT US",
}
# Fields the detail API returns but the public job page does not publish.
NONPUBLIC_FIELDS = frozenset({
    "InternalQualificationsStr", "InternalResponsibilitiesStr",
    "HiringManager", "ObjectVerNumberProfile", "ApplyWhenNotPostedFlag",
    "ExternalContactName", "ExternalContactEmail",
})
FILTER_FIELDS = (
    "Keyword", "Location", "LocationId", "SelectedTitlesFacet",
    "SelectedCategoriesFacet", "SelectedPostingDatesFacet",
    "SelectedLocationsFacet", "SelectedWorkLocationsFacet",
    "SelectedFlexFieldsFacets", "SelectedOrganizationsFacet",
    "SelectedWorkplaceTypesFacet", "JobFamilyId", "PostingStartDate",
    "PostingEndDate", "RequisitionId", "CandidateNumber", "OrganizationId",
    "WorkplaceType",
)
# The eight organization names observed in CX_2005's own organizationsFacet.
# They are used only as a scope sanity check; every row carries one of these
# published IDs and the facet counts sum to the listing total.
KNOWN_JFL_ORGANIZATIONS = frozenset({
    "Hongs Kitchen", "Jubilant FoodWorks", "Popeyes", "Dominos Pizza India",
    "Integrated Supply Chain", "Corporate", "Dunkin", "Dominos Pizza SriLanka",
})
BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "ul", "ol", "li", "h1", "h2", "h3",
    "h4", "h5", "h6", "br", "tr", "td", "th", "table", "blockquote", "pre",
    "hr",
})


def visible_text(html: str | None) -> str:
    """Block-aware visible text, matching how the rendered page joins spans."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    chunks: list[str] = []

    def walk(node):
        if isinstance(node, NavigableString):
            chunks.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        block = node.name in BLOCK_TAGS
        if block:
            chunks.append("\n")
        for child in node.children:
            walk(child)
        if block:
            chunks.append("\n")

    walk(soup)
    value = "".join(chunks)
    value = value.replace("\u00a0", " ").replace("\u2007", " ").replace("\u202f", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _clean(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def job_page_url(job_id: str) -> str:
    return f"{BOARD_URL}/job/{job_id}"


def apply_page_url(job_id: str) -> str:
    """The route the board's APPLY NOW button opens for this exact job."""
    return f"{job_page_url(job_id)}/apply/email"


def _board_url_from_href(href: str, base_url: str) -> str | None:
    target = urljoin(base_url, href.strip())
    parsed = urlsplit(target)
    if parsed.hostname != ORACLE_HOST:
        return None
    path = parsed.path.rstrip("/")
    if path == BOARD_PATH:
        return BOARD_URL
    return None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Validate the current JFL careers page and the single board it publishes."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https"
            or parsed.hostname not in {"www.jubilantfoodworks.com", "jubilantfoodworks.com"}
            or parsed.path.rstrip("/") != "/careers/work-with-us"):
        raise ValueError("Jubilant FoodWorks official entry is not the inspected work-with-us page")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "jubilant foodworks" not in html.casefold():
        raise ValueError("Jubilant FoodWorks official entry no longer identifies the company")
    boards: set[str] = set()
    anchor_texts: set[str] = set()
    other: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        target = urljoin(response_url, href)
        target_parts = urlsplit(target)
        if target_parts.hostname != ORACLE_HOST:
            continue
        path = target_parts.path.rstrip("/")
        board = _board_url_from_href(href, response_url)
        if board:
            boards.add(board)
            text = normalized_title(anchor.get_text(" ", strip=True))
            if text:
                anchor_texts.add(text)
        elif "/CandidateExperience/en/sites/" in path:
            other.add(urlunsplit(("https", ORACLE_HOST, path, "", "")))
    if boards != {BOARD_URL}:
        raise ValueError(f"Jubilant FoodWorks official entry no longer publishes exactly the inspected board: {sorted(boards)}")
    if other:
        raise ValueError(f"Jubilant FoodWorks official entry publishes unexpected additional board links: {sorted(other)}")
    return {
        "url": response_url,
        "title": title,
        "board_url": BOARD_URL,
        "board_anchor_texts": sorted(anchor_texts),
        "other_board_urls": sorted(other),
    }


def parse_board_shell(html: str, response_url: str = BOARD_URL) -> dict:
    """Validate the board's published base tag/site identity."""
    parsed = urlsplit(response_url)
    if parsed.hostname != ORACLE_HOST or parsed.path.rstrip("/") not in {
            BOARD_PATH, BOARD_PATH + "/jobs"}:
        raise ValueError("Jubilant FoodWorks board shell came from an unexpected Oracle route")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    og_title = None
    og_site = None
    for meta in soup.select("meta[property]"):
        if meta.get("property") == "og:title":
            og_title = meta.get("content")
        elif meta.get("property") == "og:site_name":
            og_site = meta.get("content")
    if title != "Jubilant Corporate" or og_title != "Jubilant Corporate Careers" or og_site != "Jubilant Corporate":
        raise ValueError("Jubilant FoodWorks board shell title/identity changed")
    base = soup.select_one("base[data-apibaseurl][data-sitenumber]")
    if base is None:
        raise ValueError("Jubilant FoodWorks board shell no longer publishes its site-number base tag")
    if (base.get("href") != BOARD_PATH
            or urlsplit(base.get("data-apibaseurl", "")).hostname != ORACLE_HOST
            or base.get("data-sitenumber") != SITE_NUMBER):
        raise ValueError("Jubilant FoodWorks board shell site number/API host changed")
    return {
        "url": response_url,
        "title": title,
        "og_title": og_title,
        "og_site_name": og_site,
        "base_href": base.get("href"),
        "api_base_host": urlsplit(base.get("data-apibaseurl", "")).hostname,
        "site_number": base.get("data-sitenumber"),
    }


def parse_site_settings(value: dict) -> dict:
    """Validate the public siteSettings identity for CX_2005."""
    app = value.get("app") if isinstance(value, dict) else None
    if not isinstance(app, dict):
        raise ValueError("Jubilant FoodWorks siteSettings response has no app block")
    if (app.get("siteName") != "Jubilant Corporate"
            or app.get("siteCode") != "Jubilant Corporate"
            or app.get("siteNumber") != SITE_NUMBER
            or app.get("siteLang") != "en"):
        raise ValueError("Jubilant FoodWorks siteSettings no longer identify the inspected company site")
    return {
        key: app.get(key)
        for key in ("siteName", "siteCode", "siteNumber", "siteLang", "languages", "templates")
    }


def _finder(offset: int, page_size: int = PAGE_SIZE) -> str:
    return (f"findReqs;siteNumber={SITE_NUMBER},facetsList={FACETS},"
            f"limit={page_size},sortBy=POSTING_DATES_DESC,offset={offset}")


def listing_params(offset: int, page_size: int = PAGE_SIZE) -> dict:
    return {"onlyData": "true", "expand": EXPAND, "finder": _finder(offset, page_size)}


def detail_params(job_id: str) -> dict:
    return {"expand": "all", "onlyData": "true",
            "finder": f'ById;Id="{job_id}",siteNumber={SITE_NUMBER}'}


def parse_listing_page(value: dict, requested_offset: int, page_size: int = PAGE_SIZE) -> dict:
    """Validate one unfiltered board listing batch and return its exact rows."""
    if not isinstance(value, dict):
        raise ValueError("Jubilant FoodWorks listing response is not an object")
    if value.get("errors"):
        raise ValueError(f"Jubilant FoodWorks listing response returned errors: {value.get('errors')}")
    items = value.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("Jubilant FoodWorks listing response has no single board result")
    block = items[0]
    if not isinstance(block, dict):
        raise ValueError("Jubilant FoodWorks listing result block is not an object")
    total = block.get("TotalJobsCount")
    rows = block.get("requisitionList")
    if (not isinstance(total, int) or total < 0 or not isinstance(rows, list)
            or block.get("SiteNumber") != SITE_NUMBER
            or block.get("Offset") != requested_offset
            or block.get("Limit") != page_size
            or block.get("SortBy") != "POSTING_DATES_DESC"
            or len(rows) > page_size):
        raise ValueError("Jubilant FoodWorks listing identity, pagination or unfiltered search state changed")
    applied = [key for key in FILTER_FIELDS if block.get(key) not in (None, "", False, [])]
    if applied:
        raise ValueError(f"Jubilant FoodWorks listing unexpectedly applied filters: {applied}")
    expected_rows = min(page_size, max(0, total - requested_offset))
    issues: list[str] = []
    if len(rows) != expected_rows:
        issues.append(f"jfl_listing_row_count_mismatch:{len(rows)}!={expected_rows}")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("jfl_listing_row_not_object")
            continue
        identity = row.get("Id")
        if not isinstance(identity, str) or not re.fullmatch(r"\d+", identity):
            issues.append("jfl_listing_row_missing_numeric_id")
            continue
        if identity in seen:
            issues.append(f"jfl_listing_duplicate_id:{identity}")
        seen.add(identity)
        if not isinstance(row.get("Title"), str) or not row["Title"].strip():
            issues.append(f"jfl_listing_row_missing_title:{identity}")
        posted = row.get("PostedDate")
        if posted is not None and not (isinstance(posted, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted)):
            issues.append(f"jfl_listing_row_invalid_posted_date:{identity}")
        if row.get("secondaryLocations") is not None and not isinstance(row["secondaryLocations"], list):
            issues.append(f"jfl_listing_secondary_locations_not_list:{identity}")
        if row.get("requisitionFlexFields") is not None and not isinstance(row["requisitionFlexFields"], list):
            issues.append(f"jfl_listing_flex_fields_not_list:{identity}")
    organizations = block.get("organizationsFacet")
    if not isinstance(organizations, list):
        organizations = []
    org_issues = []
    for item in organizations:
        if not isinstance(item, dict) or not isinstance(item.get("Name"), str) or not isinstance(item.get("TotalCount"), int):
            org_issues.append("jfl_listing_organization_facet_malformed")
    return {
        "rows": rows,
        "total": total,
        "offset": requested_offset,
        "page_size": page_size,
        "site_number": block.get("SiteNumber"),
        "organizations_facet": organizations,
        "workplace_types_facet": block.get("workplaceTypesFacet") or [],
        "organizations_total": sum(item.get("TotalCount", 0) for item in organizations if isinstance(item, dict)),
        "issues": issues + org_issues,
    }


def parse_detail_page(value: dict, job_id: str, expected_title: str | None = None) -> dict:
    """Validate the exact detail response for one listed Id."""
    if not isinstance(value, dict):
        raise ValueError("Jubilant FoodWorks detail response is not an object")
    items = value.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError(f"Jubilant FoodWorks detail response for {job_id} has no single posting")
    row = items[0]
    if not isinstance(row, dict):
        raise ValueError(f"Jubilant FoodWorks detail response for {job_id} is not an object")
    if str(row.get("Id")) != str(job_id):
        raise ValueError(f"Jubilant FoodWorks detail response returned a different posting than {job_id}")
    if expected_title is not None and normalized_title(row.get("Title")) != normalized_title(expected_title):
        raise ValueError(f"Jubilant FoodWorks detail title changed for {job_id}")
    if row.get("RequisitionId") is None:
        raise ValueError(f"Jubilant FoodWorks detail for {job_id} has no requisition id")
    for field in BODY_FIELDS:
        if field not in row:
            raise ValueError(f"Jubilant FoodWorks detail for {job_id} omitted inspected section {field}")
        if row[field] is not None and not isinstance(row[field], str):
            raise ValueError(f"Jubilant FoodWorks detail section {field} for {job_id} is not HTML text")
    return row


def public_detail(row: dict) -> dict:
    """Retain the public detail row; internal/personnel fields are not published."""
    return {key: value for key, value in row.items()
            if key not in NONPUBLIC_FIELDS and not key.startswith("Internal")}


def description_sections(detail: dict) -> tuple[list[dict], str, list[str]]:
    """Return the exact non-empty rendered sections, role text and missing fields."""
    sections, missing = [], []
    for field in BODY_FIELDS:
        value = detail.get(field)
        text = visible_text(value) if isinstance(value, str) else ""
        if text:
            sections.append({
                "field": field,
                "label": BODY_LABELS[field],
                "html": value,
                "text": text,
                "text_chars": len(text),
                "text_sha256": sha256(text.encode()).hexdigest(),
            })
        else:
            missing.append(field)
    role_text = "\n".join(section["text"] for section in sections if section["field"] in ROLE_FIELDS)
    return sections, role_text, missing


def assemble_description(sections: list[dict]) -> str:
    return "\n".join(
        f'<section data-source-field="{section["field"]}"><h2>{section["label"]}</h2>{section["html"]}</section>'
        for section in sections
    )


def _location_string(item: dict) -> str | None:
    name = item.get("LocationName") or item.get("Name") or item.get("AddressLine1")
    parts = [
        name,
        item.get("TownOrCity"),
        item.get("Region2") or item.get("Region1"),
        item.get("PostalCode"),
        item.get("Country"),
    ]
    value = ", ".join(str(part).strip() for part in parts if isinstance(part, str) and part.strip())
    return value or None


def location_values(detail: dict, listing: dict | None = None) -> list[str]:
    """Public location names in the order the board publishes them."""
    listing = listing or {}
    values: list[str] = []

    def add(value):
        if isinstance(value, str):
            value = normalized_title(value)
            if value and value not in values:
                values.append(value)

    add(detail.get("PrimaryLocation") or listing.get("PrimaryLocation"))
    for key in ("workLocation", "otherWorkLocations", "secondaryLocations"):
        source = detail.get(key)
        if not source and key == "secondaryLocations":
            source = listing.get(key)
        for item in source or []:
            if isinstance(item, dict):
                add(_location_string(item))
            elif isinstance(item, str):
                add(item)
    return values


def organization_details(detail: dict, listing: dict, org_map: dict[str, str]) -> dict:
    org_id = detail.get("OrganizationId")
    if org_id is None:
        org_id = listing.get("OrganizationId")
    key = str(org_id) if org_id is not None else None
    return {
        "organization_id": key,
        "organization_name": org_map.get(key) if key else None,
        "business_unit_id": detail.get("BusinessUnitId") or listing.get("BusinessUnitId"),
        "legal_employer_id": detail.get("LegalEmployerId") or listing.get("LegalEmployerId"),
        "source": "CX_2005 organizationsFacet + row OrganizationId",
    }


class JubilantFoodWorks(Adapter):
    """Collect only the membership published on the official CX_2005 board."""

    VERIFICATION_REVISION = "jubilant-foodworks.official-CX_2005.1"
    PAGE_SIZE = PAGE_SIZE

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._board_url = BOARD_URL

    # ---------------------------------------------------------------- entry
    async def _entry(self) -> float:
        started = time.perf_counter()
        entry = await self.http.get(self.company.careers_url)
        if str(entry.url).rstrip("/") != OFFICIAL_ENTRY.rstrip("/"):
            raise ValueError(f"Jubilant FoodWorks official entry redirected: {self.company.careers_url} -> {entry.url}")
        entry_info = parse_official_entry(entry.text, str(entry.url))

        ledger_status = None
        ledger_error = None
        try:
            ledger = await self.http.get(LEDGER_ENTRY)
            ledger_status = ledger.status_code
        except FetchError as exc:
            ledger_error = str(exc)
            match = re.search(r"HTTP (\d{3})", ledger_error)
            ledger_status = int(match.group(1)) if match else None

        board = await self.http.get(entry_info["board_url"])
        board_info = parse_board_shell(board.text, str(board.url))
        jobs_page = await self.http.get(self._board_url + "/jobs")
        jobs_info = parse_board_shell(jobs_page.text, str(jobs_page.url))
        settings = await self.http.json(ORACLE_ORIGIN + SETTINGS_PATH)
        site_config = parse_site_settings(settings)
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=str(entry.url),
            official_entry_title=entry_info["title"],
            official_board_anchor_texts=entry_info["board_anchor_texts"],
            official_entry_other_board_urls=entry_info["other_board_urls"],
            ledger_entry_url=LEDGER_ENTRY,
            ledger_entry_status=ledger_status,
            ledger_entry_error=ledger_error,
            company_listing_url=str(jobs_page.url),
            board_shell_url=str(board.url),
            board_shell=board_info,
            jobs_shell=jobs_info,
            site_number=SITE_NUMBER,
            site_settings=site_config,
            listing_authority=(
                "Jubilant FoodWorks official Work With Us page publishes 'View All Jobs' "
                "to its Oracle Candidate Experience board hcmUI/.../sites/jubilant "
                "(CX_2005); the board's unfiltered findReqs listing is the membership authority"
            ),
            native_page_size=self.PAGE_SIZE,
            description_sections=[BODY_LABELS[field] for field in BODY_FIELDS],
            internal_fields_omitted=sorted(NONPUBLIC_FIELDS | {"Internal*"}),
            date_behavior=(
                "date_posted = board date-only PostedDate; detail ExternalPostedStartDate kept raw; "
                "the page's localised Posting Date and request time are never used"
            ),
        )
        return time.perf_counter() - started

    # --------------------------------------------------------------- evidence
    def evidence(self, response, value, *, source_kind: str):
        """Archive public API JSON; detail responses omit internal fields."""
        if not self.http.output:
            return
        encoded = json.dumps(value, ensure_ascii=False).encode()
        digest = sha256(encoded).hexdigest()
        target = self.http.output / "responses" / f"{digest}.gz"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(gzip.compress(encoded))
        record = {
            "url": str(response.request.url),
            "final_url": str(response.url),
            "method": "GET",
            "request_body": None,
            "status": response.status_code,
            "sha256": digest,
            "content_type": response.headers.get("content-type"),
            "redirects": [str(item.url) for item in response.history],
            "fetched_at": now(),
            "source_kind": source_kind,
            "public_fields_only": source_kind == "oracle_exact_detail",
            "omitted_fields": sorted(NONPUBLIC_FIELDS | {"Internal*"})
            if source_kind == "oracle_exact_detail" else [],
        }
        with (self.http.output / "requests.jsonl").open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # --------------------------------------------------------------- listing
    async def _listing(self, api, offset: int):
        if not self.page_guard():
            raise ValueError("Jubilant FoodWorks native listing pagination budget exhausted")
        response = await api.get(ORACLE_ORIGIN + LISTING_PATH,
                                 params=listing_params(offset, self.PAGE_SIZE))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Jubilant FoodWorks listing page at offset {offset} was not JSON") from exc
        page = parse_listing_page(payload, offset, self.PAGE_SIZE)
        if page["issues"]:
            raise ValueError(f"Jubilant FoodWorks listing page at offset {offset} has issues: {page['issues']}")
        self.evidence(response, payload, source_kind="oracle_unfiltered_listing")
        return page, str(response.url)

    async def _collect_listing(self, api) -> dict:
        started = time.perf_counter()
        ids: list[str] = []
        pages: list[dict] = []
        rows_by_id: dict[str, dict] = {}
        row_urls: dict[str, str] = {}
        total = None
        organizations_facet = []
        workplace_types_facet = []
        offset = 0
        while True:
            page, listing_url = await self._listing(api, offset)
            if total is None:
                total = page["total"]
                organizations_facet = page["organizations_facet"]
                workplace_types_facet = page["workplace_types_facet"]
            elif page["total"] != total:
                raise ValueError(f"Jubilant FoodWorks listing total changed during collection: {total} -> {page['total']}")
            current = [str(row["Id"]) for row in page["rows"]]
            if len(set(current)) != len(current):
                raise ValueError(f"Jubilant FoodWorks listing duplicate ids at offset {offset}")
            if set(ids).intersection(current):
                raise ValueError(f"Jubilant FoodWorks listing overlap at offset {offset}")
            ids.extend(current)
            for row in page["rows"]:
                identity = str(row["Id"])
                rows_by_id[identity] = row
                row_urls[identity] = listing_url
            pages.append({
                "offset": offset,
                "rows": len(current),
                "first_id": current[0] if current else None,
                "last_id": current[-1] if current else None,
                "source_total": page["total"],
                "url": listing_url,
            })
            if len(ids) >= total:
                break
            if not page["rows"]:
                raise ValueError(f"Jubilant FoodWorks listing empty page before total at offset {offset}")
            if len(page["rows"]) < self.PAGE_SIZE:
                raise ValueError(f"Jubilant FoodWorks listing short page before total at offset {offset}")
            offset += self.PAGE_SIZE
        if len(ids) != total or len(set(ids)) != total:
            raise ValueError(f"Jubilant FoodWorks listing membership mismatch: ids={len(ids)} unique={len(set(ids))} total={total}")
        return {
            "ids": ids,
            "total": total,
            "pages": pages,
            "rows_by_id": rows_by_id,
            "row_urls": row_urls,
            "organizations_facet": organizations_facet,
            "workplace_types_facet": workplace_types_facet,
            "duration_seconds": max(0.0, time.perf_counter() - started),
        }

    # ---------------------------------------------------------------- detail
    async def _detail(self, api, job):
        response = await api.get(ORACLE_ORIGIN + DETAILS_PATH, params=detail_params(job.job_id))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Jubilant FoodWorks detail for {job.job_id} was not JSON") from exc
        row = parse_detail_page(payload, job.job_id, job.title)
        public = public_detail(row)
        self.evidence(response, {**payload, "items": [public]}, source_kind="oracle_exact_detail")
        sections, role_text, missing = description_sections(public)
        job.raw["detail"] = public
        job.raw["detail_request_url"] = str(response.url)
        job.raw["detail_public_fields_only"] = True
        job.raw["omitted_internal_fields"] = sorted(NONPUBLIC_FIELDS | {"Internal*"})
        org = organization_details(public, job.raw["listing"], getattr(self, "_org_map", {}))
        job.raw["organization"] = org
        job.raw["published_body_sections"] = [
            {"field": section["field"], "label": section["label"],
             "text_chars": section["text_chars"], "text_sha256": section["text_sha256"]}
            for section in sections
        ]
        job.raw["missing_sections"] = missing
        job.raw["role_description_chars"] = len(role_text)
        job.raw["corporate_description_chars"] = len(visible_text(public.get("CorporateDescriptionStr")))
        job.description_method = "jubilant.official_CX_2005.oracle_external_sections"
        job.description_source_url = str(response.url)
        job.requisition_id = str(public["RequisitionId"])
        job.apply_url = apply_page_url(job.job_id)
        job.date_posted = job.raw["listing"].get("PostedDate")
        job.date_posted_raw = {
            "listing_PostedDate": job.raw["listing"].get("PostedDate"),
            "detail_ExternalPostedStartDate": public.get("ExternalPostedStartDate"),
            "page_posting_date_is_localised": True,
        }
        job.date_updated = None
        job.valid_through = public.get("ExternalPostedEndDate") or job.raw["listing"].get("PostingEndDate") or None
        job.language = public.get("ContentLocale") or job.raw["listing"].get("Language") or None
        job.locations = location_values(public, job.raw["listing"])
        job.department = _clean(public.get("Category") or job.raw["listing"].get("Category"))
        job.team = org.get("organization_name")
        job.employment_type = _clean(public.get("JobSchedule") or job.raw["listing"].get("JobSchedule")
                                     or public.get("JobType") or job.raw["listing"].get("JobType"))
        job.workplace_type = _clean(public.get("WorkplaceType") or public.get("WorkplaceTypeCode")
                                    or job.raw["listing"].get("WorkplaceType")
                                    or job.raw["listing"].get("WorkplaceTypeCode"))
        job.salary = None
        job.qualifications = public.get("ExternalQualificationsStr") or None
        job.responsibilities = public.get("ExternalResponsibilitiesStr") or None
        job.education = _clean(public.get("StudyLevel"))
        job.experience = _clean(public.get("JobLevel"))
        job.employer = _clean(public.get("LegalEmployer"))
        if len(role_text.strip()) < 100:
            # The publisher has no role-description sections (or only a very short
            # title-like fragment). Do not substitute the corporate ABOUT US text.
            job.description_html = ""
            job.description_text = ""
            job.description_unavailable = True
            job.warnings.append("source_description_missing" if not role_text.strip()
                                else "publisher_description_too_short")
            if job.raw["corporate_description_chars"] and not role_text.strip():
                job.warnings.append("published_body_is_about_us_only")
            return job
        job.description_html = assemble_description(sections)
        job.description_text = visible_text(job.description_html)
        job.description_unavailable = False
        return job

    # ------------------------------------------------------------------- run
    def _job_from_listing(self, row: dict, listing_url: str, org_map: dict[str, str]):
        identity = str(row["Id"])
        org = organization_details({}, row, org_map)
        job = self.job(
            {"listing": row, "organization": org},
            title=row["Title"],
            job_id=identity,
            url=job_page_url(identity),
            apply_url=apply_page_url(identity),
            date_posted=row.get("PostedDate"),
            date_posted_raw=row.get("PostedDate"),
            employment_type=_clean(row.get("JobSchedule")),
            workplace_type=_clean(row.get("WorkplaceType") or row.get("WorkplaceTypeCode")),
            language=row.get("Language") or None,
        )
        job.record_identity = f"jubilant-foodworks:{identity}"
        job.raw["listing_request_url"] = listing_url
        job.raw["site_number"] = SITE_NUMBER
        job.raw["board_url"] = self._board_url
        if org["organization_id"] and not org["organization_name"]:
            job.warnings.append("unmapped_organization_id")
        return job

    def _role_description_status(self) -> dict:
        status = {
            "verified_role_text_ge_100": 0,
            "publisher_empty_role_text": 0,
            "publisher_short_role_text": 0,
            "corporate_only": 0,
            "no_body_section": 0,
            "description_verified": 0,
            "description_unavailable": 0,
        }
        for job in self.result.jobs:
            chars = job.raw.get("role_description_chars", 0)
            corporate = job.raw.get("corporate_description_chars", 0)
            if chars >= 100:
                status["verified_role_text_ge_100"] += 1
            elif chars == 0:
                status["publisher_empty_role_text"] += 1
                if corporate:
                    status["corporate_only"] += 1
                else:
                    status["no_body_section"] += 1
            else:
                status["publisher_short_role_text"] += 1
            if job.description_verified:
                status["description_verified"] += 1
            if job.description_unavailable:
                status["description_unavailable"] += 1
        return status

    async def run(self):
        entry_seconds = await self._entry()
        self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
        async with HTTP(None, delay=self.http.delay, timeout=self.http.client.timeout,
                        retries=self.http.retries,
                        max_response_bytes=self.http.max_response_bytes) as api:
            listing_started = time.perf_counter()
            listing = await self._collect_listing(api)
            listing_seconds = max(0.0, time.perf_counter() - listing_started)
            self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
            self.result.timings["source_collection_seconds"] = round(entry_seconds + listing_seconds, 6)
            self.result.expected_total = listing["total"]
            self.result.metadata["listing_pages"] = listing["pages"]
            self.result.metadata["company_published_job_ids"] = listing["ids"]
            self.result.metadata["native_page_size"] = self.PAGE_SIZE
            self.result.metadata["listing_requests"] = {
                "endpoint": ORACLE_ORIGIN + LISTING_PATH,
                "finder": _finder(0),
                "pagination": "offset=0,25,...,425; limit=25",
            }
            org_map: dict[str, str] = {}
            org_counts: dict[str, int] = {}
            listing_page_facets = listing["organizations_facet"]
            if listing_page_facets:
                org_map = {str(item["Id"]): item["Name"] for item in listing_page_facets}
                org_counts = {item["Name"]: item["TotalCount"] for item in listing_page_facets}
                self.result.metadata["organizations_facet"] = listing_page_facets
                self.result.metadata["organization_counts"] = org_counts
            self._org_map = org_map
            self.result.metadata["workplace_types_facet"] = listing["workplace_types_facet"]
            for identity in listing["ids"]:
                self.add(self._job_from_listing(listing["rows_by_id"][identity],
                                                listing["row_urls"][identity], org_map))
            # Scope sanity: every row must map to a published organization facet
            # name and every facet name must be one of the eight observed JFL brands.
            row_org_ids = {str(row.get("OrganizationId")) for row in listing["rows_by_id"].values()
                           if row.get("OrganizationId") is not None}
            unexpected = sorted(set(org_map.values()) - KNOWN_JFL_ORGANIZATIONS)
            unmapped = sorted(row_org_ids - set(org_map))
            facet_total = sum(org_counts.values())
            scope_verified = (not unexpected and not unmapped and facet_total == listing["total"]
                              and bool(org_map))
            self.result.metadata.update(
                company_scope_verified=scope_verified,
                company_scope={
                    "site_name": "Jubilant Corporate",
                    "site_number": SITE_NUMBER,
                    "linked_from": OFFICIAL_ENTRY,
                    "organizations_facet": self.result.metadata.get("organizations_facet", []),
                    "organization_counts": org_counts,
                    "row_organization_ids": sorted(row_org_ids),
                    "unmapped_organization_ids": unmapped,
                    "unexpected_organization_names": unexpected,
                    "facet_total": facet_total,
                    "listing_total": listing["total"],
                    "observed_jfl_brands": sorted(KNOWN_JFL_ORGANIZATIONS),
                },
                scope_reason=(
                    "The official Jubilant FoodWorks Work With Us page publishes 'View All Jobs' "
                    "to Oracle CX_2005 'Jubilant Corporate Careers'. Its unfiltered organizationsFacet "
                    "contains only eight Jubilant FoodWorks brands and sums to the full 437-row listing."
                ),
            )
            if not scope_verified:
                self.result.errors.append(
                    f"company_scope_unexpected_organizations:{unexpected or unmapped}")

            self.result.metadata["listing_complete_initial"] = (
                len(self.result.jobs) == listing["total"] and not self.result.errors)
            await self.hydrate(self.result.jobs, lambda job: self._detail(api, job))
            self.result.metadata["role_description_status"] = self._role_description_status()

            final_started = time.perf_counter()
            final_error = None
            final = None
            try:
                final = await self._collect_listing(api)
            except Exception as exc:
                final_error = f"{type(exc).__name__}: {exc}"
                self.result.errors.append(f"final_listing_recheck_failed: {final_error}")
            final_seconds = max(0.0, time.perf_counter() - final_started)
            self.result.timings["final_listing_phase_seconds"] = round(final_seconds, 6)

            reconciliation = {
                "initial_total": listing["total"],
                "initial_ids": listing["ids"],
                "initial_pages": len(listing["pages"]),
                "final_total": None,
                "final_ids": [],
                "final_pages": [],
                "added_ids": [],
                "removed_ids": [],
                "sets_match": False,
                "recheck_error": final_error,
                "recheck_passes": 1 if final is not None else 0,
            }
            if final is not None:
                collected = [job.job_id for job in self.result.jobs]
                new_ids = [identity for identity in final["ids"] if identity not in set(collected)]
                if new_ids:
                    new_jobs = []
                    for identity in new_ids:
                        new_job = self._job_from_listing(final["rows_by_id"][identity],
                                                         final["row_urls"][identity], org_map)
                        self.add(new_job)
                        new_jobs.append(new_job)
                    await self.hydrate(new_jobs, lambda job: self._detail(api, job))
                    reconciliation["added_ids"] = new_ids
                    # A second pass makes the final snapshot stable; if the board
                    # changes again, record it instead of looping forever.
                    second_started = time.perf_counter()
                    try:
                        second = await self._collect_listing(api)
                        reconciliation["final_total"] = second["total"]
                        reconciliation["final_ids"] = second["ids"]
                        reconciliation["final_pages"] = second["pages"]
                        reconciliation["recheck_passes"] = 2
                        second_new = [identity for identity in second["ids"]
                                      if identity not in {job.job_id for job in self.result.jobs}]
                        if second_new:
                            self.result.errors.append(
                                f"final_listing_changed_again:{','.join(second_new[:20])}")
                            final = second
                        else:
                            final = second
                    except Exception as exc:
                        self.result.errors.append(f"final_listing_second_recheck_failed: {type(exc).__name__}: {exc}")
                    self.result.timings["final_listing_second_pass_seconds"] = round(
                        max(0.0, time.perf_counter() - second_started), 6)
                if not reconciliation["final_ids"]:
                    reconciliation["final_total"] = final["total"]
                    reconciliation["final_ids"] = final["ids"]
                    reconciliation["final_pages"] = final["pages"]
                collected = [job.job_id for job in self.result.jobs]
                reconciliation["removed_ids"] = [identity for identity in collected
                                                 if identity not in set(reconciliation["final_ids"])]
                reconciliation["sets_match"] = (
                    len(collected) == len(set(reconciliation["final_ids"]))
                    and set(collected) == set(reconciliation["final_ids"])
                    and reconciliation["final_total"] is not None
                    and len(collected) == reconciliation["final_total"]
                )
                if reconciliation["removed_ids"]:
                    self.result.errors.append(
                        f"final_listing_removed_ids:{','.join(reconciliation['removed_ids'][:20])}")
                self.result.expected_total = reconciliation["final_total"]
                self.result.listing_complete = (
                    reconciliation["sets_match"] and not final_error
                    and not any(error.startswith(("final_listing", "company_scope"))
                                for error in self.result.errors)
                )
            else:
                self.result.listing_complete = False
            self.result.metadata["final_listing_reconciliation"] = reconciliation
            self.result.metadata["role_description_status"] = self._role_description_status()
            self.result.metadata["detail_requests"] = {
                "endpoint": ORACLE_ORIGIN + DETAILS_PATH,
                "finder": 'ById;Id="<ID>",siteNumber=CX_2005',
                "expand": "all",
            }
            self.result.metadata["apply_route"] = ".../job/<Id>/apply/email (observed from the board's APPLY NOW button)"
        return self.result
