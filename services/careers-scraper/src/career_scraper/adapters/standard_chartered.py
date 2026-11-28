"""Standard Chartered's own SuccessFactors career site and its published search journey.

Current official journey (observed 2026-11-08):

* the ledger URL ``https://jobs.sc.com/search-jobs`` no longer resolves (the host
  is DNS-retired), so the company's own careers hub is the entry;
* ``https://www.sc.com/en/global-careers/`` links "Search our jobs" to
  ``https://jobs.standardchartered.com/``, a SAP SuccessFactors Career Site
  Builder site (companyId ``standardch``, locale ``en_GB``);
* the published listing is ``/search-jobs`` ("1 to 10 of 1004 results") whose
  own widget posts to ``/services/recruiting/v1/jobs`` with an empty
  ``facetFilters`` object, so membership is every public posting, unfiltered;
* the widget's default relevance ordering is unstable for deep pages (a full
  sweep returned 743 unique ids for 1004 rows), while the same endpoint's
  ``sortBy:"date"`` returns 1004 unique ids for the same 1004 ``totalJobs``;
* full role text is the server-rendered ``span[itemprop="description"]`` of the
  exact listing detail page ``/job/<urlTitle>/<id>-en_GB``.

The site's sitemap (1014 URLs) and its facets (Region/Market/Area of Interest/
Employment Type/Career Type) are used only as completeness cross-checks; they
never replace the listing. The sitemap retains 7 closed postings and 2 live
``en_US``-only pages that are outside the visible language menu; both are
recorded, not collected.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import html as html_module
import math
import re
import time
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError, HTTP
from ..models import text_html

OFFICIAL_ENTRY = "https://www.sc.com/en/global-careers/"
CAREERS_ORIGIN = "https://jobs.standardchartered.com"
CAREERS_HOME = CAREERS_ORIGIN + "/"
LISTING_PAGE = CAREERS_ORIGIN + "/search-jobs"
JOBS_API = CAREERS_ORIGIN + "/services/recruiting/v1/jobs"
DETAIL_BASE = CAREERS_ORIGIN + "/job/"
SITEMAP_URL = CAREERS_ORIGIN + "/sitemap.xml"
LOCALE = "en_GB"
PAGE_SIZE = 10
SORT_BY = "date"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
JSON_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": CAREERS_ORIGIN,
    "Referer": LISTING_PAGE,
}
HTML_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": LISTING_PAGE,
}
DATE_FORMAT = "%d/%m/%Y"
EMPLOYER = "Standard Chartered Bank"
DESCRIPTION_METHOD = "standardchartered_detail_itemprop_description"
FACET_FIELDS = (
    ("cust_region", "Region"),
    ("jobLocationCountry", "Market"),
    ("mfield1", "Area of Interest"),
    ("cust_csb_employmentType", "Employment Type"),
    ("cust_csb_careerType", "Career Type"),
)
JOB_ID_FROM_PATH = re.compile(r"^/job/[^/]+/(\d+)-([a-z]{2})_([A-Z]{2})/?$")
JOB_ID_FROM_APPLY = re.compile(r"/talentcommunity/apply/(\d+)/")
LOCALE_LINK = re.compile(r"locale=([a-z]{2}_[A-Z]{2})")
FACET_CONFIG = re.compile(r'fieldId\s*:\s*"([^"]+)"\s*,\s*label\s*:\s*"([^"]+)"')
SITEMAP_ENTRY = re.compile(r"<loc>(.*?)</loc>", re.S)
SITEMAP_PATH_ID = re.compile(r"^/job/[^/]+/(\d+)/?$")
EMPTY_VALUES = {"", "null", "none", "n/a", "na", "-"}
NOT_AVAILABLE = re.compile(r"can'?t view this job because it'?s not available", re.I)


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(value or "")).strip()


def normalize_title(value: str | None) -> str:
    return normalize_text(value)


def parse_date(value) -> str | None:
    """Parse Standard Chartered's en_GB dd/mm/yyyy source date, never guessing."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, DATE_FORMAT)
    except ValueError:
        return None
    return parsed.date().isoformat()


