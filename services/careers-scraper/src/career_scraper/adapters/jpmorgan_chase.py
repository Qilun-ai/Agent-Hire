"""JPMorgan Chase's official careers journey onto its Oracle CX_1001 board.

The requested careers URL ``https://careers.jpmorgan.com/global/en/home`` redirects
to ``https://www.jpmorganchase.com/careers``, whose "Join our team"/"Apply now"
links publish the company's Oracle Candidate Experience board
``jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001``.

Membership is that board's unfiltered public ``findReqs`` listing (25-row
infinite-scroll batches, ``POSTING_DATES_DESC``).  Each exact listed ``Id`` is
hydrated with ``recruitingCEJobRequisitionDetails``.  The rendered job page shows
JOB DESCRIPTION / ABOUT US / ABOUT THE TEAM (and RESPONSIBILITIES /
QUALIFICATIONS when the corresponding public fields are non-empty).  The public
"Base Pay/Salary" flex field is retained exactly and parsed only where a range is
explicit.  The board's talent-community ``tc-join`` route, filtered keyword
subsets and event-registration forms are not separate membership sources.
"""
from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://careers.jpmorgan.com/global/en/home"
ENTRY_FINAL = "https://www.jpmorganchase.com/careers"
ORACLE_HOST = "jpmc.fa.oraclecloud.com"
ORACLE_ORIGIN = "https://jpmc.fa.oraclecloud.com"
SITE_NUMBER = "CX_1001"
SITE_NAME = "JPMC Candidate Experience page"
BOARD_PATH = f"/hcmUI/CandidateExperience/en/sites/{SITE_NUMBER}"
BOARD_URL = ORACLE_ORIGIN + BOARD_PATH
BOARD_LISTING_URL = BOARD_URL + "/jobs"
BOARD_REQUISITIONS_URL = BOARD_URL + "/requisitions"
TALENT_COMMUNITY_PATH = BOARD_PATH + "/tc-join"
LISTING_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAILS_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
SETTINGS_PATH = f"/hcmRestApi/CandidateExperience/en/siteSettings/{SITE_NUMBER}"
PAGE_SIZE = 25  # the board's own rendered batch size
RECONCILE_PAGE_SIZE = 100  # same public endpoint, larger batch for the final recheck
FACETS = ("WORK_LOCATIONS;WORKPLACE_TYPES;TITLES;CATEGORIES;ORGANIZATIONS;"
          "POSTING_DATES;FLEX_FIELDS;LOCATIONS")
EXPAND = ("requisitionList.workLocation,requisitionList.otherWorkLocations,"
          "requisitionList.secondaryLocations,flexFieldsFacet.values,"
          "requisitionList.requisitionFlexFields")
SORT_BY = "POSTING_DATES_DESC"

BODY_FIELDS = (
    "ExternalDescriptionStr",
    "ExternalResponsibilitiesStr",
    "ExternalQualificationsStr",
    "CorporateDescriptionStr",
    "OrganizationDescriptionStr",
)
BODY_LABELS = {
    "ExternalDescriptionStr": "JOB DESCRIPTION",
    "ExternalResponsibilitiesStr": "RESPONSIBILITIES",
    "ExternalQualificationsStr": "QUALIFICATIONS",
    "CorporateDescriptionStr": "ABOUT US",
    "OrganizationDescriptionStr": "ABOUT THE TEAM",
}
ROLE_FIELDS = (
    "ExternalDescriptionStr",
    "ExternalResponsibilitiesStr",
    "ExternalQualificationsStr",
)
# Internal/personnel fields observed in the detail response; none are rendered on
# the public job page, so they are omitted from stored public evidence.
NONPUBLIC_FIELDS = frozenset({
    "HiringManager", "ObjectVerNumberProfile", "ApplyWhenNotPostedFlag",
})
FILTER_FIELDS = (
    "Keyword", "Location", "LocationId", "SelectedTitlesFacet",
    "SelectedCategoriesFacet", "SelectedPostingDatesFacet",
    "SelectedLocationsFacet", "SelectedWorkLocationsFacet",
    "SelectedFlexFieldsFacets", "SelectedOrganizationsFacet",
    "SelectedWorkplaceTypesFacet", "JobFamilyId", "PostingStartDate",
    "PostingEndDate", "RequisitionId", "CandidateNumber", "OrganizationId",
    "WorkplaceType", "WorkLocationCountryCode", "WorkLocationZipCode",
)
JOB_ID = re.compile(r"\d+(?:_\d+_ORA_DELETED)?")
UNDELETED_JOB_ID = re.compile(r"\d+")
ADDRESS_FIELDS = ("AddressLine1", "TownOrCity", "Region2", "PostalCode", "Country")
HEADING_LIMIT = 160
RESPONSIBILITY_HEADING = re.compile(
    r"^(?:(?:job|key|primary|main)\s+)?responsibilities(?:\s*[:\-–—]\s*(?P<rest>.*))?$",
    re.I,
)
WHAT_YOU_WILL_DO_HEADING = re.compile(
    r"^what\s+you(?:\u2019|')?ll\s+do(?:\s*[:\-–—]\s*(?P<rest>.*))?$", re.I
)
QUALIFICATION_HEADING = re.compile(
    r"^(?:(?:required|preferred|minimum|basic|desired)\s+)?"
    r"qualifications(?:,\s*(?:capabilities|skills|experience|knowledge).*?)?"
    r"(?:\s*[:\-–—]\s*(?P<rest>.*))?$",
    re.I,
)
SKILL_HEADING = re.compile(
    r"^(?:(?:required|preferred|minimum|desired)\s+)?"
    r"(?:skills|experience)(?:\s+required|\s+preferred)?"
    r"(?:\s*[:\-–—]\s*(?P<rest>.*))?$",
    re.I,
)
WHAT_YOU_NEED_HEADING = re.compile(
    r"^what\s+you(?:\u2019|')?ll\s+need(?:\s*[:\-–—]\s*(?P<rest>.*))?$", re.I
)
BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "ul", "ol", "li", "h1", "h2", "h3",
    "h4", "h5", "h6", "br", "tr", "td", "th", "table", "blockquote", "pre",
    "hr", "dl", "dt", "dd", "header", "footer", "main", "nav",
})
_CAREER_ATS_HOSTS = (
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "ashbyhq.com",
    "smartrecruiters.com", "workable.com", "jobvite.com", "icims.com",
    "taleo.net", "successfactors.com", "kula.ai", "recruitee.com",
    "bamboohr.com", "personio.com", "oraclecloud.com",
)


