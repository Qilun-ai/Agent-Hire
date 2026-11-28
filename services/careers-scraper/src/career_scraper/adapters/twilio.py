"""Twilio's official careers journey: twilio.com/en-us/company/jobs -> jobs.twilio.com PCSX."""
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


OFFICIAL_ENTRY = "https://www.twilio.com/en-us/company/jobs"
PORTAL_ORIGIN = "https://jobs.twilio.com"
LISTING_URL = PORTAL_ORIGIN + "/careers?domain=twilio.com"
SEARCH_API = PORTAL_ORIGIN + "/api/pcsx/search"
DETAIL_API = PORTAL_ORIGIN + "/api/pcsx/position_details"
SITEMAP_INDEX_URL = PORTAL_ORIGIN + "/careers/sitemap_index.xml?domain=twilio.com"
JOBS_SITEMAP_URL = PORTAL_ORIGIN + "/careers/sitemap.xml?domain=twilio.com"
DOMAIN = "twilio.com"
PAGE_SIZE = 10
PORTAL_HOST = "jobs.twilio.com"

BLOCK_TAGS = ("p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5",
              "h6", "table", "blockquote")

HEADINGS = {
    "see yourself at twilio", "about the job", "responsibilities", "qualifications",
    "qualifications:", "required", "required:", "*required", "*required:", "desired", "desired:",
    "compensation", "location", "travel", "what we offer", "what we offer:",
    "who we are & why we’re hiring", "who we are", "equal opportunity employer",
    "application deadline information", "key skills", "the role", "about the role",
    "job summary", "schedule", "subsidiary",
}

RESPONSIBILITY_HEADING = re.compile(
    r"(?i)\b(?:responsibilit(?:y|ies)|what you'?ll do|what you will do|duties|"
    r"in this role)\b"
)
QUALIFICATION_HEADING = re.compile(
    r"(?i)\b(?:qualifications?|requirements?|required|desired|skills|"
    r"what you'?ll bring|who you are|minimum qualifications?)\b"
)
EXPERIENCE_HEADING = re.compile(r"(?i)\bexperience\b")
EDUCATION_HEADING = re.compile(r"(?i)\beducation\b")

PAY_RANGE = re.compile(
    r"(?P<prefix>[^$€£]{0,240}?)(?P<cur>[$€£])\s*"
    r"(?P<min>\d[\d,]*(?:\.\d+)?)(?P<minunit>\s*/\s*(?:hr|hour))?\s*"
    r"(?:\([^)]{0,80}\))?\s*(?:-|–|—|to|--)\s*(?P<cur2>[$€£])?\s*"
    r"(?P<max>\d[\d,]*(?:\.\d+)?)"
)
BONUS_RANGE = re.compile(r"(?i)Target Bonus Percentage\s*[:=]?\s*([\d.]+%)")
SYMBOL_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP"}
GENERIC_PAY_PREFIX = re.compile(
    r"(?i)^(?:the estimated pay|estimated pay|pay range|salary range|base salary|"
    r"compensation|the base salary|the salary)"
)

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
MONTH_RE = (r"(?P<month>January|February|March|April|May|June|July|August|"
            r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|"
            r"Sept|Sep|Oct|Nov|Dec)")