def parse_sitemap(xml: str) -> dict:
    if not isinstance(xml, str) or "<urlset" not in xml:
        raise ValueError("Standard Chartered sitemap response is not a urlset document")
    locs = [normalize_text(value) for value in SITEMAP_ENTRY.findall(xml)]
    rows, issues, seen = [], [], set()
    for loc in locs:
        parsed = urlsplit(loc)
        if parsed.scheme != "https" or parsed.netloc != "jobs.standardchartered.com":
            issues.append(f"sitemap_foreign_url:{loc[:120]}")
            continue
        match = SITEMAP_PATH_ID.match(parsed.path)
        if not match:
            issues.append(f"sitemap_unrecognised_path:{parsed.path[:120]}")
            continue
        identity = match.group(1)
        if identity in seen:
            issues.append(f"sitemap_duplicate_internal_id:{identity}")
            continue
        seen.add(identity)
        rows.append({"url": loc, "internal_id": identity})
    if not rows:
        raise ValueError("Standard Chartered sitemap exposed no job URLs")
    return {"urls": len(locs), "rows": rows, "internal_ids": sorted(seen), "issues": issues}


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.sc.com"
            or not parsed.path.startswith("/en/global-careers")):
        raise ValueError("Standard Chartered official careers entry changed host or path")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalize_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Standard Chartered" not in title:
        raise ValueError("Standard Chartered official careers entry identity changed")
    jobs_links, hot_jobs, locale_links = [], [], []
    for anchor in soup.select("a[href]"):
        href = urljoin(response_url, anchor.get("href") or "")
        parts = urlsplit(href)
        text = normalize_title(anchor.get_text(" ", strip=True))
        if parts.netloc == "jobs.standardchartered.com":
            entry = {"text": text, "href": href}
            jobs_links.append(entry)
            if parts.path == "/search/" and "q=" in (parts.query or ""):
                hot_jobs.append(entry)
        if parts.netloc == "www.sc.com" and parts.path.startswith("/en/global-careers"):
            locale = "en_GB"
            if (text, parts.path, locale) not in [(row["text"], urlsplit(row["href"]).path, locale) for row in locale_links]:
                locale_links.append({"text": text, "href": href})
    if not jobs_links:
        raise ValueError("Standard Chartered careers hub no longer links to jobs.standardchartered.com")
    if not any("Search our jobs" in row["text"] or row["href"].rstrip("/") == CAREERS_HOME for row in jobs_links):
        raise ValueError("Standard Chartered careers hub no longer publishes its 'Search our jobs' link")
    deduped = {}
    for row in jobs_links:
        deduped.setdefault(row["href"], row)
    return {
        "request_url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "title": title,
        "jobs_links": list(deduped.values()),
        "hot_job_links": hot_jobs,
        "careers_hub_links": locale_links[:40],
    }


def parse_search_page(html: str, response_url: str = LISTING_PAGE) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "jobs.standardchartered.com" or parsed.path != "/search-jobs":
        raise ValueError("Standard Chartered search page URL changed")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalize_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != "Standard Chartered Bank Jobs":
        raise ValueError("Standard Chartered search page title changed")
    if "companyId: 'standardch'" not in (html or ""):
        raise ValueError("Standard Chartered search page companyId changed")
    facets = [{"field": field, "label": label} for field, label in FACET_CONFIG.findall(html or "")]
    if not any(row["field"] == "jobLocationCountry" for row in facets):
        raise ValueError("Standard Chartered search page facet configuration changed")
    locales = sorted({match for match in LOCALE_LINK.findall(html or "")})
    return {
        "url": response_url,
        "title": title,
        "company_id": "standardch",
        "facet_fields": facets,
        "locale_links": locales,
        "has_keyword_input": bool(soup.select_one('[data-testid="searchByKeywords"]')),
        "has_sort_control": "Sort by" in (html or ""),
    }


def search_request(page_number: int, *, locale: str = LOCALE) -> dict:
    """The exact body Standard Chartered's own search widget posts."""
    return {
        "locale": locale,
        "pageNumber": int(page_number),
        "sortBy": SORT_BY,
        "keywords": "",
        "location": "",
        "facetFilters": {},
        "brand": "",
        "skills": [],
        "categoryId": 0,
        "alertId": "",
        "rcmCandidateId": "",
    }


def facet_request(locale: str = LOCALE) -> dict:
    return {
        "facetingOnly": True,
        "categoryId": 0,
        "brand": "",
        "locale": locale,
        "keywords": "",
        "location": "",
        "facetFields": [field for field, _ in FACET_FIELDS],
        "facetFilters": {},
        "alertId": "",
        "skills": [],
        "rcmCandidateId": "",
    }


def _row_country(row: dict) -> list[str]:
    values = row.get("jobLocationCountry")
    if isinstance(values, list):
        return [normalize_title(value) for value in values if normalize_title(value)]
    return []


def parse_search_response(value: dict, requested_page: int) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Standard Chartered jobs API response is not an object")
    total = value.get("totalJobs")
    rows = value.get("jobSearchResult")
    if not isinstance(total, int) or total < 0 or not isinstance(rows, list):
        raise ValueError("Standard Chartered jobs API response is missing totalJobs/jobSearchResult")
    if len(rows) > PAGE_SIZE:
        raise ValueError("Standard Chartered jobs API returned more rows than its page size")
    parsed_rows, issues, seen = [], [], set()
    for item in rows:
        response = item.get("response") if isinstance(item, dict) else None
        if not isinstance(response, dict):
            issues.append("row_not_object")
            continue
        identity = str(response.get("id") or "")
        if not re.fullmatch(r"\d+", identity):
            issues.append("row_missing_numeric_id")
            continue
        if identity in seen:
            issues.append(f"row_duplicate_id:{identity}")
            continue
        if not normalize_title(response.get("unifiedStandardTitle")):
            issues.append(f"row_missing_title:{identity}")
            continue
        if not (response.get("urlTitle") or response.get("unifiedUrlTitle")):
            issues.append(f"row_missing_url_title:{identity}")
            continue
        if parse_date(response.get("unifiedStandardStart")) is None:
            issues.append(f"row_missing_or_invalid_posting_start:{identity}")
        seen.add(identity)
        parsed_rows.append(dict(response))
    expected_page_rows = min(PAGE_SIZE, max(0, total - requested_page * PAGE_SIZE))
    if len(rows) != expected_page_rows:
        issues.append("page_row_count_mismatch")
    if requested_page == 0 and total and not parsed_rows:
        issues.append("first_page_has_no_rows")
    return {
        "rows": parsed_rows,
        "total": total,
        "page": requested_page,
        "page_size": PAGE_SIZE,
        "issues": issues,
    }


