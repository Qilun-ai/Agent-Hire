"""Icertis' official careers journey: icertis.com careers -> Oracle CX_1 board.

The requested entry page (https://www.icertis.com/company/careers/) publishes
"Explore Open Roles" to the company's own Oracle Candidate Experience board
``Jobs-at-Icertis`` (site number CX_1).  That board's public shell, siteSettings
and unfiltered ``findReqs`` listing are the membership authority used here.  The
talent-community sign-up route on the same board exposes no requisition IDs and
is deliberately excluded.

The board renders 25 cards per batch and loads the remaining rows on scroll.
The flow mirrors that 25-row offset pagination, then hydrates every exact listed
Id through ``recruitingCEJobRequisitionDetails``.  The rendered job page shows
five public sections: JOB DESCRIPTION, RESPONSIBILITIES, QUALIFICATIONS, ABOUT
US and ABOUT THE TEAM, backed by ExternalDescriptionStr,
ExternalResponsibilitiesStr, ExternalQualificationsStr, CorporateDescriptionStr
and OrganizationDescriptionStr.  Internal qualifications/responsibilities,
hiring-manager fields and admin flags are not public and are omitted from
stored evidence.

Dates: the board's date-only ``PostedDate`` is stored as ``date_posted``.  The
page's "Posting Date" is a localised rendering of ``ExternalPostedStartDate``
(our browser renders it in IST), so it is retained as raw evidence only.
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
from ..http import HTTP
from ..models import now


OFFICIAL_ENTRY = "https://www.icertis.com/company/careers/"
ORACLE_HOST = "iaaviz.fa.ocs.oraclecloud.com"
ORACLE_ORIGIN = "https://iaaviz.fa.ocs.oraclecloud.com"
SITE_NUMBER = "CX_1"
SITE_SHORT_URL = "Jobs-at-Icertis"
BOARD_PATH = f"/hcmUI/CandidateExperience/en/sites/{SITE_SHORT_URL}"
BOARD_URL = ORACLE_ORIGIN + BOARD_PATH
LISTING_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAILS_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
SETTINGS_PATH = f"/hcmRestApi/CandidateExperience/en/siteSettings/{SITE_NUMBER}"
TALENT_COMMUNITY_PATH = BOARD_PATH + "/join-talent-community"
PAGE_SIZE = 25  # the board's own batch size (POST body limit=25)
FACETS = ("WORK_LOCATIONS;WORKPLACE_TYPES;TITLES;CATEGORIES;ORGANIZATIONS;"
          "POSTING_DATES;FLEX_FIELDS;LOCATIONS")
EXPAND = ("requisitionList.workLocation,requisitionList.otherWorkLocations,"
          "requisitionList.secondaryLocations,flexFieldsFacet.values,"
          "requisitionList.requisitionFlexFields")
ROLE_FIELDS = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
               "ExternalQualificationsStr")
PAGE_FIELDS = ("CorporateDescriptionStr", "OrganizationDescriptionStr")
BODY_FIELDS = ROLE_FIELDS + PAGE_FIELDS
BODY_LABELS = {
    "ExternalDescriptionStr": "JOB DESCRIPTION",
    "ExternalResponsibilitiesStr": "RESPONSIBILITIES",
    "ExternalQualificationsStr": "QUALIFICATIONS",
    "CorporateDescriptionStr": "ABOUT US",
    "OrganizationDescriptionStr": "ABOUT THE TEAM",
}
# Internal/personnel fields seen in the detail response; never published on the
# job page and therefore not retained as public source evidence.
NONPUBLIC_FIELDS = frozenset({
    "InternalQualificationsStr", "InternalResponsibilitiesStr",
    "HiringManager", "ObjectVerNumberProfile", "ApplyWhenNotPostedFlag",
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
BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "ul", "ol", "li", "h1", "h2", "h3",
    "h4", "h5", "h6", "br", "tr", "td", "th", "table", "blockquote", "pre",
    "hr",
})
COMPENSATION_END_MARKERS = (
    "Our Commitments", "What We Offer", "Benefits", "About Us",
    "Equal Employment", "Why Join", "Perks", "About Icertis",
)
MONEY = r"\$([\d,]+)"


def visible_text(html: str | None) -> str:
    """Block-aware visible text, matching how a browser joins inline spans.

    A naive ``get_text("\\n")`` turns adjacent inline spans (``Who we a`` +
    ``re:``) into ``Who we a re:``; this walker inserts separators only around
    block-level elements, so the result matches the rendered job page.
    """
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
        return urlunsplit(("https", ORACLE_HOST, BOARD_PATH, "", ""))
    return None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Validate the official entry page and the single board it publishes."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname not in {"www.icertis.com", "icertis.com"}
            or parsed.path.rstrip("/") != "/company/careers"):
        raise ValueError("Icertis official careers entry is not the inspected icertis.com page")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "icertis" not in title.casefold() or "career" not in title.casefold():
        raise ValueError("Icertis official careers page title/identity changed")
    boards: set[str] = set()
    anchor_texts: set[str] = set()
    talent: set[str] = set()
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
        elif path == TALENT_COMMUNITY_PATH:
            talent.add(urlunsplit(("https", ORACLE_HOST, TALENT_COMMUNITY_PATH, "", "")))
        elif "/CandidateExperience/en/sites/" in path:
            other.add(target)
    if boards != {BOARD_URL}:
        raise ValueError(f"Icertis official entry no longer publishes exactly the inspected board: {sorted(boards)}")
    if other:
        raise ValueError(f"Icertis official entry publishes unexpected additional board links: {sorted(other)}")
    return {
        "url": response_url,
        "title": title,
        "board_url": BOARD_URL,
        "board_anchor_texts": sorted(anchor_texts),
        "talent_community_urls": sorted(talent),
    }


def parse_board_shell(html: str, response_url: str = BOARD_URL) -> dict:
    """Validate the board's published base tag/site identity."""
    parsed = urlsplit(response_url)
    if parsed.hostname != ORACLE_HOST or parsed.path.rstrip("/") not in {
            BOARD_PATH, BOARD_PATH + "/jobs"}:
        raise ValueError("Icertis board shell came from an unexpected Oracle route")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    og_title = None
    og_site = None
    for meta in soup.select("meta[property]"):
        if meta.get("property") == "og:title":
            og_title = meta.get("content")
        elif meta.get("property") == "og:site_name":
            og_site = meta.get("content")
    if "icertis" not in title.casefold() or og_title != "Icertis Careers" or og_site != "Icertis":
        raise ValueError("Icertis board shell title/identity changed")
    base = soup.select_one("base[data-apibaseurl][data-sitenumber]")
    if base is None:
        raise ValueError("Icertis board shell no longer publishes its site-number base tag")
    if (base.get("href") != BOARD_PATH
            or urlsplit(base.get("data-apibaseurl", "")).hostname != ORACLE_HOST
            or base.get("data-sitenumber") != SITE_NUMBER):
        raise ValueError("Icertis board shell site number/API host changed")
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
    """Validate the public siteSettings identity for CX_1."""
    app = value.get("app") if isinstance(value, dict) else None
    if not isinstance(app, dict):
        raise ValueError("Icertis siteSettings response has no app block")
    if (app.get("siteName") != "Icertis" or app.get("siteNumber") != SITE_NUMBER
            or app.get("siteCode") != "01" or app.get("siteLang") != "en"):
        raise ValueError("Icertis siteSettings no longer identify the inspected company site")
    templates = app.get("templates") or {}
    if not isinstance(templates, dict) or not templates.get("SEARCH_PAGE") or not templates.get("JOB_DETAILS"):
        raise ValueError("Icertis siteSettings no longer publish search/detail templates")
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
        raise ValueError("Icertis listing response is not an object")
    if value.get("errors"):
        raise ValueError(f"Icertis listing response returned errors: {value.get('errors')}")
    items = value.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("Icertis listing response has no single board result")
    block = items[0]
    if not isinstance(block, dict):
        raise ValueError("Icertis listing result block is not an object")
    total = block.get("TotalJobsCount")
    rows = block.get("requisitionList")
    if (not isinstance(total, int) or total < 0 or not isinstance(rows, list)
            or block.get("SiteNumber") != SITE_NUMBER
            or block.get("Offset") != requested_offset
            or block.get("Limit") != page_size
            or block.get("SortBy") != "POSTING_DATES_DESC"
            or len(rows) > page_size):
        raise ValueError("Icertis listing identity, pagination or unfiltered search state changed")
    applied = [key for key in FILTER_FIELDS if block.get(key) not in (None, "", False, [])]
    if applied:
        raise ValueError(f"Icertis listing unexpectedly applied filters: {applied}")
    expected_rows = min(page_size, max(0, total - requested_offset))
    issues: list[str] = []
    if len(rows) != expected_rows:
        issues.append(f"icertis_listing_row_count_mismatch:{len(rows)}!={expected_rows}")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("icertis_listing_row_not_object")
            continue
        identity = row.get("Id")
        if not isinstance(identity, str) or not re.fullmatch(r"\d+", identity):
            issues.append("icertis_listing_row_missing_numeric_id")
            continue
        if identity in seen:
            issues.append(f"icertis_listing_duplicate_id:{identity}")
        seen.add(identity)
        if not isinstance(row.get("Title"), str) or not row["Title"].strip():
            issues.append(f"icertis_listing_row_missing_title:{identity}")
        posted = row.get("PostedDate")
        if posted is not None and not (isinstance(posted, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted)):
            issues.append(f"icertis_listing_row_invalid_posted_date:{identity}")
        if row.get("secondaryLocations") is not None and not isinstance(row["secondaryLocations"], list):
            issues.append(f"icertis_listing_secondary_locations_not_list:{identity}")
        if row.get("requisitionFlexFields") is not None and not isinstance(row["requisitionFlexFields"], list):
            issues.append(f"icertis_listing_flex_fields_not_list:{identity}")
    return {
        "rows": rows,
        "total": total,
        "offset": requested_offset,
        "page_size": page_size,
        "site_number": block.get("SiteNumber"),
        "issues": issues,
    }