class FatalSourceError(RuntimeError):
    """A 403/429/challenge means stop scheduling further detail requests."""


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _normalized_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ").replace("\u2007", " ")
                  .replace("\u202f", " ")).strip()


def visible_text(html: str | None) -> str:
    """Block-aware visible text, matching a browser's collapsed inner text."""
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
    return _normalized_space("".join(chunks))


def visible_lines(html: str | None) -> list[str]:
    """Non-empty visible lines, preserving block/list boundaries."""
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
    value = "".join(chunks).replace("\u00a0", " ").replace("\u2007", " ")
    return [line.strip() for line in re.split(r"\n+", value) if line.strip()]


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def job_page_url(job_id: str) -> str:
    return f"{BOARD_URL}/job/{job_id}"


def apply_page_url(job_id: str) -> str:
    """The route the rendered APPLY NOW button navigates to for this exact job."""
    return f"{job_page_url(job_id)}/apply/email"


def record_classification(job_id: str) -> str:
    if "_ORA_DELETED" in job_id:
        return "oracle_deleted_requisition_id_still_listed"
    return "published_requisition"


def _address_label(item: dict) -> str | None:
    values = [item.get(name) for name in ADDRESS_FIELDS]
    text = ", ".join(str(value).strip() for value in values if value not in (None, ""))
    return text or item.get("LocationName") or item.get("Name")