def parse_facet_response(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Standard Chartered facet response is not an object")
    total = value.get("totalJobs")
    fields = value.get("facetFields")
    if not isinstance(total, int) or not isinstance(fields, list) or not fields:
        raise ValueError("Standard Chartered facet response is missing totalJobs/facetFields")
    parsed, issues = {}, []
    labels = dict(FACET_FIELDS)
    for field in fields:
        name = field.get("name") if isinstance(field, dict) else None
        values = field.get("values") if isinstance(field, dict) else None
        if not isinstance(name, str) or not isinstance(values, list):
            issues.append("facet_field_not_identified")
            continue
        counts = {}
        for row in values:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("count"), int):
                issues.append(f"facet_value_invalid:{name}")
                continue
            counts[normalize_title(row["name"])] = row["count"]
        parsed[name] = {
            "label": labels.get(name),
            "value_count": field.get("valueCount"),
            "counts": counts,
            "sum": sum(counts.values()),
        }
    if "jobLocationCountry" not in parsed or "cust_region" not in parsed:
        issues.append("required_facet_missing")
    return {"total": total, "facets": parsed, "issues": issues}


def job_detail_url(row: dict, *, locale: str = LOCALE) -> str:
    title = html_module.unescape(str(row.get("urlTitle") or row.get("unifiedUrlTitle") or "job"))
    title = quote(title, safe="%-._~!$&'()*+,;=:@")
    return f"{DETAIL_BASE}{title}/{row['id']}-{locale}"


def description_sections(description_html: str) -> dict:
    """Return employer-authored heading/body pairs without synthesising text."""
    soup = BeautifulSoup(f'<div data-sc-body="1">{description_html or ""}</div>', "lxml")
    root = soup.select_one("[data-sc-body]")
    sections: dict[str, str] = {}
    if root is None:
        return sections
    for heading in root.find_all(["h1", "h2", "h3", "h4", "h5"]):
        text = normalize_title(heading.get_text(" ", strip=True))
        if not text:
            continue
        wrapper = heading.find_parent("div")
        body = wrapper.find_next_sibling("div") if wrapper is not None else None
        body_text = text_html(str(body)) if body is not None else ""
        if body_text:
            sections.setdefault(text, body_text)
    return sections


def _section_text(sections: dict, needles: tuple[str, ...]) -> str | None:
    parts = []
    for heading, body in sections.items():
        lowered = heading.lower()
        if not any(needle in lowered for needle in needles):
            continue
        if body.strip().lower() in EMPTY_VALUES:
            continue
        parts.append(f"{heading}\n{body.strip()}")
    return "\n\n".join(parts) or None


def parse_salary(tokens: dict, listing_row: dict | None = None) -> dict | None:
    labels = {label: value for label, value in tokens.items() if "salary" in label.lower() and value}
    if not labels:
        return None
    currency = None
    if isinstance(listing_row, dict):
        currencies = listing_row.get("currency")
        if isinstance(currencies, list) and currencies:
            currency = currencies[0]
    return {
        "source_text": labels,
        "currency": currency,
        "source_field": "detail.joblayouttoken",
    }


def _token_map(soup: BeautifulSoup) -> tuple[str, dict[str, str], list[dict]]:
    """Split the detail page's label/value tokens from its unlabelled title token."""
    title = ""
    values: dict[str, str] = {}
    ordered: list[dict] = []
    for token in soup.select(".jobColumnOne .joblayouttoken"):
        label_node = token.select_one(".joblayouttoken-label")
        value_node = token.select_one('span[itemprop="description"]') or token.select_one("span.rtltextaligneligible")
        label = normalize_title(label_node.get_text(" ", strip=True)) if label_node else ""
        value = value_node.get_text(" ", strip=True) if value_node is not None else ""
        if not label:
            if not title and value:
                title = normalize_title(value)
            continue
        key = label.rstrip(":：").strip()
        values[key] = normalize_title(value) if key != "Job Description" else value
        ordered.append({"label": label, "value": values[key] if key != "Job Description" else "<description-html>"})
    return title, values, ordered


