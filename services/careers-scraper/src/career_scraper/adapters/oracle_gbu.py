"""Oracle's own careers journey: oracle.com/careers -> careers.oracle.com job search.

The requested official entry https://www.oracle.com/careers/ publishes "Search
jobs at Oracle" links to Oracle's own Candidate Experience board
``careers.oracle.com/en/sites/jobsearch`` (Oracle Cloud HCM site ``CX_45001``,
API host ``eeho.fa.us2.oraclecloud.com``).  That board's unfiltered
``recruitingCEJobRequisitions`` finder is the membership authority used here;
the entry deep-links to the same board with a United States location filter, so
that query parameter is dropped to collect every public posting (the project
contract forbids country filters).

The board renders 14 cards per "Show More Results" batch and reports an
advertised count of 2836 at observation time.  Offset enumeration of the
unfiltered board yields 2834 unique requisitions; the board's own public job
sitemap also contains exactly those 2834 unique job IDs, so the two advertised
records that are never returned by any unfiltered page are recorded as an
upstream count gap rather than replaced with fabricated rows.

Every exact listed Id is hydrated through ``recruitingCEJobRequisitionDetails``.
The rendered job page publishes JOB DESCRIPTION, RESPONSIBILITIES,
QUALIFICATIONS, ABOUT US and (when present) ABOUT THE TEAM backed by
ExternalDescriptionStr, ExternalResponsibilitiesStr, ExternalQualificationsStr,
CorporateDescriptionStr and OrganizationDescriptionStr.  Internal
qualifications/responsibilities, recruiter contact fields and admin flags are
not public and are omitted from stored evidence.

Dates: the board's date-only ``PostedDate`` is stored as ``date_posted``; the
detail's ``ExternalPostedStartDate`` timestamp is retained raw because the page's
"Posting Date" is a localised rendering of it.  Request time is never used.
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


OFFICIAL_ENTRY = "https://www.oracle.com/careers/"
VANITY_ORIGIN = "https://careers.oracle.com"
BOARD_PATH = "/en/sites/jobsearch"
BOARD_URL = VANITY_ORIGIN + BOARD_PATH + "/jobs"
TALENT_COMMUNITY_PATH = BOARD_PATH + "/join-talent-community"
ORACLE_HOST = "eeho.fa.us2.oraclecloud.com"
ORACLE_ORIGIN = "https://eeho.fa.us2.oraclecloud.com"
SITE_NUMBER = "CX_45001"
LISTING_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAILS_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
SETTINGS_PATH = f"/hcmRestApi/CandidateExperience/en/siteSettings/{SITE_NUMBER}"
UI_PAGE_SIZE = 14  # the board's own "Show More Results" batch size
PAGE_SIZE = 200  # largest page the public finder returns; same membership, fewer requests
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
# public job page and therefore not retained as public source evidence.
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
BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "ul", "ol", "li", "h1", "h2", "h3",
    "h4", "h5", "h6", "br", "tr", "td", "th", "table", "blockquote", "pre",
    "hr",
})
MONEY = r"\$([\d,]+)(?:\.\d+)?"
# oracle.com's CDN returns 403 to the generic scraper UA on the public careers
# page only; a normal browser User-Agent renders it (no CAPTCHA or challenge).
PUBLIC_PAGE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def visible_text(html: str | None) -> str:
    """Block-aware visible text, matching how a browser joins inline spans.

    A naive ``get_text("\\n")`` turns adjacent inline spans into separate words;
    this walker inserts separators only around block-level elements, so the
    result matches the rendered job page after whitespace normalisation.
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
    return f"{VANITY_ORIGIN}{BOARD_PATH}/job/{job_id}"


def apply_page_url(job_id: str) -> str:
    """The route the board's APPLY NOW button opens for this exact job."""
    return f"{job_page_url(job_id)}/apply/email"


