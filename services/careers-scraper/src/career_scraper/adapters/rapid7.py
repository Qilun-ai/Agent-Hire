"""Rapid7's official Clinch careers site: unfiltered job listing and JobPosting details.

Observed journey (2026-11-08, this worker):

- ``https://www.rapid7.com/careers/`` (the company's current careers link)
  redirects to ``https://careers.rapid7.com/``, the Clinch Talent-hosted
  Rapid7 careers site.  The ledger entry ``https://careers.rapid7.com/jobs`` is
  now the site's own "Page not found" page; its navigation publishes
  ``https://careers.rapid7.com/jobs/search`` as "Search All Roles".
- The unfiltered listing is server-rendered HTML with 30 rows per page and
  one-based ``?page=N`` pagination.  Rows expose title, requisition identifier,
  department, location(s) and workplace type.  Filters (workplace type,
  department, country, state, city) are visible; none is applied.
- Each ``/jobs/<slug>`` detail page publishes exactly one schema.org
  ``JobPosting`` JSON-LD object with the full employer-authored
  ``description``, ``identifier.value`` (32 hex), ``datePosted``,
  ``validThrough``, ``employmentType``, ``hiringOrganization`` and
  ``jobLocation``.  The visible ``.job-description`` body matches that
  description after whitespace normalization.  The visible requisition
  identifier is separate from the JobPosting identifier.
- Plain httpx requests receive the Clinch/AWS WAF 202 JavaScript challenge
  (``x-amzn-waf-action: challenge``).  A normal headless Chromium executes the
  site's own challenge JS and the public pages render; no CAPTCHA is solved,
  no token is injected and no stealth/automation-evasion option is used.  The
  runner archives every rendered page and this adapter parses that captured
  official source.  A page that does not render is reported, never treated as
  an empty posting list.

The listing is the membership authority.  The sitemap is only a cross-check:
it can contain stale roles and never adds a job to the collection.
"""
from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter


LEDGER_ENTRY = "https://careers.rapid7.com/jobs"
OFFICIAL_ENTRY = "https://www.rapid7.com/careers/"
CAREERS_HOME = "https://careers.rapid7.com/"
LISTING_URL = "https://careers.rapid7.com/jobs/search"
SITEMAP_URL = "https://careers.rapid7.com/sitemap.xml"
HOST = "careers.rapid7.com"
PAGE_SIZE = 30
JOB_PATH = re.compile(r"^/jobs/[a-z0-9][a-z0-9-]*$", re.I)
JOB_UID = re.compile(r"^[0-9a-f]{32}$", re.I)
REQUISITION_ID = re.compile(r"^R\d{3,}$")
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
WAF_MARKERS = (
    "awsWafCookieDomainList",
    "gokuProps",
    "token.awswaf.com",
    "challenge-container",
    "AwsWafIntegration",
)
FILTER_FIELDS = (
    "workplace_type[]",
    "department_uids[]",
    "country_codes[]",
    "states[]",
    "cities[]",
)
RESPONSIBILITY_HEADING = re.compile(
    r"responsib|what you.?ll do|what you will do|key duties|your impact|"
    r"about the role|the role|day to day",
    re.I,
)
QUALIFICATION_HEADING = re.compile(
    r"qualification|requirement|what you.?ll bring|what you will bring|"
    r"skills and qualities|skills|who you are|nice to have|preferred|experience",
    re.I,
)
SALARY_LABEL = re.compile(
    r"(?:^|[.!?]\s+)(?:The\s+)?(?:annual\s+|base\s+|target\s+|hourly\s+)?"
    r"(?:salary|pay|compensation)\s+range\b[^.!?]{0,180}?(?::|\bis\b)",
    re.I,
)
SALARY_RANGE = re.compile(
    r"(?:(?P<label>[A-Z][A-Za-z .&/()-]{1,40}?)\s*:\s*)?"
    r"(?P<cur1>Kč|[$£€])?\s*(?P<min>\d[\d\s.,]*\d|\d)\s*"
    r"(?:-|–|—|to)\s*(?P<cur2>Kč|[$£€])?\s*"
    r"(?P<max>\d[\d\s.,]*\d|\d)\s*"
    r"(?P<code>USD|CAD|GBP|EUR|INR|AUD|PLN|CZK)?",
    re.I,
)
CURRENCY_BY_SYMBOL = {"$": "USD", "£": "GBP", "€": "EUR", "Kč": "CZK"}


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    """Approximate rendered body text for stored/source body comparisons."""
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


