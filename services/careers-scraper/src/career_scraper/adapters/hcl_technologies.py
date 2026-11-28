"""HCLTech's own SuccessFactors career site at careers.hcltech.com.

The official HCLTech entry (https://www.hcltech.com/careers) answers with a
301 to https://careers.hcltech.com/, a SuccessFactors Career Site Builder
(CSB) site.  Its listing is the site's own unfiltered search widget.  The
widget posts to /services/recruiting/v1/jobs; the same request is used here
with an empty ``facetFilters`` object, so membership is every public posting
on HCLTech's global career site (no country, seniority, keyword or category
filter).

The widget's default ordering is not stable across page requests (the same
page number can return overlapping rows).  The site's own endpoint accepts
``sortBy=date``, which is deterministic (newest first) and returns the same
15,4xx unfiltered membership.  Pages are fetched in reverse order and then a
final top-up pass is made so postings added while the crawl is running are
still captured.  The company sitemap and the country facet counts are used
only as completeness cross-checks; they never replace the listing.

Full role text comes from the exact company detail page for each listed
posting: ``span[itemprop="description"]`` inside ``.jobColumnOne``.  The
``Why HCLTech?`` column, cookie text, headers, footers, map text and apply
form are never treated as role descriptions.
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


OFFICIAL_ENTRY = "https://www.hcltech.com/careers"
CAREERS_ORIGIN = "https://careers.hcltech.com"
CAREERS_HOME = CAREERS_ORIGIN + "/"
LISTING_PAGE = CAREERS_ORIGIN + "/search/"
JOBS_API = CAREERS_ORIGIN + "/services/recruiting/v1/jobs"
DETAIL_BASE = CAREERS_ORIGIN + "/job/"
SITEMAP_URL = CAREERS_ORIGIN + "/sitemap.xml"
LOCALE = "en_US"
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
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": LISTING_PAGE,
}
DATE_FORMAT = "%m/%d/%y"
SALARY_LABEL = re.compile(
    r"^(?P<kind>Maximum|Minimum)\s+Salary(?:\s*\((?P<region>[^)]+)\))?\s*:\s*(?P<value>.+)$",
    re.I,
)
JOB_ID_FROM_APPLY = re.compile(r"/talentcommunity/apply/(\d+)/")
JOB_ID_FROM_PATH = re.compile(r"^/job/[^/]+/(\d+)-[a-z]{2}_[A-Z]{2}/?$")
EMPTY_VALUES = {"", "null", "none", "n/a", "na", "-"}
# The publisher repeats the same short phrase in all four joblayouttoken
# sections for template-only postings.  Those stubs are not role descriptions.
MIN_DESCRIPTION_CHARS = 200
PLACEHOLDER_MAX_CHARS = 600
MAX_STUB_LINES = 2
MAX_STUB_LINE_CHARS = 120
# Round-3: internal-note-only means less than this many unique characters of
# role text survive once internal-note sentences are removed (186406 keeps
# 172 genuine Teamcenter characters and stays verified; the 19 true stubs
# keep at most 129).
INTERNAL_NOTE_UNIQUE_MAX_CHARS = 150
# Round-3: a >600-char body whose unique content lines total less than this
# is a single repeated phrase, not a full role body.  Genuine duplicates
# whose unique block is 250+ chars stay verified.
REPEATED_STUB_UNIQUE_MAX_CHARS = 200
REPEATED_STUB_MIN_CHARS = 600
SECTION_LABELS = {
    "job summary", "key responsibilities", "job responsibilities",
    "skill requirements", "skill requirement", "other requirements",
    "other requirement", "job description", "job role", "jd",
}
# Internal HR / rebadging / PRF notes that HCLTech publishes in the role body
# field even though they are not job descriptions (verified against the live
# rendered pages; the text below is copied, never invented). Round-3 repair
# adds the offer-release / vendor phrases seen in the remaining 20
# internal-note-only bodies (n+1 PRF, rebadging, offer release).
INTERNAL_NOTE_PATTERN = re.compile(
    r"\b(?:re-?badging|prf|t2id|n\+1|hackathon)\b"
    r"|this sr is"
    r"|release offer"
    r"|offer release"
    r"|do not attach any profile"
    r"|demand id is specifically"
    r"|utilising sse resource"
    r"|job description is shared already"
    r"|this is to (?:hire|rehire|select and onboard)"
    r"|refrain from attaching"
    r"|please approve"
    r"|demand so that"
    r"|incumbant vendor"
    r"|incumbent vendor",
    re.I,
)


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(value or "")).strip()


def normalize_title(value: str | None) -> str:
    return normalize_text(value)


def parse_date(value) -> str | None:
    """Parse the company's M/D/YY date without inventing a date."""
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