def _board_url_from_href(href: str, base_url: str) -> str | None:
    target = urljoin(base_url, href.strip())
    parsed = urlsplit(target)
    if parsed.hostname != "careers.oracle.com":
        return None
    path = parsed.path.rstrip("/")
    if path == BOARD_PATH + "/jobs":
        return BOARD_URL
    return None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Validate the official Oracle careers entry and the one board it publishes."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname not in {"www.oracle.com", "oracle.com"}
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Oracle official careers entry is not the inspected oracle.com/careers page")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "oracle" not in title.casefold() or "career" not in title.casefold():
        raise ValueError("Oracle official careers page title/identity changed")
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
        if target_parts.hostname != "careers.oracle.com":
            continue
        path = target_parts.path.rstrip("/")
        board = _board_url_from_href(href, response_url)
        if board:
            boards.add(board)
            text = normalized_title(anchor.get_text(" ", strip=True))
            if text:
                anchor_texts.add(text)
        elif path == TALENT_COMMUNITY_PATH:
            talent.add(urlunsplit(("https", "careers.oracle.com", TALENT_COMMUNITY_PATH, "", "")))
        elif "/sites/" in path:
            other.add(target)
    if boards != {BOARD_URL}:
        raise ValueError(f"Oracle official entry no longer publishes exactly the inspected board: {sorted(boards)}")
    if other:
        raise ValueError(f"Oracle official entry publishes unexpected additional board links: {sorted(other)}")
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
    if parsed.hostname != "careers.oracle.com" or parsed.path.rstrip("/") not in {
            BOARD_PATH, BOARD_PATH + "/jobs"}:
        raise ValueError("Oracle board shell came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    og_title = None
    og_site = None
    for meta in soup.select("meta[property]"):
        if meta.get("property") == "og:title":
            og_title = meta.get("content")
        elif meta.get("property") == "og:site_name":
            og_site = meta.get("content")
    if "oracle" not in title.casefold() or og_site != "Oracle":
        raise ValueError("Oracle board shell title/identity changed")
    base = soup.select_one("base[data-apibaseurl][data-sitenumber]")
    if base is None:
        raise ValueError("Oracle board shell no longer publishes its site-number base tag")
    if (base.get("href") != BOARD_PATH
            or urlsplit(base.get("data-apibaseurl", "")).hostname != ORACLE_HOST
            or base.get("data-sitenumber") != SITE_NUMBER):
        raise ValueError("Oracle board shell site number/API host changed")
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
    """Validate the public siteSettings identity for the Oracle CX_45001 site."""
    app = value.get("app") if isinstance(value, dict) else None
    if not isinstance(app, dict):
        raise ValueError("Oracle siteSettings response has no app block")
    if (app.get("siteName") != "Oracle" or app.get("siteNumber") != SITE_NUMBER
            or app.get("siteCode") != SITE_NUMBER or app.get("siteLang") != "en"):
        raise ValueError("Oracle siteSettings no longer identify the inspected company site")
    templates = app.get("templates") or {}
    if not isinstance(templates, dict) or not templates.get("SEARCH_PAGE") or not templates.get("JOB_DETAILS"):
        raise ValueError("Oracle siteSettings no longer publish search/detail templates")
    return {
        key: app.get(key)
        for key in ("siteName", "siteCode", "siteNumber", "siteLang", "languages",
                    "templates", "shortUrl", "vanityBaseUrl")
    }


def _finder(offset: int, page_size: int = PAGE_SIZE) -> str:
    return (f"findReqs;siteNumber={SITE_NUMBER},facetsList={FACETS},"
            f"limit={page_size},sortBy=POSTING_DATES_DESC,offset={offset}")


def listing_url(offset: int, page_size: int = PAGE_SIZE) -> str:
    """Full public listing request, exactly as the board's widget shapes it."""
    return (f"{ORACLE_ORIGIN}{LISTING_PATH}?onlyData=true&expand={EXPAND}"
            f"&finder={_finder(offset, page_size)}")


def detail_url(job_id: str) -> str:
    return (f"{ORACLE_ORIGIN}{DETAILS_PATH}?expand=all&onlyData=true"
            f"&finder=ById%3BId%3D%22{job_id}%22%2CsiteNumber%3D{SITE_NUMBER}")


def parse_listing_page(value: dict, requested_offset: int, page_size: int = PAGE_SIZE) -> dict:
    """Validate one unfiltered board listing batch and return its exact rows."""
    if not isinstance(value, dict):
        raise ValueError("Oracle listing response is not an object")
    if value.get("errors"):
        raise ValueError(f"Oracle listing response returned errors: {value.get('errors')}")
    items = value.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("Oracle listing response has no single board result")
    block = items[0]
    if not isinstance(block, dict):
        raise ValueError("Oracle listing result block is not an object")
    total = block.get("TotalJobsCount")
    rows = block.get("requisitionList")
    if (not isinstance(total, int) or total < 0 or not isinstance(rows, list)
            or block.get("SiteNumber") != SITE_NUMBER
            or block.get("Offset") != requested_offset
            or block.get("Limit") != page_size
            or block.get("SortBy") != "POSTING_DATES_DESC"
            or len(rows) > page_size):
        raise ValueError("Oracle listing identity, pagination or unfiltered search state changed")
    applied = [key for key in FILTER_FIELDS if block.get(key) not in (None, "", False, [])]
    if applied:
        raise ValueError(f"Oracle listing unexpectedly applied filters: {applied}")
    expected_rows = min(page_size, max(0, total - requested_offset))
    issues: list[str] = []
    if len(rows) != expected_rows:
        issues.append(f"oracle_listing_row_count_mismatch:{len(rows)}!={expected_rows}")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("oracle_listing_row_not_object")
            continue
        identity = row.get("Id")
        if not isinstance(identity, str) or not re.fullmatch(r"\d+", identity):
            issues.append("oracle_listing_row_missing_numeric_id")
            continue
        if identity in seen:
            issues.append(f"oracle_listing_duplicate_id:{identity}")
        seen.add(identity)
        if not isinstance(row.get("Title"), str) or not row["Title"].strip():
            issues.append(f"oracle_listing_row_missing_title:{identity}")
        posted = row.get("PostedDate")
        if posted is not None and not (isinstance(posted, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted)):
            issues.append(f"oracle_listing_row_invalid_posted_date:{identity}")
        if row.get("secondaryLocations") is not None and not isinstance(row["secondaryLocations"], list):
            issues.append(f"oracle_listing_secondary_locations_not_list:{identity}")
    return {
        "rows": rows,
        "total": total,
        "offset": requested_offset,
        "page_size": page_size,
        "site_number": block.get("SiteNumber"),
        "issues": issues,
    }


def parse_detail_page(value: dict, job_id: str, expected_title: str | None = None) -> dict:
    """Validate the exact public detail response for one listed Id."""
    if not isinstance(value, dict):
        raise ValueError("Oracle detail response is not an object")
    items = value.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError(f"Oracle detail response for {job_id} has no single posting")
    row = items[0]
    if not isinstance(row, dict):
        raise ValueError(f"Oracle detail response for {job_id} is not an object")
    if str(row.get("Id")) != str(job_id):
        raise ValueError(f"Oracle detail response returned a different posting than {job_id}")
    if expected_title is not None and normalized_title(row.get("Title")) != normalized_title(expected_title):
        raise ValueError(f"Oracle detail title changed for {job_id}")
    if row.get("RequisitionId") is None:
        raise ValueError(f"Oracle detail for {job_id} has no requisition id")
    for field in BODY_FIELDS:
        if field not in row:
            raise ValueError(f"Oracle detail for {job_id} omitted inspected section {field}")
        if row[field] is not None and not isinstance(row[field], str):
            raise ValueError(f"Oracle detail section {field} for {job_id} is not HTML text")
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
    """Retain the published hiring range when the source states one.

    Oracle publishes ranges inside ExternalQualificationsStr, for example
    ``US: Hiring Range in USD from: $79,200 to $209,500 per annum. May be
    eligible for bonus and equity.`` or, for sales roles, a combined hourly and
    annual sentence.  Only explicit ``$X to $Y per <period>`` clauses under a
    ``Hiring Range in <CUR>`` label are parsed; the sentence is kept verbatim.
    The complete qualifications section remains in the description regardless.
    """
    text = visible_text(detail.get("ExternalQualificationsStr"))
    if not text or "Hiring Range" not in text:
        return None
    sentence = re.compile(
        r"([A-Z]{2}):\s*Hiring Range in ([A-Z]{3}) from:?\s*"
        r"(.*?)(?=\.\s|\.$|$)",
        re.S,
    )
    amount = re.compile(
        r"\$([\d,]+(?:\.\d+)?)\s*to\s*\$([\d,]+(?:\.\d+)?)\s*per\s+"
        r"(annum|year|hour|month)",
        re.I,
    )
    ranges: list[dict] = []
    sentences: list[str] = []
    for match in sentence.finditer(text):
        location, currency, clause = match.group(1).upper(), match.group(2).upper(), match.group(3)
        found = amount.findall(clause)
        if not found:
            continue
        for minimum, maximum, period in found:
            ranges.append({
                "location": location,
                "currency": currency,
                "min": float(minimum.replace(",", "")) if "." in minimum else int(minimum.replace(",", "")),
                "max": float(maximum.replace(",", "")) if "." in maximum else int(maximum.replace(",", "")),
                "period": "year" if period.lower() in ("annum", "year") else period.lower(),
            })
        value = text[match.start(): match.end()].strip().rstrip(".")
        if value not in sentences:
            sentences.append(value)
    if not ranges:
        return None
    result: dict = {
        "source": "ExternalQualificationsStr.HiringRange",
        "text": ". ".join(sentences) + ".",
        "ranges": ranges,
    }
    bonus = re.search(r"May be eligible for [^.]*\.", text)
    if bonus:
        result["bonus_note"] = bonus.group(0)
    commission = re.search(r"Eligible for commission with [^.]*\.", text)
    if commission:
        result["commission_note"] = commission.group(0)
    return result


def flex_values(detail: dict) -> dict[str, str]:
    values = {}
    for item in detail.get("requisitionFlexFields") or []:
        if isinstance(item, dict) and item.get("Prompt"):
            values[item["Prompt"]] = item.get("Value")
    return values


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
        if not source and key == "secondaryLocations":
            source = listing.get(key)
        for item in source or []:
            if isinstance(item, dict):
                add(item.get("Name") or item.get("LocationName"))
            elif isinstance(item, str):
                add(item)
    return values


class OracleGBU(Adapter):
    """Collect only the membership published on Oracle's official CX_45001 board."""

    VERIFICATION_REVISION = "oracle-gbu.official-oracle-careers-CX_45001.1"
    PAGE_SIZE = PAGE_SIZE

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._board_url = BOARD_URL

    # ---------------------------------------------------------------- entry
    async def _entry(self) -> float:
        started = time.perf_counter()
        entry = await self.http.get(self.company.careers_url, headers=PUBLIC_PAGE_HEADERS)
        if str(entry.url).rstrip("/") != OFFICIAL_ENTRY.rstrip("/"):
            raise ValueError(f"Oracle official entry redirected: {self.company.careers_url} -> {entry.url}")
        entry_info = parse_official_entry(entry.text, str(entry.url))
        board = await self.http.get(self._board_url, headers=PUBLIC_PAGE_HEADERS)
        board_info = parse_board_shell(board.text, str(board.url))
        settings = await self.http.json(ORACLE_ORIGIN + SETTINGS_PATH)
        site_config = parse_site_settings(settings)
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=str(entry.url),
            official_entry_title=entry_info["title"],
            official_board_anchor_texts=entry_info["board_anchor_texts"],
            company_listing_url=str(board.url),
            board_shell=board_info,
            site_number=SITE_NUMBER,
            site_settings=site_config,
            listing_authority=(
                "Oracle official careers page's 'Search jobs at Oracle' links to its own Candidate "
                "Experience board careers.oracle.com/en/sites/jobsearch (CX_45001); unfiltered findReqs "
                "membership, 14-row UI batches"
            ),
            entry_deep_link_filter={
                "note": "The entry page renders its search link with a United States location query for "
                        "the US-localised oracle.com page; the same board is fetched unfiltered so every "
                        "public posting is collected (no country filter).",
                "query_observed": "location=United States&locationId=300000000149325",
            },
            public_page_request_headers={
                "note": "oracle.com's public careers page returns 403 to the generic scraper UA; "
                        "a normal browser UA renders the same public page (no CAPTCHA/challenge).",
                "user_agent": PUBLIC_PAGE_HEADERS["User-Agent"],
            },
            talent_community_excluded={
                "urls": entry_info["talent_community_urls"],
                "reason": "talent-community sign-up route; no requisition IDs or vacancy listing",
            },
            native_page_size=self.PAGE_SIZE,
            ui_page_size=UI_PAGE_SIZE,
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
        return digest

    async def _listing(self, api, offset: int):
        if not self.page_guard():
            raise ValueError("Oracle native listing pagination budget exhausted")
        response = await api.get(listing_url(offset, self.PAGE_SIZE))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Oracle listing page at offset {offset} was not JSON") from exc
        page = parse_listing_page(payload, offset, self.PAGE_SIZE)
        if page["issues"] and not (len(page["rows"]) < self.PAGE_SIZE):
            raise ValueError(f"Oracle listing page at offset {offset} has issues: {page['issues']}")
        digest = self.evidence(response, payload, source_kind="oracle_unfiltered_listing")
        return page, str(response.url), digest

    # ---------------------------------------------------------------- detail
    async def _detail(self, api, job):
        response = await api.get(detail_url(job.job_id))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Oracle detail for {job.job_id} was not JSON") from exc
        row = parse_detail_page(payload, job.job_id, job.title)
        public = public_detail(row)
        digest = self.evidence(response, {**payload, "items": [public]}, source_kind="oracle_exact_detail")
        sections, role_text, missing = description_sections(public)
        flex = flex_values(public)
        job.raw["detail"] = public
        job.raw["detail_response_sha256"] = digest
        job.raw["flex_fields"] = flex
        job.raw["detail_request_url"] = str(response.url)
        job.raw["detail_public_fields_only"] = True
        job.raw["omitted_internal_fields"] = sorted(NONPUBLIC_FIELDS | {"Internal*"})
        job.raw["description_sections"] = [
            {"field": section["field"], "label": section["label"],
             "text_chars": len(section["text"])}
            for section in sections
        ]
        job.raw["missing_sections"] = missing
        job.description_method = "oracle.official_CX_45001.oracle_external_sections"
        job.description_source_url = str(response.url)
        job.requisition_id = str(public["RequisitionId"])
        job.apply_url = apply_page_url(job.job_id)
        job.date_posted = job.raw["listing"].get("PostedDate")
        job.date_posted_raw = {
            "listing_PostedDate": job.raw["listing"].get("PostedDate"),
            "detail_ExternalPostedStartDate": public.get("ExternalPostedStartDate"),
            "page_posting_date_is_localised": True,
        }
        job.valid_through = (public.get("ExternalPostedEndDate")
                             or job.raw["listing"].get("PostingEndDate") or None)
        job.language = public.get("ContentLocale") or job.raw["listing"].get("Language") or None
        job.locations = location_values(public, job.raw["listing"])
        job.department = public.get("Category") or job.raw["listing"].get("Category") or None
        job.team = public.get("JobFunction") or job.raw["listing"].get("JobFunction") or None
        job.employment_type = (flex.get("Job Type")
                               or public.get("WorkerType") or public.get("JobType")
                               or public.get("RequisitionType") or None)
        job.workplace_type = (public.get("WorkplaceType") or public.get("WorkplaceTypeCode")
                              or job.raw["listing"].get("WorkplaceType") or None)
        job.experience = flex.get("Years") or public.get("JobLevel") or None
        job.education = public.get("StudyLevel") or None
        job.employer = public.get("LegalEmployer") or None
        job.salary = compensation_fields(public)
        job.qualifications = public.get("ExternalQualificationsStr") or None
        job.responsibilities = public.get("ExternalResponsibilitiesStr") or None
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

    async def _repair_details(self, api, jobs):
        """One bounded repair pass for transient detail failures."""
        failed = [job for job in jobs if "detail_fetch_failed" in job.warnings]
        if not failed:
            return 0
        repaired = 0
        for job in failed:
            try:
                await self._detail(api, job)
            except Exception:
                continue
            job.warnings = [warning for warning in job.warnings if warning != "detail_fetch_failed"]
            self.result.validate_job(job)
            self.result.dirty_jobs.append(job)
            repaired += 1
        return repaired

    # ------------------------------------------------------------------- run
    def _job_from_listing(self, row: dict, listing_request_url: str):
        identity = str(row["Id"])
        job = self.job(
            {"listing": row},
            title=row["Title"],
            job_id=identity,
            url=job_page_url(identity),
            apply_url=apply_page_url(identity),
            date_posted=row.get("PostedDate"),
            date_posted_raw=row.get("PostedDate"),
            language=row.get("Language") or None,
        )
        job.record_identity = f"oracle-gbu:{identity}"
        job.raw["listing_request_url"] = listing_request_url
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
            advertised_total = None
            offset = 0
            enumerable_gap = 0
            while True:
                page, listing_url, listing_sha = await self._listing(api, offset)
                if advertised_total is None:
                    advertised_total = page["total"]
                elif page["total"] != advertised_total:
                    self.result.errors.append(
                        f"oracle_listing_total_changed:{advertised_total}->{page['total']}")
                    break
                current = [str(row["Id"]) for row in page["rows"]]
                if len(set(current)) != len(current):
                    self.result.errors.append(f"oracle_listing_duplicate_ids_at_offset:{offset}")
                    break
                if ids.intersection(current):
                    self.result.errors.append(f"oracle_listing_overlap_at_offset:{offset}")
                    break
                ids.update(current)
                pages.append({"offset": offset, "rows": len(current),
                              "first_id": current[0] if current else None,
                              "last_id": current[-1] if current else None,
                              "source_total": page["total"], "url": listing_url,
                              "sha256": listing_sha})
                for row in page["rows"]:
                    self.add(self._job_from_listing(row, listing_url))
                if not page["rows"]:
                    break
                if len(page["rows"]) < self.PAGE_SIZE:
                    # A short final batch.  The advertised counter can exceed
                    # the rows the public finder returns; record that gap.
                    enumerable_gap = max(0, page["total"] - len(ids))
                    if enumerable_gap:
                        self.result.warnings.append(
                            f"advertised_total_exceeds_enumerable_ids:{page['total']}->{len(ids)}")
                    break
                offset += self.PAGE_SIZE
            self.result.timings["listing_phase_seconds"] = round(
                max(0.0, time.perf_counter() - listing_started), 6)
            self.result.expected_total = advertised_total
            self.result.listing_complete = (
                advertised_total is not None and len(ids) == advertised_total
                and not self.result.errors)
            self.result.metadata["listing_pages"] = pages
            self.result.metadata["advertised_total"] = advertised_total
            self.result.metadata["enumerable_unique_ids"] = len(ids)
            self.result.metadata["advertised_enumerable_gap"] = (
                advertised_total - len(ids) if advertised_total is not None else None)
            self.result.metadata["company_published_job_ids"] = sorted(ids)

            self.result.jobs = await self.hydrate(
                self.result.jobs, lambda job: self._detail(api, job))

            repair_started = time.perf_counter()
            repaired = await self._repair_details(api, self.result.jobs)
            self.result.timings["detail_repair_seconds"] = round(
                max(0.0, time.perf_counter() - repair_started), 6)
            self.result.metadata["detail_repair_attempts"] = repaired

            final_started = time.perf_counter()
            final_ids: set[str] = set()
            final_pages: list[dict] = []
            offset = 0
            while True:
                page, listing_url, listing_sha = await self._listing(api, offset)
                current = [str(row["Id"]) for row in page["rows"]]
                if (page["total"] != advertised_total or len(set(current)) != len(current)
                        or final_ids.intersection(current)):
                    self.result.errors.append("oracle_final_listing_did_not_reconcile")
                    break
                final_ids.update(current)
                final_pages.append({"offset": offset, "ids": current, "url": listing_url,
                                    "sha256": listing_sha})
                if not page["rows"] or len(page["rows"]) < self.PAGE_SIZE:
                    break
                offset += self.PAGE_SIZE
            if final_ids and final_ids != ids:
                self.result.errors.append(
                    "oracle_listing_id_set_changed:"
                    f"added={len(final_ids - ids)},removed={len(ids - final_ids)}")
            self.result.listing_complete = (
                self.result.listing_complete and bool(final_ids) and final_ids == ids
                and not self.result.errors)
            self.result.metadata["final_listing_pages"] = final_pages
            self.result.metadata["final_listing_ids"] = sorted(final_ids)
            self.result.metadata["final_listing_added_ids"] = sorted(final_ids - ids)
            self.result.metadata["final_listing_removed_ids"] = sorted(ids - final_ids)
            self.result.metadata["final_listing_reconciled"] = bool(final_ids) and final_ids == ids
            self.result.timings["final_listing_seconds"] = round(
                max(0.0, time.perf_counter() - final_started), 6)
            self.result.metadata["membership_reconciliation"] = {
                "complete": self.result.listing_complete,
                "advertised_total": advertised_total,
                "enumerable_total": len(ids),
                "advertised_enumerable_gap": (
                    advertised_total - len(ids) if advertised_total is not None else None),
                "collected_ids": len(ids),
                "final_ids": len(final_ids),
                "final_listing_reconciled": self.result.metadata.get("final_listing_reconciled"),
                "ui_page_size": UI_PAGE_SIZE,
                "api_page_size": self.PAGE_SIZE,
                "pages": pages,
            }
        return self.result
