"""PayPal's official careers journey: careers.pypl.com -> PayPal Eightfold PCSX."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://careers.pypl.com/home/"
PORTAL_ORIGIN = "https://paypal.eightfold.ai"
LISTING_URL = PORTAL_ORIGIN + "/careers?domain=paypal.com"
SEARCH_API = PORTAL_ORIGIN + "/api/pcsx/search"
DETAIL_API = PORTAL_ORIGIN + "/api/pcsx/position_details"
SITEMAP_INDEX_URL = PORTAL_ORIGIN + "/careers/sitemap_index.xml?domain=paypal.com"
JOBS_SITEMAP_URL = PORTAL_ORIGIN + "/careers/sitemap.xml?domain=paypal.com"
DOMAIN = "paypal.com"
PAGE_SIZE = 10
LISTING_ATTEMPTS = 2

BLOCK_TAGS = ("p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote")

SECTION_HEADINGS = {
    "the company", "job summary", "job description", "essential responsibilities",
    "additional responsibilities & preferred qualifications", "additional responsibilities",
    "preferred qualifications", "required qualifications", "minimum qualifications",
    "qualifications", "requirements", "responsibilities", "key responsibilities",
    "main responsibilities", "technical knowledge and skills", "what you'll do",
    "what you will do", "what you'll bring", "what you will bring", "nice to have",
    "skills", "experience", "education", "about the role", "about the team",
    "subsidiary", "travel percent", "primary location | pay range",
    "additional location(s) | pay range", "our benefits", "who we are",
    "commitment to diversity and inclusion", "belonging at paypal",
    "equal employment opportunity", "pay transparency", "compensation", "benefits",
    "why paypal", "who you are", "what we offer", "the role", "the team",
    "job responsibilities", "basic qualifications", "preferred skills",
    "desired qualifications", "working at paypal", "additional information",
    "how to apply", "about paypal", "about us", "the opportunity",
}

RESPONSIBILITY_HEADING = re.compile(
    r"(?i)\b(?:responsibilit(?:y|ies)|what you'?ll do|what you will do|duties|"
    r"job description|main responsibilities|key responsibilities|essential responsibilities)\b"
)
QUALIFICATION_HEADING = re.compile(
    r"(?i)\b(?:qualifications?|requirements?|what you'?ll bring|what you bring|"
    r"nice to have|skills|technical knowledge|preferred qualifications|"
    r"minimum qualifications|basic qualifications|desired qualifications)\b"
)
EXPERIENCE_HEADING = re.compile(r"(?i)\bexperience\b")
EDUCATION_HEADING = re.compile(r"(?i)\beducation\b")

PAY_RANGE = re.compile(
    r"(?i)(?P<location>.{2,190}?)\s*[:;]\s*\(?\s*"
    r"(?:(?P<currency>[A-Z]{3})\s*)?(?P<symbol>US\$|C\$|A\$|S\$|HK\$|[$€£¥])?\s*"
    r"(?P<min>\d[\d,]*(?:\.\d+)?)\s*(?:-|–|—|to)\s*"
    r"(?:(?P<currency2>[A-Z]{3})\s*)?(?P<symbol2>US\$|C\$|A\$|S\$|HK\$|[$€£¥])?\s*"
    r"(?P<max>\d[\d,]*(?:\.\d+)?)\s*\)?\s*"
    r"(?P<unit>annually|annual|per year|/year|hourly|per hour|/hour|monthly|per month|weekly|daily)\b"
)
SYMBOL_CURRENCY = {"$": "USD", "US$": "USD", "C$": "CAD", "A$": "AUD",
                   "S$": "SGD", "HK$": "HKD", "€": "EUR", "£": "GBP", "¥": "JPY"}
UNIT_MAP = {"annually": "year", "annual": "year", "per year": "year", "/year": "year",
            "hourly": "hour", "per hour": "hour", "/hour": "hour",
            "monthly": "month", "per month": "month", "weekly": "week", "daily": "day"}


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def canonical_description(value: str | None) -> str:
    """Normalise API HTML, page JSON-LD text and rendered innerText for comparison."""
    text = text_html(value)
    text = text.replace("\\-", "-").replace("\\*", "*")
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s+|[*•·]\s+|-\s+)", "", text)
    text = re.sub(r"(?<=\s)[*•·]\s+", "", text)
    text = re.sub(r"(?<=\s)-\s+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"&([A-Za-z]);", r"&\1", text)
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def verification_key(value: str | None) -> str:
    """Alphanumeric-only comparison key; tolerates HTML/markdown formatting differences."""
    text = canonical_description(value)
    text = (text.replace("’", "'").replace("‘", "'")
                .replace("“", '"').replace("”", '"'))
    return "".join(char.lower() for char in text if char.isalnum())


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def iso_date(value) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value.strip())
    return match.group(1) if match else None


def epoch_seconds(value, issues: list[str] | None = None, label: str = "timestamp") -> int | None:
    number = None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    if number is None:
        if issues is not None:
            issues.append(f"{label}_missing_or_invalid")
        return None
    if number > 10_000_000_000:
        number //= 1000
    if number < 946684800 or number > 4102444800:  # 2000-01-01 .. 2100-01-01
        if issues is not None:
            issues.append(f"{label}_outside_plausible_range")
        return None
    return number


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != "careers.pypl.com" or not parsed.path.startswith("/home"):
        raise ValueError("PayPal official careers entry redirected away from careers.pypl.com/home")
    soup = BeautifulSoup(html, "lxml")
    title = clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "paypal" not in title.lower() or "career" not in title.lower():
        raise ValueError("PayPal official careers page title changed")
    targets = [anchor.get("href", "") for anchor in soup.select("a[href]")]
    targets += [node.get("data-url", "") for node in soup.select("option[data-url]")]
    links, all_links = [], []
    for value in targets:
        target = urljoin(response_url, value)
        parts = urlsplit(target)
        if parts.hostname != "paypal.eightfold.ai":
            continue
        all_links.append(target)
        if parts.path.rstrip("/") == "/careers":
            links.append(target)
    links = list(dict.fromkeys(links))
    all_links = list(dict.fromkeys(all_links))
    if not links and "paypal.eightfold.ai/careers" not in html:
        raise ValueError("PayPal official careers page no longer publishes its Eightfold job listing")
    return {
        "url": response_url,
        "title": title,
        "listing_url": LISTING_URL,
        "published_listing_links": links[:50],
        "talent_community_url": next(
            (target for target in all_links if urlsplit(target).path.rstrip("/") == "/careers/join"), None
        ),
        "search_form_target": PORTAL_ORIGIN + "/careers" if "paypal.eightfold.ai/careers" in html else None,
    }


def _clean_string_list(value) -> list[str] | None:
    if not isinstance(value, list):
        return None
    values = [clean(item) for item in value if isinstance(item, str) and clean(item)]
    return values or None


def parse_list_row(raw, issues: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        issues.append("listing_row_not_object")
        return None
    identity = raw.get("id")
    if isinstance(identity, str) and identity.isdigit():
        identity = int(identity)
    if isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0:
        issues.append("listing_row_missing_numeric_id")
        return None
    identity_text = str(identity)
    name = clean(raw.get("name"))
    if not name:
        issues.append(f"listing_row_missing_title:{identity_text}")
        return None
    locations = _clean_string_list(raw.get("locations"))
    if not locations:
        issues.append(f"listing_row_missing_locations:{identity_text}")
        locations = []
    standardized = _clean_string_list(raw.get("standardizedLocations")) or []
    posted_ts = epoch_seconds(raw.get("postedTs"), issues, f"postedTs:{identity_text}")
    creation_ts = epoch_seconds(raw.get("creationTs"), issues, f"creationTs:{identity_text}")
    expected_path = f"/careers/job/{identity_text}"
    raw_position = raw.get("positionUrl")
    if isinstance(raw_position, str):
        position_parts = urlsplit(urljoin(PORTAL_ORIGIN, raw_position))
        if position_parts.hostname not in (None, "paypal.eightfold.ai") or position_parts.path.rstrip("/") != expected_path:
            issues.append(f"listing_row_position_url_changed:{identity_text}")
    else:
        issues.append(f"listing_row_missing_position_url:{identity_text}")
    job_url = PORTAL_ORIGIN + expected_path
    display = clean(raw.get("displayJobId"))
    ats = clean(raw.get("atsJobId"))
    if not display:
        issues.append(f"listing_row_missing_requisition:{identity_text}")
    if display and ats and display != ats:
        issues.append(f"listing_row_requisition_mismatch:{identity_text}")
    row = dict(raw)
    row.update({
        "id": identity_text,
        "id_int": identity,
        "name": name,
        "locations": locations,
        "standardizedLocations": standardized,
        "postedTs": posted_ts,
        "creationTs": creation_ts,
        "positionUrl": job_url,
        "url": job_url,
        "department": clean(raw.get("department")) or None,
        "workLocationOption": clean(raw.get("workLocationOption")) or None,
        "locationFlexibility": clean(raw.get("locationFlexibility")) or None,
    })
    return row


def parse_list_page(value, requested_start: int, *, expected_count: int | None = None,
                    page_size: int = PAGE_SIZE) -> dict:
    if not isinstance(value, dict):
        raise ValueError("PayPal PCSX listing response is not an object")
    if value.get("status") != 200:
        raise ValueError(f"PayPal PCSX listing status changed: {value.get('status')}")
    error = value.get("error")
    if isinstance(error, dict) and (error.get("message") or error.get("body")):
        raise ValueError(f"PayPal PCSX listing error: {error}")
    data = value.get("data")
    if not isinstance(data, dict):
        raise ValueError("PayPal PCSX listing response is missing data")
    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("PayPal PCSX listing response is missing its total count")
    positions = data.get("positions")
    if not isinstance(positions, list):
        raise ValueError("PayPal PCSX listing response is missing positions")
    issues: list[str] = []
    if expected_count is not None and count != expected_count:
        issues.append(f"listing_count_changed:{expected_count}->{count}")
    rows, seen = [], set()
    for raw in positions:
        row = parse_list_row(raw, issues)
        if row is None:
            continue
        if row["id"] in seen:
            issues.append(f"listing_duplicate_id_on_page:{row['id']}")
            continue
        seen.add(row["id"])
        rows.append(row)
    expected_rows = min(page_size, max(0, count - requested_start))
    if len(positions) != expected_rows:
        issues.append(f"listing_page_size_mismatch:{requested_start}:{len(positions)}!={expected_rows}")
    if len(rows) != len(positions):
        issues.append(f"listing_page_rows_rejected:{requested_start}:{len(positions) - len(rows)}")
    return {
        "rows": rows,
        "count": count,
        "start": requested_start,
        "returned": len(positions),
        "page_size": page_size,
        "issues": issues,
        "sort_by": data.get("sortBy"),
        "applied_filters": data.get("appliedFilters"),
    }


def parse_detail(value, requested_id: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("PayPal PCSX detail response is not an object")
    if value.get("status") != 200:
        raise ValueError(f"PayPal PCSX detail status changed: {value.get('status')}")
    error = value.get("error")
    if isinstance(error, dict) and (error.get("message") or error.get("body")):
        raise ValueError(f"PayPal PCSX detail error: {error}")
    data = value.get("data")
    if not isinstance(data, dict):
        raise ValueError("PayPal PCSX detail response is missing data")
    identity = str(data.get("id"))
    if identity != str(requested_id):
        raise ValueError(f"PayPal PCSX detail returned a different position: {identity} != {requested_id}")
    name = clean(data.get("name"))
    if not name:
        raise ValueError(f"PayPal PCSX detail has no title for {requested_id}")
    description = data.get("jobDescription")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"PayPal PCSX detail has no jobDescription for {requested_id}")
    issues: list[str] = []
    body = canonical_description(description)
    if len(body) < 100:
        issues.append("description_short")
    locations = _clean_string_list(data.get("locations"))
    if not locations:
        issues.append("detail_locations_missing")
    posted_ts = epoch_seconds(data.get("postedTs"), issues, "postedTs")
    expected_path = f"/careers/job/{requested_id}"
    for key in ("publicUrl", "positionUrl"):
        raw_url = data.get(key)
        if isinstance(raw_url, str):
            parts = urlsplit(urljoin(PORTAL_ORIGIN, raw_url))
            if parts.hostname not in (None, "paypal.eightfold.ai") or parts.path.rstrip("/") != expected_path:
                issues.append(f"detail_{key}_changed")
        else:
            issues.append(f"detail_{key}_missing")
    time_types = _clean_string_list(data.get("efcustomTextTimeType")) or []
    apply_status = ((data.get("positionUserActions") or {}).get("applyAction") or {}).get("status")
    if apply_status != "allowed":
        issues.append(f"apply_action_not_allowed:{apply_status}")
    display = clean(data.get("displayJobId"))
    ats = clean(data.get("atsJobId"))
    if not display:
        issues.append("detail_requisition_missing")
    if display and ats and display != ats:
        issues.append("detail_requisition_mismatch")
    return {
        "data": data,
        "id": identity,
        "name": name,
        "description": description,
        "description_text": body,
        "description_chars": len(body),
        "locations": locations or [],
        "standardized_locations": _clean_string_list(data.get("standardizedLocations")) or [],
        "posted_ts": posted_ts,
        "time_types": time_types,
        "apply_status": apply_status,
        "url": PORTAL_ORIGIN + expected_path,
        "issues": issues,
    }


def parse_job_page(html: str, expected_id: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    postings = []
    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            if not isinstance(item, dict):
                continue
            types = item.get("@type")
            if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
                postings.append(item)
    if len(postings) != 1:
        raise ValueError(f"PayPal job page has {len(postings)} JobPosting schemas, expected 1")
    posting = postings[0]
    title = clean(posting.get("title"))
    if not title:
        raise ValueError("PayPal job page schema has no title")
    url = posting.get("url")
    if not isinstance(url, str) or urlsplit(url).path.rstrip("/") != f"/careers/job/{expected_id}":
        raise ValueError("PayPal job page schema URL does not identify the requested position")
    description = posting.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("PayPal job page schema has no description")
    body = canonical_description(description)
    if len(body) < 100:
        raise ValueError("PayPal job page schema description is too short")
    organization = posting.get("hiringOrganization")
    employer = organization.get("name") if isinstance(organization, dict) else None
    meta_node = soup.select_one('meta[name="description"]')
    meta_description = clean(meta_node.get("content")) if meta_node else None
    return {
        "posting": posting,
        "title": title,
        "url": url,
        "description": description,
        "description_text": body,
        "description_chars": len(body),
        "description_key": verification_key(description),
        "meta_description": meta_description,
        "meta_description_text": canonical_description(meta_description) if meta_description else None,
        "meta_description_chars": len(canonical_description(meta_description)) if meta_description else 0,
        "meta_description_key": verification_key(meta_description) if meta_description else None,
        "date_posted": iso_date(posting.get("datePosted")),
        "valid_through": iso_date(posting.get("validThrough")),
        "employment_type": posting.get("employmentType"),
        "employer": clean(employer) or None,
        "language": posting.get("inLanguage"),
        "job_location_type": posting.get("jobLocationType"),
        "applicant_location_requirements": posting.get("applicantLocationRequirements"),
        "job_locations": posting.get("jobLocation"),
    }


def parse_sitemap_index(xml: str, response_url: str = SITEMAP_INDEX_URL) -> dict:
    parsed = urlsplit(response_url)
    if parsed.hostname != "paypal.eightfold.ai" or parsed.path != "/careers/sitemap_index.xml":
        raise ValueError("PayPal sitemap index redirected outside the company portal")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"PayPal sitemap index is not XML: {exc}") from exc
    locations = [node.text.strip() for node in root.iter()
                 if node.tag.endswith("loc") and node.text and node.text.strip()]
    jobs_urls = [url for url in locations
                 if urlsplit(url).hostname == "paypal.eightfold.ai"
                 and urlsplit(url).path == "/careers/sitemap.xml"]
    if len(jobs_urls) != 1:
        raise ValueError(f"PayPal sitemap index exposes {len(jobs_urls)} jobs sitemaps, expected 1")
    return {"url": response_url, "sitemap_urls": locations, "jobs_sitemap_url": jobs_urls[0]}


def parse_jobs_sitemap(xml: str, response_url: str = JOBS_SITEMAP_URL) -> dict:
    parsed = urlsplit(response_url)
    if parsed.hostname != "paypal.eightfold.ai" or parsed.path != "/careers/sitemap.xml":
        raise ValueError("PayPal jobs sitemap redirected outside the company portal")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"PayPal jobs sitemap is not XML: {exc}") from exc
    if not root.tag.endswith("urlset"):
        raise ValueError("PayPal jobs sitemap is not a urlset")
    rows, seen, issues = [], set(), []
    missing_lastmod = 0
    for node in root.iter():
        if not node.tag.endswith("url"):
            continue
        loc = lastmod = None
        for child in node:
            if child.tag.endswith("loc"):
                loc = (child.text or "").strip()
            elif child.tag.endswith("lastmod"):
                lastmod = (child.text or "").strip()
        if not loc:
            continue
        parts = urlsplit(loc)
        if parts.hostname != "paypal.eightfold.ai" or not parts.path.startswith("/careers/job/"):
            continue
        match = re.match(r"^/careers/job/(\d+)(?:-|$)", parts.path)
        if not match:
            issues.append(f"jobs_sitemap_url_without_numeric_id:{parts.path[:80]}")
            continue
        identity = match.group(1)
        if identity in seen:
            issues.append(f"jobs_sitemap_duplicate_id:{identity}")
            continue
        seen.add(identity)
        if not lastmod:
            missing_lastmod += 1
        rows.append({
            "id": identity,
            "url": f"{PORTAL_ORIGIN}/careers/job/{identity}",
            "sitemap_url": loc,
            "lastmod": lastmod,
            "lastmod_date": iso_date(lastmod),
            "slug": parts.path.rsplit("/", 1)[-1],
        })
    if not rows:
        raise ValueError("PayPal jobs sitemap exposes no job URLs")
    if missing_lastmod:
        issues.append(f"jobs_sitemap_missing_lastmod:{missing_lastmod}")
    return {"url": response_url, "rows": rows, "count": len(rows),
            "unique": len(seen), "issues": list(dict.fromkeys(issues)),
            "missing_lastmod": missing_lastmod}


def _heading(value: str) -> str | None:
    candidate = clean(value).strip().rstrip(":").strip()
    if not candidate or len(candidate) > 110:
        return None
    key = candidate.lower().replace("’", "'")
    if key in SECTION_HEADINGS:
        return candidate
    if value.strip().endswith(":") and len(candidate.split()) <= 8 and not candidate[0].isdigit():
        return candidate
    return None


def _iter_blocks(node):
    for child in node.children:
        if isinstance(child, NavigableString):
            for line in str(child).splitlines():
                text = clean(line)
                if text and not re.fullmatch(r"[:;,\-–—]+", text):
                    yield "text", text
            continue
        if not isinstance(child, Tag):
            continue
        if child.name in ("script", "style", "noscript"):
            continue
        if child.name in ("ul", "ol"):
            items = []
            for item in child.find_all("li", recursive=False):
                text = clean(item.get_text(" ", strip=True))
                if text:
                    items.append(text)
            if items:
                yield "items", items
            else:
                text = clean(child.get_text(" ", strip=True))
                if text:
                    yield "text", text
            continue
        if child.name in BLOCK_TAGS:
            if child.find(BLOCK_TAGS, recursive=False):
                yield from _iter_blocks(child)
            else:
                for line in child.get_text("\n", strip=True).splitlines():
                    text = clean(line)
                    if text and not re.fullmatch(r"[:;,\-–—]+", text):
                        yield "text", text
            continue
        text = clean(child.get_text(" ", strip=True))
        if text:
            yield "text", text


def parse_description_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    body = soup.body or soup
    sections: list[dict] = []
    current = {"heading": None, "paragraphs": [], "items": []}

    def flush():
        if current["heading"] is not None or current["paragraphs"] or current["items"]:
            sections.append({
                "heading": current["heading"],
                "paragraphs": list(current["paragraphs"]),
                "items": list(current["items"]),
                "text": "\n".join(current["paragraphs"] + current["items"]).strip(),
            })

    for kind, value in _iter_blocks(body):
        if kind == "text":
            heading = _heading(value)
            if heading:
                flush()
                current = {"heading": heading, "paragraphs": [], "items": []}
            else:
                current["paragraphs"].append(value)
        else:
            current["items"].extend(value)
    flush()
    for section in sections:
        section["full_text"] = "\n".join(
            ([section["heading"] + ":"] if section["heading"] else []) + [section["text"]]
        ).strip()
    return sections


def parse_salary_ranges(lines: list[str]) -> list[dict] | None:
    ranges, seen = [], set()
    for line in lines:
        match = PAY_RANGE.search(clean(line))
        if not match:
            continue
        key = (match.group("location").strip(), match.group("min"), match.group("max"),
               match.group("unit").lower())
        if key in seen:
            continue
        seen.add(key)
        currency = (match.group("currency") or match.group("currency2") or "").upper()
        symbol = match.group("symbol") or match.group("symbol2") or ""
        currency = currency or SYMBOL_CURRENCY.get(symbol)
        unit = UNIT_MAP.get(match.group("unit").lower(), match.group("unit").lower())
        ranges.append({
            "location": match.group("location").strip(" ;"),
            "min": float(match.group("min").replace(",", "")),
            "max": float(match.group("max").replace(",", "")),
            "min_text": match.group("min"),
            "max_text": match.group("max"),
            "currency": currency,
            "unit": unit,
            "raw": clean(line),
        })
    return ranges or None


def _first_text(sections: list[dict], headings: set[str]) -> str | None:
    for section in sections:
        if (section.get("heading") or "").lower().replace("’", "'") in headings:
            text = section.get("text") or ""
            if text:
                return text
    return None


def classify_sections(sections: list[dict]) -> dict:
    responsibilities, qualifications, experience, education = [], [], [], []
    for section in sections:
        heading = section.get("heading") or ""
        text = section.get("text") or ""
        if not text:
            continue
        if RESPONSIBILITY_HEADING.search(heading):
            responsibilities.append(text)
        if QUALIFICATION_HEADING.search(heading):
            qualifications.append(text)
        if EXPERIENCE_HEADING.search(heading):
            experience.append(text)
        if EDUCATION_HEADING.search(heading):
            education.append(text)
    salary_lines = []
    for section in sections:
        heading = (section.get("heading") or "").lower()
        if "pay range" in heading:
            salary_lines.extend(section.get("paragraphs") or [])
            salary_lines.extend(section.get("items") or [])
    subsidiary = _first_text(sections, {"subsidiary"})
    travel = _first_text(sections, {"travel percent"})
    if travel:
        travel = travel.splitlines()[0].strip()
    travel_value = None
    if travel:
        match = re.search(r"\d+(?:\.\d+)?", travel)
        if match:
            travel_value = float(match.group(0))
            if travel_value.is_integer():
                travel_value = int(travel_value)
    return {
        "job_summary": _first_text(sections, {"job summary"}),
        "responsibilities": "\n\n".join(responsibilities) or None,
        "qualifications": "\n\n".join(qualifications) or None,
        "experience": "\n\n".join(experience) or None,
        "education": "\n\n".join(education) or None,
        "subsidiary": clean(subsidiary) or None if subsidiary else None,
        "travel_percent": travel_value,
        "travel_percent_raw": clean(travel) if travel else None,
        "salary_lines": list(dict.fromkeys(line for line in salary_lines if clean(line))),
        "salary": parse_salary_ranges(salary_lines) if salary_lines else None,
    }


def workplace(value: str | None) -> str | None:
    return {
        "onsite": "On-site",
        "hybrid": "Hybrid",
        "remote_local": "Remote",
        "remote": "Remote",
    }.get((value or "").lower(), value or None)


def epoch_date(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).date().isoformat()


class PayPal(Adapter):
    """PayPal's own careers site and the Eightfold PCSX listing it publishes."""

    VERIFICATION_REVISION = "paypal.pypl-eightfold-pcsx-company-listing.1"

    async def _official_entry(self) -> dict:
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY, headers={"Accept": "text/html"})
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != OFFICIAL_ENTRY:
            raise ValueError(f"PayPal official careers entry redirected to {response.url}")
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=round(seconds, 6),
                      response_bytes=len(response.content), final_url=str(response.url))
        self.result.timings["official_entry_seconds"] = seconds
        return parsed

    async def _sitemap(self) -> dict:
        started = time.perf_counter()
        if not self.page_guard():
            raise ValueError("PayPal sitemap page budget exhausted")
        index_response = await self.http.get(
            SITEMAP_INDEX_URL, headers={"Accept": "application/xml"}
        )
        parts = urlsplit(str(index_response.url))
        if parts.hostname != "paypal.eightfold.ai" or parts.path != "/careers/sitemap_index.xml":
            raise ValueError("PayPal sitemap index redirected outside the company portal")
        index = parse_sitemap_index(index_response.text, str(index_response.url))
        if not self.page_guard():
            raise ValueError("PayPal jobs sitemap page budget exhausted")
        jobs_url = index["jobs_sitemap_url"]
        jobs_response = await self.http.get(
            jobs_url, headers={"Accept": "application/xml"}
        )
        parts = urlsplit(str(jobs_response.url))
        if parts.hostname != "paypal.eightfold.ai" or parts.path != "/careers/sitemap.xml":
            raise ValueError("PayPal jobs sitemap redirected outside the company portal")
        jobs = parse_jobs_sitemap(jobs_response.text, str(jobs_response.url))
        seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["sitemap_phase_seconds"] = seconds
        return {
            "index_url": SITEMAP_INDEX_URL,
            "index_final_url": str(index_response.url),
            "index_status": index_response.status_code,
            "sitemap_urls": index["sitemap_urls"],
            "jobs_sitemap_url": jobs_url,
            "jobs_sitemap_final_url": str(jobs_response.url),
            "jobs_sitemap_status": jobs_response.status_code,
            "count": jobs["count"],
            "unique": jobs["unique"],
            "rows": jobs["rows"],
            "issues": jobs["issues"],
            "missing_lastmod": jobs["missing_lastmod"],
            "seconds": round(seconds, 6),
        }

    async def _listing_api_sample(self, expected: int, sitemap_ids: set[str]) -> dict:
        started = time.perf_counter()
        pages, issues = [], []
        last_start = max(0, expected - 5)
        for label, offset in (("first", 0), ("last", last_start)):
            if not self.page_guard():
                issues.append("listing_api_page_budget_exhausted")
                break
            response = await self.http.get(
                SEARCH_API,
                params={"domain": DOMAIN, "query": "", "location": "",
                        "start": offset, "sort_by": "timestamp"},
                headers={"Accept": "application/json", "Referer": LISTING_URL},
            )
            parts = urlsplit(str(response.url))
            if parts.hostname != "paypal.eightfold.ai" or parts.path != "/api/pcsx/search":
                raise ValueError("PayPal PCSX listing sample redirected outside its company API")
            page = parse_list_page(response.json(), offset, expected_count=expected)
            ids = [row["id"] for row in page["rows"]]
            unknown = [identity for identity in ids if identity not in sitemap_ids]
            if unknown:
                issues.append(f"listing_api_ids_not_in_sitemap:{label}:{unknown}")
            issues.extend(page["issues"])
            pages.append({
                "label": label,
                "start": offset,
                "count": page["count"],
                "returned": page["returned"],
                "ids": ids,
                "issues": page["issues"],
            })
        seconds = max(0.0, time.perf_counter() - started)
        return {
            "pages": pages,
            "issues": list(dict.fromkeys(issues)),
            "first_listed_id": pages[0]["ids"][0] if pages and pages[0]["ids"] else None,
            "last_listed_id": pages[-1]["ids"][-1] if pages and pages[-1]["ids"] else None,
            "seconds": round(seconds, 6),
        }

    async def _listing(self) -> dict:
        started = time.perf_counter()
        sitemap = await self._sitemap()
        expected = sitemap["count"]
        self.result.expected_total = expected
        sitemap_ids = {row["id"] for row in sitemap["rows"]}
        for row in sitemap["rows"]:
            job = self.job(
                row, title="", url=row["url"], job_id=row["id"],
                date_updated=row.get("lastmod_date"),
                employer="PayPal",
            )
            job.raw["sitemap"] = dict(row)
            job.raw["sitemap_lastmod"] = row.get("lastmod")
            self.add(job)
            if self.limit():
                break
        api_sample = None
        api_error = None
        try:
            api_sample = await self._listing_api_sample(expected, sitemap_ids)
        except Exception as exc:
            api_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(f"PayPal listing API sample: {api_error}")
        issues = list(sitemap["issues"])
        if api_sample:
            issues.extend(api_sample["issues"])
            for page in api_sample["pages"]:
                if page["count"] != expected:
                    issues.append(f"listing_api_count_changed:{page['label']}:{expected}->{page['count']}")
        sample_ok = api_sample is not None and not api_sample["issues"]
        sitemap_complete = (
            expected is not None
            and len(sitemap["rows"]) == expected
            and len(sitemap_ids) == expected
            and len(self.result.jobs) == expected
            and "sample_limit" not in self.result.warnings
        )
        self.result.listing_complete = sitemap_complete and sample_ok
        if issues:
            self.result.errors.extend(f"paypal_{issue}" for issue in issues)
        seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["listing_phase_seconds"] = seconds
        pages = [
            {"kind": "sitemap_index", "url": SITEMAP_INDEX_URL,
             "count": len(sitemap["sitemap_urls"]), "ids": []},
            {"kind": "jobs_sitemap", "url": sitemap["jobs_sitemap_url"],
             "count": sitemap["count"], "ids": [row["id"] for row in sitemap["rows"]]},
        ]
        if api_sample:
            pages.extend(api_sample["pages"])
        return {
            "url": LISTING_URL,
            "api": SEARCH_API,
            "sitemap_index_url": SITEMAP_INDEX_URL,
            "jobs_sitemap_url": sitemap["jobs_sitemap_url"],
            "params": {"domain": DOMAIN, "query": "", "location": "",
                       "start": "0 and last page", "sort_by": "timestamp"},
            "count": expected,
            "unique_ids": len(sitemap_ids),
            "short_url_count": len(sitemap["rows"]),
            "pages": pages,
            "page_count": len(pages),
            "page_size": None,
            "issues": issues,
            "seconds": round(seconds, 6),
            "sitemap": {key: sitemap[key] for key in
                        ("index_url", "jobs_sitemap_url", "count", "unique",
                         "issues", "missing_lastmod", "seconds")},
            "api_sample": api_sample,
            "api_error": api_error,
            "first_listed_id": api_sample.get("first_listed_id") if api_sample else None,
            "last_listed_id": api_sample.get("last_listed_id") if api_sample else None,
            "job_ids_in_listing_order": [row["id"] for row in sitemap["rows"]],
        }

    def _detail_url(self, position_id: str) -> str:
        return DETAIL_API + "?" + urlencode(
            {"position_id": position_id, "domain": DOMAIN, "hl": "en"}
        )

    async def _detail(self, job):
        api_url = self._detail_url(job.job_id)
        response = await self.http.get(
            DETAIL_API,
            params={"position_id": job.job_id, "domain": DOMAIN, "hl": "en"},
            headers={"Accept": "application/json", "Referer": job.url},
        )
        parts = urlsplit(str(response.url))
        if parts.hostname != "paypal.eightfold.ai" or parts.path != "/api/pcsx/position_details":
            raise ValueError("PayPal PCSX detail request redirected outside its company API")
        parsed = parse_detail(response.json(), job.job_id)
        data = parsed["data"]
        stats = self.result.metadata.setdefault("detail_verification", {
            "api_details_ok": 0, "job_pages_ok": 0, "description_matches": 0,
            "description_mismatches": [], "page_failures": [],
            "schema_dates": 0, "salary_parsed": 0, "apply_allowed": 0,
        })
        stats["api_details_ok"] += 1
        if parsed["apply_status"] == "allowed":
            stats["apply_allowed"] += 1
        for issue in parsed["issues"]:
            job.warnings.append(f"source_{issue}")
            self.result.errors.append(f"{job.job_id}: {issue}")

        sections = parse_description_sections(parsed["description"])
        classified = classify_sections(sections)
        salary = classified["salary"]
        if salary is None:
            salary = parse_salary_ranges(parsed["description_text"].splitlines())
        if salary:
            stats["salary_parsed"] += 1

        listing_snapshot = dict(job.raw)
        job.description_html = parsed["description"]
        job.description_method = "paypal.pcsx.position_details.jobDescription"
        job.description_source_url = api_url
        job.title = parsed["name"] or job.title
        job.requisition_id = clean(data.get("displayJobId")) or job.requisition_id
        job.locations = parsed["locations"] or job.locations
        job.department = clean(data.get("department")) or job.department
        job.employment_type = parsed["time_types"][0] if parsed["time_types"] else job.employment_type
        job.workplace_type = workplace(data.get("workLocationOption")) or job.workplace_type
        job.date_posted = epoch_date(parsed["posted_ts"]) or job.date_posted
        job.date_posted_raw = {
            "postedTs": parsed["posted_ts"],
            "posted_ts_iso": epoch_date(parsed["posted_ts"]),
            "listing_postedTs": listing_snapshot.get("postedTs"),
            "creationTs": data.get("creationTs"),
            "schema_datePosted": None,
        }
        job.employer = classified["subsidiary"] or "PayPal"
        job.salary = {"ranges": salary, "currency": salary[0].get("currency"),
                      "unit": salary[0].get("unit"),
                      "source_field": "jobDescription.Primary Location | Pay Range",
                      "verbatim_lines": [item["raw"] for item in salary]} if salary else None
        job.qualifications = classified["qualifications"]
        job.responsibilities = classified["responsibilities"]
        job.experience = classified["experience"]
        job.education = classified["education"]
        job.apply_url = f"{PORTAL_ORIGIN}/careers/apply?pid={job.job_id}"
        job.raw["listing"] = listing_snapshot
        job.raw["detail"] = {key: value for key, value in data.items() if key != "jobDescription"}
        job.raw["detail"]["jobDescription_sha256"] = sha256(
            parsed["description"].encode()
        ).hexdigest()
        job.raw["description_sections"] = sections
        job.raw["classified_sections"] = {
            key: classified[key] for key in
            ("job_summary", "responsibilities", "qualifications", "experience", "education",
             "subsidiary", "travel_percent", "travel_percent_raw")
        }
        job.raw["salary_lines"] = classified["salary_lines"]
        job.raw["apply_url_method"] = "observed visible Apply Now route /careers/apply?pid=<id>"
        job.raw["source_dates"] = {
            "postedTs": parsed["posted_ts"],
            "creationTs": data.get("creationTs"),
            "schema_datePosted": None,
            "schema_validThrough": None,
        }

        page_started = time.perf_counter()
        try:
            page_response = await self.http.get(
                job.url, headers={"Accept": "text/html", "Referer": LISTING_URL}
            )
            page_seconds = max(0.0, time.perf_counter() - page_started)
            self.result.timings["job_page_verification_seconds"] = (
                self.result.timings.get("job_page_verification_seconds", 0.0) + page_seconds
            )
            if str(page_response.url) != job.url:
                raise ValueError(f"job page redirected to {page_response.url}")
            schema = parse_job_page(page_response.text, job.job_id)
        except Exception as exc:
            stats["page_failures"].append({"id": job.job_id, "url": job.url,
                                           "reason": f"{type(exc).__name__}: {exc}"})
            job.warnings.extend(["detail_fetch_failed", "job_page_unverified"])
            self.result.errors.append(f"Job page {job.url}: {type(exc).__name__}: {exc}")
            job.raw["verification"] = {
                "api_detail_ok": True, "job_page_ok": False,
                "page_description_matches_api": False,
                "page_error": f"{type(exc).__name__}: {exc}",
            }
            return job

        stats["job_pages_ok"] += 1
        api_key = verification_key(parsed["description"])
        schema_key = schema["description_key"]
        meta_key = schema["meta_description_key"]
        jsonld_matches = api_key == schema_key
        meta_matches = bool(meta_key) and api_key == meta_key
        matches = jsonld_matches or meta_matches
        if matches:
            stats["description_matches"] += 1
            if meta_matches:
                stats["meta_description_matches"] = stats.get("meta_description_matches", 0) + 1
            if not jsonld_matches:
                stats["jsonld_description_differs"] = stats.get("jsonld_description_differs", 0) + 1
        else:
            stats["description_mismatches"].append(job.job_id)
            job.warnings.extend(["description_not_full_field", "description_mismatch_with_job_page"])
            self.result.errors.append(
                f"{job.job_id}: API jobDescription and job-page descriptions differ"
            )
        if schema["date_posted"]:
            stats["schema_dates"] += 1
        job.raw["job_page"] = {key: value for key, value in schema.items()
                               if key not in ("description", "description_text", "posting",
                                              "meta_description_text")}
        job.raw["job_page"]["posting_keys"] = sorted(schema["posting"].keys())
        matched_source = "jsonld" if jsonld_matches else "meta_description" if meta_matches else None
        matched_text = schema["description_text"] if jsonld_matches else schema["meta_description_text"]
        job.raw["verification"] = {
            "api_detail_ok": True,
            "job_page_ok": True,
            "page_description_matches_api": matches,
            "matched_page_description_source": matched_source,
            "jsonld_description_matches_api": jsonld_matches,
            "meta_description_matches_api": meta_matches,
            "api_description_key_chars": len(api_key),
            "jsonld_description_key_chars": len(schema_key),
            "meta_description_key_chars": len(meta_key or ""),
            "page_description_normalized_chars": len(matched_text) if matched_text else 0,
            "page_description_normalized_sha256": sha256(
                (matched_text or "").encode()
            ).hexdigest(),
            "page_url": job.url,
            "page_schema_title": schema["title"],
            "page_schema_datePosted": schema["date_posted"],
            "page_schema_validThrough": schema["valid_through"],
            "page_schema_jobLocationType": schema["job_location_type"],
        }
        job.raw["source_dates"].update({
            "schema_datePosted": schema["date_posted"],
            "schema_validThrough": schema["valid_through"],
        })
        job.date_posted = schema["date_posted"] or job.date_posted
        job.date_posted_raw.update({"schema_datePosted": schema["date_posted"]})
        job.valid_through = schema["valid_through"]
        job.employer = classified["subsidiary"] or schema["employer"] or job.employer
        job.language = schema["language"] or job.language
        if not job.employment_type and schema["employment_type"]:
            job.employment_type = schema["employment_type"]
        if schema["job_location_type"]:
            job.raw["job_location_type"] = schema["job_location_type"]
        if schema["applicant_location_requirements"]:
            job.raw["applicant_location_requirements"] = schema["applicant_location_requirements"]
        return job

    async def run(self):
        entry = await self._official_entry()
        self.result.metadata["official_entry"] = entry
        self.result.metadata["scope_reason"] = (
            "careers.pypl.com is PayPal's own careers site; its published search and "
            "location links target the company-scoped Eightfold portal domain=paypal.com."
        )
        listing = await self._listing()
        self.result.metadata["listing"] = listing
        self.result.jobs = await self.hydrate(self.result.jobs, self._detail)
        self.result.metadata["detail_verification"] = self.result.metadata.get("detail_verification", {})
        return self.result