def parse_detail_page(html: str, response_url: str, listing_row: dict) -> dict:
    if not isinstance(html, str) or len(html) < 500:
        raise ValueError("Standard Chartered detail response is empty")
    soup = BeautifulSoup(html, "lxml")
    error = soup.select_one("p.jobErrMsg")
    if error is not None and NOT_AVAILABLE.search(error.get_text(" ", strip=True)):
        raise LookupError("standardchartered_detail_not_available")
    path_match = JOB_ID_FROM_PATH.match(urlsplit(response_url).path)
    listing_id = str(listing_row.get("id"))
    if path_match is None:
        raise ValueError("Standard Chartered detail URL no longer exposes the unified job id")
    detail_id, detail_lang, detail_country = path_match.groups()
    if detail_id != listing_id:
        raise ValueError(f"Standard Chartered detail id {detail_id} does not match listed id {listing_id}")

    title_node = soup.select_one('span[itemprop="title"]')
    title = normalize_title(title_node.get_text(" ", strip=True) if title_node else "")
    token_title, tokens, ordered = _token_map(soup)
    title = title or token_title
    if not title:
        raise ValueError("Standard Chartered detail page has no title")

    desc_node = soup.select_one('span[itemprop="description"]')
    description_html = "".join(str(child) for child in desc_node.contents).strip() if desc_node else ""
    description_text = text_html(description_html)

    apply_node = soup.select_one(".jobColumnOne a.unify-apply-now[href]") or soup.select_one('a[href*="/talentcommunity/apply/"]')
    apply_url = urljoin(response_url, apply_node.get("href")) if apply_node is not None else None
    apply_match = JOB_ID_FROM_APPLY.search(apply_url or "")
    apply_id = apply_match.group(1) if apply_match else None
    if apply_id and apply_id != listing_id:
        raise ValueError(f"Standard Chartered apply id {apply_id} does not match listed id {listing_id}")

    requisition = normalize_title(tokens.get("Requisition Number"))
    if requisition and requisition != listing_id:
        raise ValueError(f"Standard Chartered requisition {requisition} does not match listed id {listing_id}")

    raw_location = normalize_title(tokens.get("Job Location"))
    location_parts = [normalize_title(part) for part in raw_location.split("|") if normalize_title(part)]
    countries = _row_country(listing_row)
    locations = []
    for index, part in enumerate(location_parts):
        locations.append({
            "source_text": part,
            "country": countries[index] if index < len(countries) else (countries[0] if len(countries) == 1 else None),
            "listing_city": (listing_row.get("jobLocationShort") or [None])[index]
            if index < len(listing_row.get("jobLocationShort") or []) else None,
        })
    if not locations and countries:
        locations = [{"source_text": country, "country": country, "listing_city": None} for country in countries]

    sections = description_sections(description_html)
    start_raw = normalize_title(tokens.get("Posting Start Date"))
    end_raw = normalize_title(tokens.get("Posting End Date"))
    detail_start = parse_date(start_raw)
    detail_end = parse_date(end_raw)
    listing_start = parse_date(listing_row.get("unifiedStandardStart"))
    listing_end = parse_date(listing_row.get("unifiedStandardEnd"))
    if detail_start and listing_start and detail_start != listing_start:
        raise ValueError(f"Standard Chartered detail start {detail_start} != listing start {listing_start}")
    if detail_end and listing_end and detail_end != listing_end:
        detail_end = listing_end

    employer = {"name": EMPLOYER, "source": "site title/brand"}
    return {
        "title": title,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(normalize_text(description_text)),
        "description_body_chars": len(description_body_text(description_html)),
        "description_sha256": sha256(description_text.encode()).hexdigest() if description_text else None,
        "apply_url": apply_url,
        "apply_id": apply_id,
        "detail_id": detail_id,
        "detail_locale": f"{detail_lang}_{detail_country}",
        "requisition_id": requisition,
        "tokens": {key: value for key, value in tokens.items() if key != "Job Description"},
        "token_order": ordered,
        "location": raw_location,
        "locations": locations,
        "country": countries[0] if countries else None,
        "countries": countries,
        "global_grade": normalize_title(tokens.get("Global Grade")) or None,
        "work_type": normalize_title(tokens.get("Work Type")) or None,
        "employment_type": normalize_title(tokens.get("Employment Type")) or None,
        "date_posted": detail_start,
        "date_posted_raw": start_raw or None,
        "valid_through": detail_end,
        "valid_through_raw": end_raw or None,
        "salary": parse_salary(tokens, listing_row),
        "sections": sections,
        "responsibilities": _section_text(sections, ("responsibilit", "key responsibilities", "what you will do", "duties")),
        "qualifications": _section_text(sections, ("qualification", "skills and experience", "requirements", "our ideal candidate")),
        "experience": _section_text(sections, ("experience",)),
        "education": _section_text(sections, ("education",)),
        "employer": employer,
        "listing_title": normalize_title(listing_row.get("unifiedStandardTitle")),
    }