def is_waf_challenge(status, text) -> bool:
    """True only for the observed Clinch/AWS WAF JavaScript challenge page."""
    if int(status or 0) != 202:
        return False
    body = text or ""
    return (
        "awsWafCookieDomainList" in body
        and "token.awswaf.com" in body
        and "challenge-container" in body
    )


def waf_challenge_evidence(status, text, headers=None) -> dict:
    headers = headers or {}
    body = text or ""
    return {
        "status": status,
        "markers": [marker for marker in WAF_MARKERS if marker in body],
        "waf_action": headers.get("x-amzn-waf-action") or headers.get("X-Amzn-Waf-Action"),
        "content_type": headers.get("content-type") or headers.get("Content-Type"),
        "bytes": len(body.encode("utf-8")),
    }


def _clean(value) -> str:
    return normalized_title(value if isinstance(value, str) else "")


def _iso_or_none(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        from datetime import datetime

        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return raw


def _validate_listing_url(response_url: str) -> None:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname != HOST or parts.path != "/jobs/search"):
        raise ValueError(f"Rapid7 listing response came from an unexpected URL: {response_url}")
    query = parse_qs(parts.query)
    if set(query) - {"page"}:
        raise ValueError(f"Rapid7 listing response unexpectedly carried filters: {response_url}")
    if "page" in query and (len(query["page"]) != 1 or not query["page"][0].isdigit()):
        raise ValueError(f"Rapid7 listing page parameter is not one-based numeric: {response_url}")


def _parse_filters(soup: BeautifulSoup) -> dict[str, list[dict]]:
    filters: dict[str, list[dict]] = {}
    for field in FILTER_FIELDS:
        options = []
        for checkbox in soup.select(f'input[type="checkbox"][name="{field}"]'):
            label_node = checkbox.find_parent("label")
            label = _clean(label_node.get_text(" ", strip=True) if label_node else "")
            match = re.search(r"^(.*?)\s*\((\d+)\s+items?\)$", label, re.I)
            if not match:
                continue
            name = _clean(match.group(1))
            if not name:
                continue
            options.append({
                "value": checkbox.get("value"),
                "label": name,
                "count": int(match.group(2)),
            })
        if options:
            filters[field] = options
    return filters


def parse_listing_page(html: str, response_url: str = LISTING_URL,
                       requested_page: int | None = None) -> dict:
    """Parse one unfiltered Rapid7 listing page and all exposed facet counts."""
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
        if (parts.scheme != "https" or parts.hostname != HOST
                or not JOB_PATH.fullmatch(parts.path) or parts.query or parts.fragment):
            issues.append(f"rapid7_listing_row_unexpected_job_url:{url}")
            continue
        if title_anchor is not None:
            href = urlsplit(title_anchor.get("href") or "")
            if (href.scheme != parts.scheme or href.hostname != parts.hostname
                    or href.path != parts.path or href.query or href.fragment):
                issues.append(f"rapid7_listing_row_title_href_mismatch:{url}")
        if not title:
            issues.append(f"rapid7_listing_row_missing_title:{url}")
            continue
        if url in seen:
            issues.append(f"rapid7_listing_duplicate_job_url:{url}")
            continue
        seen.add(url)
        requisition_node = tr.select_one(".job-search-results-requisition-identifiers")
        requisition = _clean(requisition_node.get_text(" ", strip=True)) if requisition_node else ""
        departments = [
            _clean(node.get_text(" ", strip=True))
            for node in tr.select(".job-search-results-department li")
        ]
        locations = [
            _clean(node.get_text(" ", strip=True))
            for node in tr.select(".job-search-results-location li")
        ]
        workplace_node = tr.select_one(".job-search-results-workplace-types")
        workplace = _clean(workplace_node.get_text(" ", strip=True)) if workplace_node else ""
        if requisition and not REQUISITION_ID.fullmatch(requisition):
            issues.append(f"rapid7_listing_row_unexpected_requisition:{requisition}:{url}")
        if not requisition:
            issues.append(f"rapid7_listing_row_missing_requisition:{url}")
        if not departments:
            issues.append(f"rapid7_listing_row_missing_department:{url}")
        if not locations:
            issues.append(f"rapid7_listing_row_missing_location:{url}")
        if workplace not in {"Hybrid", "Remote", "On-Site", "Onsite"}:
            issues.append(f"rapid7_listing_row_unexpected_workplace:{workplace}:{url}")
        rows.append({
            "url": url,
            "title": title,
            "requisition_id": requisition or None,
            "departments": departments,
            "department": " / ".join(departments) or None,
            "locations": locations,
            "workplace_type": workplace or None,
            "row_html": str(tr),
        })

    filters = _parse_filters(soup)
    workplace_counts = {item["label"]: item["count"] for item in filters.get("workplace_type[]", [])}
    department_counts = {item["label"]: item["count"] for item in filters.get("department_uids[]", [])}
    total = sum(workplace_counts.values()) if workplace_counts else None
    if total is not None and total < len(rows):
        issues.append("rapid7_listing_workplace_facet_total_below_page_rows")

    page = requested_page
    if page is None:
        query_page = parse_qs(urlsplit(response_url).query).get("page")
        page = int(query_page[0]) if query_page else 1

    pagination = []
    for anchor in soup.select('a[href*="page="]'):
        href = anchor.get("href") or ""
        page_match = re.search(r"(?:^|[?&])page=(\d+)", href)
        if not page_match:
            continue
        pagination.append({
            "text": _clean(anchor.get_text(" ", strip=True)),
            "href": href,
            "rel": anchor.get("rel"),
            "page": int(page_match.group(1)),
        })
    linked_pages = sorted({item["page"] for item in pagination})
    computed_pages = math.ceil(total / PAGE_SIZE) if total else None
    pages = computed_pages
    if pages is None and linked_pages:
        pages = max(linked_pages)
    if pages is not None and linked_pages and linked_pages != list(range(1, pages + 1)):
        issues.append("rapid7_listing_pagination_pages_mismatch")
    if pages is not None and not (1 <= page <= pages):
        issues.append(f"rapid7_listing_page_out_of_range:{page}/{pages}")
    if pages is not None:
        expected_rows = (PAGE_SIZE if page < pages else total - (page - 1) * PAGE_SIZE)
        if expected_rows >= 0 and len(rows) != expected_rows:
            issues.append(
                f"rapid7_listing_page_row_count_mismatch:page={page}:"
                f"expected={expected_rows}:observed={len(rows)}"
            )

    return {
        "url": response_url,
        "page": page,
        "pages": pages,
        "page_size": PAGE_SIZE,
        "total": total,
        "rows": rows,
        "job_urls": [row["url"] for row in rows],
        "pagination": pagination,
        "filters": filters,
        "workplace_counts": workplace_counts,
        "department_counts": department_counts,
        "filters_applied": False,
        "issues": list(dict.fromkeys(issues)),
    }