def parse_detail_page(value: dict, job_id: str, expected_title: str | None = None) -> dict:
    """Validate the exact detail response for one listed Id."""
    if not isinstance(value, dict):
        raise ValueError("Icertis detail response is not an object")
    items = value.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError(f"Icertis detail response for {job_id} has no single posting")
    row = items[0]
    if not isinstance(row, dict):
        raise ValueError(f"Icertis detail response for {job_id} is not an object")
    if str(row.get("Id")) != str(job_id):
        raise ValueError(f"Icertis detail response returned a different posting than {job_id}")
    if expected_title is not None and normalized_title(row.get("Title")) != normalized_title(expected_title):
        raise ValueError(f"Icertis detail title changed for {job_id}")
    if row.get("RequisitionId") is None:
        raise ValueError(f"Icertis detail for {job_id} has no requisition id")
    for field in BODY_FIELDS:
        if field not in row:
            raise ValueError(f"Icertis detail for {job_id} omitted inspected section {field}")
        if row[field] is not None and not isinstance(row[field], str):
            raise ValueError(f"Icertis detail section {field} for {job_id} is not HTML text")
    return row


def public_detail(row: dict) -> dict:
    """Retain the public detail row; internal/personnel fields are not published."""
    return {key: value for key, value in row.items()
            if key not in NONPUBLIC_FIELDS and not key.startswith("Internal")}