def parse_official_entry(html: str, response_url: str = CAREERS_HOME) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "careers.hcltech.com" or parsed.path not in ("", "/"):
        raise ValueError("HCLTech official careers entry no longer redirects to careers.hcltech.com")
    soup = BeautifulSoup(html or "", "lxml")
    if not soup.select_one('a[href*="/go/"]'):
        raise ValueError("HCLTech career site no longer exposes its country / explore links")
    explore = [
        {"text": normalize_text(a.get_text(" ", strip=True)), "href": urljoin(response_url, a.get("href", ""))}
        for a in soup.select('a[href*="/go/"]')
        if a.get_text(" ", strip=True)
    ]
    return {
        "request_url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "title": normalize_text(soup.title.get_text(" ", strip=True) if soup.title else ""),
        "explore_links": explore,
        "language_options": [
            {"text": normalize_text(a.get_text(" ", strip=True)), "href": urljoin(response_url, a.get("href", ""))}
            for a in soup.select('a[href*="locale="]')
        ],
        "has_talent_community": bool(soup.select_one('a[href*="talentcommunity"]')),
    }


def parse_search_page(html: str, response_url: str = LISTING_PAGE) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "careers.hcltech.com" or not parsed.path.startswith("/search"):
        raise ValueError("HCLTech search page URL changed")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "HCLTech Jobs" not in title:
        raise ValueError("HCLTech search page title changed")
    facet_block = re.search(r"var facetFields = \[\];(.*?)resultStyles\.searchFacet\.facetFields", html or "", re.S)
    facet_fields = re.findall(r'fieldId\s*:\s*"([^"]+)"\s*,\s*label\s*:\s*"([^"]+)"', facet_block.group(1) if facet_block else "")
    api_scripts = [s.get("src") for s in soup.select("script[src]") if "search" in (s.get("src") or "").lower()]
    return {
        "url": response_url,
        "title": title,
        "has_keyword_input": bool(soup.select_one('input[name="q"]')),
        "facet_fields": [{"field_id": field, "label": label} for field, label in facet_fields],
        "search_scripts": api_scripts,
        "mentions_search_widget": any("searchManager" in (src or "") for src in api_scripts),
        "mentions_sitemap": bool(soup.select_one('a[href*="sitemap"]')),
    }


def search_request(page_number: int, *, facet_filters: dict | None = None,
                   keywords: str = "", location: str = "") -> dict:
    """Exact request shape used by HCLTech's own search widget."""
    return {
        "locale": LOCALE,
        "pageNumber": int(page_number),
        "sortBy": SORT_BY,
        "keywords": keywords,
        "location": location,
        "facetFilters": facet_filters or {},
        "brand": "",
        "skills": [],
        "categoryId": 0,
        "alertId": "",
        "rcmCandidateId": "",
    }


def facet_request(field: str = "custCountryRegion") -> dict:
    return {
        "facetingOnly": True,
        "categoryId": 0,
        "brand": "",
        "locale": LOCALE,
        "keywords": "",
        "location": "",
        "facetFields": [field],
        "facetFilters": {},
        "alertId": "",
        "skills": [],
        "rcmCandidateId": "",
    }