def clean(value) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def canonical_description(value: str | None) -> str:
    """Normalize API HTML and plain JSON-LD text for equality checks."""
    raw = value or ""
    if re.search(r"<\s*(?:p|div|ul|ol|li|strong|b|h[1-6]|br|table|section)\b", raw, re.I):
        text = text_html(raw)
    else:
        text = raw
    text = text.replace("\\-", "-").replace("\\*", "*")
    # JSON-LD renders HTML lists as markdown ("* item", "1. item"); the API HTML
    # carries the same content without list markers. Drop only line-leading
    # markers so the two published representations compare equal.
    text = re.sub(r"(?m)^\s*(?:[*•·]|[-–—])\s+", "", text)
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def verification_key(value: str | None) -> str:
    """Formatting-insensitive key: letters/digits only, case folded."""
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
    match = re.match(r"^\s*(\d{4}-\d{2}-\d{2})", value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


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


def epoch_date(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).date().isoformat()


def clean_string_list(value) -> list[str] | None:
    if not isinstance(value, list):
        return None
    values = [clean(item) for item in value if isinstance(item, str) and clean(item)]
    return values or None


def workplace(value: str | None) -> str | None:
    return {
        "onsite": "On-site",
        "hybrid": "Hybrid",
        "remote_local": "Remote",
        "remote": "Remote",
    }.get((value or "").lower(), value or None)


def parse_official_entry(html: str, response_url: str, redirects: list[str] | None = None) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.hostname != PORTAL_HOST or parts.path.rstrip("/") != "/careers":
        raise ValueError(f"Twilio official careers entry ended outside jobs.twilio.com/careers: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    title = clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "twilio" not in title.lower() or "career" not in title.lower():
        raise ValueError(f"Twilio official careers page title changed: {title!r}")
    group_match = re.search(r'window\._EF_GROUP_ID\s*=\s*"([^"]+)"', html)
    product_match = re.search(r'window\._EF_PRODUCT\s*=\s*"([^"]+)"', html)
    group_id = group_match.group(1) if group_match else None
    product = product_match.group(1) if product_match else None
    if group_id != DOMAIN:
        raise ValueError(f"Twilio careers page no longer targets the twilio.com Eightfold group: {group_id!r}")
    canonical = soup.select_one("link[rel='canonical']")
    canonical_url = canonical.get("href") if canonical else None
    listing_links = []
    for anchor in soup.select("a[href]"):
        target = urljoin(response_url, anchor.get("href", ""))
        target_parts = urlsplit(target)
        if target_parts.hostname == PORTAL_HOST and target_parts.path.rstrip("/") == "/careers":
            listing_links.append(target)
    return {
        "url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "title": title,
        "canonical_url": canonical_url,
        "eightfold_group_id": group_id,
        "eightfold_product": product,
        "domain": DOMAIN,
        "listing_url": LISTING_URL,
        "published_listing_links": list(dict.fromkeys(listing_links))[:20],
        "redirects": list(redirects or []),
    }


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
    locations = clean_string_list(raw.get("locations")) or []
    if not locations:
        issues.append(f"listing_row_missing_locations:{identity_text}")
    expected_path = f"/careers/job/{identity_text}"
    position_url = raw.get("positionUrl")
    if isinstance(position_url, str):
        position_parts = urlsplit(urljoin(PORTAL_ORIGIN, position_url))
        if position_parts.hostname not in (None, PORTAL_HOST) or position_parts.path.rstrip("/") != expected_path:
            issues.append(f"listing_row_position_url_changed:{identity_text}")
    else:
        issues.append(f"listing_row_missing_position_url:{identity_text}")
    display = clean(raw.get("displayJobId"))
    ats = clean(raw.get("atsJobId"))
    if not display:
        issues.append(f"listing_row_missing_requisition:{identity_text}")
    posted_ts = None if raw.get("postedTs") in (0, "0", None) else epoch_seconds(
        raw.get("postedTs"), issues, f"postedTs:{identity_text}")
    creation_ts = epoch_seconds(raw.get("creationTs"), issues, f"creationTs:{identity_text}")
    row = dict(raw)
    row.update({
        "id": identity_text,
        "id_int": identity,
        "name": name,
        "locations": locations,
        "standardizedLocations": clean_string_list(raw.get("standardizedLocations")) or [],
        "postedTs": posted_ts,
        "creationTs": creation_ts,
        "positionUrl": expected_path,
        "url": PORTAL_ORIGIN + expected_path,
        "displayJobId": display or None,
        "atsJobId": ats or None,
        "department": clean(raw.get("department")) or None,
        "workLocationOption": clean(raw.get("workLocationOption")) or None,
        "locationFlexibility": clean(raw.get("locationFlexibility")) or None,
    })
    return row


def parse_list_page(value, requested_start: int, *, expected_count: int | None = None,
                    page_size: int = PAGE_SIZE) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Twilio PCSX listing response is not an object")
    if value.get("status") != 200:
        raise ValueError(f"Twilio PCSX listing status changed: {value.get('status')}")
    error = value.get("error")
    if isinstance(error, dict) and (error.get("message") or error.get("body")):
        raise ValueError(f"Twilio PCSX listing error: {error}")
    data = value.get("data")
    if not isinstance(data, dict):
        raise ValueError("Twilio PCSX listing response is missing data")
    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("Twilio PCSX listing response is missing its total count")
    positions = data.get("positions")
    if not isinstance(positions, list):
        raise ValueError("Twilio PCSX listing response is missing positions")
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
    if any(row["displayJobId"] is None for row in rows):
        issues.append(f"listing_page_missing_requisitions:{requested_start}")
    return {
        "rows": rows,
        "count": count,
        "start": requested_start,
        "returned": len(positions),
        "page_size": page_size,
        "issues": issues,
        "sort_by": data.get("sortBy"),
        "applied_filters": data.get("appliedFilters") or {},
        "filter_definition": data.get("filterDef") or {},
    }


def parse_sitemap_index(xml: str, response_url: str = SITEMAP_INDEX_URL) -> dict:
    parsed = urlsplit(response_url)
    if parsed.hostname != PORTAL_HOST or parsed.path != "/careers/sitemap_index.xml":
        raise ValueError("Twilio sitemap index redirected outside the company portal")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"Twilio sitemap index is not XML: {exc}") from exc
    locations = [node.text.strip() for node in root.iter()
                 if node.tag.endswith("loc") and node.text and node.text.strip()]
    jobs_urls = [url for url in locations
                 if urlsplit(url).hostname == PORTAL_HOST
                 and urlsplit(url).path == "/careers/sitemap.xml"]
    if len(jobs_urls) != 1:
        raise ValueError(f"Twilio sitemap index exposes {len(jobs_urls)} jobs sitemaps, expected 1")
    return {"url": response_url, "sitemap_urls": locations, "jobs_sitemap_url": jobs_urls[0]}


def parse_jobs_sitemap(xml: str, response_url: str = JOBS_SITEMAP_URL) -> dict:
    parsed = urlsplit(response_url)
    if parsed.hostname != PORTAL_HOST or parsed.path != "/careers/sitemap.xml":
        raise ValueError("Twilio jobs sitemap redirected outside the company portal")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"Twilio jobs sitemap is not XML: {exc}") from exc
    if not root.tag.endswith("urlset"):
        raise ValueError("Twilio jobs sitemap is not a urlset")
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
        if parts.hostname != PORTAL_HOST or not parts.path.startswith("/careers/job/"):
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
        raise ValueError("Twilio jobs sitemap exposes no job URLs")
    if missing_lastmod:
        issues.append(f"jobs_sitemap_missing_lastmod:{missing_lastmod}")
    return {"url": response_url, "rows": rows, "count": len(rows), "unique": len(seen),
            "issues": list(dict.fromkeys(issues)), "missing_lastmod": missing_lastmod}


