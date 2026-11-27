"""Wipro's own SuccessFactors Career Site Builder site at careers.wipro.com.

The official Wipro entry (https://www.wipro.com/careers/) redirects to the
company-owned https://careers.wipro.com/ site.  Its listing is the site's own
unfiltered search widget at /search/, which posts the exact request body below
to /services/recruiting/v1/jobs.  The widget has no configured facets and no
country/seniority filter; membership is every public posting it advertises
(``totalJobs`` and 705 pages of 10 at inspection time).

The search backend alternates between two result states for the same page
(observed by repeating an identical request), so a single pass is not a
complete membership snapshot.  The worker therefore makes repeated passes and
retains the union of the company's own result rows; the advertised total and
the final listing page are re-checked after detail collection.

Full role text comes from the exact company detail page for each listed
posting.  The page publishes three ``span[itemprop="description"]`` fields:
a shared Wipro corporate introduction, the role body, and a short
skills/experience/compensation block followed by a shared "Reinvent your
world" footer.  The two shared corporate blocks are retained as provenance but
are not role descriptions; the role body and the job-specific
skills/experience/compensation text are kept verbatim.
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


OFFICIAL_ENTRY = "https://www.wipro.com/careers/"
CAREERS_ORIGIN = "https://careers.wipro.com"
CAREERS_HOME = CAREERS_ORIGIN + "/"
LISTING_PAGE = CAREERS_ORIGIN + "/search/"
JOBS_API = CAREERS_ORIGIN + "/services/recruiting/v1/jobs"
DETAIL_BASE = CAREERS_ORIGIN + "/job/"
LOCALE = "en_US"
PAGE_SIZE = 10
# The site's own default ordering.  ``sortBy=date`` ties are also reordered
# between identical requests, so no single sort is deterministic.
SORT_BY = ""
MAX_LISTING_PASSES = 3
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
NOT_AVAILABLE_TEXT = "You can't view this job because it's not available at this time."
COMPANY_INTRO_PREFIX = (
    "Wipro Limited (NYSE: WIT, BSE: 507685, NSE: WIPRO) is a leading technology "
    "services and consulting company focused on building innovative solutions"
)
COMPANY_INTRO_SUFFIX = "For additional information, visit us at www.wipro.com."
COMPANY_FOOTER_MARKER = "Reinvent your world."
JOB_ID_FROM_PATH = re.compile(r"^/job/.+/(\d+)-[a-z]{2}_[A-Z]{2}/?$")
JOB_ID_FROM_SCRIPT = re.compile(r"jobID\s*:\s*'?\s*(\d+)")
MIN_DESCRIPTION_CHARS = 100
SALARY_RANGE = re.compile(
    r"(?:The\s+)?expected\s+(?:annual\s+)?(?:compensation|pay)\s+for\s+this\s+role\s+"
    r"ranges?\s+from\s+(?P<minimum>[^.;\n]+?)\s+to\s+(?P<maximum>[^.;\n]+)",
    re.I,
)
SALARY_TOKEN = re.compile(
    r"^(?P<kind>Maximum|Minimum)\s+Salary(?:\s*\((?P<region>[^)]+)\))?\s*:\s*(?P<value>.+)$",
    re.I,
)
EMPTY_LABELS = {"", "n/a", "na", "none", "null", "-"}
SECTION_HEADINGS = {
    "job description", "role", "role purpose", "do", "deliver", "area of responsibility",
    "responsibilities", "key responsibilities", "qualifications", "mandatory skills",
    "experience", "education", "skills", "requirements", "about wipro",
}


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(value or "")).strip()


def normalize_title(value: str | None) -> str:
    return normalize_text(value)


def parse_date(value) -> str | None:
    """Parse the company's published M/D/YY date; never guess a missing date."""
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


def strip_br(value: str | None) -> str:
    return normalize_text(re.sub(r"<br\s*/?>", " ", value or "", flags=re.I))


def parse_official_entry(html: str, response_url: str = CAREERS_HOME) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "careers.wipro.com" or parsed.path not in ("", "/"):
        raise ValueError("Wipro official careers entry no longer redirects to careers.wipro.com")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalize_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != "Wipro Careers | Reinvent Your World":
        raise ValueError("Wipro careers home title changed")
    search_links = sorted({
        urljoin(response_url, a.get("href", ""))
        for a in soup.select('a[href*="/search"]')
        if a.get_text(" ", strip=True)
    })
    if not search_links:
        raise ValueError("Wipro careers home no longer exposes its Search Jobs link")
    return {
        "request_url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "title": title,
        "search_links": search_links,
        "language_options": sorted({
            urljoin(response_url, a.get("href", ""))
            for a in soup.select('a[href*="locale="]')
        }),
        "has_talent_network": bool(soup.select_one('a[href*="talent"]')),
    }