def description_body_text(description_html: str) -> str:
    """Employer body text below its own headings (empty when only headings exist)."""
    soup = BeautifulSoup(f'<div data-sc-body="1">{description_html or ""}</div>', "lxml")
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        heading.decompose()
    root = soup.select_one("[data-sc-body]")
    return " ".join(text_html(str(root)).split()) if root is not None else ""


def description_is_empty(parsed: dict) -> bool:
    body = (parsed.get("description_text") or "").strip().lower()
    if body in EMPTY_VALUES or len(parsed.get("description_text") or "") < 100:
        return True
    # Some requisitions publish only the section headings ("Job Summary",
    # "Key Responsibilities", ...) with no body under any of them.
    return len(description_body_text(parsed.get("description_html") or "")) < 60


class StandardChartered(Adapter):
    """Follow only Standard Chartered's published search listing and its job pages."""

    VERIFICATION_REVISION = "standardchartered.successfactors-csb.global-search-and-detail.1"

    def __init__(self, http, company, source, options, *, sitemap_resolution: dict | None = None,
                 detail_output=None):
        super().__init__(http, company, source, options)
        self.detail_output = detail_output
        self.detail_http = None
        self.detail_transport = None
        self.rows: dict[str, dict] = {}
        self.row_pages: dict[str, int] = {}
        self.row_locales: dict[str, str] = {}
        self.duplicate_rows = 0
        self.detail_blocked = False
        self.sitemap_resolution = sitemap_resolution or {}

    async def _get_json(self, body: dict) -> dict:
        return await self.http.json(JOBS_API, method="POST", json=body, headers=JSON_HEADERS)

    def _ingest(self, parsed: dict, page: int, *, locale: str = LOCALE) -> dict:
        new_rows = []
        for row in parsed["rows"]:
            identity = str(row["id"])
            if identity in self.rows:
                self.duplicate_rows += 1
                continue
            self.rows[identity] = row
            self.row_pages[identity] = page
            self.row_locales[identity] = locale
            new_rows.append(row)
        return {"page": page, "rows": len(parsed["rows"]), "new": len(new_rows), "total": parsed["total"],
                "issues": parsed["issues"]}

    async def _enumerate_listing(self) -> None:
        started = time.perf_counter()
        if not self.page_guard():
            raise ValueError("Standard Chartered listing page budget exhausted")
        first = parse_search_response(await self._get_json(search_request(0)), 0)
        total_initial = first["total"]
        self.result.expected_total = total_initial
        self.result.metadata["listing_total_initial"] = total_initial
        self._ingest(first, 0)
        page_count = max(1, math.ceil(total_initial / PAGE_SIZE))
        # `sortBy=date` is newest-first. Read the oldest pages first so postings
        # published while the crawl runs cannot shift pages already read.
        for page in range(page_count - 1, 0, -1):
            if not self.page_guard():
                break
            parsed = parse_search_response(await self._get_json(search_request(page)), page)
            self.result.expected_total = parsed["total"]
            self._ingest(parsed, page)
        # Top-up pass over the newest pages, then the final reconcile.
        for page in range(0, min(20, page_count)):
            if not self.page_guard():
                break
            parsed = parse_search_response(await self._get_json(search_request(page)), page)
            self.result.expected_total = parsed["total"]
            self._ingest(parsed, page)
        self.result.timings["listing_crawl_seconds"] = max(0.0, time.perf_counter() - started)

    async def _final_reconcile(self) -> None:
        started = time.perf_counter()
        if not self.page_guard():
            return
        first = parse_search_response(await self._get_json(search_request(0)), 0)
        final_total = first["total"]
        last_page = max(0, math.ceil(final_total / PAGE_SIZE) - 1)
        if not self.page_guard():
            return
        last = parse_search_response(await self._get_json(search_request(last_page)), last_page)
        self.result.expected_total = final_total
        self.result.metadata["listing_total_final"] = final_total
        self.result.metadata["listing_last_page"] = last_page
        self.result.metadata["listing_last_page_rows"] = len(last["rows"])
        self.result.metadata["listing_unique_ids"] = len(self.rows)
        self.result.metadata["listing_duplicate_rows"] = self.duplicate_rows
        self.result.metadata["listing_pages_fetched"] = self.result.pages
        self.result.timings["final_listing_reconciliation_seconds"] = max(0.0, time.perf_counter() - started)

    def _add_job(self, row: dict) -> None:
        identity = str(row["id"])
        title = normalize_title(row.get("unifiedStandardTitle"))
        url = job_detail_url(row, locale=self.row_locales.get(identity, LOCALE))
        countries = _row_country(row)
        short_locations = [normalize_title(value) for value in (row.get("jobLocationShort") or []) if normalize_title(value)]
        locations = []
        for index, value in enumerate(short_locations):
            locations.append({"source_text": value, "country": countries[index] if index < len(countries) else None})
        if not locations and countries:
            locations = [{"source_text": country, "country": country} for country in countries]
        functions = [normalize_title(value) for value in (row.get("mfield1") or []) if normalize_title(value)]
        self.add(self.job(
            {"listing": row, "listing_page": self.row_pages.get(identity), "listing_locale": self.row_locales.get(identity)},
            title=title,
            url=url,
            job_id=identity,
            requisition_id=identity,
            locations=locations,
            department=functions[0] if functions else None,
            employer=EMPLOYER,
            language=self.row_locales.get(identity),
            date_posted=parse_date(row.get("unifiedStandardStart")),
            date_posted_raw=row.get("unifiedStandardStart"),
            valid_through=parse_date(row.get("unifiedStandardEnd")),
            method=None,
            description_url=url,
        ))

    async def detail(self, job):
        if self.detail_blocked:
            raise RuntimeError("detail_fetch_stopped_after_rate_limit")
        if self.detail_http is None:
            raise RuntimeError("detail HTTP client not initialised")
        try:
            response = await self.detail_http.get(job.url, headers=HTML_HEADERS)
        except FetchError as exc:
            if "429" in str(exc):
                self.detail_blocked = True
            raise
        listing_row = job.raw["listing"]
        try:
            parsed = parse_detail_page(response.text, str(response.url), listing_row)
        except LookupError:
            job.description_unavailable = True
            job.warnings.append("detail_not_available")
            job.raw["detail"] = {
                "response_sha256": sha256(response.content).hexdigest(),
                "final_url": str(response.url),
                "response_bytes": len(response.content),
                "not_available": True,
            }
            return job
        if parsed["listing_title"] and normalize_title(parsed["title"]) != parsed["listing_title"]:
            job.warnings.append("detail_listing_title_differs")
        job.title = parsed["title"] or job.title
        job.requisition_id = parsed["requisition_id"] or job.requisition_id
        job.apply_url = parsed["apply_url"]
        job.description_html = parsed["description_html"]
        job.description_method = DESCRIPTION_METHOD
        job.description_source_url = job.url
        job.locations = parsed["locations"] or job.locations
        job.department = job.department or None
        job.employment_type = parsed["employment_type"]
        job.workplace_type = parsed["work_type"]
        job.employer = parsed["employer"]
        job.language = parsed["detail_locale"]
        job.date_posted = parsed["date_posted"] or job.date_posted
        job.date_posted_raw = parsed["date_posted_raw"] or job.date_posted_raw
        job.valid_through = parsed["valid_through"] or job.valid_through
        job.salary = parsed["salary"]
        job.qualifications = parsed["qualifications"]
        job.responsibilities = parsed["responsibilities"]
        job.experience = parsed["experience"]
        job.education = parsed["education"]
        job.raw["detail"] = {
            "response_sha256": sha256(response.content).hexdigest(),
            "final_url": str(response.url),
            "response_bytes": len(response.content),
            "description_normalized_chars": parsed["description_normalized_chars"],
            "description_body_chars": parsed["description_body_chars"],
            "description_sha256": parsed["description_sha256"],
            "description_text_sha256": parsed["description_sha256"],
            "apply_id": parsed["apply_id"],
            "detail_id": parsed["detail_id"],
            "detail_locale": parsed["detail_locale"],
            "requisition_id": parsed["requisition_id"],
            "global_grade": parsed["global_grade"],
            "work_type": parsed["work_type"],
            "employment_type": parsed["employment_type"],
            "location": parsed["location"],
            "countries": parsed["countries"],
            "sections": parsed["sections"],
            "tokens": parsed["tokens"],
            "token_order": parsed["token_order"],
            "listing_title": parsed["listing_title"],
            "salary": parsed["salary"],
            "date_posted_detail": parsed["date_posted"],
            "date_posted_detail_raw": parsed["date_posted_raw"],
            "valid_through_detail": parsed["valid_through"],
            "valid_through_detail_raw": parsed["valid_through_raw"],
        }
        if description_is_empty(parsed):
            job.description_unavailable = True
            job.warnings.append("publisher_empty_description")
        return job

    def _country_facet_check(self, facets: dict, facet_total: int | None = None) -> dict:
        facet = facets.get("jobLocationCountry") or {}
        counts = facet.get("counts") or {}
        collected: dict[str, int] = {}
        for row in self.rows.values():
            for country in _row_country(row):
                collected[country] = collected.get(country, 0) + 1
        mismatch = {
            country: {"facet": counts.get(country, 0), "collected": collected.get(country, 0)}
            for country in sorted(set(counts) | set(collected))
            if counts.get(country, 0) != collected.get(country, 0)
        }
        return {
            "facet_total": facet_total,
            "facet_sum": facet.get("sum"),
            "collected_rows": len(self.rows),
            "collected_location_slots": sum(collected.values()),
            "mismatches": mismatch,
            "counts_match": not mismatch,
        }

    async def _sitemap_cross_check(self, sitemap: dict, sitemap_sha256: str) -> dict:
        listing_ids = set(self.rows)
        known = self.sitemap_resolution if isinstance(self.sitemap_resolution, dict) else {}
        raw_entries = known.get("entries") if isinstance(known.get("entries"), dict) else {}
        # The sitemap XML escapes ampersands in <loc>; the pre-run resolution was
        # captured verbatim, so normalise both sides before comparing URL sets.
        entries = {normalize_text(url): value for url, value in raw_entries.items()
                   if isinstance(value, dict)}
        current_urls = {row["url"] for row in sitemap["rows"]}
        # <lastmod> changes daily, so reuse the pre-run resolution whenever the
        # URL set is unchanged and resolve only genuinely new sitemap URLs.
        reused = bool(entries) and current_urls <= set(entries)
        newly_resolved = {}
        if not reused:
            for url in sorted(current_urls - set(entries)):
                try:
                    response = await self.http.get(url, headers=HTML_HEADERS)
                except FetchError as exc:
                    newly_resolved[url] = {"status": None, "location": None, "id": None, "error": str(exc)}
                    continue
                match = JOB_ID_FROM_PATH.match(urlsplit(str(response.url)).path)
                locale = f"{match.group(2)}_{match.group(3)}" if match else None
                newly_resolved[url] = {
                    "status": response.status_code,
                    "location": str(response.url),
                    "id": match.group(1) if match else None,
                    "locale": locale,
                }
            entries.update(newly_resolved)
        mapped: dict[str, str] = {}
        locales: dict[str, str] = {}
        for url, value in entries.items():
            identity = value.get("id") if isinstance(value, dict) else None
            if identity:
                mapped.setdefault(str(identity), url)
                locales.setdefault(str(identity), value.get("locale") or "")
        sitemap_ids = set(mapped)
        return {
            "sitemap_urls": sitemap["urls"],
            "sitemap_unique_internal_ids": len(sitemap["internal_ids"]),
            "sitemap_resolution_reused": reused,
            "sitemap_resolution_sha256": known.get("sitemap_sha256"),
            "sitemap_sha256": sitemap_sha256,
            "sitemap_url_set_unchanged": current_urls == set(entries) if entries else False,
            "sitemap_new_urls_resolved": len(newly_resolved),
            "sitemap_resolution_error_urls": sorted(
                url for url, value in newly_resolved.items() if not value.get("id")),
            "listing_ids_present_in_sitemap": len(listing_ids & sitemap_ids),
            "sitemap_only_ids": sorted(sitemap_ids - listing_ids),
            "listing_only_ids": sorted(listing_ids - sitemap_ids),
            "locale_variants": {identity: locale for identity, locale in locales.items()
                                if locale and locale != LOCALE},
        }

    async def _audit_sitemap_only(self, cross: dict) -> list[dict]:
        """Classify sitemap-only ids: closed postings vs live locale-only pages."""
        entries = (self.sitemap_resolution or {}).get("entries") or {}
        by_id = {}
        for url, value in entries.items():
            if isinstance(value, dict) and value.get("id"):
                by_id.setdefault(str(value["id"]), (url, value.get("location") or url, value.get("locale") or LOCALE))
        audited = []
        for identity in cross.get("sitemap_only_ids") or []:
            url, location, locale = by_id.get(str(identity), (None, None, LOCALE))
            if not location:
                audited.append({"id": identity, "url": None, "outcome": "unresolved_no_sitemap_resolution"})
                continue
            detail_url = urljoin(CAREERS_ORIGIN, location)
            try:
                response = await self.detail_http.get(detail_url, headers=HTML_HEADERS)
            except FetchError as exc:
                audited.append({"id": identity, "url": detail_url, "outcome": "request_failed", "error": str(exc)})
                continue
            soup = BeautifulSoup(response.text, "lxml")
            error = soup.select_one("p.jobErrMsg")
            desc = soup.select_one('span[itemprop="description"]')
            title_node = soup.select_one('span[itemprop="title"]')
            closed = error is not None and NOT_AVAILABLE.search(error.get_text(" ", strip=True))
            audited.append({
                "id": identity,
                "url": detail_url,
                "locale": locale,
                "status": response.status_code,
                "outcome": "closed_not_available" if closed else "live_public_page_outside_listing",
                "title": normalize_title(title_node.get_text(" ", strip=True) if title_node else ""),
                "description_chars": len(desc.get_text(" ", strip=True)) if desc else 0,
                "response_sha256": sha256(response.content).hexdigest(),
            })
        return audited

    async def run(self):
        started = time.perf_counter()
        entry = await self.http.get(OFFICIAL_ENTRY, headers=HTML_HEADERS)
        official = parse_official_entry(entry.text, str(entry.url))
        self.result.metadata["official_entry"] = {
            **official,
            "status": entry.status_code,
            "redirects": [
                {"status": item.status_code, "url": str(item.url), "location": item.headers.get("location")}
                for item in entry.history
            ],
        }
        search = await self.http.get(LISTING_PAGE, headers=HTML_HEADERS)
        self.result.metadata["search_page"] = {**parse_search_page(search.text, str(search.url)),
                                               "status": search.status_code}
        home = await self.http.get(CAREERS_HOME, headers=HTML_HEADERS)
        self.result.metadata["careers_home"] = {
            "url": str(home.url), "status": home.status_code,
            "title": normalize_title(BeautifulSoup(home.text, "lxml").title.get_text(" ", strip=True)
                                     if BeautifulSoup(home.text, "lxml").title else ""),
        }

        facets = parse_facet_response(await self._get_json(facet_request()))
        self.result.metadata["facets"] = facets
        self.result.metadata["scope"] = {
            "official_entry": OFFICIAL_ENTRY,
            "final_url": str(entry.url),
            "careers_site": CAREERS_HOME,
            "listing_url": LISTING_PAGE,
            "jobs_api": JOBS_API,
            "filters": "facetFilters={} (no country, keyword, category, grade or seniority filter)",
            "sort": SORT_BY,
            "page_size": PAGE_SIZE,
            "listing_locale": LOCALE,
            "locales_offered_by_site": self.result.metadata["search_page"].get("locale_links"),
        }

        await self._enumerate_listing()

        sitemap_response = await self.http.get(SITEMAP_URL, headers=HTML_HEADERS)
        sitemap = parse_sitemap(sitemap_response.text)
        sitemap_sha = sha256(sitemap_response.content).hexdigest()
        sitemap_cross = await self._sitemap_cross_check(sitemap, sitemap_sha)
        self.result.metadata["sitemap_cross_check"] = sitemap_cross

        await self._final_reconcile()

        rows_in_date_order = sorted(
            self.rows.values(),
            key=lambda row: (parse_date(row.get("unifiedStandardStart")) or "", str(row.get("id"))),
            reverse=True,
        )
        for row in rows_in_date_order:
            self._add_job(row)
        country_reconciliation = self._country_facet_check(facets["facets"], facets["total"])
        facet_sums = {
            name: facet.get("sum")
            for name, facet in facets["facets"].items()
            if name in ("cust_region", "cust_csb_employmentType", "cust_csb_careerType")
        }
        facets_sum_ok = all(value == facets["total"] for value in facet_sums.values())
        membership_complete = (
            len(self.rows) == self.result.metadata.get("listing_total_final")
            and country_reconciliation["counts_match"]
            and facets_sum_ok
        )
        self.result.metadata["listing_membership"] = {
            "unique_ids": len(self.rows),
            "initial_total": self.result.metadata.get("listing_total_initial"),
            "final_total": self.result.metadata.get("listing_total_final"),
            "duplicate_rows": self.duplicate_rows,
            "complete": membership_complete,
        }
        self.result.metadata["country_reconciliation"] = country_reconciliation
        self.result.metadata["facet_sums"] = {"sums": facet_sums, "total": facets["total"], "match": facets_sum_ok}
        self.result.listing_complete = membership_complete
        if not membership_complete:
            self.result.errors.append("listing_membership_incomplete")

        detail_concurrency = max(1, int(self.options.detail_concurrency or 2))
        try:
            async with HTTP(self.detail_output, delay=0.08, concurrency=detail_concurrency,
                            retries=2, timeout=45, transport=self.detail_transport) as detail_http:
                self.detail_http = detail_http
                await self.hydrate(self.result.jobs, self.detail)
                self.result.metadata["sitemap_only_audit"] = await self._audit_sitemap_only(sitemap_cross)
                await self._post_hydration_recheck()
        finally:
            self.detail_http = None
        self.result.timings["source_collection_seconds"] = max(0.0, time.perf_counter() - started)
        return self.result

    async def _post_hydration_recheck(self) -> None:
        """Catch postings published while details were being hydrated."""
        if self.detail_blocked:
            self.result.warnings.append("post_hydration_recheck_skipped_after_detail_stop")
            return
        started = time.perf_counter()
        parsed = parse_search_response(await self._get_json(search_request(0)), 0)
        known = set(self.rows)
        new_rows = [row for row in parsed["rows"] if str(row["id"]) not in known]
        added = 0
        if new_rows:
            for row in new_rows:
                self.rows[str(row["id"])] = row
                self.row_pages[str(row["id"])] = 0
                self._add_job(row)
            new_ids = {str(row["id"]) for row in new_rows}
            new_jobs = [job for job in self.result.jobs if str(job.job_id) in new_ids]
            await self.hydrate(new_jobs, self.detail)
            added = len(new_rows)
        self.result.metadata["post_hydration_recheck"] = {
            "total_jobs": parsed["total"],
            "first_page_rows": len(parsed["rows"]),
            "new_rows_added": added,
            "unique_ids": len(self.rows),
            "complete": len(self.rows) == parsed["total"],
            "issues": parsed["issues"],
        }
        if len(self.rows) != parsed["total"]:
            self.result.listing_complete = False
            self.result.errors.append("listing_membership_changed_during_details")
        self.result.timings["post_hydration_recheck_seconds"] = max(0.0, time.perf_counter() - started)