def description_sections(detail: dict) -> tuple[list[dict], str, list[str]]:
    """Return the exact non-empty rendered sections, their role text and misses."""
    sections, missing = [], []
    for field in BODY_FIELDS:
        value = detail.get(field)
        text = visible_text(value) if isinstance(value, str) else ""
        if text:
            sections.append({"field": field, "label": BODY_LABELS[field], "html": value, "text": text})
        else:
            missing.append(field)
    role_text = "\n".join(section["text"] for section in sections if section["field"] in ROLE_FIELDS)
    return sections, role_text, missing


def assemble_description(sections: list[dict]) -> str:
    return "\n".join(
        f'<section data-source-field="{section["field"]}"><h2>{section["label"]}</h2>{section["html"]}</section>'
        for section in sections
    )


def compensation_fields(detail: dict) -> dict | None:
    """Retain the published Compensation block and parse only explicit patterns.

    ``requisitionFlexFields`` is empty for every Icertis posting, so the only
    published salary/OTE evidence is the Compensation block inside
    ExternalQualificationsStr.  The block text is always retained; numeric
    fields are added only when the exact source pattern is present.
    """
    text = visible_text(detail.get("ExternalQualificationsStr"))
    if "Compensation" not in text:
        return None
    start = text.find("Compensation")
    block = text[start:]
    for marker in COMPENSATION_END_MARKERS:
        position = block.find(marker, len("Compensation"))
        if position > 0:
            block = block[:position]
    block = block.strip()
    if not block:
        return None
    result: dict = {"source": "ExternalQualificationsStr.Compensation", "text": block}
    minimum = re.search(rf"Min\s+{MONEY}\s*-\s*Max\s+{MONEY}\s+USD\s+per\s+year", block, re.I)
    if minimum:
        result.update(min=int(minimum.group(1).replace(",", "")),
                      max=int(minimum.group(2).replace(",", "")),
                      currency="USD", period="year")
    ote = re.search(rf"OTE\s*:?\s*{MONEY}\s*(?:to|-|–)\s*{MONEY}", block, re.I)
    if ote:
        result.update(ote_min=int(ote.group(1).replace(",", "")),
                      ote_max=int(ote.group(2).replace(",", "")))
    base = re.search(rf"Base\s*:?\s*{MONEY}\s*(?:to|-|–)\s*{MONEY}", block, re.I)
    if base:
        result.update(base_min=int(base.group(1).replace(",", "")),
                      base_max=int(base.group(2).replace(",", "")))
    bonus = re.search(r"plus a (\d+)% annual bonus target", block, re.I)
    if bonus:
        result["bonus_target_percent"] = int(bonus.group(1))
    return result


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
    for key in ("secondaryLocations", "workLocation", "otherWorkLocations"):
        source = detail.get(key)
        if not source:
            source = listing.get(key) if key == "secondaryLocations" else None
        for item in source or []:
            if isinstance(item, dict):
                add(item.get("Name") or item.get("LocationName"))
            elif isinstance(item, str):
                add(item)
    return values