def parse_search_page(html: str, response_url: str = LISTING_PAGE) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "careers.wipro.com" or not parsed.path.startswith("/search"):
        raise ValueError("Wipro search page URL changed")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalize_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != "Wipro Limited Jobs":
        raise ValueError("Wipro search page title changed")
    keyword_input = soup.select_one('input[name="q"]')
    if keyword_input is None:
        raise ValueError("Wipro search page no longer exposes its keyword box")
    return {
        "url": response_url,
        "title": title,
        "has_keyword_input": True,
        "keyword_placeholder": keyword_input.get("placeholder"),
        "has_sort_control": bool(re.search(r"sortBy", html or "")),
        "has_pagination_control": bool(re.search(r"pagination", html or "", re.I)),
        "company_id": "wiprolimitP2",
    }


def search_request(page_number: int, *, sort_by: str = SORT_BY,
                   facet_filters: dict | None = None,
                   keywords: str = "", location: str = "") -> dict:
    """The exact request shape used by Wipro's own search widget."""
    return {
        "locale": LOCALE,
        "pageNumber": int(page_number),
        "sortBy": sort_by,
        "keywords": keywords,
        "location": location,
        "facetFilters": facet_filters or {},
        "brand": "",
        "skills": [],
        "categoryId": 0,
        "alertId": "",
        "rcmCandidateId": "",
    }


def facet_request(field: str = "custRMKMappingPicklist") -> dict:
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
        raise ValueError("Wipro jobs API response is not an object")
    if value.get("error"):
        raise ValueError(f"Wipro jobs API returned an error: {value['error']}")
    total = value.get("totalJobs")
    rows = value.get("jobSearchResult")
    if not isinstance(total, int) or total < 0 or not isinstance(rows, list):
        raise ValueError("Wipro jobs API response is missing totalJobs/jobSearchResult")
    if len(rows) > PAGE_SIZE:
        raise ValueError("Wipro jobs API returned more rows than its page size")
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
        if not normalize_title(response.get("unifiedStandardTitle")) and not normalize_title(response.get("urlTitle")):
            issues.append(f"row_missing_title:{identity}")
            continue
        if not response.get("urlTitle") and not response.get("unifiedUrlTitle"):
            issues.append(f"row_missing_url_title:{identity}")
            continue
        parsed_rows.append(dict(response))
    expected_page_rows = min(PAGE_SIZE, max(0, total - requested_page * PAGE_SIZE))
    if len(rows) != expected_page_rows:
        issues.append("page_row_count_mismatch")
    return {
        "rows": parsed_rows,
        "total": total,
        "page": requested_page,
        "page_size": PAGE_SIZE,
        "issues": list(dict.fromkeys(issues)),
    }


def parse_facet_response(value: dict, expected_field: str = "custRMKMappingPicklist") -> dict:
    if not isinstance(value, dict):
        raise ValueError("Wipro facet response is not an object")
    if value.get("error"):
        raise ValueError(f"Wipro facet response returned an error: {value['error']}")
    total = value.get("totalJobs")
    fields = value.get("facetFields")
    if not isinstance(total, int) or not isinstance(fields, list) or not fields:
        raise ValueError("Wipro facet response is missing totalJobs/facetFields")
    field = fields[0]
    if not isinstance(field, dict) or field.get("name") != expected_field:
        raise ValueError("Wipro facet response field changed")
    values = field.get("values")
    if not isinstance(values, list):
        raise ValueError("Wipro facet response is missing values")
    counts: dict[str, int] = {}
    for row in values:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("count"), int):
            raise ValueError("Wipro facet response contains an invalid row")
        counts[row["name"]] = row["count"]
    return {
        "field": field.get("name"),
        "total": total,
        "counts": counts,
        "facet_sum": sum(counts.values()),
        "value_count": field.get("valueCount"),
    }


def job_detail_url(row: dict) -> str:
    title = html_module.unescape(str(row.get("urlTitle") or row.get("unifiedUrlTitle") or "job"))
    title = quote(title, safe="%-._~!$&'()*+,;=:@")
    return f"{DETAIL_BASE}{title}/{row['id']}-{LOCALE}"


def _span_html(span) -> str:
    return "".join(str(child) for child in span.contents).strip()


def _is_company_intro(text: str) -> bool:
    value = normalize_text(text)
    return value.startswith(COMPANY_INTRO_PREFIX) and value.endswith(COMPANY_INTRO_SUFFIX)


def _is_company_footer(text: str) -> bool:
    value = normalize_text(text)
    return value.startswith(COMPANY_FOOTER_MARKER)