def parse_search_response(value: dict, requested_page: int) -> dict:
    if not isinstance(value, dict):
        raise ValueError("HCLTech jobs API response is not an object")
    total = value.get("totalJobs")
    rows = value.get("jobSearchResult")
    if not isinstance(total, int) or total < 0 or not isinstance(rows, list):
        raise ValueError("HCLTech jobs API response is missing totalJobs/jobSearchResult")
    if len(rows) > PAGE_SIZE:
        raise ValueError("HCLTech jobs API returned more rows than its page size")
    parsed_rows, issues = [], []
    for item in rows:
        response = item.get("response") if isinstance(item, dict) else None
        if not isinstance(response, dict):
            issues.append("row_not_object")
            continue
        identity = str(response.get("id") or "")
        if not re.fullmatch(r"\d+", identity):
            issues.append("row_missing_numeric_id")
            continue
        if not normalize_title(response.get("unifiedStandardTitle")):
            issues.append(f"row_missing_title:{identity}")
            continue
        if not response.get("urlTitle") and not response.get("unifiedUrlTitle"):
            issues.append(f"row_missing_url_title:{identity}")
            continue
        countries = response.get("custCountryRegion")
        if not isinstance(countries, list) or not countries:
            issues.append(f"row_missing_country:{identity}")
            countries = []
        parsed_rows.append(dict(response))
    if requested_page == 0 and total and not parsed_rows:
        issues.append("first_page_has_no_rows")
    expected_page_rows = min(PAGE_SIZE, max(0, total - requested_page * PAGE_SIZE))
    if len(rows) != expected_page_rows:
        issues.append("page_row_count_mismatch")
    return {
        "rows": parsed_rows,
        "total": total,
        "page": requested_page,
        "page_size": PAGE_SIZE,
        "issues": issues,
    }


def parse_facet_response(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("HCLTech facet response is not an object")
    total = value.get("totalJobs")
    fields = value.get("facetFields")
    if not isinstance(total, int) or not isinstance(fields, list) or not fields:
        raise ValueError("HCLTech facet response is missing totalJobs/facetFields")
    field = fields[0]
    values = field.get("values") if isinstance(field, dict) else None
    if not isinstance(values, list):
        raise ValueError("HCLTech facet response is missing values")
    counts = {}
    for row in values:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("count"), int):
            raise ValueError("HCLTech facet response contains an invalid country row")
        counts[row["name"]] = row["count"]
    if sum(counts.values()) != total:
        raise ValueError("HCLTech country facet counts do not sum to totalJobs")
    return {"field": field.get("name"), "total": total, "counts": counts, "value_count": field.get("valueCount")}


def job_detail_url(row: dict) -> str:
    title = html_module.unescape(str(row.get("urlTitle") or row.get("unifiedUrlTitle") or "job"))
    title = quote(title, safe="%-._~!$&'()*+,;=:@")
    return f"{DETAIL_BASE}{title}/{row['id']}-{LOCALE}"


def parse_salary(tokens: list[str], listing_row: dict | None = None) -> dict | None:
    labels = []
    values = {}
    for token in tokens:
        match = SALARY_LABEL.match(token)
        if not match:
            continue
        labels.append(token)
        kind = match.group("kind").lower()
        raw = match.group("value").strip()
        numeric = re.sub(r"[^0-9.]", "", raw)
        try:
            number = float(numeric) if numeric and numeric.count(".") <= 1 else None
        except ValueError:
            number = None
        values[kind] = {"raw": raw, "number": number, "region": match.group("region")}
    if not labels:
        return None
    currency = None
    if isinstance(listing_row, dict):
        currencies = listing_row.get("currency")
        if isinstance(currencies, list) and currencies:
            currency = currencies[0]
    return {
        "minimum": values.get("minimum", {}).get("number"),
        "maximum": values.get("maximum", {}).get("number"),
        "currency": currency,
        "source_text": labels,
        "source_field": "detail.joblayouttoken",
        "region": next((v.get("region") for v in values.values() if v.get("region")), None),
        "raw_values": {key: value["raw"] for key, value in values.items()},
    }


def description_sections(description_html: str) -> dict:
    """Return employer-authored h2 sections without synthesising text."""
    soup = BeautifulSoup(f'<div data-hcl-body="1">{description_html or ""}</div>', "lxml")
    root = soup.select_one("[data-hcl-body]")
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