def parse_detail(value, requested_id: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Twilio PCSX detail response is not an object")
    if value.get("status") != 200:
        raise ValueError(f"Twilio PCSX detail status changed: {value.get('status')}")
    error = value.get("error")
    if isinstance(error, dict) and (error.get("message") or error.get("body")):
        raise ValueError(f"Twilio PCSX detail error: {error}")
    data = value.get("data")
    if not isinstance(data, dict):
        raise ValueError("Twilio PCSX detail response is missing data")
    identity = str(data.get("id"))
    if identity != str(requested_id):
        raise ValueError(f"Twilio PCSX detail returned a different position: {identity} != {requested_id}")
    name = clean(data.get("name"))
    if not name:
        raise ValueError(f"Twilio PCSX detail has no title for {requested_id}")
    description = data.get("jobDescription")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"Twilio PCSX detail has no jobDescription for {requested_id}")
    issues: list[str] = []
    body = canonical_description(description)
    if len(body) < 100:
        issues.append("description_short")
    locations = clean_string_list(data.get("locations"))
    if not locations:
        issues.append("detail_locations_missing")
    post_dates = clean_string_list(data.get("efcustomTextPostDate")) or []
    post_date = None
    for item in post_dates:
        parsed_date = iso_date(item)
        if parsed_date:
            post_date = parsed_date
            break
    if post_date is None:
        issues.append("detail_post_date_missing")
    creation_ts = epoch_seconds(data.get("creationTs"), issues, "detail_creationTs")
    posted_ts = None if data.get("postedTs") in (0, "0", None) else epoch_seconds(
        data.get("postedTs"), issues, "detail_postedTs")
    expected_path = f"/careers/job/{requested_id}"
    for key in ("publicUrl", "positionUrl"):
        raw_url = data.get(key)
        if isinstance(raw_url, str):
            parts = urlsplit(urljoin(PORTAL_ORIGIN, raw_url))
            if parts.hostname not in (None, PORTAL_HOST) or parts.path.rstrip("/") != expected_path:
                issues.append(f"detail_{key}_changed")
        else:
            issues.append(f"detail_{key}_missing")
    display = clean(data.get("displayJobId"))
    ats = clean(data.get("atsJobId"))
    if not display:
        issues.append("detail_requisition_missing")
    apply_status = ((data.get("positionUserActions") or {}).get("applyAction") or {}).get("status")
    if apply_status != "allowed":
        issues.append(f"apply_action_not_allowed:{apply_status}")
    return {
        "data": data,
        "id": identity,
        "name": name,
        "description": description,
        "description_text": body,
        "description_chars": len(body),
        "locations": locations or [],
        "standardized_locations": clean_string_list(data.get("standardizedLocations")) or [],
        "department": clean(data.get("department")) or None,
        "work_location_option": clean(data.get("workLocationOption")) or None,
        "location_flexibility": clean(data.get("locationFlexibility")) or None,
        "post_dates_raw": post_dates,
        "post_date": post_date,
        "creation_ts": creation_ts,
        "creation_date": epoch_date(creation_ts),
        "posted_ts": posted_ts,
        "display_job_id": display or None,
        "ats_job_id": ats or None,
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
        raise ValueError(f"Twilio job page has {len(postings)} JobPosting schemas, expected 1")
    posting = postings[0]
    title = clean(posting.get("title"))
    if not title:
        raise ValueError("Twilio job page schema has no title")
    url = posting.get("url")
    if not isinstance(url, str) or urlsplit(url).path.rstrip("/") != f"/careers/job/{expected_id}":
        raise ValueError("Twilio job page schema URL does not identify the requested position")
    description = posting.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Twilio job page schema has no description")
    body = canonical_description(description)
    if len(body) < 100:
        raise ValueError("Twilio job page schema description is too short")
    organization = posting.get("hiringOrganization")
    employer = organization.get("name") if isinstance(organization, dict) else None
    page_title = clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
    return {
        "posting": posting,
        "title": title,
        "page_title": page_title,
        "url": url,
        "description": description,
        "description_text": body,
        "description_chars": len(body),
        "description_key": verification_key(description),
        "date_posted": iso_date(posting.get("datePosted")),
        "date_posted_raw": posting.get("datePosted"),
        "valid_through": iso_date(posting.get("validThrough")),
        "valid_through_raw": posting.get("validThrough"),
        "employment_type": posting.get("employmentType"),
        "employer": clean(employer) or None,
        "language": posting.get("inLanguage"),
        "job_location_type": posting.get("jobLocationType"),
        "applicant_location_requirements": posting.get("applicantLocationRequirements"),
        "job_location": posting.get("jobLocation"),
    }


def _heading_value(node: Tag) -> str | None:
    text = clean(node.get_text(" ", strip=True))
    text = re.sub(r"\s+:", ":", text)
    if not text or len(text) > 110:
        return None
    if node.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return text
    if text.lower().rstrip(":") in HEADINGS:
        return text
    strong = node.find(["strong", "b"])
    if strong is not None:
        strong_text = clean(strong.get_text(" ", strip=True))
        if strong_text and len(strong_text) <= 100:
            remainder = text.replace(strong_text, "", 1).strip(" :;")
            if remainder == "":
                return text
    return None


def _iter_blocks(node):
    for child in node.children:
        if isinstance(child, NavigableString):
            text = clean(str(child))
            if text:
                yield "text", text
            continue
        if not isinstance(child, Tag):
            continue
        if child.name in ("script", "style", "noscript"):
            continue
        if child.name in ("ul", "ol"):
            items = [clean(item.get_text(" ", strip=True))
                     for item in child.find_all("li", recursive=False)]
            items = [item for item in items if item]
            if items:
                yield "items", items
            else:
                text = clean(child.get_text(" ", strip=True))
                if text:
                    yield "text", text
            continue
        if child.name in BLOCK_TAGS:
            if child.find(BLOCK_TAGS + ("ul", "ol"), recursive=False):
                yield from _iter_blocks(child)
                continue
            for br in child.find_all("br"):
                br.replace_with("\n")
            heading = _heading_value(child)
            if heading:
                yield "heading", heading
                continue
            lines = child.get_text("\n", strip=True).splitlines()
            for line in lines:
                text = clean(line)
                if text:
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
        if kind == "heading":
            flush()
            current = {"heading": value, "paragraphs": [], "items": []}
        elif kind == "text":
            if value.strip() in (":", "："):
                if current["paragraphs"]:
                    current["paragraphs"][-1] = current["paragraphs"][-1] + value.strip()
                continue
            current["paragraphs"].append(value)
        else:
            current["items"].extend(value)
    flush()
    for section in sections:
        section["full_text"] = "\n".join(
            ([section["heading"] + ":"] if section["heading"] else []) + [section["text"]]
        ).strip()
    return sections


def _first_section_text(sections: list[dict], predicate) -> str | None:
    for section in sections:
        if predicate((section.get("heading") or "").lower()):
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
    summary = _first_section_text(
        sections, lambda h: h in {"see yourself at twilio", "about the job", "the role",
                                  "about the role", "job summary", "who we are & why we’re hiring"}
    )
    location_text = _first_section_text(sections, lambda h: h == "location")
    travel_text = _first_section_text(sections, lambda h: h == "travel")
    benefits_text = _first_section_text(
        sections, lambda h: h in {"what we offer", "what we offer:", "benefits"}
    )
    return {
        "job_summary": summary,
        "responsibilities": "\n\n".join(responsibilities) or None,
        "qualifications": "\n\n".join(qualifications) or None,
        "experience": "\n\n".join(experience) or None,
        "education": "\n\n".join(education) or None,
        "location_section": location_text,
        "travel_section": travel_text,
        "benefits_section": benefits_text,
    }


def _range_location(prefix: str) -> str | None:
    text = clean(prefix)
    index = text.lower().rfind("based in")
    if index < 0:
        return None
    location = text[index + len("based in"):].strip()
    location = re.sub(r"\s*:\s*$", "", location).strip(" ,;:")
    if location.endswith(".") and not location.endswith(("D.C.", "d.c.")):
        location = location[:-1].rstrip()
    return location or None


def parse_salary_ranges(text: str) -> dict | None:
    if not text:
        return None
    collapsed = re.sub(r"\s+", " ", text).strip()
    ranges, seen = [], set()
    for match in PAY_RANGE.finditer(collapsed):
        min_text = match.group("min")
        max_text = match.group("max")
        try:
            minimum = float(min_text.replace(",", ""))
            maximum = float(max_text.replace(",", ""))
        except ValueError:
            continue
        if minimum <= 0 or maximum <= 0 or minimum > maximum:
            continue
        currency = SYMBOL_CURRENCY.get(match.group("cur"))
        second_currency = SYMBOL_CURRENCY.get(match.group("cur2") or match.group("cur"))
        if currency and second_currency and currency != second_currency:
            continue
        after = collapsed[match.end():match.end() + 24]
        hourly = bool(match.group("minunit")) or bool(
            re.search(r"^\s*/\s*(?:hr|hour)", after, re.I))
        unit = "hour" if hourly else "year"
        location = _range_location(match.group("prefix"))
        key = (location, minimum, maximum, unit, currency)
        if key in seen:
            continue
        seen.add(key)
        verbatim = clean(match.group(0)[len(match.group("prefix")):])
        raw = f"Based in {location}: {verbatim}" if location else verbatim
        ranges.append({
            "location": location,
            "min": minimum,
            "max": maximum,
            "min_text": min_text,
            "max_text": max_text,
            "currency": currency,
            "unit": unit,
            "raw": raw,
        })
    if not ranges:
        return None
    labeled = {(item["min"], item["max"], item["unit"], item["currency"])
               for item in ranges if item["location"]}
    ranges = [item for item in ranges
              if item["location"] or (item["min"], item["max"], item["unit"], item["currency"]) not in labeled]
    bonus = BONUS_RANGE.search(collapsed)
    return {
        "ranges": ranges,
        "currency": ranges[0]["currency"],
        "unit": ranges[0]["unit"],
        "target_bonus_percentage": bonus.group(1) if bonus else None,
        "verbatim_lines": [item["raw"] for item in ranges],
        "source_field": "jobDescription compensation text",
    }


def _parse_date_phrase(phrase: str) -> tuple[str | None, bool]:
    """Return (ISO date or None, whether a year was published)."""
    text = clean(phrase)
    if not text:
        return None, False
    numeric = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text)
    if numeric:
        month, day = int(numeric.group(1)), int(numeric.group(2))
        year = int(numeric.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).date().isoformat(), True
        except ValueError:
            return None, True
    month_first = re.search(
        r"(?i)\b(" + r"January|February|March|April|May|June|July|August|"
        r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|"
        r"Sept|Sep|Oct|Nov|Dec" + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?",
        text,
    )
    if month_first:
        month = MONTHS[month_first.group(1).lower()]
        day = int(month_first.group(2))
        year_text = month_first.group(3)
    else:
        day_first = re.search(
            r"(?i)\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + r"January|February|March|"
            r"April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|"
            r"Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec" + r")\.?(?:,?\s+(\d{4}))?",
            text,
        )
        if not day_first:
            return None, False
        day = int(day_first.group(1))
        month = MONTHS[day_first.group(2).lower()]
        year_text = day_first.group(3)
    if year_text:
        try:
            return datetime(int(year_text), month, day).date().isoformat(), True
        except ValueError:
            return None, True
    return None, False


