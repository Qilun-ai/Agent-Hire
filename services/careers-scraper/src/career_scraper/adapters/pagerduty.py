"""PagerDuty's official Phenom careers listing and JobPosting detail pages.

The official entry ``https://www.pagerduty.com/careers/`` redirects to
``https://careers.pagerduty.com/``.  The company's own unfiltered listing is
``https://careers.pagerduty.com/jobs/search`` (30 rows per page, one-based
``?page=N``).  Each row links to a company detail URL under ``/jobs/<slug>``;
the detail page publishes one schema.org ``JobPosting`` JSON-LD object with the
full employer-authored description, its identifier, datePosted, validThrough,
employmentType, hiringOrganization and jobLocation.

Programmatic httpx requests receive empty HTTP 202 AWS WAF challenge responses
(``x-amzn-waf-action: challenge``).  A normal headless Chromium executes the
site's own JS challenge and renders the public pages; no CAPTCHA is presented
or solved and no token is injected.  The runner archives the rendered HTML and
this adapter parses that captured official source.  If the browser cannot
render a page, the missing page/detail is reported and the source is partial.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import math
import re
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://www.pagerduty.com/careers/"
CAREERS_ORIGIN = "https://careers.pagerduty.com"
LISTING_URL = CAREERS_ORIGIN + "/jobs/search"
PAGE_SIZE = 30
JOB_PATH = re.compile(r"^/jobs/[a-z0-9][a-z0-9-]*$", re.I)
JOB_UID = re.compile(r"^[0-9a-f]{32}$", re.I)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
JOB_UID_SCRIPT = re.compile(r'js_vars\.job=\{"uid":"([0-9a-f]{32})"\}', re.I)
SALARY_RE = re.compile(
    r"(?P<prefix>(?:base\s+)?salary(?:\s+range)?(?:\s+for\s+this\s+position)?"
    r"(?:\s+in\s+[A-Za-z][A-Za-z ]{0,30})?\s*(?:is|:)\s*)"
    r"(?P<cur1>[$£€])?\s*(?P<min>\d[\d,]*(?:\.\d+)?)\s*(?:-|–|—|to)\s*"
    r"(?P<cur2>[$£€])?\s*(?P<max>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<code>USD|CAD|CDN|GBP|EUR|AUD|CLP)?",
    re.I,
)
RESPONSIBILITY_HEADING = re.compile(
    r"responsib|what you.?ll do|what you will do|key duties|your impact|what you.?ll own",
    re.I,
)
QUALIFICATION_HEADING = re.compile(
    r"qualification|requirement|what you.?ll bring|what you will bring|what we.?re looking for|"
    r"who you are|nice to have|preferred|experience",
    re.I,
)


def normalized_title(value: str | None) -> str:
    """Normalize Unicode composition and whitespace while preserving title words."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    """Approximate rendered body text and collapse whitespace for body audits."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def normalized_visible_text(value: str | None) -> str:
    """Normalize browser innerText/description text for body hash comparisons."""
    text = re.sub(r"\s+", " ", value or "").strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _clean(value) -> str:
    return normalized_title(value if isinstance(value, str) else "")