def _split_company_footer(html: str, text: str) -> tuple[str, str | None]:
    """Return role HTML without the shared 'Reinvent your world' footer."""
    if not _is_company_footer(text) and COMPANY_FOOTER_MARKER not in text:
        return html, None
    footer_text = None
    marker = COMPANY_FOOTER_MARKER
    if _is_company_footer(text):
        # The whole field is the shared corporate footer.
        return "", html
    index = text.find(marker)
    if index >= 0:
        footer_text = text[index:].strip()
    html_index = html.find(marker)
    if html_index >= 0:
        html = html[:html_index]
        # Close any element left open by the textual cut.
        soup = BeautifulSoup(html, "lxml")
        root = soup.body if soup.body is not None else soup
        html = "".join(str(child) for child in root.contents).strip()
    elif footer_text:
        # The footer is split across markup; retain only the exact role text.
        html = f"<p>{text[:index].strip()}</p>" if index > 0 else ""
    return html, footer_text


def description_sections(description_html: str) -> list[dict]:
    """Return employer-authored headings and the exact body below each one."""
    soup = BeautifulSoup(f'<div data-wipro-body="true">{description_html or ""}</div>', "lxml")
    root = soup.select_one("[data-wipro-body]")
    sections: list[dict] = []
    if root is None:
        return sections
    for heading in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        title = normalize_title(heading.get_text(" ", strip=True))
        if not title:
            continue
        wrapper = heading.find_parent("div")
        body = wrapper.find_next_sibling("div") if wrapper is not None else None
        body_html = str(body) if body is not None else ""
        body_text = text_html(body_html) if body_html else ""
        if not body_text:
            # Some postings put the body directly after the heading inside the
            # same wrapper; use the wrapper text minus the heading.
            wrapper_text = text_html(str(wrapper)) if wrapper is not None else ""
            if title in wrapper_text:
                wrapper_text = wrapper_text.replace(title, "", 1).strip()
            body_text = wrapper_text
        if body_text or title:
            sections.append({"heading": title, "html": body_html, "text": body_text})
    return sections


def _section_text(sections: list[dict], needles: tuple[str, ...]) -> str | None:
    parts = []
    for section in sections:
        heading = (section.get("heading") or "").lower()
        body = (section.get("text") or "").strip()
        if not body or body.lower() in EMPTY_LABELS:
            continue
        if any(needle in heading for needle in needles):
            parts.append(f"{section['heading']}\n{body}")
    return "\n\n".join(parts) or None


def parse_salary(text: str, tokens: dict[str, str]) -> dict | None:
    labels: list[str] = []
    minimum = maximum = None
    currency = None
    region = None
    for label, value in tokens.items():
        match = SALARY_TOKEN.match(f"{label}: {value}")
        if not match:
            continue
        labels.append(f"{label}: {value}")
        kind = match.group("kind").lower()
        raw = match.group("value").strip()
        number = None
        numeric = re.sub(r"[^0-9.]", "", raw)
        try:
            number = float(numeric) if numeric and numeric.count(".") <= 1 else None
        except ValueError:
            number = None
        if kind == "minimum":
            minimum = number
        else:
            maximum = number
        region = region or match.group("region")
        currency = currency or ("USD" if "$" in raw else None)
    range_match = SALARY_RANGE.search(text)
    if range_match:
        raw_min, raw_max = range_match.group("minimum").strip(), range_match.group("maximum").strip()
        labels.append(range_match.group(0).strip())
        for target, raw in (("minimum", raw_min), ("maximum", raw_max)):
            numeric = re.sub(r"[^0-9.]", "", raw)
            try:
                value = float(numeric) if numeric and numeric.count(".") <= 1 else None
            except ValueError:
                value = None
            if target == "minimum":
                minimum = minimum if minimum is not None else value
            else:
                maximum = maximum if maximum is not None else value
        if range_match.group(0).find("$") >= 0:
            currency = currency or "USD"
    if not labels:
        return None
    return {
        "minimum": minimum,
        "maximum": maximum,
        "currency": currency,
        "region": region,
        "source_text": labels,
        "source_field": "detail.description_and_joblayouttoken",
    }


def _detail_tokens(soup: BeautifulSoup) -> tuple[list[dict], dict[str, str]]:
    rows: list[dict] = []
    values: dict[str, str] = {}
    for token in soup.select(".jobColumnOne .joblayouttoken"):
        label = token.select_one(".joblayouttoken-label")
        if label is None:
            continue
        label_text = normalize_text(label.get_text(" ", strip=True)).rstrip(":").strip()
        value_node = label.find_next_sibling("span")
        value_text = normalize_text(value_node.get_text(" ", strip=True)) if value_node is not None else ""
        rows.append({"label": label_text, "value": value_text})
        if label_text:
            values.setdefault(label_text, value_text)
    return rows, values