class Icertis(Adapter):
    """Collect only the membership published on the official CX_1 board."""

    VERIFICATION_REVISION = "icertis.official-Jobs-at-Icertis-CX_1.1"
    PAGE_SIZE = PAGE_SIZE

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._board_url = BOARD_URL

    # ---------------------------------------------------------------- entry
    async def _entry(self) -> float:
        started = time.perf_counter()
        entry = await self.http.get(self.company.careers_url)
        if str(entry.url).rstrip("/") != OFFICIAL_ENTRY.rstrip("/"):
            raise ValueError(f"Icertis official entry redirected: {self.company.careers_url} -> {entry.url}")
        entry_info = parse_official_entry(entry.text, str(entry.url))
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
            company_listing_url=str(jobs_page.url),
            board_shell_url=str(board.url),
            board_shell=board_info,
            jobs_shell=jobs_info,
            site_number=SITE_NUMBER,
            site_settings=site_config,
            listing_authority=(
                "Icertis official 'Explore Open Roles' link to its Oracle Candidate Experience "
                "board Jobs-at-Icertis (CX_1); unfiltered findReqs membership"
            ),
            talent_community_excluded={
                "urls": entry_info["talent_community_urls"],
                "reason": "talent-community sign-up route; no requisition IDs or vacancy listing",
            },
            native_page_size=self.PAGE_SIZE,
            description_sections=[BODY_LABELS[field] for field in BODY_FIELDS],
            internal_fields_omitted=sorted(NONPUBLIC_FIELDS | {"Internal*"}),
            date_behavior=(
                "date_posted = board date-only PostedDate; ExternalPostedStartDate kept raw; "
                "the page's localised Posting Date and request time are never used"
            ),
        )
        return time.perf_counter() - started

    # --------------------------------------------------------------- listing
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

    async def _listing(self, api, offset: int):
        if not self.page_guard():
            raise ValueError("Icertis native listing pagination budget exhausted")
        response = await api.get(ORACLE_ORIGIN + LISTING_PATH, params=listing_params(offset, self.PAGE_SIZE))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Icertis listing page at offset {offset} was not JSON") from exc
        page = parse_listing_page(payload, offset, self.PAGE_SIZE)
        if page["issues"]:
            raise ValueError(f"Icertis listing page at offset {offset} has issues: {page['issues']}")
        self.evidence(response, payload, source_kind="oracle_unfiltered_listing")
        return page, str(response.url)

    # ---------------------------------------------------------------- detail
    async def _detail(self, api, job):
        response = await api.get(ORACLE_ORIGIN + DETAILS_PATH, params=detail_params(job.job_id))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Icertis detail for {job.job_id} was not JSON") from exc
        row = parse_detail_page(payload, job.job_id, job.title)
        public = public_detail(row)
        self.evidence(response, {**payload, "items": [public]}, source_kind="oracle_exact_detail")
        sections, role_text, missing = description_sections(public)
        job.raw["detail"] = public
        job.raw["detail_request_url"] = str(response.url)
        job.raw["detail_public_fields_only"] = True
        job.raw["omitted_internal_fields"] = sorted(NONPUBLIC_FIELDS | {"Internal*"})
        job.raw["description_sections"] = [
            {"field": section["field"], "label": section["label"],
             "text_chars": len(section["text"])}
            for section in sections
        ]
        job.raw["missing_sections"] = missing
        job.description_method = "icertis.official_Jobs-at-Icertis.oracle_external_sections"
        job.description_source_url = str(response.url)
        job.requisition_id = str(public["RequisitionId"])
        job.apply_url = apply_page_url(job.job_id)
        job.date_posted = job.raw["listing"].get("PostedDate")
        job.date_posted_raw = {
            "listing_PostedDate": job.raw["listing"].get("PostedDate"),
            "detail_ExternalPostedStartDate": public.get("ExternalPostedStartDate"),
            "page_posting_date_is_localised": True,
        }
        job.valid_through = public.get("ExternalPostedEndDate") or job.raw["listing"].get("PostingEndDate") or None
        job.language = public.get("ContentLocale") or job.raw["listing"].get("Language") or None
        job.locations = location_values(public, job.raw["listing"])
        job.department = public.get("Category") or job.raw["listing"].get("Category") or None
        job.team = public.get("JobFunction") or job.raw["listing"].get("JobFunction") or None
        job.employment_type = (public.get("JobSchedule") or job.raw["listing"].get("JobSchedule")
                               or public.get("JobType") or None)
        job.workplace_type = (public.get("WorkplaceType") or job.raw["listing"].get("WorkplaceType")
                              or public.get("WorkplaceTypeCode") or None)
        job.salary = compensation_fields(public)
        job.qualifications = public.get("ExternalQualificationsStr") or None
        job.responsibilities = public.get("ExternalResponsibilitiesStr") or None
        job.education = public.get("StudyLevel") or None
        job.experience = public.get("JobLevel") or None
        job.employer = public.get("LegalEmployer") or None
        if len(role_text.strip()) < 100:
            # No external role sections: do not substitute corporate boilerplate.
            job.description_html = ""
            job.description_text = ""
            job.description_unavailable = True
            job.warnings.extend(["source_description_missing", "description_not_full_field"])
            return job
        job.description_html = assemble_description(sections)
        job.description_text = visible_text(job.description_html)
        return job

    # ------------------------------------------------------------------- run
    def _job_from_listing(self, row: dict, listing_url: str):
        identity = str(row["Id"])
        job = self.job(
            {"listing": row},
            title=row["Title"],
            job_id=identity,
            url=job_page_url(identity),
            apply_url=apply_page_url(identity),
            date_posted=row.get("PostedDate"),
            date_posted_raw=row.get("PostedDate"),
            employment_type=row.get("JobSchedule") or None,
            workplace_type=row.get("WorkplaceType") or row.get("WorkplaceTypeCode") or None,
            language=row.get("Language") or None,
        )
        job.record_identity = f"icertis:{identity}"
        job.raw["listing_request_url"] = listing_url
        job.raw["site_number"] = SITE_NUMBER
        job.raw["board_url"] = self._board_url
        return job

    async def run(self):
        entry_seconds = await self._entry()
        self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
        listing_started = time.perf_counter()
        async with HTTP(None, delay=self.http.delay, timeout=self.http.client.timeout,
                        retries=self.http.retries,
                        max_response_bytes=self.http.max_response_bytes) as api:
            ids: set[str] = set()
            pages: list[dict] = []
            expected_total = None
            offset = 0
            while True:
                page, listing_url = await self._listing(api, offset)
                if expected_total is None:
                    expected_total = page["total"]
                    self.result.expected_total = expected_total
                elif page["total"] != expected_total:
                    self.result.errors.append(
                        f"icertis_listing_total_changed:{expected_total}->{page['total']}")
                    break
                current = [str(row["Id"]) for row in page["rows"]]
                if len(set(current)) != len(current):
                    self.result.errors.append(f"icertis_listing_duplicate_ids_at_offset:{offset}")
                    break
                if ids.intersection(current):
                    self.result.errors.append(f"icertis_listing_overlap_at_offset:{offset}")
                    break
                ids.update(current)
                pages.append({"offset": offset, "rows": len(current),
                              "first_id": current[0] if current else None,
                              "last_id": current[-1] if current else None,
                              "source_total": page["total"], "url": listing_url})
                for row in page["rows"]:
                    self.add(self._job_from_listing(row, listing_url))
                if len(ids) >= expected_total:
                    break
                if not page["rows"]:
                    self.result.errors.append(f"icertis_listing_empty_page_before_total:{offset}")
                    break
                if len(page["rows"]) < self.PAGE_SIZE:
                    self.result.errors.append(f"icertis_listing_short_page_before_total:{offset}")
                    break
                offset += self.PAGE_SIZE
            self.result.timings["listing_phase_seconds"] = round(
                max(0.0, time.perf_counter() - listing_started), 6)
            self.result.listing_complete = (
                expected_total is not None and len(ids) == expected_total and not self.result.errors
                and len(self.result.jobs) == expected_total)
            self.result.metadata["listing_pages"] = pages
            self.result.metadata["company_published_job_ids"] = sorted(ids)

            self.result.jobs = await self.hydrate(
                self.result.jobs, lambda job: self._detail(api, job))

            if self.result.listing_complete and "sample_limit" not in self.result.warnings:
                final_started = time.perf_counter()
                final_ids: set[str] = set()
                final_pages: list[dict] = []
                offset = 0
                while True:
                    page, listing_url = await self._listing(api, offset)
                    current = [str(row["Id"]) for row in page["rows"]]
                    if (page["total"] != expected_total or len(set(current)) != len(current)
                            or final_ids.intersection(current) or (not current and expected_total)):
                        self.result.errors.append("icertis_final_listing_did_not_reconcile")
                        break
                    final_ids.update(current)
                    final_pages.append({"offset": offset, "ids": current, "url": listing_url})
                    if len(final_ids) >= expected_total:
                        break
                    if len(page["rows"]) < self.PAGE_SIZE:
                        self.result.errors.append("icertis_final_listing_short_page")
                        break
                    offset += self.PAGE_SIZE
                if final_ids and final_ids != ids:
                    self.result.errors.append("icertis_listing_id_set_changed")
                self.result.listing_complete = (
                    self.result.listing_complete and bool(final_ids) and final_ids == ids
                    and not self.result.errors)
                self.result.metadata["final_listing_pages"] = final_pages
                self.result.metadata["final_listing_ids"] = sorted(final_ids)
                self.result.metadata["final_listing_reconciled"] = bool(final_ids) and final_ids == ids
                self.result.timings["final_listing_seconds"] = round(
                    max(0.0, time.perf_counter() - final_started), 6)
            self.result.metadata["membership_reconciliation"] = {
                "complete": self.result.listing_complete,
                "expected_total": expected_total,
                "collected_ids": len(ids),
                "final_ids": len(self.result.metadata.get("final_listing_ids") or []),
                "final_listing_reconciled": self.result.metadata.get("final_listing_reconciled"),
                "ui_batch_size": self.PAGE_SIZE,
                "pages": pages,
            }
        return self.result