def parse_application_deadline(sections: list[dict], description_text: str = "") -> dict | None:
    section = next((item for item in sections
                    if (item.get("heading") or "").lower().startswith("application deadline")), None)
    heading = section.get("heading") if section else None
    body = clean(section.get("text") or "") if section else ""
    heading_phrase = None
    if heading:
        parenthetical = re.search(r"\(([^)]*)\)", heading)
        if parenthetical and re.search(r"\d", parenthetical.group(1)):
            heading_phrase = clean(parenthetical.group(1))
    body_phrase = None
    evidence = body or None
    if body:
        match = re.search(r"accepted until\s+(.+?)(?:,?\s*but may change|\.?$)", body, re.I)
        if match:
            body_phrase = clean(match.group(1))
    if not body_phrase and description_text:
        collapsed = re.sub(r"\s+", " ", description_text).strip()
        match = re.search(
            r"Applications? for this role[^.]{0,60}?intended to be accepted until\s+"
            r"(.+?)(?:,?\s*but may change|\.(?:\s|$))",
            collapsed, re.I,
        )
        if match:
            body_phrase = clean(match.group(1))
            evidence = clean(match.group(0))
    if not heading and not body_phrase:
        return None
    heading_date, heading_year = _parse_date_phrase(heading_phrase) if heading_phrase else (None, False)
    body_date, body_year = _parse_date_phrase(body_phrase) if body_phrase else (None, False)
    conflict = bool(heading_date and body_date and heading_date != body_date)
    parsed_date = None
    if not conflict:
        if body_date and body_year:
            parsed_date = body_date
        elif heading_date and heading_year:
            parsed_date = heading_date
    return {
        "section_heading": heading,
        "heading_phrase": heading_phrase,
        "body_phrase": body_phrase,
        "heading_date": heading_date,
        "body_date": body_date,
        "parsed_date": parsed_date,
        "conflict": conflict,
        "year_published": bool(body_year or heading_year),
        "section_present": section is not None,
        "raw": evidence,
    }