def _iso_or_none(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return raw


def _facet_counts(soup: BeautifulSoup, field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for input_node in soup.select(f'input[name="{field}"]'):
        label = input_node.find_parent("label")
        text = _clean(label.get_text(" ", strip=True) if label else "")
        match = re.search(r"^(.*?)\s*\((\d+)\s+items?\)$", text, re.I)
        if match:
            name = _clean(match.group(1))
            if name:
                counts[name] = int(match.group(2))
    return counts


def _validate_listing_url(response_url: str) -> None:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname != "careers.pagerduty.com"
            or parts.path != "/jobs/search"):
        raise ValueError(f"PagerDuty listing response came from an unexpected URL: {response_url}")
    query = parse_qs(parts.query)
    if set(query) - {"page"}:
        raise ValueError(f"PagerDuty listing response unexpectedly carried filters: {response_url}")
    if "page" in query and (len(query["page"]) != 1 or not query["page"][0].isdigit()):
        raise ValueError(f"PagerDuty listing page parameter is not one-based numeric: {response_url}")


def parse_listing_page(html: str, response_url: str = LISTING_URL,
                       requested_page: int | None = None) -> dict:
    """Parse one unfiltered company listing page and its count/facet controls."""
    _validate_listing_url(response_url)
    soup = BeautifulSoup(html or "", "lxml")
    rows: list[dict] = []
    seen: set[str] = set()
    issues: list[str] = []

    for tr in soup.select("tr[data-job-url]"):
        data_url = (tr.get("data-job-url") or "").strip()
        title_anchor = tr.select_one(".job-search-results-title a[href]")
        title = _clean(title_anchor.get_text(" ", strip=True) if title_anchor else "")
        url = data_url or (title_anchor.get("href") if title_anchor else "")
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != "careers.pagerduty.com"
                or not JOB_PATH.fullmatch(parts.path) or parts.query or parts.fragment):
            issues.append(f"pagerduty_listing_row_unexpected_job_url:{url}")
            continue
        if title_anchor is not None:
            href = urlsplit(title_anchor.get("href") or "")
            if (href.scheme != parts.scheme or href.hostname != parts.hostname
                    or href.path != parts.path or href.query or href.fragment):
                issues.append(f"pagerduty_listing_row_title_href_mismatch:{url}")
        if not title:
            issues.append(f"pagerduty_listing_row_missing_title:{url}")
            continue
        if url in seen:
            issues.append(f"pagerduty_listing_duplicate_job_url:{url}")
            continue
        seen.add(url)
        category_nodes = tr.select(".job-search-results-category li")
        location_nodes = tr.select(".job-search-results-location li")
        remote_node = tr.select_one(".job-search-results-remote")
        rows.append({
            "url": url,
            "title": title,
            "category": " / ".join(_clean(node.get_text(" ", strip=True)) for node in category_nodes)
            if category_nodes else None,
            "location": " / ".join(_clean(node.get_text(" ", strip=True)) for node in location_nodes)
            if location_nodes else None,
            "remote_label": _clean(remote_node.get_text(" ", strip=True)) if remote_node else "",
            "row_html": str(tr),
        })

    count_text = None
    count_node = soup.select_one(".table-counts")
    if count_node is not None:
        count_text = re.sub(r"\s+", " ", count_node.get_text(" ", strip=True)).strip()
    range_match = re.search(
        r"Displaying\s+([\d,]+)\s*[–-]\s*([\d,]+)\s+of\s+([\d,]+)\s+in\s+total",
        count_text or "", re.I,
    )
    start = end = total = None
    if range_match:
        start, end, total = (int(value.replace(",", "")) for value in range_match.groups())
    else:
        issues.append("pagerduty_listing_display_count_missing")

    page = requested_page
    if page is None:
        query_page = parse_qs(urlsplit(response_url).query).get("page")
        page = int(query_page[0]) if query_page else 1
    pages = math.ceil(total / PAGE_SIZE) if total else None
    if total is not None and pages is not None and not (1 <= page <= pages):
        issues.append(f"pagerduty_listing_page_out_of_range:{page}/{pages}")
    if total is not None and start is not None and end is not None:
        expected_rows = end - start + 1
        if len(rows) != expected_rows:
            issues.append("pagerduty_listing_display_range_row_count_mismatch")
        if start != (page - 1) * PAGE_SIZE + 1:
            issues.append("pagerduty_listing_page_start_mismatch")

    pagination = []
    for anchor in soup.select('nav[aria-label="Pagination"] a'):
        text = _clean(anchor.get_text(" ", strip=True))
        href = anchor.get("href") or ""
        page_match = re.search(r"(?:^|[?&])page=(\d+)", href)
        pagination.append({
            "text": text,
            "href": href,
            "rel": anchor.get("rel"),
            "page": int(page_match.group(1)) if page_match else None,
        })
    linked_pages = sorted({item["page"] for item in pagination if item["page"]})
    if pages is not None and linked_pages and linked_pages != list(range(1, pages + 1)):
        issues.append("pagerduty_listing_pagination_pages_mismatch")

    facets = {
        "categories": _facet_counts(soup, "category_uids[]"),
        "countries": _facet_counts(soup, "country_codes[]"),
        "states": _facet_counts(soup, "states[]"),
        "cities": _facet_counts(soup, "cities[]"),
    }
    for key, expected in (("categories", total), ("countries", total)):
        if facets[key] and expected is not None and sum(facets[key].values()) != expected:
            issues.append(f"pagerduty_listing_{key}_facet_sum_mismatch")

    return {
        "url": response_url,
        "page": page,
        "pages": pages,
        "page_size": PAGE_SIZE,
        "total": total,
        "display_start": start,
        "display_end": end,
        "count_text": count_text,
        "rows": rows,
        "job_urls": [row["url"] for row in rows],
        "pagination": pagination,
        "facets": facets,
        "filters_applied": False,
        "issues": list(dict.fromkeys(issues)),
    }