def parse_sitemap(xml_text: str) -> dict:
    """Parse sitemap URLs as candidates only; never use lastmod as a posting date."""
    pairs = re.findall(
        r"<loc>\s*([^<]+?)\s*</loc>\s*<lastmod>\s*([^<]+?)\s*</lastmod>",
        xml_text or "",
        flags=re.S,
    )
    locs = [loc.strip() for loc, _ in pairs]
    if not locs:
        locs = [loc.strip() for loc in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml_text or "", flags=re.S)]
    job_urls = [url for url in locs if re.match(rf"^https://{re.escape(HOST)}/jobs/", url)]
    return {
        "locs": locs,
        "job_urls": job_urls,
        "non_job_urls": [url for url in locs if url not in job_urls],
        "lastmod": {loc.strip(): date.strip() for loc, date in pairs},
        "lastmod_is_not_posting_date": True,
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
        raise ValueError(
            f"Rapid7 detail must expose exactly one JobPosting JSON-LD object; found {len(found)}"
        )
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
        raise ValueError("Rapid7 JobPosting identifier is missing")
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
    soup = BeautifulSoup(f'<div data-rapid7-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-rapid7-body]")
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
            strong = block.find(["b", "strong"], recursive=False)
            full_text = _clean(block.get_text(" ", strip=True))
            strong_text = _clean(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 140:
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


def _salary_amount(value: str) -> float:
    value = value.replace("\u00a0", "").replace(" ", "")
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        tail = value.rsplit(",", 1)[-1]
        value = value.replace(",", ".") if len(tail) == 2 else value.replace(",", "")
    return float(value)


def extract_salary(html: str) -> dict | None:
    """Retain employer-authored salary-range text and parse its ranges.

    Clinch splits the label and the range into adjacent blocks (often a bare
    text node), so this works on the employer-authored text rather than only
    on <p> descendants.
    """
    text = normalized_body_text(html)
    blocks: list[dict] = []
    for label_match in SALARY_LABEL.finditer(text):
        start = label_match.start()
        if text[start] in ".!?":
            start += 1
        window = text[label_match.end():label_match.end() + 500]
        stop = re.search(r"Salary ranges may vary|This range does not include", window, re.I)
        end = label_match.end() + (stop.start() if stop else len(window))
        raw_text = text[start:end].strip()
        if not raw_text:
            continue
        ranges = []
        for match in SALARY_RANGE.finditer(raw_text):
            if not (match.group("cur1") or match.group("cur2") or match.group("code")):
                continue
            minimum = _salary_amount(match.group("min"))
            maximum = _salary_amount(match.group("max"))
            if maximum < minimum:
                minimum, maximum = maximum, minimum
            code = (match.group("code") or "").upper() or CURRENCY_BY_SYMBOL.get(
                match.group("cur1") or match.group("cur2") or "", ""
            )
            ranges.append({
                "label": _clean(match.group("label")) or None,
                "min": int(minimum) if minimum.is_integer() else minimum,
                "max": int(maximum) if maximum.is_integer() else maximum,
                "currency": code or None,
            })
        if not ranges:
            continue
        blocks.append({
            "raw_text": raw_text,
            "ranges": ranges,
            "period": "hour" if re.search(r"hourly|per\s+hour", raw_text, re.I) else "year",
            "source": "JobPosting.description",
            "field": "employer-published salary range",
        })
    if not blocks:
        return None
    return {"blocks": blocks}


def parse_detail_page(html: str, expected_row: dict, response_url: str) -> dict:
    """Parse one exposed Rapid7 detail page and check it against its listing row."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname != HOST
            or not JOB_PATH.fullmatch(parts.path) or parts.query or parts.fragment):
        raise ValueError(f"Rapid7 detail came from an unexpected URL: {response_url}")
    if response_url != expected_row.get("url"):
        raise ValueError("Rapid7 detail URL does not match its exposed listing row")

    soup = BeautifulSoup(html or "", "lxml")
    posting = _jobposting(soup)
    issues: list[str] = []

    identity = _identifier(posting)
    if not JOB_UID.fullmatch(identity):
        raise ValueError(f"Rapid7 JobPosting identifier is not a 32-hex public id: {identity!r}")
    title = _clean(posting.get("title"))
    visible_title_node = soup.select_one("h3.job-title")
    visible_title = _clean(visible_title_node.get_text(" ", strip=True)) if visible_title_node else ""
    if not title:
        raise ValueError("Rapid7 JobPosting title is missing")
    if visible_title != title:
        raise ValueError(f"Rapid7 visible detail title does not match JobPosting: {visible_title!r} != {title!r}")
    if title != _clean(expected_row.get("title")):
        issues.append("rapid7_detail_title_does_not_match_listing_row")

    description_html = posting.get("description")
    if not isinstance(description_html, str) or len(normalized_body_text(description_html)) < 100:
        raise ValueError("Rapid7 JobPosting description is missing or too short")
    description_text = normalized_body_text(description_html)

    visible_description = soup.select_one(".job-description")
    if visible_description is None:
        raise ValueError("Rapid7 visible .job-description body is missing")
    visible_text = normalized_visible_text(visible_description.get_text(" ", strip=True))
    visible_matches = visible_text == normalized_visible_text(description_text)
    if not visible_matches:
        issues.append("rapid7_visible_description_does_not_match_jobposting_description")

    requisition_node = soup.select_one(".job-component-requisition-identifier")
    requisition = _clean(requisition_node.get_text(" ", strip=True)) if requisition_node else ""
    if requisition != _clean(expected_row.get("requisition_id")):
        issues.append("rapid7_detail_requisition_does_not_match_listing_row")
    if requisition and not REQUISITION_ID.fullmatch(requisition):
        issues.append("rapid7_detail_unexpected_requisition_format")

    visible_locations = [
        _clean(node.get_text(" ", strip=True))
        for node in soup.select(".job-component-list-location li")
    ]
    row_locations = list(expected_row.get("locations") or [])
    if visible_locations != row_locations:
        issues.append("rapid7_detail_locations_do_not_match_listing_row")

    employment_type = _clean(posting.get("employmentType")) or None
    telecommute = _clean(posting.get("jobLocationType")).upper() == "TELECOMMUTE"
    structured_locations = _parse_locations(posting.get("jobLocation"), telecommute)
    employer = _parse_employer(posting.get("hiringOrganization"))
    employer_name = _clean(employer.get("name")) if employer else None
    if employer_name != "Rapid7":
        issues.append(f"rapid7_detail_unexpected_hiring_organization:{employer_name}")
    listing_workplace = _clean(expected_row.get("workplace_type"))
    if listing_workplace == "Remote" and structured_locations and not telecommute:
        issues.append("rapid7_remote_listing_row_without_telecommute_jobposting")
    if listing_workplace == "Hybrid" and telecommute:
        issues.append("rapid7_hybrid_listing_row_marked_telecommute")

    date_posted_raw = posting.get("datePosted")
    valid_through_raw = posting.get("validThrough")
    date_posted = _iso_or_none(date_posted_raw)
    valid_through = _iso_or_none(valid_through_raw)
    if date_posted_raw and date_posted is None:
        issues.append("rapid7_detail_date_posted_not_iso")
    if valid_through_raw and valid_through is None:
        issues.append("rapid7_detail_valid_through_not_iso")

    sections = description_sections(description_html)
    description_soup = BeautifulSoup(description_html, "lxml")
    links = [
        {"text": _clean(anchor.get_text(" ", strip=True)), "url": anchor.get("href")}
        for anchor in description_soup.select("a[href]")
    ]
    attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]

    forms = []
    for form in soup.select('form[action*="/call_to_actions/"]'):
        action = (form.get("action") or "").replace("&amp;", "&")
        parsed = urlsplit(action)
        query = parse_qs(parsed.query)
        button = form.select_one("button")
        button_label = _clean(button.get_text(" ", strip=True)) if button else None
        forms.append({
            "action": action,
            "method": form.get("method"),
            "job_id": query.get("job_id", [None])[0],
            "page_id": query.get("page_id", [None])[0],
            "button_label": button_label,
            "is_application_form": bool(button_label and button_label.lower().startswith("apply")),
        })
    application_forms = [form for form in forms if form["is_application_form"]]
    apply_control = next(
        (
            _clean(button.get_text(" ", strip=True))
            for button in soup.select("button")
            if "apply" in _clean(button.get_text(" ", strip=True)).lower()
        ),
        None,
    )
    if not application_forms:
        issues.append("rapid7_detail_application_form_missing")
    elif application_forms[0]["job_id"] != identity:
        issues.append("rapid7_detail_application_form_job_id_mismatch")

    salary = extract_salary(description_html)
    js_vars_uid = None
    match = re.search(r'js_vars\.job\s*=\s*\{"uid":"([0-9a-f]{32})"\}', html or "", re.I)
    if match:
        js_vars_uid = match.group(1)
        if js_vars_uid != identity:
            issues.append("rapid7_detail_js_vars_job_uid_mismatch")

    return {
        "url": response_url,
        "title": title,
        "visible_title": visible_title,
        "identity": identity,
        "jobposting": posting,
        "identifier": posting.get("identifier"),
        "js_vars_job_uid": js_vars_uid,
        "requisition_id": requisition or None,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(normalized_visible_text(description_text)),
        "description_fnv1a64": fnv1a64(normalized_visible_text(description_text)),
        "visible_description_matches": visible_matches,
        "visible_description_text": visible_text,
        "visible_description_chars": len(visible_text),
        "visible_description_fnv1a64": fnv1a64(visible_text),
        "description_sections": sections,
        "description_links": links,
        "role_attachments": attachments,
        "date_posted": date_posted,
        "date_posted_raw": date_posted_raw,
        "valid_through": valid_through,
        "valid_through_raw": valid_through_raw,
        "date_updated": None,
        "employment_type": employment_type,
        "job_location_type": _clean(posting.get("jobLocationType")) or None,
        "locations_structured": structured_locations,
        "locations_visible": visible_locations,
        "hiring_organization": employer,
        "employer": employer_name,
        "salary": salary,
        "application_forms": forms,
        "application_form": application_forms[0] if application_forms else None,
        "apply_control": apply_control,
        "direct_apply": posting.get("directApply"),
        "issues": issues,
    }


def membership_snapshot(pages: list[dict]) -> dict:
    """Concatenate parsed listing pages and verify page order/count coverage."""
    ordered = sorted(pages, key=lambda item: item["page"])
    rows = [row for page in ordered for row in page["rows"]]
    issues: list[str] = []
    numbers = [page["page"] for page in ordered]
    pages_count = ordered[0].get("pages") if ordered else None
    totals = {page.get("total") for page in ordered if page.get("total") is not None}
    if numbers != list(range(1, (pages_count or len(ordered)) + 1)):
        issues.append("rapid7_listing_page_sequence_incomplete")
    if len(totals) > 1:
        issues.append("rapid7_listing_total_changed_between_pages")
    total = max(totals) if totals else None
    if total is not None and total != len(rows):
        issues.append("rapid7_listing_total_does_not_match_retained_rows")
    urls = [row["url"] for row in rows]
    if len(urls) != len(set(urls)):
        issues.append("rapid7_listing_duplicate_job_url_across_pages")
    for page in ordered:
        issues.extend(page.get("issues") or [])
    return {
        "pages": numbers,
        "pages_count": pages_count,
        "total": total,
        "rows": rows,
        "job_urls": urls,
        "row_counts_by_page": {page["page"]: len(page["rows"]) for page in ordered},
        "filters": ordered[0].get("filters") if ordered else {},
        "workplace_counts": ordered[0].get("workplace_counts") if ordered else {},
        "department_counts": ordered[0].get("department_counts") if ordered else {},
        "pagination": ordered[0].get("pagination") if ordered else [],
        "issues": list(dict.fromkeys(issues)),
    }


class Rapid7(Adapter):
    """Read Rapid7's own unfiltered Clinch listing and its JobPosting details."""

    VERIFICATION_REVISION = "rapid7.clinch-company-listing-jobposting.1"

    def __init__(self, http, company, source, options, *, capture: dict | None = None):
        super().__init__(http, company, source, options)
        self.capture = capture

    async def _http_capture(self):
        response = await self.http.get(LISTING_URL)
        if is_waf_challenge(response.status_code, response.text):
            raise RuntimeError(
                "Rapid7 listing returned the AWS WAF 202 challenge; a normal "
                "browser-rendered capture is required and was not supplied"
            )
        raise RuntimeError(
            "Rapid7 collection requires the browser-rendered official capture; "
            "direct HTTP responses are not used for membership"
        )

    @staticmethod
    def _capture_pages(capture: dict, key: str = "listing_pages") -> list[dict]:
        pages = [
            page for page in capture.get(key) or []
            if isinstance(page, dict) and page.get("html")
        ]
        for page in pages:
            page.setdefault("page", 1)
            page.setdefault("final_url", page.get("url") or LISTING_URL)
        return sorted(pages, key=lambda item: item["page"])

    @staticmethod
    def _capture_details(capture: dict) -> dict[str, dict]:
        return {
            url: detail for url, detail in (capture.get("details") or {}).items()
            if isinstance(detail, dict) and detail.get("html")
        }

    def _unavailable_job(self, row: dict, warning: str):
        job = self.job(
            {"listing_row": row, "detail": None},
            title=row["title"],
            url=row["url"],
            job_id=None,
            requisition_id=row.get("requisition_id"),
            apply_url=row["url"],
            description="",
            method=None,
            description_url=row["url"],
            locations=list(row.get("locations") or []),
            department=row.get("department"),
            employment_type=None,
            workplace_type=row.get("workplace_type"),
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            valid_through=None,
            salary=None,
            employer=None,
            language="en",
        )
        job.description_unavailable = True
        job.warnings.append(warning)
        job.raw["detail_capture"] = warning
        return job

    def _make_job(self, row: dict, detail: dict | None, parsed: dict | None = None):
        if detail is None:
            return self._unavailable_job(row, "detail_capture_missing")
        if parsed is None:
            try:
                parsed = parse_detail_page(detail["html"], row, detail.get("final_url") or row["url"])
            except Exception:
                return self._unavailable_job(row, "detail_parse_failed")
        job = self.job(
            {
                "listing_row": row,
                "detail": {
                    "identity": parsed["identity"],
                    "identifier": parsed["identifier"],
                    "jobposting": parsed["jobposting"],
                    "description_sections": parsed["description_sections"],
                    "description_links": parsed["description_links"],
                    "role_attachments": parsed["role_attachments"],
                    "locations_structured": parsed["locations_structured"],
                    "locations_visible": parsed["locations_visible"],
                    "job_location_type": parsed["job_location_type"],
                    "salary": parsed["salary"],
                    "application_forms": parsed["application_forms"],
                    "application_form": parsed["application_form"],
                    "apply_control": parsed["apply_control"],
                    "direct_apply": parsed["direct_apply"],
                    "visible_description_chars": parsed["visible_description_chars"],
                    "visible_description_fnv1a64": parsed["visible_description_fnv1a64"],
                    "description_normalized_chars": parsed["description_normalized_chars"],
                    "description_fnv1a64": parsed["description_fnv1a64"],
                    "visible_description_matches": parsed["visible_description_matches"],
                    "js_vars_job_uid": parsed["js_vars_job_uid"],
                    "dates": {
                        "datePosted": parsed["date_posted_raw"],
                        "validThrough": parsed["valid_through_raw"],
                        "date_updated": None,
                        "mapping": {
                            "date_posted": "JobPosting.datePosted",
                            "valid_through": "JobPosting.validThrough",
                            "date_updated": "no updated date published",
                        },
                    },
                    "detail_issues": parsed["issues"],
                    "detail_sha256": sha256(detail["html"].encode("utf-8")).hexdigest(),
                    "detail_fetched_at": detail.get("fetched_at"),
                    "detail_waf_document_statuses": detail.get("document_statuses"),
                },
            },
            title=parsed["title"],
            url=row["url"],
            job_id=parsed["identity"],
            requisition_id=parsed["requisition_id"],
            apply_url=row["url"],
            description=parsed["description_html"],
            method="rapid7.clinch.jobposting.description",
            description_url=row["url"],
            locations=list(row.get("locations") or []),
            department=row.get("department"),
            employment_type=parsed["employment_type"],
            workplace_type=row.get("workplace_type"),
            date_posted=parsed["date_posted"],
            date_posted_raw=parsed["date_posted_raw"],
            date_updated=parsed["date_updated"],
            valid_through=parsed["valid_through"],
            salary=parsed["salary"],
            qualifications=_matching_sections(parsed["description_sections"], QUALIFICATION_HEADING),
            responsibilities=_matching_sections(parsed["description_sections"], RESPONSIBILITY_HEADING),
            employer=parsed["employer"],
            language="en",
        )
        job.description_text = parsed["description_text"]
        job.warnings.extend(parsed["issues"])
        job.company_scope_verified = self.company.scope == "company"
        return job

    async def run(self):
        source_started = time.perf_counter()
        try:
            capture = self.capture or await self._http_capture()
        except Exception as exc:
            self.result.errors.append(f"Rapid7 browser capture unavailable: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        for error in capture.get("errors") or []:
            if isinstance(error, dict):
                self.result.errors.append(
                    f"rapid7_browser_capture_error:{error.get('stage')}:{error.get('url')}:{error.get('error')}"
                )
            else:
                self.result.errors.append(f"rapid7_browser_capture_error:{error}")
        initial_pages = self._capture_pages(capture, "listing_pages")
        final_pages = self._capture_pages(capture, "final_listing_pages")
        details = self._capture_details(capture)
        if not initial_pages:
            self.result.errors.append("rapid7_listing_capture_missing")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        parse_started = time.perf_counter()
        parsed_pages = [
            parse_listing_page(page["html"], page.get("final_url") or LISTING_URL,
                               requested_page=page["page"])
            for page in initial_pages
        ]
        initial = membership_snapshot(parsed_pages)
        self.result.expected_total = initial["total"]
        self.result.pages = len(initial_pages)
        self.result.errors.extend(initial["issues"])

        jobs: list = []
        detail_parse_issues: list[str] = []
        for row in initial["rows"]:
            detail = details.get(row["url"])
            parsed = None
            if detail is None:
                detail_parse_issues.append(f"rapid7_detail_capture_missing:{row['url']}")
            else:
                try:
                    parsed = parse_detail_page(
                        detail["html"], row, detail.get("final_url") or row["url"]
                    )
                    detail_parse_issues.extend(parsed["issues"])
                except Exception as exc:
                    detail_parse_issues.append(
                        f"rapid7_detail_parse_failed:{row['url']}:{type(exc).__name__}:{exc}"
                    )
            job = self._make_job(row, detail, parsed)
            self.result.validate_job(job)
            jobs.append(job)

        identities = [job.job_id for job in jobs if job.job_id]
        if len(identities) != len(set(identities)):
            self.result.errors.append("rapid7_duplicate_job_posting_identifiers")
        if len(details) != len(initial["rows"]):
            self.result.errors.append("rapid7_detail_capture_count_does_not_match_listing_rows")
        extra_details = sorted(set(details) - set(initial["job_urls"]))
        if extra_details:
            self.result.errors.append("rapid7_detail_capture_contains_unlisted_jobs")

        final = None
        final_matches = False
        if final_pages:
            final_parsed = [
                parse_listing_page(page["html"], page.get("final_url") or LISTING_URL,
                                   requested_page=page["page"])
                for page in final_pages
            ]
            final = membership_snapshot(final_parsed)
            initial_rows = {row["url"]: row for row in initial["rows"]}
            final_rows = {row["url"]: row for row in final["rows"]}
            changed_fields = {}
            for url in initial["job_urls"]:
                if url not in final_rows:
                    continue
                changed = sorted(
                    key for key in set(initial_rows[url]) | set(final_rows[url])
                    if key != "row_html" and initial_rows[url].get(key) != final_rows[url].get(key)
                )
                if changed:
                    changed_fields[url] = changed
            final_matches = bool(
                initial["job_urls"] == final["job_urls"]
                and initial["total"] == final["total"]
                and initial["row_counts_by_page"] == final["row_counts_by_page"]
                and initial["filters"] == final["filters"]
                and not changed_fields
                and not final["issues"]
            )
            if not initial["job_urls"] == final["job_urls"]:
                self.result.errors.append("rapid7_listing_membership_changed_during_collection")
            if changed_fields:
                self.result.errors.append("rapid7_listing_row_fields_changed_during_collection")
            if initial["total"] != final["total"]:
                self.result.errors.append("rapid7_listing_total_changed_during_collection")
            if initial["filters"] != final["filters"]:
                self.result.errors.append("rapid7_listing_filters_changed_during_collection")
            self.result.errors.extend(final["issues"])
        else:
            self.result.errors.append("rapid7_final_listing_recheck_missing")

        sitemap = None
        if capture.get("sitemap_xml"):
            sitemap = parse_sitemap(capture["sitemap_xml"])
            listed = set(initial["job_urls"])
            candidates = set(sitemap["job_urls"])
            sitemap["in_listing_not_in_sitemap"] = sorted(listed - candidates)
            sitemap["in_sitemap_not_in_listing"] = sorted(candidates - listed)
            sitemap["candidate_count"] = len(candidates)
            sitemap["listing_count"] = len(listed)

        parse_seconds = max(0.0, time.perf_counter() - parse_started)
        self.result.jobs = jobs
        self.result.timings["inline_detail_parse_seconds_sum"] = parse_seconds
        self.result.timings["detail_phase_seconds"] = parse_seconds
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "ledger_entry_url": LEDGER_ENTRY,
            "careers_home_url": CAREERS_HOME,
            "listing_url": LISTING_URL,
            "listing_authority": (
                "Rapid7's own unfiltered /jobs/search listing on careers.rapid7.com; "
                "all pages and rows are the membership authority."
            ),
            "listing_request_shape": {
                "method": "GET",
                "path": "/jobs/search",
                "pagination": "one-based ?page=N",
                "page_size": PAGE_SIZE,
                "filters_applied": False,
                "filter_controls": sorted(initial["filters"]),
                "search_query": None,
            },
            "detail_method": (
                "Each linked /jobs/<slug> detail publishes one schema.org JobPosting "
                "JSON-LD object with the full employer-authored description."
            ),
            "browser_capture": {
                key: value for key, value in capture.items()
                if key not in {"listing_pages", "final_listing_pages", "details", "sitemap_xml"}
            },
            "waf_access": capture.get("waf_access", {}),
            "initial_listing": {
                key: value for key, value in initial.items()
                if key not in {"rows", "job_urls", "filters"}
            },
            "initial_filters": initial["filters"],
            "current_listing": ({
                key: value for key, value in final.items()
                if key not in {"rows", "job_urls", "filters"}
            } if final else None),
            "membership_reconciliation": {
                "initial_ids": initial["job_urls"],
                "final_ids": final["job_urls"] if final else None,
                "initial_count": initial["total"],
                "final_count": final["total"] if final else None,
                "initial_pages": len(initial_pages),
                "final_pages": len(final_pages),
                "same_ids_and_order": bool(final and initial["job_urls"] == final["job_urls"]),
                "same_row_counts": bool(final and initial["row_counts_by_page"] == final["row_counts_by_page"]),
                "same_filters": bool(final and initial["filters"] == final["filters"]),
                "all_row_fields_match": bool(final_matches),
                "new_job_ids": ([url for url in final["job_urls"] if url not in initial["job_urls"]]
                                if final else None),
                "removed_job_ids": ([url for url in initial["job_urls"] if url not in final["job_urls"]]
                                    if final else None),
                "complete": bool(final_matches),
            },
            "detail_capture": {
                "listing_rows": len(initial["rows"]),
                "details_captured": len(details),
                "jobs_built": len(jobs),
                "detail_parse_issues": detail_parse_issues,
                "descriptions_verified": sum(1 for job in jobs if job.description_verified),
            },
            "sitemap_cross_check": sitemap,
            "date_handling": {
                "date_posted": "JobPosting.datePosted only; null when absent",
                "valid_through": "JobPosting.validThrough; never used as a posting date",
                "date_updated": "no updated date published; remains null",
                "request_time_never_used_as_posting_date": True,
            },
            "salary_handling": (
                "Employer-published salary-range blocks from JobPosting.description are "
                "retained verbatim with parsed ranges; no salary is inferred."
            ),
        })
        self.result.errors.extend(detail_parse_issues)
        self.result.listing_complete = bool(
            final_matches
            and len(jobs) == len(initial["rows"]) == initial["total"]
            and all(job.description_verified for job in jobs)
            and not detail_parse_issues
        )
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        self.result.timings["listing_parse_seconds"] = parse_seconds
        return self.result