def parse_detail_page(html: str, response_url: str, listing_row: dict) -> dict:
    if not isinstance(html, str) or len(html) < 500:
        raise ValueError("HCLTech detail response is empty")
    soup = BeautifulSoup(html, "lxml")
    if "/errorpage/" in response_url or soup.select_one("#errorPage, .error-page"):
        raise ValueError("HCLTech detail page returned its error page")
    title_node = soup.select_one('.jobColumnOne span[itemprop="title"]') or soup.select_one('span[itemprop="title"]')
    title = normalize_title(title_node.get_text(" ", strip=True) if title_node else "")
    if not title:
        meta = soup.select_one('meta[property="og:title"]')
        title = normalize_title(meta.get("content") if meta else "")
    if not title:
        raise ValueError("HCLTech detail page has no title")

    desc_node = soup.select_one('.jobColumnOne span[itemprop="description"]') or soup.select_one('span[itemprop="description"]')
    description_html = ""
    if desc_node is not None:
        description_html = "".join(str(child) for child in desc_node.contents).strip()
    description_text = text_html(description_html)

    apply_node = soup.select_one('.jobColumnOne a.unify-apply-now[href]') or soup.select_one('a[href*="/talentcommunity/apply/"]')
    apply_url = urljoin(response_url, apply_node.get("href")) if apply_node is not None else None
    apply_match = JOB_ID_FROM_APPLY.search(apply_url or "")
    apply_id = apply_match.group(1) if apply_match else None
    path_match = JOB_ID_FROM_PATH.match(urlsplit(response_url).path)
    path_id = path_match.group(1) if path_match else None
    if apply_id and str(listing_row.get("id")) != apply_id:
        raise ValueError(f"HCLTech detail apply id {apply_id} does not match listed id {listing_row.get('id')}")
    if path_id and str(listing_row.get("id")) != path_id:
        raise ValueError(f"HCLTech detail path id {path_id} does not match listed id {listing_row.get('id')}")

    tokens = [
        normalize_title(token.get_text(" ", strip=True))
        for token in soup.select(".jobColumnOne .joblayouttoken")
    ]
    # Keep only the short field tokens; the full role body is stored once in
    # description_html and must not be duplicated inside every raw token list.
    tokens = [token for token in tokens if token and len(token) <= 400]
    country = None
    if isinstance(listing_row.get("custCountryRegion"), list) and listing_row["custCountryRegion"]:
        country = listing_row["custCountryRegion"][0]
    detail_country = None
    for token in tokens:
        if country and normalize_title(token).lower() == normalize_title(country).lower():
            detail_country = token
            break
    if detail_country is None and len(tokens) >= 2:
        detail_country = tokens[1]

    location = None
    if desc_node is not None:
        desc_token = desc_node.find_parent("div", class_="joblayouttoken")
        if desc_token is not None:
            previous = desc_token.find_previous_sibling("div", class_="joblayouttoken")
            if previous is not None:
                location = normalize_title(previous.get_text(" ", strip=True))
    if not location:
        for token in tokens:
            if "," in token and token not in (title, detail_country):
                location = token
                break

    sections = description_sections(description_html)
    responsibilities = _section_text(sections, ("responsibilit", "what you will do", "duties", "key responsibilities"))
    qualifications = _section_text(sections, ("skill requirement", "qualification", "requirements", "other requirements"))
    experience = _section_text(sections, ("experience",))
    education = _section_text(sections, ("education",))
    employment_type = department = workplace_type = None
    for token in tokens:
        match = re.match(r"^(Job Category|Department|Function|Employment Type|Job Type|Workplace|Work Mode|Travel)\s*:\s*(.+)$", token, re.I)
        if not match:
            continue
        label, value = match.group(1).lower(), match.group(2).strip()
        if label in ("job category", "department", "function"):
            department = value
        elif label in ("employment type", "job type"):
            employment_type = value
        elif label in ("workplace", "work mode"):
            workplace_type = value

    salary = parse_salary(tokens, listing_row)
    footer = soup.select_one("footer, .footer, .copyright")
    footer_text = normalize_text(footer.get_text(" ", strip=True)) if footer is not None else ""
    return {
        "title": title,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(normalize_text(description_text)),
        "description_sha256": sha256(description_text.encode()).hexdigest() if description_text else None,
        "apply_url": apply_url,
        "apply_id": apply_id,
        "detail_id": path_id,
        "country": country,
        "detail_country": detail_country,
        "location": location,
        "locations": [{
            "source_text": location,
            "detail_country": detail_country,
            "country": country,
            "listing_city": listing_row.get("custprimecity"),
        }] if (location or country) else [],
        "salary": salary,
        "responsibilities": responsibilities,
        "qualifications": qualifications,
        "experience": experience,
        "education": education,
        "employment_type": employment_type,
        "department": department,
        "workplace_type": workplace_type,
        "sections": sections,
        "tokens": tokens,
        "footer": footer_text,
        "listing_title": normalize_title(listing_row.get("unifiedStandardTitle")),
    }