def _jobposting(soup: BeautifulSoup) -> dict:
    found = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        nodes = value if isinstance(value, list) else [value]
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "JobPosting":
                found.append(node)
    if len(found) != 1:
        raise ValueError(f"PagerDuty detail must expose exactly one JobPosting JSON-LD object; found {len(found)}")
    return found[0]


def _identifier(posting: dict) -> str:
    identifier = posting.get("identifier")
    if isinstance(identifier, dict):
        value = identifier.get("value")
    elif isinstance(identifier, str):
        value = identifier
    else:
        value = None
    value = _clean(value)
    if not value:
        raise ValueError("PagerDuty JobPosting identifier is missing")
    return value


def _parse_locations(value, telecommute: bool) -> list[dict]:
    values = value if isinstance(value, list) else ([value] if value else [])
    locations = []
    for place in values:
        if isinstance(place, str):
            locations.append({"location": _clean(place), "address": None, "telecommute": telecommute})
            continue
        if not isinstance(place, dict):
            continue
        address = place.get("address") if isinstance(place.get("address"), dict) else {}
        locality = _clean(address.get("addressLocality"))
        region = _clean(address.get("addressRegion"))
        country = _clean(address.get("addressCountry"))
        display = ", ".join(part for part in (locality, region, country) if part)
        locations.append({
            "location": display or None,
            "address": dict(address),
            "telecommute": telecommute,
        })
    return locations


def _parse_employer(value) -> dict | None:
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, dict)), None)
    if not isinstance(value, dict):
        return None
    return dict(value)