class Twilio(Adapter):
    """Twilio's own careers site and the Eightfold PCSX listing it publishes."""

    VERIFICATION_REVISION = "twilio.com-eightfold-pcsx-company-listing.1"

    async def _official_entry(self) -> dict:
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY, headers={"Accept": "text/html"})
        seconds = max(0.0, time.perf_counter() - started)
        redirects = [f"{item.url} -> {item.headers.get('location')}" for item in response.history]
        parsed = parse_official_entry(response.text, str(response.url), redirects)
        parsed.update(status=response.status_code, seconds=round(seconds, 6),
                      response_bytes=len(response.content), final_url=str(response.url))
        self.result.timings["official_entry_seconds"] = seconds
        return parsed

    async def _sitemap(self) -> dict:
        started = time.perf_counter()
        if not self.page_guard():
            raise ValueError("Twilio sitemap page budget exhausted")
        index_response = await self.http.get(SITEMAP_INDEX_URL, headers={"Accept": "application/xml"})
        parts = urlsplit(str(index_response.url))
        if parts.hostname != PORTAL_HOST or parts.path != "/careers/sitemap_index.xml":
            raise ValueError("Twilio sitemap index redirected outside the company portal")
        index = parse_sitemap_index(index_response.text, str(index_response.url))
        if not self.page_guard():
            raise ValueError("Twilio jobs sitemap page budget exhausted")
        jobs_response = await self.http.get(index["jobs_sitemap_url"], headers={"Accept": "application/xml"})
        parts = urlsplit(str(jobs_response.url))
        if parts.hostname != PORTAL_HOST or parts.path != "/careers/sitemap.xml":
            raise ValueError("Twilio jobs sitemap redirected outside the company portal")
        jobs = parse_jobs_sitemap(jobs_response.text, str(jobs_response.url))
        seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["sitemap_phase_seconds"] = seconds
        return {
            "index_url": SITEMAP_INDEX_URL,
            "index_final_url": str(index_response.url),
            "index_status": index_response.status_code,
            "sitemap_urls": index["sitemap_urls"],
            "jobs_sitemap_url": index["jobs_sitemap_url"],
            "jobs_sitemap_final_url": str(jobs_response.url),
            "jobs_sitemap_status": jobs_response.status_code,
            "count": jobs["count"],
            "unique": jobs["unique"],
            "rows": jobs["rows"],
            "issues": jobs["issues"],
            "missing_lastmod": jobs["missing_lastmod"],
            "seconds": round(seconds, 6),
        }

    async def _listing(self) -> dict:
        started = time.perf_counter()
        sitemap = await self._sitemap()
        expected = sitemap["count"]
        self.result.expected_total = expected
        sitemap_ids = {row["id"] for row in sitemap["rows"]}
        pages, rows, issues = [], [], []
        start = 0
        while True:
            if not self.page_guard():
                issues.append("listing_api_page_budget_exhausted")
                break
            response = await self.http.get(
                SEARCH_API,
                params={"domain": DOMAIN, "query": "", "location": "", "start": start},
                headers={"Accept": "application/json", "Referer": LISTING_URL},
            )
            parts = urlsplit(str(response.url))
            if parts.hostname != PORTAL_HOST or parts.path != "/api/pcsx/search":
                raise ValueError("Twilio PCSX listing redirected outside its company API")
            page = parse_list_page(response.json(), start, expected_count=expected)
            page_ids = [row["id"] for row in page["rows"]]
            duplicates = [identity for identity in page_ids if identity in {row["id"] for row in rows}]
            if duplicates:
                issues.append(f"listing_duplicate_id_across_pages:{duplicates[:5]}")
            issues.extend(page["issues"])
            pages.append({
                "start": start,
                "count": page["count"],
                "returned": page["returned"],
                "ids": page_ids,
                "issues": page["issues"],
                "sort_by": page["sort_by"],
                "filter_fields": {
                    "smartFilters": [item.get("filterName") for item in
                                     (page["filter_definition"].get("smartFilters") or [])],
                    "allFilters": [item.get("filterName") for item in
                                   (page["filter_definition"].get("allFilters") or [])],
                    "facets": sorted((page["filter_definition"].get("facets") or {}).keys()),
                },
            })
            rows.extend(page["rows"])
            if page["returned"] == 0 or start + page["returned"] >= expected:
                break
            start += PAGE_SIZE
        listing_ids = [row["id"] for row in rows]
        listing_set = set(listing_ids)
        missing_from_listing = sorted(sitemap_ids - listing_set)
        extra_in_listing = sorted(listing_set - sitemap_ids)
        if missing_from_listing:
            issues.append(f"listing_missing_sitemap_ids:{missing_from_listing[:10]}")
        if extra_in_listing:
            issues.append(f"listing_ids_not_in_sitemap:{extra_in_listing[:10]}")
        if len(listing_ids) != expected:
            issues.append(f"listing_rows_total_mismatch:{len(listing_ids)}!={expected}")
        if len(listing_set) != len(listing_ids):
            issues.append(f"listing_duplicate_ids:{len(listing_ids) - len(listing_set)}")
        if any(page["count"] != expected for page in pages):
            issues.append("listing_count_not_stable_across_pages")
        for row in rows:
            job = self.job(
                {"listing": dict(row)},
                title=row["name"], url=row["url"], job_id=row["id"],
                department=row["department"], locations=row["locations"],
                workplace_type=workplace(row["workLocationOption"]),
                employer="Twilio",
            )
            job.raw["listing"] = dict(row)
            job.raw["sitemap"] = dict(next(
                (item for item in sitemap["rows"] if item["id"] == row["id"]), {}
            ))
            self.add(job)
            if self.limit():
                break
        issues = list(dict.fromkeys(issues))
        complete = (
            expected is not None
            and not issues
            and len(rows) == expected
            and len(listing_set) == expected
            and listing_set == sitemap_ids
            and "sample_limit" not in self.result.warnings
        )
        self.result.listing_complete = complete
        if issues:
            self.result.errors.extend(f"twilio_{issue}" for issue in issues)
        seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["listing_phase_seconds"] = seconds
        listing = {
            "url": LISTING_URL,
            "api": SEARCH_API,
            "sitemap_index_url": SITEMAP_INDEX_URL,
            "jobs_sitemap_url": sitemap["jobs_sitemap_url"],
            "params": {"domain": DOMAIN, "query": "", "location": "", "start": "0..last"},
            "count": expected,
            "unique_ids": len(listing_set),
            "short_url_count": sitemap["count"],
            "pages": pages,
            "page_count": len(pages),
            "page_size": PAGE_SIZE,
            "issues": issues,
            "seconds": round(seconds, 6),
            "sitemap": {key: sitemap[key] for key in
                        ("index_url", "jobs_sitemap_url", "count", "unique",
                         "issues", "missing_lastmod", "seconds")}
            | {"ids": [row["id"] for row in sitemap["rows"]]},
            "sort_by": pages[0]["sort_by"] if pages else None,
            "applied_filters": {},
            "filters_observed": pages[0]["filter_fields"] if pages else {},
            "first_listed_id": listing_ids[0] if listing_ids else None,
            "last_listed_id": listing_ids[-1] if listing_ids else None,
            "job_ids_in_listing_order": listing_ids,
            "missing_from_listing": missing_from_listing,
            "extra_in_listing": extra_in_listing,
            "counts_per_page": [page["returned"] for page in pages],
            "counts_expected_per_page": [min(PAGE_SIZE, max(0, expected - page["start"]))
                                         for page in pages],
        }
        self.result.metadata["listing"] = listing
        return listing

    def _detail_url(self, position_id: str) -> str:
        return DETAIL_API + "?" + urlencode(
            {"position_id": position_id, "domain": DOMAIN, "hl": "en"}
        )

    async def _detail(self, job):
        api_url = self._detail_url(job.job_id)
        api_started = time.perf_counter()
        response = await self.http.get(
            DETAIL_API,
            params={"position_id": job.job_id, "domain": DOMAIN, "hl": "en"},
            headers={"Accept": "application/json", "Referer": job.url},
        )
        api_seconds = max(0.0, time.perf_counter() - api_started)
        self.result.timings["detail_api_request_seconds"] = (
            self.result.timings.get("detail_api_request_seconds", 0.0) + api_seconds
        )
        parts = urlsplit(str(response.url))
        if parts.hostname != PORTAL_HOST or parts.path != "/api/pcsx/position_details":
            raise ValueError("Twilio PCSX detail request redirected outside its company API")
        parsed = parse_detail(response.json(), job.job_id)
        data = parsed["data"]
        stats = self.result.metadata.setdefault("detail_verification", {
            "api_details_ok": 0, "job_pages_ok": 0, "description_matches": 0,
            "description_mismatches": [], "page_failures": [],
            "apply_allowed": 0, "salary_parsed": 0, "deadline_parsed": 0,
            "deadline_conflicts": [], "title_mismatches": [], "location_mismatches": [],
        })
        stats["api_details_ok"] += 1
        if parsed["apply_status"] == "allowed":
            stats["apply_allowed"] += 1
        for issue in parsed["issues"]:
            job.warnings.append(f"source_{issue}")
            self.result.errors.append(f"{job.job_id}: {issue}")

        listing_snapshot = dict(job.raw.get("listing") or {})
        if listing_snapshot.get("name") and listing_snapshot["name"] != parsed["name"]:
            stats["title_mismatches"].append({
                "id": job.job_id, "listing": listing_snapshot["name"], "detail": parsed["name"],
            })
            job.warnings.append("title_changed_between_listing_and_detail")
        if listing_snapshot.get("locations") and parsed["locations"]:
            if set(listing_snapshot["locations"]) != set(parsed["locations"]):
                stats["location_mismatches"].append({
                    "id": job.job_id,
                    "listing": listing_snapshot["locations"], "detail": parsed["locations"],
                })
                job.warnings.append("locations_changed_between_listing_and_detail")

        parse_started = time.perf_counter()
        sections = parse_description_sections(parsed["description"])
        classified = classify_sections(sections)
        salary = parse_salary_ranges(parsed["description_text"])
        deadline = parse_application_deadline(sections, parsed["description_text"])
        parse_seconds = max(0.0, time.perf_counter() - parse_started)
        self.result.timings["description_parse_seconds"] = (
            self.result.timings.get("description_parse_seconds", 0.0) + parse_seconds
        )
        if salary:
            stats["salary_parsed"] += 1
        if deadline:
            if deadline["parsed_date"]:
                stats["deadline_parsed"] += 1
            if deadline["conflict"]:
                stats["deadline_conflicts"].append(job.job_id)
                job.warnings.append("application_deadline_conflict")

        job.description_html = parsed["description"]
        job.description_text = text_html(parsed["description"])
        job.description_method = "twilio.pcsx.position_details.jobDescription"
        job.description_source_url = api_url
        job.title = parsed["name"] or job.title
        job.requisition_id = parsed["display_job_id"] or job.requisition_id
        job.locations = parsed["locations"] or job.locations
        job.department = parsed["department"] or job.department
        job.workplace_type = workplace(parsed["work_location_option"]) or job.workplace_type
        job.date_posted = parsed["post_date"]
        job.date_posted_raw = {
            "efcustomTextPostDate": parsed["post_dates_raw"],
            "efcustomTextPostDate_iso": parsed["post_date"],
            "creationTs": parsed["creation_ts"],
            "creationTs_iso": parsed["creation_date"],
            "listing_postedTs": listing_snapshot.get("postedTs"),
            "jsonld_datePosted": None,
            "note": "date_posted uses the visible 'Date posted' field; creationTs is the "
                    "Eightfold record creation time and is retained raw only.",
        }
        job.date_updated = None
        job.valid_through = deadline["parsed_date"] if deadline else None
        job.salary = salary
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
             "location_section", "travel_section", "benefits_section")
        }
        job.raw["salary"] = salary
        job.raw["application_deadline"] = deadline
        job.raw["apply_url_method"] = "observed visible Apply Now link /careers/apply?pid=<id>"
        job.raw["source_dates"] = {
            "efcustomTextPostDate": parsed["post_dates_raw"],
            "creationTs": parsed["creation_ts"],
            "creation_date": parsed["creation_date"],
            "postedTs": parsed["posted_ts"],
            "sitemap_lastmod": (job.raw.get("sitemap") or {}).get("lastmod"),
            "jsonld_datePosted": None,
            "jsonld_validThrough": None,
        }

        page_started = time.perf_counter()
        try:
            page_response = await self.http.get(job.url, headers={"Accept": "text/html"})
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
        matches = bool(schema_key) and api_key == schema_key
        if matches:
            stats["description_matches"] += 1
        else:
            stats["description_mismatches"].append(job.job_id)
            job.warnings.extend(["description_not_full_field", "description_mismatch_with_job_page"])
            self.result.errors.append(
                f"{job.job_id}: API jobDescription and job-page JSON-LD descriptions differ"
            )
        if schema["title"] != parsed["name"]:
            stats.setdefault("page_title_mismatches", []).append({
                "id": job.job_id, "detail": parsed["name"], "page": schema["title"],
            })
            job.warnings.append("page_title_differs_from_detail")
        if schema["job_location_type"]:
            job.raw["job_location_type"] = schema["job_location_type"]
        if schema["applicant_location_requirements"]:
            job.raw["applicant_location_requirements"] = schema["applicant_location_requirements"]
        if schema["job_location"]:
            job.raw["job_location"] = schema["job_location"]
        job.raw["job_page"] = {
            key: value for key, value in schema.items()
            if key not in ("description", "description_text", "posting")
        }
        job.raw["job_page"]["posting_keys"] = sorted(schema["posting"].keys())
        job.raw["verification"] = {
            "api_detail_ok": True,
            "job_page_ok": True,
            "page_description_matches_api": matches,
            "api_description_key_chars": len(api_key),
            "page_description_key_chars": len(schema_key),
            "page_description_normalized_chars": len(schema["description_text"]),
            "page_description_normalized_sha256": sha256(
                schema["description_text"].encode()
            ).hexdigest(),
            "page_description_fnv1a64": fnv1a64(schema["description_text"]),
            "page_url": job.url,
            "page_schema_title": schema["title"],
            "page_schema_datePosted": schema["date_posted"],
            "page_schema_validThrough": schema["valid_through"],
            "page_schema_jobLocationType": schema["job_location_type"],
        }
        job.raw["source_dates"].update({
            "jsonld_datePosted": schema["date_posted_raw"],
            "jsonld_validThrough": schema["valid_through_raw"],
            "jsonld_datePosted_iso": schema["date_posted"],
            "jsonld_validThrough_iso": schema["valid_through"],
        })
        job.date_posted_raw["jsonld_datePosted"] = schema["date_posted_raw"]
        job.employer = schema["employer"] or job.employer
        job.language = schema["language"] or job.language
        if not job.employment_type and schema["employment_type"]:
            job.employment_type = schema["employment_type"]
        if schema["employment_type"] and not job.raw.get("employment_type"):
            job.raw["employment_type"] = schema["employment_type"]
        return job

    async def _final_recheck(self, listing: dict) -> dict:
        started = time.perf_counter()
        expected = listing["count"]
        checks = []
        for label, start in (("first", 0), ("last", max(0, expected - 1) // PAGE_SIZE * PAGE_SIZE)):
            response = await self.http.get(
                SEARCH_API,
                params={"domain": DOMAIN, "query": "", "location": "", "start": start},
                headers={"Accept": "application/json", "Referer": LISTING_URL},
            )
            parts = urlsplit(str(response.url))
            if parts.hostname != PORTAL_HOST or parts.path != "/api/pcsx/search":
                raise ValueError("Twilio PCSX final recheck redirected outside its company API")
            page = parse_list_page(response.json(), start)
            checks.append({
                "label": label,
                "start": start,
                "count": page["count"],
                "returned": page["returned"],
                "ids": [row["id"] for row in page["rows"]],
                "issues": page["issues"],
            })
        first = checks[0]
        last = checks[-1]
        expected_first_ids = listing["job_ids_in_listing_order"][:PAGE_SIZE]
        expected_last_start = (max(0, expected - 1) // PAGE_SIZE) * PAGE_SIZE
        expected_last_ids = listing["job_ids_in_listing_order"][expected_last_start:expected_last_start + PAGE_SIZE]
        matches = (
            first["count"] == expected
            and last["count"] == expected
            and first["ids"] == expected_first_ids
            and last["ids"] == expected_last_ids
            and not first["issues"]
            and not last["issues"]
        )
        seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["final_membership_recheck_seconds"] = seconds
        if not matches:
            self.result.listing_complete = False
            self.result.errors.append("twilio_membership_changed_during_run")
        return {
            "matches": matches,
            "expected_count": expected,
            "checks": checks,
            "expected_first_ids": expected_first_ids,
            "expected_last_ids": expected_last_ids,
            "seconds": round(seconds, 6),
        }

    async def run(self):
        entry = await self._official_entry()
        self.result.metadata["official_entry"] = entry
        self.result.metadata["scope_reason"] = (
            "twilio.com/en-us/company/jobs redirects to Twilio's own jobs.twilio.com "
            "careers portal; the rendered listing and the /api/pcsx/search requests it "
            "makes are scoped to the Twilio Eightfold group domain=twilio.com, and every "
            "returned position id is also published in Twilio's own jobs sitemap."
        )
        listing = await self._listing()
        self.result.jobs = await self.hydrate(self.result.jobs, self._detail)
        self.result.metadata["final_membership_recheck"] = await self._final_recheck(listing)
        self.result.metadata["detail_verification"] = self.result.metadata.get("detail_verification", {})
        return self.result