def parse_official_entry(html: str, response_url: str = ENTRY_FINAL) -> dict:
    """Validate the final official careers page and the single board it publishes."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname not in {"www.jpmorganchase.com", "jpmorganchase.com"}
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("JPMorgan Chase official careers entry is not the inspected jpmorganchase.com/careers page")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    og_title = og_url = None
    for meta in soup.select("meta[property]"):
        if meta.get("property") == "og:title":
            og_title = meta.get("content")
        elif meta.get("property") == "og:url":
            og_url = meta.get("content")
    if "JPMorgan" not in title or "Careers" not in title:
        raise ValueError("JPMorgan Chase official careers page title/identity changed")
    if og_url and urlsplit(og_url).path.rstrip("/") != "/careers":
        raise ValueError("JPMorgan Chase official careers og:url changed")

    unfiltered: set[str] = set()
    filtered: list[dict] = []
    talent: set[str] = set()
    other_oracle: set[str] = set()
    foreign_ats: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        target = urljoin(response_url, href)
        parts = urlsplit(target)
        host = (parts.hostname or "").casefold()
        if host == ORACLE_HOST:
            path = parts.path.rstrip("/")
            label = normalized_title(anchor.get_text(" ", strip=True))
            if path == f"{BOARD_PATH}/requisitions" or path == f"{BOARD_PATH}/jobs":
                query = parse_qs(parts.query)
                if query:
                    keyword = (query.get("keyword") or [None])[0]
                    filtered.append({
                        "label": label,
                        "url": urlunsplit(("https", ORACLE_HOST, path, parts.query, "")),
                        "keyword": keyword,
                    })
                else:
                    unfiltered.add(urlunsplit(("https", ORACLE_HOST, path, "", "")))
            elif path == TALENT_COMMUNITY_PATH:
                talent.add(urlunsplit(("https", ORACLE_HOST, TALENT_COMMUNITY_PATH, "", "")))
            else:
                other_oracle.add(target)
        elif any(host == ats or host.endswith("." + ats) for ats in _CAREER_ATS_HOSTS):
            foreign_ats.add(target)
    if not unfiltered:
        raise ValueError("JPMorgan Chase official careers page no longer publishes the CX_1001 job listing")
    if other_oracle:
        raise ValueError(f"JPMorgan Chase official careers page publishes an unexpected Oracle route: {sorted(other_oracle)}")
    if foreign_ats:
        raise ValueError(f"JPMorgan Chase official careers page publishes an unexpected ATS link: {sorted(foreign_ats)}")
    return {
        "url": response_url,
        "title": title,
        "og_title": og_title,
        "og_url": og_url,
        "unfiltered_board_urls": sorted(unfiltered),
        "board_url": BOARD_REQUISITIONS_URL,
        "board_listing_url": BOARD_LISTING_URL,
        "board_anchor_texts": sorted({
            normalized_title(anchor.get_text(" ", strip=True))
            for anchor in soup.select("a[href]")
            if (anchor.get("href") or "").startswith(ORACLE_ORIGIN + BOARD_PATH)
        }),
        "filtered_board_links": filtered,
        "talent_community_urls": sorted(talent),
    }


def parse_board_shell(html: str, response_url: str = BOARD_LISTING_URL) -> dict:
    """Validate the public Oracle board shell and its CX_1001 site identity."""
    parsed = urlsplit(response_url)
    allowed = {BOARD_PATH, BOARD_PATH + "/jobs", BOARD_PATH + "/requisitions"}
    if parsed.scheme != "https" or parsed.hostname != ORACLE_HOST or parsed.path.rstrip("/") not in allowed:
        raise ValueError("JPMorgan Chase board shell came from an unexpected Oracle route")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    og_title = og_site = None
    for meta in soup.select("meta[property]"):
        if meta.get("property") == "og:title":
            og_title = meta.get("content")
        elif meta.get("property") == "og:site_name":
            og_site = meta.get("content")
    if og_site != SITE_NAME or not (og_title or "").startswith(SITE_NAME):
        raise ValueError("JPMorgan Chase board shell title/identity changed")
    base = soup.select_one("base[data-apibaseurl][data-sitenumber]")
    if base is None:
        raise ValueError("JPMorgan Chase board shell no longer publishes its site-number base tag")
    if (base.get("href") != BOARD_PATH
            or urlsplit(base.get("data-apibaseurl", "")).hostname != ORACLE_HOST
            or base.get("data-sitenumber") != SITE_NUMBER):
        raise ValueError("JPMorgan Chase board shell site number/API host changed")
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
    """Validate the public siteSettings identity for CX_1001."""
    app = value.get("app") if isinstance(value, dict) else None
    if not isinstance(app, dict):
        raise ValueError("JPMorgan Chase siteSettings response has no app block")
    if (app.get("siteName") != SITE_NAME or app.get("siteNumber") != SITE_NUMBER
            or app.get("siteLang") != "en"):
        raise ValueError("JPMorgan Chase siteSettings no longer identify the inspected company site")
    templates = app.get("templates") or {}
    if not isinstance(templates, dict) or not templates.get("SEARCH_PAGE") or not templates.get("JOB_DETAILS"):
        raise ValueError("JPMorgan Chase siteSettings no longer publish search/detail templates")
    return {
        key: app.get(key)
        for key in ("siteName", "siteCode", "siteNumber", "siteLang", "languages", "templates")
    }


def _finder(offset: int, page_size: int) -> str:
    return (f"findReqs;siteNumber={SITE_NUMBER},facetsList={FACETS},"
            f"limit={page_size},sortBy={SORT_BY},offset={offset}")


def listing_params(offset: int, page_size: int = PAGE_SIZE) -> dict:
    return {"onlyData": "true", "expand": EXPAND, "finder": _finder(offset, page_size)}


def detail_params(job_id: str) -> dict:
    return {"expand": "all", "onlyData": "true",
            "finder": f'ById;Id="{job_id}",siteNumber={SITE_NUMBER}'}


def parse_listing_page(value: dict, requested_offset: int, page_size: int = PAGE_SIZE) -> dict:
    """Validate one unfiltered CX_1001 listing batch and return its exact rows."""
    if not isinstance(value, dict):
        raise ValueError("JPMorgan Chase listing response is not an object")
    if value.get("errors"):
        raise ValueError(f"JPMorgan Chase listing response returned errors: {value.get('errors')}")
    items = value.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("JPMorgan Chase listing response has no single board result")
    block = items[0]
    if not isinstance(block, dict):
        raise ValueError("JPMorgan Chase listing result block is not an object")
    total = block.get("TotalJobsCount")
    rows = block.get("requisitionList")
    if (not isinstance(total, int) or total < 0 or not isinstance(rows, list)
            or block.get("SiteNumber") != SITE_NUMBER
            or block.get("Offset") != requested_offset
            or block.get("Limit") != page_size
            or block.get("SortBy") != SORT_BY
            or len(rows) > page_size):
        raise ValueError("JPMorgan Chase listing identity, pagination or unfiltered search state changed")
    applied = [key for key in FILTER_FIELDS if block.get(key) not in (None, "", False, [])]
    if applied:
        raise ValueError(f"JPMorgan Chase listing unexpectedly applied filters: {applied}")
    expected_rows = min(page_size, max(0, total - requested_offset))
    issues: list[str] = []
    if len(rows) != expected_rows:
        issues.append(f"jpmc_listing_row_count_mismatch:{len(rows)}!={expected_rows}")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("jpmc_listing_row_not_object")
            continue
        identity = row.get("Id")
        if not isinstance(identity, str) or not JOB_ID.fullmatch(identity):
            issues.append("jpmc_listing_row_missing_id")
            continue
        if identity in seen:
            issues.append(f"jpmc_listing_duplicate_id:{identity}")
        seen.add(identity)
        if not isinstance(row.get("Title"), str) or not row["Title"].strip():
            issues.append(f"jpmc_listing_row_missing_title:{identity}")
        posted = row.get("PostedDate")
        if posted is not None and not (isinstance(posted, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted)):
            issues.append(f"jpmc_listing_row_invalid_posted_date:{identity}")
        for key in ("secondaryLocations", "otherWorkLocations", "requisitionFlexFields"):
            if row.get(key) is not None and not isinstance(row[key], list):
                issues.append(f"jpmc_listing_{key}_not_list:{identity}")
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
        raise ValueError("JPMorgan Chase detail response is not an object")
    items = value.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError(f"JPMorgan Chase detail response for {job_id} has no single posting")
    row = items[0]
    if not isinstance(row, dict):
        raise ValueError(f"JPMorgan Chase detail response for {job_id} is not an object")
    if str(row.get("Id")) != str(job_id):
        raise ValueError(f"JPMorgan Chase detail response returned a different posting than {job_id}")
    if expected_title is not None and normalized_title(row.get("Title")) != normalized_title(expected_title):
        raise ValueError(f"JPMorgan Chase detail title changed for {job_id}")
    if row.get("RequisitionId") is None:
        raise ValueError(f"JPMorgan Chase detail for {job_id} has no requisition id")
    for field in BODY_FIELDS:
        if field not in row:
            raise ValueError(f"JPMorgan Chase detail for {job_id} omitted inspected section {field}")
        if row[field] is not None and not isinstance(row[field], str):
            raise ValueError(f"JPMorgan Chase detail section {field} for {job_id} is not HTML text")
    start = row.get("ExternalPostedStartDate")
    if start is not None and not isinstance(start, str):
        raise ValueError(f"JPMorgan Chase detail start date for {job_id} is not ISO text")
    if not isinstance(row.get("requisitionFlexFields"), list):
        raise ValueError(f"JPMorgan Chase detail flex fields for {job_id} are not a list")
    return row


def public_detail(row: dict) -> dict:
    """Retain public detail fields; internal/personnel fields are not published."""
    return {key: value for key, value in row.items()
            if key not in NONPUBLIC_FIELDS and not key.startswith("Internal")}


def description_sections(detail: dict) -> tuple[list[dict], str, list[str]]:
    """Return the exact non-empty rendered sections, role text and missing fields."""
    sections, missing = [], []
    for field in BODY_FIELDS:
        value = detail.get(field)
        text = visible_text(value) if isinstance(value, str) else ""
        if text:
            sections.append({"field": field, "label": BODY_LABELS[field],
                             "html": value, "text": text})
        else:
            missing.append(field)
    role_text = "\n".join(section["text"] for section in sections if section["field"] in ROLE_FIELDS)
    return sections, role_text, missing


def assemble_description(sections: list[dict]) -> str:
    return "\n".join(
        f'<section data-source-field="{section["field"]}"><h2>{section["label"]}</h2>{section["html"]}</section>'
        for section in sections
    )


def _heading_match(line: str) -> tuple[str | None, str]:
    """Return (kind, first_body_text) when a visible line is a known heading."""
    if len(line) > HEADING_LIMIT:
        return None, ""
    for regex in (RESPONSIBILITY_HEADING, WHAT_YOU_WILL_DO_HEADING):
        match = regex.match(line)
        if match:
            return "responsibilities", (match.groupdict().get("rest") or "").strip()
    for regex in (QUALIFICATION_HEADING, SKILL_HEADING, WHAT_YOU_NEED_HEADING):
        match = regex.match(line)
        if match:
            return "qualifications", (match.groupdict().get("rest") or "").strip()
    return None, ""


def role_subsections(html: str | None) -> list[dict]:
    """Extract the employer's embedded responsibility/qualification subsections.

    JPMorgan's Oracle detail puts responsibilities and qualifications inside
    ``ExternalDescriptionStr`` rather than in dedicated API fields.  The full
    HTML is retained separately; this returns labelled text for the structured
    fields without changing the stored description.
    """
    sections: list[dict] = []
    current: dict | None = None
    for line in visible_lines(html):
        kind, rest = _heading_match(line)
        if kind:
            if current is not None and current["lines"]:
                sections.append(current)
            current = {"kind": kind, "heading": line, "lines": ([rest] if rest else [])}
            continue
        if current is not None:
            current["lines"].append(line)
    if current is not None and current["lines"]:
        sections.append(current)
    for section in sections:
        section["text"] = _normalized_space(" ".join(section.pop("lines")))
    return [section for section in sections if section["text"]]


def responsibilities_sections(html: str | None) -> list[dict] | None:
    values = [section for section in role_subsections(html) if section["kind"] == "responsibilities"]
    return values or None


def qualifications_sections(html: str | None) -> list[dict] | None:
    values = [section for section in role_subsections(html) if section["kind"] == "qualifications"]
    return values or None


def _parse_money(value: str) -> float | None:
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def salary_fields(detail: dict) -> dict | None:
    """Retain the public Base Pay/Salary flex field and parse explicit ranges."""
    prompts = [
        item for item in (detail.get("requisitionFlexFields") or [])
        if isinstance(item, dict) and item.get("Prompt") == "Base Pay/Salary"
        and isinstance(item.get("Value"), str) and item["Value"].strip()
    ]
    if not prompts:
        return None
    raw_values = [item["Value"].strip() for item in prompts]
    entries: list[dict] = []
    for raw in raw_values:
        for part in raw.split(";"):
            part = part.strip()
            match = re.fullmatch(
                r"(?P<location>.+?)\s+\$(?P<min>[\d,]+(?:\.\d+)?)\s*-\s*"
                r"\$(?P<max>[\d,]+(?:\.\d+)?)",
                part,
            )
            if not match:
                continue
            minimum = _parse_money(match.group("min"))
            maximum = _parse_money(match.group("max"))
            if minimum is None or maximum is None:
                continue
            entries.append({
                "location": match.group("location").strip(),
                "min": minimum,
                "max": maximum,
                "currency": "USD",
                "raw": part,
            })
    result: dict = {
        "source": "requisitionFlexFields.Base Pay/Salary",
        "text": " | ".join(raw_values),
        "raw_values": raw_values,
    }
    if not entries:
        result.update(min=None, max=None, currency=None, period=None, entries=[])
        return result
    minimum = min(entry["min"] for entry in entries)
    maximum = max(entry["max"] for entry in entries)
    result.update(
        min=minimum,
        max=maximum,
        currency="USD",
        entries=entries,
    )
    if maximum <= 500:
        result.update(period="hour", period_inferred=True)
    elif minimum >= 1000:
        result.update(period="year", period_inferred=True)
    else:
        result.update(period=None, period_inferred=False)
    return result


def location_values(detail: dict, listing: dict | None = None) -> list[str]:
    """All public location addresses/names in source order, deduplicated."""
    listing = listing or {}
    values: list[str] = []

    def add(value):
        if isinstance(value, str):
            value = _normalized_space(value)
            if value and value not in values:
                values.append(value)

    addresses = []
    for key in ("workLocation", "otherWorkLocations"):
        for item in detail.get(key) or []:
            if isinstance(item, dict):
                label = _address_label(item)
                if label:
                    addresses.append(label)
    if addresses:
        for value in addresses:
            add(value)
    else:
        add(detail.get("PrimaryLocation") or listing.get("PrimaryLocation"))
        for item in detail.get("secondaryLocations") or listing.get("secondaryLocations") or []:
            if isinstance(item, dict):
                add(item.get("Name"))
            elif isinstance(item, str):
                add(item)
        for item in detail.get("otherWorkLocations") or listing.get("otherWorkLocations") or []:
            if isinstance(item, dict):
                add(item.get("Name"))
            elif isinstance(item, str):
                add(item)
    return values


class JpmorganChase(Adapter):
    """Collect only the membership published on the official CX_1001 board."""

    VERIFICATION_REVISION = "jpmorganchase.official-CX_1001.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.fatal_reason: str | None = None

    # ---------------------------------------------------------------- entry
    async def _entry(self) -> float:
        started = time.perf_counter()
        entry = await self.http.get(self.company.careers_url)
        entry_info = parse_official_entry(entry.text, str(entry.url))
        if str(entry.url).rstrip("/") != ENTRY_FINAL.rstrip("/"):
            raise ValueError(f"JPMorgan Chase official entry did not reach the inspected careers page: {entry.url}")
        board = await self.http.get(BOARD_LISTING_URL)
        board_info = parse_board_shell(board.text, str(board.url))
        settings = await self.http.json(ORACLE_ORIGIN + SETTINGS_PATH)
        site_config = parse_site_settings(settings)
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            requested_entry_url=str(entry.request.url),
            official_entry=str(entry.url),
            official_entry_redirects=[str(item.url) for item in entry.history],
            official_entry_title=entry_info["title"],
            official_board_anchor_texts=entry_info["board_anchor_texts"],
            filtered_board_links=entry_info["filtered_board_links"],
            company_listing_url=BOARD_LISTING_URL,
            board_shell_url=str(board.url),
            board_shell=board_info,
            site_number=SITE_NUMBER,
            site_settings=site_config,
            listing_authority=(
                "JPMorganChase official careers page 'Join our team'/'Apply now' links to its "
                "Oracle Candidate Experience board CX_1001; unfiltered findReqs membership"
            ),
            talent_community_excluded={
                "urls": entry_info["talent_community_urls"],
                "reason": "talent-community sign-up route; no requisition IDs or vacancy listing",
            },
            native_page_size=PAGE_SIZE,
            description_sections=[BODY_LABELS[field] for field in BODY_FIELDS],
            internal_fields_omitted=sorted(NONPUBLIC_FIELDS | {"Internal*"}),
            date_behavior=(
                "date_posted = board date-only PostedDate; ExternalPostedStartDate and "
                "ExternalPostedEndDate retained raw; the page's localised Posting Date and "
                "request time are never used"
            ),
            salary_behavior=(
                "public requisitionFlexFields prompt 'Base Pay/Salary' retained exactly; "
                "ranges parsed per location; hourly/annual period is labelled inferred"
            ),
        )
        return time.perf_counter() - started

    # --------------------------------------------------------------- listing
    async def _listing(self, api, offset: int, page_size: int, *, label: str):
        if not self.page_guard():
            raise ValueError("JPMorgan Chase listing pagination budget exhausted")
        response = await api.get(ORACLE_ORIGIN + LISTING_PATH,
                                 params=listing_params(offset, page_size))
        parsed_url = urlsplit(str(response.url))
        if (parsed_url.scheme != "https" or parsed_url.netloc != ORACLE_HOST
                or parsed_url.path != LISTING_PATH):
            raise ValueError("JPMorgan Chase listing redirected outside its company-scoped route")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"JPMorgan Chase listing page at offset {offset} was not JSON") from exc
        page = parse_listing_page(payload, offset, page_size)
        for issue in page["issues"]:
            self.result.warnings.append(f"{label}_{issue}")
        return page, str(response.url)

    async def _enumerate(self, api, page_size: int, *, label: str) -> dict:
        started = time.perf_counter()
        rows_by_id: dict[str, dict] = {}
        ordered: list[str] = []
        pages: list[dict] = []
        issues: list[str] = []
        totals: list[int] = []
        offset = 0
        while True:
            page, listing_url = await self._listing(api, offset, page_size, label=label)
            total = page["total"]
            totals.append(total)
            if not page["rows"]:
                issues.append(f"{label}_empty_page_before_total:{offset}")
                break
            current = [str(row["Id"]) for row in page["rows"]]
            if len(set(current)) != len(current):
                issues.append(f"{label}_duplicate_ids_at_offset:{offset}")
            for row in page["rows"]:
                identity = str(row["Id"])
                if identity in rows_by_id:
                    if rows_by_id[identity] != row:
                        issues.append(f"{label}_changed_row_for_id:{identity}")
                    continue
                rows_by_id[identity] = row
                ordered.append(identity)
            pages.append({
                "offset": offset,
                "rows": len(current),
                "first_id": current[0] if current else None,
                "last_id": current[-1] if current else None,
                "source_total": total,
                "url": listing_url,
            })
            if len(rows_by_id) >= total:
                break
            if len(page["rows"]) < page_size:
                # The board can publish a short page while its TotalJobsCount is
                # unchanged (observed one count-only row). Continue once; if the
                # next offset is empty, the gap is recorded and no ID is invented.
                if len(page["rows"]) == 0:
                    break
            offset += page_size
            if len(pages) > 2000:
                issues.append(f"{label}_page_budget_exhausted")
                break
        return {
            "rows_by_id": rows_by_id,
            "ordered_ids": ordered,
            "pages": pages,
            "issues": issues,
            "totals": totals,
            "seconds": max(0.0, time.perf_counter() - started),
        }

    # ---------------------------------------------------------------- detail
    async def _detail(self, api, job):
        if self.fatal_reason:
            raise FatalSourceError(self.fatal_reason)
        try:
            response = await api.get(ORACLE_ORIGIN + DETAILS_PATH, params=detail_params(job.job_id))
        except Exception as exc:
            text = str(exc)
            if "429" in text or "403" in text or "challenge" in text.casefold():
                self.fatal_reason = f"fatal_source_response:{text}"
                raise FatalSourceError(self.fatal_reason) from exc
            raise
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"JPMorgan Chase detail for {job.job_id} was not JSON") from exc
        row = parse_detail_page(payload, job.job_id, job.title)
        public = public_detail(row)
        sections, role_text, missing = description_sections(public)
        subsections = role_subsections(public.get("ExternalDescriptionStr"))
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
        job.raw["role_subsections"] = [
            {"kind": section["kind"], "heading": section["heading"],
             "text_chars": len(section["text"]), "text": section["text"]}
            for section in subsections
        ]
        job.raw["business_unit"] = public.get("BusinessUnit")
        job.raw["job_shift"] = public.get("JobShift")
        job.raw["requisition_type"] = public.get("RequisitionType")
        job.raw["record_classification"] = record_classification(str(job.job_id))
        job.description_method = "jpmorganchase.official_CX_1001.oracle_external_sections"
        job.description_source_url = str(response.url)
        job.requisition_id = str(public["RequisitionId"])
        job.apply_url = apply_page_url(job.job_id)
        job.date_posted = job.raw["listing"].get("PostedDate")
        job.date_posted_raw = {
            "listing_PostedDate": job.raw["listing"].get("PostedDate"),
            "detail_ExternalPostedStartDate": public.get("ExternalPostedStartDate"),
            "detail_ExternalPostedEndDate": public.get("ExternalPostedEndDate"),
            "page_posting_date_is_localised": True,
        }
        job.valid_through = public.get("ExternalPostedEndDate") or None
        job.language = public.get("ContentLocale") or job.raw["listing"].get("Language") or None
        job.locations = location_values(public, job.raw["listing"])
        job.department = public.get("Category") or job.raw["listing"].get("JobFamily") or None
        job.team = public.get("JobFunction") or job.raw["listing"].get("JobFunction") or None
        job.employment_type = (public.get("JobSchedule") or job.raw["listing"].get("JobSchedule")
                               or None)
        workplace = (public.get("WorkplaceTypeCode") or public.get("WorkplaceType")
                     or job.raw["listing"].get("WorkplaceTypeCode")
                     or job.raw["listing"].get("WorkplaceType") or None)
        job.workplace_type = workplace if workplace not in ("",) else None
        job.salary = salary_fields(public)
        job.qualifications = qualifications_sections(public.get("ExternalDescriptionStr"))
        job.responsibilities = responsibilities_sections(public.get("ExternalDescriptionStr"))
        job.education = public.get("StudyLevel") or None
        job.experience = public.get("JobLevel") or None
        job.employer = public.get("LegalEmployer") or None
        if len(role_text.strip()) < 100:
            # Keep an exact but publisher-short body; never substitute corporate
            # boilerplate when no role body exists.
            if role_text.strip():
                job.description_html = assemble_description(sections)
                job.description_text = visible_text(job.description_html)
                job.warnings.append("source_description_short")
            else:
                job.description_html = ""
                job.description_text = ""
                job.warnings.append("source_description_missing")
            job.description_unavailable = True
            job.warnings.append("description_not_full_field")
            return job
        job.description_html = assemble_description(sections)
        job.description_text = visible_text(job.description_html)
        return job

    def confirmed_unavailable(self, job):
        """Recheck publisher-empty/short postings on resume; a fix may be new."""
        return bool(
            job.description_unavailable and job.description_method and job.description_source_url
            and "detail_fetch_failed" not in job.warnings
            and not any(
                warning in job.warnings
                for warning in ("source_description_missing", "source_description_short")
            )
        )

    # --------------------------------------------------------------- hydrate
    async def _hydrate(self, api, jobs):
        started = time.perf_counter()
        sem = asyncio.Semaphore(self.options.detail_concurrency)
        stats = {"total": len(jobs), "checked": 0, "fetched_jobs": 0,
                 "verified_cache_hits": 0, "unavailable_cache_hits": 0,
                 "failed": 0, "skipped_after_fatal": 0}
        self.result.metadata["details"] = stats
        self.result.metadata.setdefault("detail_phases", []).append(stats)
        completed = 0

        async def one(index, job):
            nonlocal completed
            if self.fatal_reason:
                stats["skipped_after_fatal"] += 1
                job.warnings.append("detail_skipped_after_fatal_source_error")
                completed += 1
                stats["checked"] = completed
                return job
            async with sem:
                if self.fatal_reason:
                    stats["skipped_after_fatal"] += 1
                    job.warnings.append("detail_skipped_after_fatal_source_error")
                    completed += 1
                    stats["checked"] = completed
                    return job
                try:
                    cached = self.options.cached_jobs.get(job.url)
                    same_posting = (cached is not None and cached.job_id == job.job_id
                                    and normalized_title(cached.title) == normalized_title(job.title))
                    if same_posting and cached.description_verified:
                        result = cached
                        result.raw["listing"] = job.raw.get("listing")
                        stats["verified_cache_hits"] += 1
                    elif same_posting and self.options.reuse_unavailable and self.confirmed_unavailable(cached):
                        result = cached
                        result.raw["listing"] = job.raw.get("listing")
                        stats["unavailable_cache_hits"] += 1
                    else:
                        stats["fetched_jobs"] += 1
                        result = await self._detail(api, job)
                except FatalSourceError as exc:
                    stats["failed"] += 1
                    job.warnings.append("detail_fetch_failed")
                    job.warnings.append("fatal_source_response")
                    job.description_unavailable = True
                    self.result.errors.append(f"Detail {job.url}: {type(exc).__name__}: {exc}")
                    result = job
                except Exception as exc:
                    stats["failed"] += 1
                    job.warnings.append("detail_fetch_failed")
                    job.description_unavailable = True
                    self.result.errors.append(f"Detail {job.url}: {type(exc).__name__}: {exc}")
                    result = job
                jobs[index] = result
                result.record_identity = job.record_identity or job.key
                result.company_scope_verified = self.company.scope == "company"
                self.result.validate_job(result)
                self.result.dirty_jobs.append(result)
                completed += 1
                stats["checked"] = completed
                if self.options.checkpoint and completed % 100 == 0:
                    self.options.checkpoint(self.result)
                return result

        try:
            return await asyncio.gather(*(one(i, job) for i, job in enumerate(jobs)))
        finally:
            stats["duration_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
            self.result.timings["detail_phase_seconds"] = (
                self.result.timings.get("detail_phase_seconds", 0.0)
                + max(0.0, time.perf_counter() - started))

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
            workplace_type=(row.get("WorkplaceTypeCode") or row.get("WorkplaceType") or None),
            language=row.get("Language") or None,
        )
        job.record_identity = f"jpmc:{identity}"
        job.raw["listing_request_url"] = listing_url
        job.raw["site_number"] = SITE_NUMBER
        job.raw["board_url"] = BOARD_URL
        job.raw["record_classification"] = record_classification(identity)
        return job

    async def run(self):
        entry_seconds = await self._entry()
        self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
        api = self.http
        initial = await self._enumerate(api, PAGE_SIZE, label="initial")
        self.result.timings["listing_phase_seconds"] = round(initial["seconds"], 6)
        advertised_initial = initial["totals"][0] if initial["totals"] else None
        self.result.metadata["listing_pages"] = initial["pages"]
        self.result.metadata["listing_issues"] = initial["issues"]
        self.result.metadata["advertised_total_initial"] = advertised_initial
        self.result.metadata["company_published_job_ids"] = initial["ordered_ids"]
        self.result.metadata["unique_listed_ids"] = len(initial["ordered_ids"])
        self.result.metadata["listing_count_gap_initial"] = (
            advertised_initial - len(initial["ordered_ids"])
            if advertised_initial is not None else None)
        for issue in initial["issues"]:
            self.result.warnings.append(f"initial_{issue}")

        for row in initial["rows_by_id"].values():
            self.add(self._job_from_listing(row, BOARD_LISTING_URL))
        self.result.expected_total = len(initial["ordered_ids"])

        self.result.jobs = await self._hydrate(api, self.result.jobs)
        if self.options.checkpoint:
            self.options.checkpoint(self.result)

        # Post-detail membership recheck.  The board is rolling; additions are
        # hydrated and removals are marked rather than silently dropped.
        recheck_started = time.perf_counter()
        rows_by_id = dict(initial["rows_by_id"])
        collected_ids = {str(job.job_id) for job in self.result.jobs}
        rounds: list[dict] = []
        added_total: list[str] = []
        removed_total: list[str] = []
        final_ids: set[str] = set(initial["ordered_ids"])
        for round_number in (1, 2):
            final = await self._enumerate(api, RECONCILE_PAGE_SIZE,
                                          label=f"final{round_number}")
            final_ids = set(final["ordered_ids"])
            added = [identity for identity in final["ordered_ids"]
                     if identity not in rows_by_id]
            removed = [identity for identity in rows_by_id
                       if identity not in final_ids]
            rounds.append({
                "round": round_number,
                "page_size": RECONCILE_PAGE_SIZE,
                "advertised_total": final["totals"][0] if final["totals"] else None,
                "rows": len(final["ordered_ids"]),
                "ordered_ids": final["ordered_ids"],
                "pages": final["pages"],
                "issues": final["issues"],
                "added_ids": added,
                "removed_ids": removed,
            })
            added_total.extend(added)
            removed_total.extend(removed)
            if added:
                new_jobs = []
                for identity in added:
                    row = final["rows_by_id"][identity]
                    new_jobs.append(self._job_from_listing(row, BOARD_LISTING_URL))
                start = len(self.result.jobs)
                for job in new_jobs:
                    self.add(job)
                self.result.jobs[start:] = await self._hydrate(api, new_jobs)
                collected_ids = {str(job.job_id) for job in self.result.jobs}
            for identity in removed:
                for job in self.result.jobs:
                    if str(job.job_id) == identity:
                        job.warnings.append("delisted_during_final_recheck")
            rows_by_id.update(final["rows_by_id"])
            if not added:
                break
        self.result.timings["final_listing_seconds"] = round(
            max(0.0, time.perf_counter() - recheck_started), 6)
        for round_value in rounds:
            for issue in round_value["issues"]:
                self.result.warnings.append(f"final{round_value['round']}_{issue}")
        added_total = list(dict.fromkeys(added_total))
        removed_total = list(dict.fromkeys(removed_total))
        all_known_ids = set(rows_by_id)
        # Do not silently claim the source total when the source itself has a
        # count-only row (observed one).  The union is the reconciled set of IDs
        # this company's own listing actually exposed.
        self.result.expected_total = len(all_known_ids)
        advertised_final = rounds[-1]["advertised_total"] if rounds else None
        self.result.metadata["final_listing_pages"] = rounds[-1]["pages"] if rounds else []
        self.result.metadata["final_listing_ids"] = sorted(all_known_ids)
        self.result.metadata["final_listing_ordered_ids"] = (
            rounds[-1].get("ordered_ids") if rounds else initial["ordered_ids"])
        self.result.metadata["final_listing_reconciled"] = (
            bool(all_known_ids) and all_known_ids <= collected_ids)
        self.result.metadata["advertised_total_final"] = advertised_final
        self.result.metadata["listing_count_gap_final"] = (
            advertised_final - len(final_ids) if advertised_final is not None else None)
        self.result.metadata["added_during_run"] = added_total
        self.result.metadata["removed_during_run"] = removed_total
        self.result.metadata["membership_reconciliation"] = {
            "complete": bool(all_known_ids) and all_known_ids <= collected_ids and not self.fatal_reason,
            "advertised_total_initial": advertised_initial,
            "unique_listed_ids_initial": len(initial["ordered_ids"]),
            "advertised_total_final": advertised_final,
            "final_exposed_rows": len(final_ids),
            "known_union_rows": len(all_known_ids),
            "collected_ids": len(collected_ids),
            "added_during_run": added_total,
            "removed_during_run": removed_total,
            "final_listing_reconciled": self.result.metadata["final_listing_reconciled"],
            "ui_batch_size": PAGE_SIZE,
            "recheck_batch_size": RECONCILE_PAGE_SIZE,
            "initial_pages": initial["pages"],
            "final_rounds": rounds,
        }
        self.result.listing_complete = (
            bool(all_known_ids) and all_known_ids <= collected_ids
            and not self.fatal_reason and not self.result.errors
            and len(self.result.jobs) == len(all_known_ids))
        if advertised_initial is not None and advertised_initial > len(initial["ordered_ids"]):
            self.result.errors.append(
                f"jpmc_listing_count_gap:{advertised_initial - len(initial['ordered_ids'])}")
        return self.result