def description_sections(html: str) -> list[dict]:
    """Split the employer-authored body into heading + exact body sections."""
    soup = BeautifulSoup(f'<div data-pagerduty-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-pagerduty-body]")
    if root is None:
        return []
    sections: list[dict] = []
    current: dict | None = None
    for block in root.contents:
        if not isinstance(block, Tag):
            continue
        heading = ""
        if block.name in HEADING_TAGS:
            heading = _clean(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = _clean(block.get_text(" ", strip=True))
            strong_text = _clean(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text
        if heading:
            if current is not None:
                current["text"] = normalized_body_text(current["html"])
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = normalized_body_text(current["html"])
        sections.append(current)
    return sections


def _matching_sections(sections: list[dict], pattern: re.Pattern) -> list[dict] | None:
    found = [
        section for section in sections
        if section.get("heading") and pattern.search(section["heading"]) and section.get("html")
    ]
    return found or None


def extract_salary(description_text: str, country_hint: str | None = None) -> dict | None:
    """Retain a published base-salary range from the employer description, if any."""
    text = re.sub(r"\s+", " ", description_text or "").strip()
    for match in SALARY_RE.finditer(text):
        context = text[max(0, match.start() - 180):match.end() + 220]
        if not re.search(r"salary|compensation|base\s+pay", context, re.I):
            continue
        minimum = float(match.group("min").replace(",", ""))
        maximum = float(match.group("max").replace(",", ""))
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        code = (match.group("code") or "").upper()
        if code == "CDN":
            code = "CAD"
        symbol = match.group("cur1") or match.group("cur2")
        currency = code or {"$": "USD", "£": "GBP", "€": "EUR"}.get(symbol or "")
        if not currency and country_hint:
            currency = {"US": "USD", "CA": "CAD", "GB": "GBP", "PT": "EUR", "CL": "CLP"}.get(country_hint.upper())
        period = "hour" if re.search(r"per\s+hour|hourly", context, re.I) else "year"
        # Start at the last explicit salary label, else the preceding sentence boundary.
        prefix_start = max(0, match.start() - 300)
        window = text[prefix_start:match.end()]
        labels = list(re.finditer(
            r"(?:the\s+)?(?:base\s+)?salary\s+range|base\s+salary|compensation",
            window, re.I,
        ))
        if labels:
            prefix_start += labels[-1].start()
        else:
            prefix = text[prefix_start:match.start()]
            boundary = max(prefix.rfind(". "), prefix.rfind("! "), prefix.rfind("? "))
            if boundary >= 0:
                prefix_start += boundary + 2
        suffix = text[match.end():match.end() + 250]
        endings = [pos for pos in (suffix.find(". "), suffix.find("! "), suffix.find("? ")) if pos >= 0]
        if endings:
            suffix_end = match.end() + min(endings) + 1
        elif suffix.rstrip().endswith((".", "!", "?")):
            suffix_end = match.end() + len(suffix.rstrip())
        else:
            suffix_end = match.end()
        return {
            "raw_text": _clean(text[prefix_start:suffix_end])[:500],
            "min": int(minimum) if minimum.is_integer() else minimum,
            "max": int(maximum) if maximum.is_integer() else maximum,
            "currency": currency,
            "period": period,
            "source": "JobPosting.description",
            "field": "base salary range",
        }
    return None


def _parse_apply(soup: BeautifulSoup, response_url: str, job_uid: str) -> dict:
    block = soup.select_one(
        '.block-call-to-action[data-call-to-action--form-kind-value="apply_url"]'
    )
    frame = block.select_one("turbo-frame[src]") if block is not None else None
    form = block.select_one("form[action]") if block is not None else None
    form_action = form.get("action").replace("&amp;", "&") if form is not None and form.get("action") else None
    frame_url = frame.get("src").replace("&amp;", "&") if frame is not None and frame.get("src") else None
    form_job_id = None
    if form_action:
        form_job_id = parse_qs(urlsplit(form_action).query).get("job_id", [None])[0]
    cta = soup.select_one("#applycta")
    apply_url = response_url + "#applycta" if cta is not None or block is not None else form_action
    return {
        "apply_url": apply_url,
        "apply_form_action": form_action,
        "apply_frame_url": frame_url,
        "apply_form_present": form is not None,
        "apply_cta_present": cta is not None,
        "form_job_id": form_job_id,
        "form_job_id_matches": form_job_id == job_uid if form_job_id else None,
    }


def parse_job_detail(html: str, response_url: str, listing_row: dict | None = None) -> dict:
    """Parse a company job detail page and its one schema.org JobPosting object."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname != "careers.pagerduty.com"
            or not JOB_PATH.fullmatch(parts.path) or parts.query or parts.fragment):
        raise ValueError(f"PagerDuty detail response came from an unexpected URL: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    posting = _jobposting(soup)
    identity = _identifier(posting)
    title = normalized_title(posting.get("title"))
    if not title:
        raise ValueError("PagerDuty JobPosting title is missing")
    description_html = posting.get("description")
    if not isinstance(description_html, str):
        raise ValueError("PagerDuty JobPosting description is not a string")
    description_text = normalized_body_text(description_html)
    if len(description_text) < 100:
        raise ValueError("PagerDuty JobPosting description is missing or truncated")

    issues: list[str] = []
    visible_node = soup.select_one(".job-description")
    visible_html = str(visible_node) if visible_node is not None else ""
    visible_text = normalized_body_text(visible_html)
    visible_matches = bool(visible_text) and visible_text == description_text
    if not visible_text:
        issues.append("pagerduty_visible_description_missing")
    elif not visible_matches:
        issues.append("pagerduty_visible_description_text_mismatch")

    visible_title_node = soup.select_one("h3.job-title")
    visible_title = normalized_title(visible_title_node.get_text(" ", strip=True) if visible_title_node else "")
    if not visible_title:
        issues.append("pagerduty_visible_title_missing")
    elif visible_title != title:
        issues.append("pagerduty_jsonld_visible_title_mismatch")

    uid_match = JOB_UID_SCRIPT.search(html or "")
    script_uid = uid_match.group(1) if uid_match else None
    class_uid_match = re.search(r"job-component-details-([0-9a-f]{32})", html or "", re.I)
    class_uid = class_uid_match.group(1) if class_uid_match else None
    if not JOB_UID.fullmatch(identity):
        issues.append("pagerduty_jobposting_identifier_is_not_32_hex")
    if script_uid and script_uid.lower() != identity.lower():
        issues.append("pagerduty_js_job_uid_mismatch")
    if class_uid and class_uid.lower() != identity.lower():
        issues.append("pagerduty_component_job_uid_mismatch")

    details_node = soup.select_one('[class*="job-component-details-"]')
    component_details = _clean(details_node.get_text(" ", strip=True)) if details_node else None
    component_lists = {}
    for key in ("location", "category", "employment_type"):
        node = soup.select_one(f".job-component-list-{key}")
        component_lists[key] = [
            _clean(item.get_text(" ", strip=True)) for item in node.select("li") if _clean(item.get_text(" ", strip=True))
        ] if node is not None else []
    visible_location = " / ".join(component_lists["location"]) or None
    visible_category = " / ".join(component_lists["category"]) or None
    visible_employment = " / ".join(component_lists["employment_type"]) or None

    if listing_row:
        if normalized_title(listing_row.get("title")) != title:
            issues.append("pagerduty_listing_detail_title_mismatch")
        if listing_row.get("location") and visible_location and listing_row["location"] != visible_location:
            issues.append("pagerduty_listing_detail_location_mismatch")
        if listing_row.get("category") and visible_category and listing_row["category"] != visible_category:
            issues.append("pagerduty_listing_detail_category_mismatch")

    telecommute = _clean(posting.get("jobLocationType")).upper() == "TELECOMMUTE"
    locations = _parse_locations(posting.get("jobLocation"), telecommute)
    if listing_row and listing_row.get("location"):
        for location in locations:
            location["listing_location"] = listing_row["location"]
        if len(locations) == 1:
            locations[0]["location"] = listing_row["location"]
    employer = _parse_employer(posting.get("hiringOrganization"))
    employer_name = _clean(employer.get("name")) if employer else None
    country_hint = None
    if locations and isinstance(locations[0].get("address"), dict):
        country_hint = locations[0]["address"].get("addressCountry")
    salary = extract_salary(description_text, country_hint)
    sections = description_sections(description_html)
    responsibilities = _matching_sections(sections, RESPONSIBILITY_HEADING)
    qualifications = _matching_sections(sections, QUALIFICATION_HEADING)
    apply = _parse_apply(soup, response_url, identity)
    if apply["form_job_id_matches"] is False:
        issues.append("pagerduty_apply_form_job_id_mismatch")

    date_posted_raw = posting.get("datePosted")
    valid_through_raw = posting.get("validThrough")
    date_posted = _iso_or_none(date_posted_raw)
    valid_through = _iso_or_none(valid_through_raw)
    if date_posted_raw and date_posted is None:
        issues.append("pagerduty_date_posted_not_iso")
    if valid_through_raw and valid_through is None:
        issues.append("pagerduty_valid_through_not_iso")

    posting_without_description = {key: value for key, value in posting.items() if key != "description"}
    return {
        "identity": identity,
        "title": title,
        "visible_title": visible_title,
        "description_html": description_html,
        "description_text": description_text,
        "description_method": "pagerduty.phenom.jobposting_jsonld.description",
        "visible_description_html": visible_html,
        "visible_description_text": visible_text,
        "visible_description_matches": visible_matches,
        "visible_location": visible_location,
        "visible_category": visible_category,
        "visible_employment_type": visible_employment,
        "component_details": component_details,
        "date_posted": date_posted,
        "date_posted_raw": date_posted_raw,
        "date_updated": None,
        "valid_through": valid_through,
        "valid_through_raw": valid_through_raw,
        "employment_type": posting.get("employmentType"),
        "workplace_type": "Remote" if telecommute else None,
        "telecommute": telecommute,
        "applicant_location_requirements": posting.get("applicantLocationRequirements"),
        "locations": locations,
        "employer": employer_name,
        "employer_raw": employer,
        "salary": salary,
        "sections": sections,
        "responsibilities": responsibilities,
        "qualifications": qualifications,
        "apply": apply,
        "jsonld": posting_without_description,
        "jsonld_description_sha256": sha256(description_html.encode()).hexdigest(),
        "description_normalized_chars": len(description_text),
        "description_fnv1a64": fnv1a64(description_text),
        "issues": list(dict.fromkeys(issues)),
    }


def _challenge_response(response) -> bool:
    if response.status_code == 202:
        return True
    return "challenge" in (response.headers.get("x-amzn-waf-action", "") or "").lower()


class PagerDuty(Adapter):
    """Official PagerDuty careers listing, parsed from captured rendered pages."""

    VERIFICATION_REVISION = "pagerduty.official-phenom.20261108.1"

    def __init__(self, http, company, source, options, *, capture: dict | None = None):
        super().__init__(http, company, source, options)
        self.capture = capture or {}

    @staticmethod
    def _capture_pages(capture: dict) -> list[dict]:
        pages = capture.get("listing_pages") or []
        if isinstance(pages, dict):
            pages = [pages[key] for key in sorted(pages, key=lambda value: int(value))]
        return sorted(pages, key=lambda value: int(value.get("page") or 0))

    @staticmethod
    def _capture_details(capture: dict) -> dict[str, dict]:
        details = capture.get("details") or {}
        if isinstance(details, list):
            return {item["url"]: item for item in details if item.get("url")}
        return details

    async def _http_capture(self) -> dict:
        if self.http is None:
            raise RuntimeError("PagerDuty has no HTTP client and no captured rendered pages")
        response = await self.http.get(LISTING_URL)
        if _challenge_response(response):
            raise RuntimeError(
                "PagerDuty listing returned an AWS WAF challenge; browser rendering is required"
            )
        first = parse_listing_page(response.text, str(response.url), requested_page=1)
        pages = [{"page": 1, "url": str(response.url), "html": response.text, "status": response.status_code}]
        for page_no in range(2, (first.get("pages") or 1) + 1):
            page_response = await self.http.get(f"{LISTING_URL}?page={page_no}")
            if _challenge_response(page_response):
                raise RuntimeError(f"PagerDuty listing page {page_no} returned an AWS WAF challenge")
            pages.append({"page": page_no, "url": str(page_response.url), "html": page_response.text,
                          "status": page_response.status_code})
        details = {}
        for row in first["rows"]:
            detail_response = await self.http.get(row["url"])
            if _challenge_response(detail_response):
                raise RuntimeError(f"PagerDuty detail {row['url']} returned an AWS WAF challenge")
            details[row["url"]] = {"url": row["url"], "html": detail_response.text,
                                   "status": detail_response.status_code,
                                   "final_url": str(detail_response.url)}
        return {"listing_pages": pages, "details": details,
                "timings": {"http_fallback_used": True}}

    async def run(self):
        started = time.perf_counter()
        capture = self.capture or await self._http_capture()
        listing_started = time.perf_counter()
        parsed_pages = []
        for page in self._capture_pages(capture):
            parsed_pages.append(parse_listing_page(
                page["html"], page.get("final_url") or page.get("url") or LISTING_URL,
                requested_page=int(page["page"]) if page.get("page") is not None else None,
            ))
        listing_parse_seconds = max(0.0, time.perf_counter() - listing_started)

        if not parsed_pages:
            self.result.errors.append("pagerduty_listing_capture_missing")
            self.result.listing_complete = False
            self.result.metadata["waf_access"] = capture.get("waf_access", {})
            return self.result

        first = parsed_pages[0]
        totals = {page["total"] for page in parsed_pages if page["total"] is not None}
        total = next(iter(totals)) if len(totals) == 1 else None
        self.result.expected_total = total
        self.result.pages = len(parsed_pages)
        page_numbers = [page["page"] for page in parsed_pages]
        expected_pages = math.ceil(total / PAGE_SIZE) if total else None
        listing_complete = (
            total is not None
            and expected_pages is not None
            and page_numbers == list(range(1, expected_pages + 1))
        )
        for page in parsed_pages:
            if page["issues"]:
                self.result.errors.extend(page["issues"])
                listing_complete = False
        rows = [row for page in parsed_pages for row in page["rows"]]
        unique_urls = list(dict.fromkeys(row["url"] for row in rows))
        if total is not None and len(unique_urls) != total:
            self.result.errors.append("pagerduty_listing_unique_url_count_mismatch")
            listing_complete = False

        details = self._capture_details(capture)
        jobs = []
        detail_parse_seconds = 0.0
        visible_matches = 0
        for index, row in enumerate(rows):
            detail = details.get(row["url"])
            if detail is None:
                self.result.errors.append(f"pagerduty_detail_capture_missing:{row['url']}")
                listing_complete = False
                continue
            detail_started = time.perf_counter()
            try:
                parsed = parse_job_detail(
                    detail.get("html", ""), detail.get("final_url") or row["url"], row
                )
            except Exception as exc:
                self.result.errors.append(f"PagerDuty detail {row['url']}: {type(exc).__name__}: {exc}")
                listing_complete = False
                detail_parse_seconds += max(0.0, time.perf_counter() - detail_started)
                continue
            detail_parse_seconds += max(0.0, time.perf_counter() - detail_started)
            if parsed["visible_description_matches"]:
                visible_matches += 1
            if parsed["issues"]:
                self.result.warnings.extend(f"{row['url']}:{issue}" for issue in parsed["issues"])
            job = self._make_job(row, parsed, detail, index)
            jobs.append(job)
            self.add(job)
        if total is not None and len(jobs) != total:
            self.result.errors.append("pagerduty_retained_rows_do_not_match_company_listing_count")
            listing_complete = False
        identities = [job.job_id for job in jobs]
        if len(identities) != len(set(identities)):
            self.result.errors.append("pagerduty_duplicate_job_identifiers")
            listing_complete = False

        self.result.listing_complete = bool(listing_complete)
        membership = {
            "listing_url": LISTING_URL,
            "pages_fetched": page_numbers,
            "page_size": PAGE_SIZE,
            "advertised_total": total,
            "expected_pages": expected_pages,
            "listing_rows": len(rows),
            "unique_job_urls": len(unique_urls),
            "jobs_collected": len(jobs),
            "unique_job_ids": len(set(identities)),
            "listing_pages": [
                {key: value for key, value in page.items() if key != "rows"} for page in parsed_pages
            ],
            "facet_sums": {
                key: sum(value.values()) for key, value in first["facets"].items()
            },
            "filters_applied": False,
            "pagination": "one-based ?page=N; 30 rows per page; last page displays 31-43 of 43",
            "complete": bool(listing_complete and len(jobs) == total),
        }
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_careers_origin": CAREERS_ORIGIN,
            "listing_url": LISTING_URL,
            "listing_authority": "PagerDuty's own unfiltered Phenom /jobs/search listing.",
            "membership_reconciliation": membership,
            "source_contract": {
                "listing_method": "Rendered HTML from the company's own /jobs/search?page=N pages.",
                "detail_method": "Rendered company /jobs/<slug> page; one schema.org JobPosting JSON-LD object.",
                "description_field": "JobPosting.description",
                "identifier_field": "JobPosting.identifier.value",
                "date_fields": ["JobPosting.datePosted", "JobPosting.validThrough"],
                "location_field": "JobPosting.jobLocation / jobLocationType",
                "employer_field": "JobPosting.hiringOrganization",
                "apply_route": "Detail page #applycta anchor and Phenom apply form turbo-frame.",
            },
            "date_handling": {
                "date_posted": "JobPosting.datePosted; null if absent/invalid.",
                "valid_through": "JobPosting.validThrough; retained separately, never used as posting date.",
                "date_updated": None,
                "visible_posting_date_present": False,
                "note": "No visible posting date or separate updated date is published; request time is never used.",
            },
            "salary_handling": {
                "structured_jsonld_baseSalary": False,
                "extracted_from_description_when_published": True,
                "note": "Ranges are retained from employer-authored description text; roles without a published range stay null.",
            },
            "waf_access": capture.get("waf_access", {}),
            "browser_capture": {key: value for key, value in capture.items()
                                if key not in {"listing_pages", "details"}},
            "description_verification": {
                "jsonld_visible_text_matches": visible_matches,
                "jobs_with_visible_match": visible_matches,
                "jobs_checked": len(jobs),
            },
        })
        self.result.timings.update({
            "adapter_parse_wall_seconds": max(0.0, time.perf_counter() - started),
            "listing_parse_seconds": listing_parse_seconds,
            "detail_parse_seconds": detail_parse_seconds,
        })
        if capture.get("timings"):
            for key in ("source_collection_seconds", "detail_collection_seconds"):
                value = capture["timings"].get(key)
                if value is not None:
                    self.result.timings[key] = value
        return self.result

    def _make_job(self, row: dict, parsed: dict, detail: dict, index: int):
        description_html = parsed["description_html"]
        job = self.job(
            {
                "listing": {
                    "index": index,
                    "page_row": row,
                },
                "detail": {
                    "capture": {
                        "status": detail.get("status"),
                        "final_url": detail.get("final_url"),
                        "elapsed_seconds": detail.get("elapsed_seconds"),
                        "html_sha256": detail.get("html_sha256"),
                    },
                    "jsonld": parsed["jsonld"],
                    "visible": {
                        "title": parsed["visible_title"],
                        "location": parsed["visible_location"],
                        "category": parsed["visible_category"],
                        "employment_type": parsed["visible_employment_type"],
                        "component_details": parsed["component_details"],
                        "description_matches_jsonld": parsed["visible_description_matches"],
                        "description_html_sha256": sha256(parsed["visible_description_html"].encode()).hexdigest(),
                    },
                    "sections": parsed["sections"],
                    "apply": parsed["apply"],
                    "dates": {
                        "datePosted": parsed["date_posted_raw"],
                        "validThrough": parsed["valid_through_raw"],
                        "mapping": {
                            "date_posted": "JobPosting.datePosted",
                            "valid_through": "JobPosting.validThrough",
                            "date_updated": None,
                        },
                    },
                    "salary": parsed["salary"],
                    "issues": parsed["issues"],
                },
            },
            title=parsed["title"],
            url=row["url"],
            job_id=parsed["identity"],
            requisition_id=None,
            apply_url=parsed["apply"]["apply_url"],
            description=description_html,
            method=parsed["description_method"],
            description_url=row["url"],
            locations=parsed["locations"],
            department=row.get("category"),
            employment_type=parsed["employment_type"],
            workplace_type=parsed["workplace_type"],
            date_posted=parsed["date_posted"],
            date_posted_raw={"json_ld_datePosted": parsed["date_posted_raw"]},
            date_updated=None,
            valid_through=parsed["valid_through"],
            salary=parsed["salary"],
            qualifications=parsed["qualifications"],
            responsibilities=parsed["responsibilities"],
            employer=parsed["employer"],
            language="en",
        )
        job.description_text = parsed["description_text"]
        job.raw["detail"]["description_normalized_chars"] = parsed["description_normalized_chars"]
        job.raw["detail"]["description_fnv1a64"] = parsed["description_fnv1a64"]
        job.raw["detail"]["description_sha256"] = parsed["jsonld_description_sha256"]
        job.raw["detail"]["jobposting_identifier"] = parsed["identity"]
        job.raw["detail"]["employer_raw"] = parsed["employer_raw"]
        job.raw["detail"]["applicant_location_requirements"] = parsed["applicant_location_requirements"]
        if not parsed["visible_description_matches"]:
            job.warnings.append("description_not_full_field")
        job.company_scope_verified = self.company.scope == "company"
        return job