def _locations(row: dict, tokens: dict[str, str]) -> list[dict]:
    countries = row.get("jobLocationCountry") if isinstance(row.get("jobLocationCountry"), list) else []
    country = countries[0] if countries else None
    cities = row.get("sfstd_jobLocation_obj") if isinstance(row.get("sfstd_jobLocation_obj"), list) else []
    states = row.get("jobLocationState") if isinstance(row.get("jobLocationState"), list) else []
    short_values = row.get("jobLocationShort") if isinstance(row.get("jobLocationShort"), list) else []
    coordinate_map = {}
    raw_coordinates = row.get("jobLocationShortWithCoordinates")
    if isinstance(raw_coordinates, list):
        for item in raw_coordinates:
            if isinstance(item, dict) and item.get("value") and item.get("key"):
                coordinate_map[normalize_text(item["value"])] = item["key"]
    detail_city = tokens.get("City") or None
    detail_state = tokens.get("State/Province") or None
    single_state = states[0] if len(states) == 1 else None
    values = []
    for index, short in enumerate(short_values):
        source_text = strip_br(short)
        city = cities[index] if index < len(cities) else (cities[0] if cities else None)
        if city is None and source_text:
            city = normalize_text(source_text.split(",")[0])
        values.append({
            "source_text": source_text,
            "city": city,
            # Multiple-state listings are not ordered with their city list on
            # this site (Bengaluru/Gurugram -> Haryana/Karnataka), so only a
            # single unambiguous state is attached to one location.
            "state": single_state,
            "states": states or None,
            "country": country,
            "coordinates": coordinate_map.get(normalize_text(short)),
            "detail_city": detail_city,
            "detail_state": detail_state,
        })
    if not values and (country or detail_city or detail_state):
        values.append({
            "source_text": ", ".join(x for x in (detail_city, detail_state, country) if x) or None,
            "city": cities[0] if cities else detail_city,
            "state": single_state,
            "states": states or None,
            "country": country,
            "coordinates": next(iter(coordinate_map.values()), None),
            "detail_city": detail_city,
            "detail_state": detail_state,
        })
    return values