def _content_lines(value: str) -> list[str]:
    """Return employer content lines without the site's section labels."""
    lines = []
    for raw_line in (value or "").splitlines():
        line = normalize_text(raw_line)
        if not line:
            continue
        if line.rstrip(":-").strip().lower() in SECTION_LABELS:
            continue
        lines.append(line)
    return lines


def _sentence_lines(value: str) -> list[str]:
    """Split the employer content into sentences, one section line at a time."""
    sentences = []
    for line in _content_lines(value):
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            sentence = normalize_text(sentence)
            if sentence:
                sentences.append(sentence)
    return sentences


def _internal_note_share(value: str) -> float:
    sentences = _sentence_lines(value)
    total = sum(len(sentence) for sentence in sentences)
    if not total:
        return 0.0
    notes = sum(len(sentence) for sentence in sentences if INTERNAL_NOTE_PATTERN.search(sentence))
    return notes / total


def _without_internal_notes(value: str) -> str:
    """Drop the internal HR note lines while keeping real role text."""
    kept = [line for line in _content_lines(value) if not INTERNAL_NOTE_PATTERN.search(line)]
    return normalize_text(" ".join(kept))


def _is_template_stub(value: str) -> bool:
    """True when the whole body is one or two short lines repeated per section."""
    unique: list[str] = []
    for line in _content_lines(value):
        if line.lower() not in {item.lower() for item in unique}:
            unique.append(line)
    if not unique or len(unique) > MAX_STUB_LINES:
        return False
    return sum(len(line) for line in unique) < MAX_STUB_LINE_CHARS


def _unique_content_line_chars(value: str) -> int:
    """Total chars of deduplicated employer content lines (labels stripped).

    A genuine >600-char body has hundreds of unique characters even when
    the publisher duplicates the same block into every section; a
    single-phrase repeat has <200.
    """
    seen: dict[str, str] = {}
    for line in _content_lines(value):
        seen.setdefault(line.lower(), line)
    return sum(len(line) for line in seen.values())


def _unique_non_note_chars(value: str) -> int:
    """Unique role characters surviving internal-note removal.

    Sentences are taken per content line and deduplicated, so fragmented
    rebadging notes split across lines ("Hello WPC Team," / "rebadging
    process" / ". Request you...") collapse to their fragments, while a
    genuine giant paragraph keeps its long unique tail even when one
    incidental "PRF No." / "hackathon" phrase appears inside it.
    """
    seen: dict[str, str] = {}
    for sentence in _sentence_lines(value):
        if sentence.lower() in EMPTY_VALUES or set(sentence) <= set(". "):
            continue
        seen.setdefault(sentence.lower(), sentence)
    return sum(len(s) for s in seen.values() if not INTERNAL_NOTE_PATTERN.search(s))


def description_placeholder_reason(parsed: dict) -> str | None:
    """Classify what the publisher actually returned for the role body.

    ``publisher_empty_description`` is a genuinely empty field;
    ``publisher_placeholder_description`` is a header-only, title-only,
    internal-note-only or single-phrase-repeated body that the employer
    exposes as the whole body.  Both are unavailable descriptions and
    must never be counted as verified.
    """
    raw = parsed.get("description_text") or ""
    text = normalize_text(raw)
    if not text or text.lower() in EMPTY_VALUES:
        return "publisher_empty_description"
    if len(text) < MIN_DESCRIPTION_CHARS:
        return "publisher_placeholder_description"
    # Round-3 internal-note-only (any length): the whole body is an HR note
    # (n+1 PRF / rebadging / offer release) with <150 unique role chars
    # left.  Kept alongside the legacy share check for 200-599 bodies.
    if INTERNAL_NOTE_PATTERN.search(text) and _unique_non_note_chars(raw) < INTERNAL_NOTE_UNIQUE_MAX_CHARS:
        return "publisher_placeholder_description"
    if len(text) < PLACEHOLDER_MAX_CHARS and INTERNAL_NOTE_PATTERN.search(text):
        if _internal_note_share(raw) >= 0.5 and len(_without_internal_notes(raw)) < MIN_DESCRIPTION_CHARS:
            return "publisher_placeholder_description"
    # Round-3 repeated-stub >600: one short phrase repeated into every
    # section, i.e. <200 unique chars inside a long body.
    if len(text) > REPEATED_STUB_MIN_CHARS and _unique_content_line_chars(raw) < REPEATED_STUB_UNIQUE_MAX_CHARS:
        return "publisher_placeholder_description"
    if MIN_DESCRIPTION_CHARS <= len(text) < PLACEHOLDER_MAX_CHARS and _is_template_stub(raw):
        return "publisher_placeholder_description"
    return None