def parse_detail_page(html: str, response_url: str, listing_row: dict) -> dict:
    if not isinstance(html, str) or len(html) < 500:
        raise ValueError("Wipro detail response is empty")
    soup = BeautifulSoup(html, "lxml")
    if "/errorpage/" in response_url:
        raise ValueError("Wipro detail page redirected to its error page")
    page_text = normalize_text(soup.get_text(" ", strip=True))
    if NOT_AVAILABLE_TEXT in page_text:
        return {
            "available": False,
            "reason": "publisher_job_not_available",
            "final_url": response_url,
            "title": None,
            "description_html": "",
            "description_text": "",
        }

    title_node = soup.select_one('.jobColumnOne span[itemprop="title"]') or soup.select_one('span[itemprop="title"]')
    title = normalize_title(title_node.get_text(" ", strip=True) if title_node else "")
    if not title:
        meta = soup.select_one('meta[property="og:title"]')
        title = normalize_title(meta.get("content") if meta else "")
    if not title:
        raise ValueError("Wipro detail page has no job title")

    column = soup.select_one(".jobColumnOne") or soup
    spans = column.select('span[itemprop="description"]')
    company_intro = []
    company_footer = []
    role_html_parts = []
    for span in spans:
        span_html = _span_html(span)
        span_text = text_html(span_html)
        if not span_text:
            continue
        if _is_company_intro(span_text):
            company_intro.append({"html": span_html, "text": span_text})
            continue
        if _is_company_footer(span_text):
            company_footer.append({"html": span_html, "text": span_text})
            continue
        cleaned_html, footer = _split_company_footer(span_html, span_text)
        if footer:
            company_footer.append({"html": span_html, "text": footer})
        if cleaned_html and text_html(cleaned_html):
            role_html_parts.append(cleaned_html)
    description_html = "\n".join(role_html_parts).strip()
    description_text = text_html(description_html)

    tokens, token_values = _detail_tokens(soup)
    path_match = JOB_ID_FROM_PATH.match(urlsplit(response_url).path)
    path_id = path_match.group(1) if path_match else None
    script_match = JOB_ID_FROM_SCRIPT.search(html)
    script_id = script_match.group(1) if script_match else None
    listed_id = str(listing_row.get("id"))
    for name, value in (("path", path_id), ("script", script_id)):
        if value and value != listed_id:
            raise ValueError(f"Wipro detail {name} id {value} does not match listed id {listed_id}")

    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None
    apply_controls = [
        normalize_title(node.get_text(" ", strip=True))
        for node in column.select("button, a[role='menuitem'], a.applyOption")
        if normalize_title(node.get_text(" ", strip=True))
    ]
    start_raw = token_values.get("Posting Start Date") or listing_row.get("unifiedStandardStart")
    end_raw = token_values.get("Posting End Date") or listing_row.get("unifiedStandardEnd")
    sections = description_sections(description_html)
    responsibilities = _section_text(sections, ("responsibilit", "duties", "area of responsibility", "deliver"))
    if responsibilities is None:
        responsibilities = _section_text(sections, ("do",))
    qualifications = _section_text(sections, ("qualif", "skill", "requirement", "mandatory", "education"))
    experience = _section_text(sections, ("experience",))
    education = _section_text(sections, ("education",))
    # The site publishes job-specific mandatory skills and experience as the
    # first lines of its short roles block, without a heading element.
    mandatory = re.search(r"Mandatory Skills:\s*([^\n]+)", description_text, re.I)
    if mandatory:
        qualifications = f"Mandatory Skills: {mandatory.group(1).strip()}"
    experience_match = re.search(r"Experience:\s*([^\n]+)", description_text, re.I)
    if experience_match:
        experience = experience or f"Experience: {experience_match.group(1).strip()}"
    salary = parse_salary(description_text, token_values)
    employment_type = None
    workplace_type = None
    for label, value in token_values.items():
        lowered = label.lower()
        if lowered in ("employment type", "job type", "worker type", "schedule"):
            employment_type = value or None
        if lowered in ("workplace", "workplace type", "work mode", "remote"):
            workplace_type = value or None
    return {
        "available": True,
        "title": title,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(normalize_text(description_text)),
        "description_sha256": sha256(description_text.encode()).hexdigest() if description_text else None,
        "company_intro": company_intro,
        "company_footer": company_footer,
        "tokens": tokens,
        "token_values": token_values,
        "sections": sections,
        "responsibilities": responsibilities,
        "qualifications": qualifications,
        "experience": experience,
        "education": education,
        "salary": salary,
        "employment_type": employment_type,
        "workplace_type": workplace_type,
        "date_posted_raw": start_raw,
        "date_posted": parse_date(start_raw),
        "valid_through_raw": end_raw,
        "valid_through": parse_date(end_raw),
        "path_id": path_id,
        "script_id": script_id,
        "canonical_url": canonical_url,
        "apply_controls": apply_controls,
        "listing_title": normalize_title(listing_row.get("unifiedStandardTitle")),
    }


# Invisible separator characters Wipro inserts between empty "Job Description"
# blocks (U+034F and friends).  They carry no substantive content.
_INVISIBLE_CHARS = re.compile(r"[\u034f\u200b-\u200d\ufeff\u2060\u180e]")
_TEMPLATE_LINE_PATTERNS = (
    re.compile(r"^\s*job description\s*$", re.I),
    re.compile(r"^\s*mandatory skills:.*$", re.I),
    re.compile(r"^\s*experience:.*$", re.I),
    re.compile(r".*expected (?:annual )?(?:compensation|pay) for this role ranges.*", re.I),
    re.compile(r".*final compensation will depend.*", re.I),
    re.compile(r".*standard benefits including.*", re.I),
    re.compile(r".*applicants are advised.*", re.I),
    re.compile(r".*equal (?:opportunity|employment).*", re.I),
    re.compile(r"^\s*\.?\s*$", re.I),
)