def description_is_empty(parsed: dict) -> bool:
    return description_placeholder_reason(parsed) is not None


class HCLTechnologies(Adapter):
    """Follow only HCLTech's published global search and its exact job pages."""

    VERIFICATION_REVISION = "hcltech.successfactors-csb.global-search-and-detail.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.detail_http = None
        self.detail_transport = None
        self.rows: dict[str, dict] = {}
        self.row_pages: dict[str, int] = {}
        self.listing_seconds = 0.0
        self.duplicate_rows = 0
        self.detail_blocked = False

    async def _get_json(self, body: dict) -> dict:
        return await self.http.json(JOBS_API, method="POST", json=body, headers=JSON_HEADERS)

    async def _enumerate_listing(self) -> None:
        started = time.perf_counter()
        if not self.page_guard():
            raise ValueError("HCLTech listing page budget exhausted")
        first = await self._get_json(search_request(0))
        parsed = parse_search_response(first, 0)
        total_initial = parsed["total"]
        self.result.expected_total = total_initial
        self.result.metadata["listing_total_initial"] = total_initial
        self._ingest(parsed, 0)
        page_count = max(1, math.ceil(total_initial / PAGE_SIZE))
        # Reverse order: the date sort is newest-first, so fetching the oldest
        # pages first means postings added at the top do not shift pages we have
        # already read.
        for page in range(page_count - 1, 0, -1):
            if not self.page_guard():
                break
            data = await self._get_json(search_request(page))
            parsed_page = parse_search_response(data, page)
            self.result.expected_total = parsed_page["total"]
            self._ingest(parsed_page, page)
        # A final top-up pass over the newest pages catches postings added while
        # the long crawl was running.
        for page in range(0, min(20, page_count)):
            if not self.page_guard():
                break
            data = await self._get_json(search_request(page))
            parsed_page = parse_search_response(data, page)
            self.result.expected_total = parsed_page["total"]
            self._ingest(parsed_page, page)
        self.listing_seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["listing_seconds"] = self.listing_seconds

    def _ingest(self, parsed: dict, page: int) -> None:
        for row in parsed["rows"]:
            identity = str(row["id"])
            if identity in self.rows:
                self.duplicate_rows += 1
                continue
            self.rows[identity] = row
            self.row_pages[identity] = page

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
        title = normalize_title(row.get("unifiedStandardTitle")) or normalize_title(row.get("urlTitle"))
        url = job_detail_url(row)
        start = row.get("unifiedStandardStart")
        end = row.get("unifiedStandardEnd")
        countries = row.get("custCountryRegion") if isinstance(row.get("custCountryRegion"), list) else []
        locations = [{
            "country": countries[0] if countries else None,
            "listing_city": row.get("custprimecity"),
            "source_text": ", ".join(
                value for value in (row.get("custprimecity"), countries[0] if countries else None) if value
            ) or None,
        }]
        self.add(self.job(
            {"listing": row, "listing_page": self.row_pages.get(identity)},
            title=title,
            url=url,
            job_id=identity,
            locations=locations,
            date_posted=parse_date(start),
            date_posted_raw=start,
            valid_through=parse_date(end),
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
        parsed = parse_detail_page(response.text, str(response.url), listing_row)
        job.title = parsed["title"] or job.title
        job.apply_url = parsed["apply_url"]
        job.description_html = parsed["description_html"]
        job.description_method = "hcltech_detail_itemprop_description"
        job.description_source_url = job.url
        job.locations = parsed["locations"] or job.locations
        job.salary = parsed["salary"]
        job.qualifications = parsed["qualifications"]
        job.responsibilities = parsed["responsibilities"]
        job.experience = parsed["experience"]
        job.education = parsed["education"]
        job.employment_type = parsed["employment_type"]
        job.department = parsed["department"]
        job.workplace_type = parsed["workplace_type"]
        job.raw["detail"] = {
            "response_sha256": sha256(response.content).hexdigest(),
            "final_url": str(response.url),
            "description_normalized_chars": parsed["description_normalized_chars"],
            "description_sha256": parsed["description_sha256"],
            "apply_id": parsed["apply_id"],
            "detail_id": parsed["detail_id"],
            "detail_country": parsed["detail_country"],
            "location": parsed["location"],
            "tokens": parsed["tokens"],
            "sections": parsed["sections"],
            "footer": parsed["footer"],
            "listing_title": parsed["listing_title"],
            "response_bytes": len(response.content),
        }
        placeholder_reason = description_placeholder_reason(parsed)
        if placeholder_reason:
            job.description_unavailable = True
            job.warnings.append(placeholder_reason)
        return job

    async def run(self):
        started = time.perf_counter()
        # Official entry redirect and the published search page are part of the
        # company journey; both are archived by the shared HTTP client.
        entry = await self.http.get(OFFICIAL_ENTRY, headers=HTML_HEADERS)
        official = parse_official_entry(entry.text, str(entry.url))
        self.result.metadata["official_entry"] = {
            "request_url": OFFICIAL_ENTRY,
            "final_url": str(entry.url),
            "status": entry.status_code,
            "redirects": [str(item.url) for item in entry.history],
            **official,
        }
        search = await self.http.get(LISTING_PAGE, headers=HTML_HEADERS)
        search_meta = parse_search_page(search.text, str(search.url))
        self.result.metadata["search_page"] = {**search_meta, "status": search.status_code}

        facets = parse_facet_response(await self._get_json(facet_request()))
        self.result.metadata["country_facets"] = facets
        self.result.metadata["scope"] = {
            "official_entry": OFFICIAL_ENTRY,
            "final_url": str(entry.url),
            "listing_url": LISTING_PAGE,
            "jobs_api": JOBS_API,
            "filters": "facetFilters={} (no country, keyword, category or seniority filter)",
            "sort": SORT_BY,
            "page_size": PAGE_SIZE,
        }

        await self._enumerate_listing()
        await self._final_reconcile()

        rows_in_date_order = sorted(
            self.rows.values(),
            key=lambda row: (parse_date(row.get("unifiedStandardStart")) or "", str(row.get("id"))),
            reverse=True,
        )
        for row in rows_in_date_order:
            self._add_job(row)
        self.result.metadata["listing_membership"] = {
            "unique_ids": len(self.rows),
            "initial_total": self.result.metadata.get("listing_total_initial"),
            "final_total": self.result.metadata.get("listing_total_final"),
            "duplicate_rows": self.duplicate_rows,
            "complete": len(self.rows) == self.result.metadata.get("listing_total_final"),
        }

        # Country counts are an independent cross-check of the unfiltered
        # membership; they never replace the listing.
        collected_counts: dict[str, int] = {}
        for row in self.rows.values():
            countries = row.get("custCountryRegion") if isinstance(row.get("custCountryRegion"), list) else []
            country = countries[0] if countries else "UNKNOWN"
            collected_counts[country] = collected_counts.get(country, 0) + 1
        facet_counts = facets["counts"]
        mismatches = {
            country: {"facet": facet_counts.get(country, 0), "collected": collected_counts.get(country, 0)}
            for country in sorted(set(facet_counts) | set(collected_counts))
            if facet_counts.get(country, 0) != collected_counts.get(country, 0)
        }
        self.result.metadata["country_reconciliation"] = {
            "facet_total": facets["total"],
            "collected_total": sum(collected_counts.values()),
            "mismatches": mismatches,
            "counts_match": not mismatches,
        }
        membership_complete = (
            len(self.rows) == self.result.metadata.get("listing_total_final")
            and not mismatches
        )
        self.result.listing_complete = membership_complete
        if not membership_complete:
            self.result.errors.append("listing_membership_incomplete")

        detail_concurrency = max(1, int(self.options.detail_concurrency or 2))
        try:
            async with HTTP(None, delay=0.05, concurrency=detail_concurrency,
                            retries=1, timeout=45, transport=self.detail_transport) as detail_http:
                self.detail_http = detail_http
                await self.hydrate(self.result.jobs, self.detail)
        finally:
            self.detail_http = None
        self.result.timings["source_collection_seconds"] = max(0.0, time.perf_counter() - started)
        return self.result