def _template_remaining_text(text: str) -> str:
    """Remove the publisher's Mandatory Skills/Experience and compensation,
    benefits and EEO boilerplate.  What remains is the role body, if any."""
    value = _INVISIBLE_CHARS.sub("", text or "")
    kept: list[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(pattern.match(stripped) for pattern in _TEMPLATE_LINE_PATTERNS):
            continue
        if not re.search(r"[A-Za-z0-9]", stripped):
            continue
        kept.append(stripped)
    return " ".join(kept)


def description_placeholder_reason(parsed: dict) -> str | None:
    text = normalize_text(parsed.get("description_text") or "")
    if not text or text.lower() in EMPTY_LABELS:
        return "publisher_empty_description"
    if len(text) < MIN_DESCRIPTION_CHARS:
        return "publisher_placeholder_description"
    if text.lower() in SECTION_HEADINGS:
        return "publisher_placeholder_description"
    # Content-based rule: a publisher placeholder publishes only the shared
    # Mandatory Skills / Experience stub plus compensation, benefits and EEO
    # boilerplate, with no role body.  Length alone is not enough because the
    # invisible separators and boilerplate can push such stubs past the
    # character threshold.
    if not _template_remaining_text(parsed.get("description_text") or ""):
        return "publisher_placeholder_description"
    return None


class Wipro(Adapter):
    """Follow only Wipro's published global search and its exact job pages."""

    VERIFICATION_REVISION = "wipro.successfactors-csb.global-search-and-detail.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.detail_http = None
        self.detail_blocked = False
        self.rows: dict[str, dict] = {}
        self.row_pages: dict[str, int] = {}
        self.row_passes: dict[str, int] = {}
        self.duplicate_rows = 0
        self.pass_stats: list[dict] = []
        self.listing_seconds = 0.0

    async def _post_json(self, http, body):
        return await http.json(JOBS_API, method="POST", json=body, headers=JSON_HEADERS)

    async def _listing_pass(self, http, pass_number: int) -> dict:
        import asyncio

        started = time.perf_counter()
        if not self.page_guard():
            raise ValueError("Wipro listing page budget exhausted")
        first = parse_search_response(await self._post_json(http, search_request(0)), 0)
        total = first["total"]
        page_count = max(1, math.ceil(total / PAGE_SIZE))
        issues: list[str] = []
        sem = asyncio.Semaphore(4)

        async def one(page: int):
            async with sem:
                if not self.page_guard():
                    return page, None, "page_budget_exhausted"
                try:
                    value = await self._post_json(http, search_request(page))
                    return page, parse_search_response(value, page), None
                except Exception as exc:  # keep other pages usable
                    return page, None, f"{type(exc).__name__}: {exc}"

        results = {0: first}
        errors = []
        pending = [page for page in range(1, page_count)]
        for start in range(0, len(pending), 100):
            batch = pending[start:start + 100]
            for page, parsed, error in await asyncio.gather(*(one(page) for page in batch)):
                if error:
                    errors.append(f"page_{page}:{error}")
                elif parsed is not None:
                    results[page] = parsed
        pass_ids: set[str] = set()
        for page in sorted(results):
            parsed = results[page]
            if parsed["total"] != total:
                issues.append(f"listing_total_changed:{total}->{parsed['total']}")
            issues.extend(parsed["issues"])
            self._ingest(parsed, page, pass_number, pass_ids)
        issues.extend(errors)
        seconds = max(0.0, time.perf_counter() - started)
        stats = {
            "pass": pass_number,
            "total": total,
            "pages": page_count,
            "pages_fetched": len(results),
            "rows": len(pass_ids),
            "unique_after_pass": len(self.rows),
            "seconds": seconds,
            "issues": list(dict.fromkeys(issues)),
        }
        self.pass_stats.append(stats)
        return stats

    def _ingest(self, parsed: dict, page: int, pass_number: int, pass_ids: set[str]) -> None:
        for row in parsed["rows"]:
            identity = str(row["id"])
            pass_ids.add(identity)
            if identity in self.rows:
                self.duplicate_rows += 1
                continue
            self.rows[identity] = row
            self.row_pages[identity] = page
            self.row_passes[identity] = pass_number

    async def _enumerate_listing(self) -> None:
        started = time.perf_counter()
        async with HTTP(self.http.output, delay=0.1, concurrency=4, retries=2, timeout=45) as listing_http:
            for pass_number in range(1, MAX_LISTING_PASSES + 1):
                stats = await self._listing_pass(listing_http, pass_number)
                if len(self.rows) >= stats["total"]:
                    break
        self.listing_seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["listing_seconds"] = self.listing_seconds

    def _add_job(self, row: dict):
        identity = str(row["id"])
        title = normalize_title(row.get("unifiedStandardTitle")) or normalize_title(row.get("urlTitle"))
        url = job_detail_url(row)
        start = row.get("unifiedStandardStart")
        end = row.get("unifiedStandardEnd")
        departments = row.get("custRMKMappingPicklist") if isinstance(row.get("custRMKMappingPicklist"), list) else []
        job = self.job(
            {
                "listing": row,
                "listing_page": self.row_pages.get(identity),
                "listing_pass": self.row_passes.get(identity),
                "listing_valid_through_raw": end,
            },
            title=title,
            url=url,
            job_id=identity,
            locations=_locations(row, {}),
            department=departments[0] if departments else None,
            date_posted=parse_date(start),
            date_posted_raw=start,
            valid_through=parse_date(end),
            employer="Wipro Limited",
            language="en",
            method=None,
            description_url=url,
        )
        self.add(job)
        return job

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
        if not parsed["available"]:
            job.description_unavailable = True
            job.warnings.append(parsed["reason"])
            job.raw["detail"] = {
                "response_sha256": sha256(response.content).hexdigest(),
                "response_bytes": len(response.content),
                "final_url": str(response.url),
                "available": False,
                "reason": parsed["reason"],
            }
            return job
        if parsed["title"] != job.title:
            job.warnings.append("listing_title_changed")
            self.result.metadata.setdefault("title_mismatches", {})[job.job_id] = {
                "listing": job.title,
                "detail": parsed["title"],
            }
        job.title = parsed["title"]
        job.apply_url = job.url
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "wipro.successfactors.job_detail.itemprop_description"
        job.description_source_url = job.url
        job.locations = _locations(listing_row, parsed["token_values"]) or job.locations
        job.salary = parsed["salary"]
        job.qualifications = parsed["qualifications"]
        job.responsibilities = parsed["responsibilities"]
        job.experience = parsed["experience"]
        job.education = parsed["education"]
        job.employment_type = parsed["employment_type"]
        job.workplace_type = parsed["workplace_type"]
        job.date_posted = parsed["date_posted"] or job.date_posted
        job.date_posted_raw = parsed["date_posted_raw"]
        job.valid_through = parsed["valid_through"] or job.valid_through
        job.raw["detail"] = {
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "final_url": str(response.url),
            "canonical_url": parsed["canonical_url"],
            "detail_id": parsed["path_id"],
            "script_id": parsed["script_id"],
            "listing_title": parsed["listing_title"],
            "tokens": parsed["tokens"],
            "sections": parsed["sections"],
            "company_intro": parsed["company_intro"],
            "company_footer": parsed["company_footer"],
            "description_normalized_chars": parsed["description_normalized_chars"],
            "description_sha256": parsed["description_sha256"],
            "dates": {
                "posting_start_raw": parsed["date_posted_raw"],
                "posting_start_iso": parsed["date_posted"],
                "posting_end_raw": parsed["valid_through_raw"],
                "posting_end_iso": parsed["valid_through"],
                "posting_end_source": "detail.joblayouttoken" if parsed["token_values"].get("Posting End Date") else "listing.unifiedStandardEnd",
            },
            "salary": parsed["salary"],
            "apply_controls": parsed["apply_controls"],
            "apply_url_note": "Wipro exposes an on-page Apply now widget; no static application URL is published in the page.",
        }
        placeholder_reason = description_placeholder_reason(parsed)
        if placeholder_reason:
            job.description_unavailable = True
            job.warnings.append(placeholder_reason)
        return job

    async def _final_check(self) -> dict:
        """Re-check the listing after detail collection and top up new rows."""
        started = time.perf_counter()
        final = {"started_at": time.perf_counter(), "new_ids": [], "removed_ids": [],
                 "total": None, "page_count": None, "checked_pages": []}
        async with HTTP(self.http.output, delay=0.1, concurrency=2, retries=2, timeout=45) as final_http:
            first = parse_search_response(await self._post_json(final_http, search_request(0)), 0)
            total = first["total"]
            page_count = max(1, math.ceil(total / PAGE_SIZE))
            last = parse_search_response(
                await self._post_json(final_http, search_request(page_count - 1)), page_count - 1
            )
            final.update(total=total, page_count=page_count,
                         checked_pages=[0, page_count - 1])
            first_ids = {str(row["id"]) for row in first["rows"]}
            last_ids = {str(row["id"]) for row in last["rows"]}
            unknown = (first_ids | last_ids) - set(self.rows)
            if unknown or total != self.result.expected_total:
                # A full top-up pass is only needed when the advertised total or
                # the exposed edge pages changed during the long crawl.
                stats = await self._listing_pass(final_http, len(self.pass_stats) + 1)
                final["top_up_pass"] = stats
                final["total"] = stats["total"]
                final["page_count"] = stats["pages"]
        current_ids = {job.job_id for job in self.result.jobs}
        final["new_ids"] = [identity for identity in self.rows if identity not in current_ids]
        final["removed_ids"] = [job.job_id for job in self.result.jobs if job.job_id not in self.rows]
        final["seconds"] = max(0.0, time.perf_counter() - started)
        return final

    async def _hydrate_new_rows(self, rows: list[dict]):
        jobs = [self._add_job(row) for row in rows]
        if not jobs:
            return []
        async with HTTP(self.http.output, delay=0.1,
                        concurrency=max(1, int(self.options.detail_concurrency or 5)),
                        retries=2, timeout=60) as detail_http:
            self.detail_http = detail_http
            try:
                await self.hydrate(jobs, self.detail)
            finally:
                self.detail_http = None
        return jobs

    async def run(self):
        started = time.perf_counter()
        # The company journey: official wipro.com entry -> careers.wipro.com
        # home -> its own Search Jobs page -> the search widget request.
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

        facets = parse_facet_response(await self._post_json(self.http, facet_request()))
        self.result.metadata["department_facets"] = facets
        self.result.metadata["scope"] = {
            "official_entry": OFFICIAL_ENTRY,
            "final_url": str(entry.url),
            "listing_url": LISTING_PAGE,
            "jobs_api": JOBS_API,
            "filters": "facetFilters={} (no keyword, country, category or seniority filter)",
            "sort": "site default (sortBy='')",
            "page_size": PAGE_SIZE,
        }

        await self._enumerate_listing()
        self.result.expected_total = self.pass_stats[-1]["total"] if self.pass_stats else None
        self.result.metadata["listing_passes"] = self.pass_stats
        self.result.metadata["listing_membership"] = {
            "unique_ids": len(self.rows),
            "advertised_total": self.result.expected_total,
            "duplicate_rows_across_passes": self.duplicate_rows,
            "passes": len(self.pass_stats),
            "complete_union": len(self.rows) >= (self.result.expected_total or 0),
        }
        if self.result.expected_total is None:
            self.result.errors.append("listing_total_unknown")
        elif len(self.rows) < self.result.expected_total:
            self.result.errors.append("listing_membership_incomplete")
        listing_issues = [
            f"listing_pass_{stats['pass']}:{issue}"
            for stats in self.pass_stats for issue in stats["issues"]
        ]
        if listing_issues:
            self.result.metadata["listing_issues"] = listing_issues
            self.result.warnings.extend(listing_issues)

        rows_in_date_order = sorted(
            self.rows.values(),
            key=lambda row: (parse_date(row.get("unifiedStandardStart")) or "", str(row.get("id"))),
            reverse=True,
        )
        for row in rows_in_date_order:
            self._add_job(row)

        detail_concurrency = max(1, int(self.options.detail_concurrency or 5))
        try:
            async with HTTP(self.http.output, delay=0.1, concurrency=detail_concurrency,
                            retries=2, timeout=60) as detail_http:
                self.detail_http = detail_http
                await self.hydrate(self.result.jobs, self.detail)
        finally:
            self.detail_http = None

        final = await self._final_check()
        new_rows = [self.rows[identity] for identity in final["new_ids"] if identity in self.rows]
        if new_rows:
            await self._hydrate_new_rows(new_rows)
        if final["total"] is not None:
            self.result.expected_total = final["total"]
        self.result.metadata["final_listing_check"] = final

        # Expired postings are not current public roles.  Keep their exact
        # identity/reason in metadata and the review, but do not count them as
        # collected descriptions.
        expired = [job for job in self.result.jobs
                   if job.description_unavailable
                   and "publisher_job_not_available" in job.warnings]
        if expired:
            self.result.jobs = [job for job in self.result.jobs if job not in expired]
            self.result.metadata["dropped_expired"] = [
                {"id": job.job_id, "url": job.url, "reason": "publisher_job_not_available"}
                for job in expired
            ]

        # Department facet counts are an independent cross-check of the
        # unfiltered membership; the facet sum can lag the total, so only
        # matching values are compared.
        collected_counts: dict[str, int] = {}
        for job in self.result.jobs:
            key = job.department or "UNKNOWN"
            collected_counts[key] = collected_counts.get(key, 0) + 1
        facet_counts = facets["counts"]
        mismatches = {
            key: {"facet": facet_counts.get(key, 0), "collected": collected_counts.get(key, 0)}
            for key in sorted(set(facet_counts) | set(collected_counts))
            if key != "UNKNOWN" and facet_counts.get(key, 0) != collected_counts.get(key, 0)
        }
        unknown = collected_counts.get("UNKNOWN", 0)
        self.result.metadata["department_reconciliation"] = {
            "facet_total": facets["total"],
            "facet_sum": facets["facet_sum"],
            "collected_total": len(self.result.jobs),
            "collected_without_department": unknown,
            "mismatches": mismatches,
            "counts_match": not mismatches,
        }
        if mismatches:
            self.result.warnings.append("department_facet_counts_do_not_match_collected_rows")

        live_ids = {job.job_id for job in self.result.jobs}
        removed = [identity for identity in final["removed_ids"] if identity not in live_ids]
        if removed:
            self.result.metadata["final_listing_check"]["removed_ids"] = removed
        membership_complete = (
            self.result.expected_total is not None
            and len(self.result.jobs) == self.result.expected_total
            and not removed
        )
        self.result.listing_complete = membership_complete
        if not membership_complete:
            self.result.errors.append("listing_membership_not_reconciled")
        self.result.metadata["job_ids"] = [job.job_id for job in self.result.jobs]
        self.result.metadata["detail_hydration_mode"] = (
            "One exact company detail page per listing row; full itemprop=description fields."
        )
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
        return self.result
