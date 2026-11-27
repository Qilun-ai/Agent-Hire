"""PlanetScale's own careers listing and the exact Greenhouse job posts it links."""
from __future__ import annotations

from hashlib import sha256
from html import unescape
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://planetscale.com/careers"
OFFICIAL_HOSTS = {"planetscale.com", "www.planetscale.com"}
BOARD_PAGE_URL = "https://job-boards.greenhouse.io/planetscale"
BOARD_API_URL = "https://boards-api.greenhouse.io/v1/boards/planetscale/jobs?content=true"
DETAIL_BASE = "https://job-boards.greenhouse.io/planetscale/jobs"
DETAIL_HOST = "job-boards.greenhouse.io"
DETAIL_PATH = re.compile(r"^/planetscale/jobs/(\d+)$")
BOARD_PAGE_PATH = "/planetscale"
BOARD_API_PATH = "/v1/boards/planetscale/jobs"
HEADING_TAGS = {"h2", "h3", "h4"}
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|pptx?|xlsx?)(?:[?#]|$)", re.I)
SALARY_LABEL = re.compile(
    r"(?i)\b(?:base\s+salary\s+range|salary\s+range|pay\s+range|compensation\s+range)\b\s*:?\s*"
)
SALARY_RANGE = re.compile(
    r"(?P<us>US\s*)?(?P<dollar1>\$)?\s*(?P<min>\d[\d,]*)\s*"
    r"(?:-|–|—|to)\s*(?P<dollar2>\$)?\s*(?P<max>\d[\d,]*)"
    r"(?:\s*(?P<currency>USD|CAD|EUR|GBP)\b)?",
    re.I,
)
RESPONSIBILITY_MARKERS = (
    "what you'll do", "what you will do", "responsibilit", "job to be done",
    "what you'll edit", "what you will edit", "the role",
)
QUALIFICATION_MARKERS = (
    "what we're looking for", "what you will need", "what you'll need",
    "what else will help", "bonus points", "requirement", "qualification",
    "about you", "attributes best describe", "who you are", "nice to have",
)


def normalized_title(value: str | None) -> str:
    """Normalize only Unicode composition and whitespace; preserve wording."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(value: str | None) -> str:
    value = re.sub(r"\s+([,;:!?])", r"\1", value or "")
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _detail_url(identity: str) -> str:
    return f"{DETAIL_BASE}/{identity}"


def _valid_detail_url(url: str) -> re.Match[str] | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != DETAIL_HOST:
        return None
    match = DETAIL_PATH.fullmatch(parsed.path)
    if match is None or parse_qs(parsed.query):
        return None
    return match


def parse_official_listing(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Read the server-rendered 'Current openings' grid from PlanetScale's page."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc not in OFFICIAL_HOSTS
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("PlanetScale official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    page_title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "PlanetScale" not in page_title or "Careers" not in page_title:
        raise ValueError("PlanetScale official careers page identity changed")
    h1 = next((normalized_title(h.get_text(" ", strip=True)) for h in soup.select("h1")), "")
    if h1 != "Careers at PlanetScale":
        raise ValueError("PlanetScale official careers page heading changed")
    heading = next(
        (node for node in soup.select("h2")
         if normalized_title(node.get_text(" ", strip=True)) == "Current openings"),
        None,
    )
    if heading is None:
        raise ValueError("PlanetScale official careers page no longer publishes 'Current openings'")
    container = heading.find_next_sibling()
    if not isinstance(container, Tag):
        raise ValueError("PlanetScale official careers 'Current openings' block is missing")
    anchors = container.find_all("a", href=True)
    rows, issues, other_links = [], [], []
    for anchor in anchors:
        target = urljoin(response_url, anchor.get("href", ""))
        match = _valid_detail_url(target)
        if match is None:
            other_links.append(target)
            continue
        identity = match.group(1)
        spans = anchor.find_all("span")
        title_node = anchor.select_one("span.font-semibold") or (spans[0] if spans else None)
        location_node = anchor.select_one("span.text-primary") or (spans[-1] if len(spans) > 1 else None)
        title = normalized_title(title_node.get_text(" ", strip=True) if title_node else "")
        location = normalized_title(location_node.get_text(" ", strip=True) if location_node else "")
        if not title or not location:
            issues.append(f"planetscale_official_card_fields_missing:{identity}")
            continue
        rows.append({
            "id": identity,
            "url": target,
            "title": title,
            "location": location,
            "card_html": str(anchor),
        })
    seen, duplicates = set(), []
    for row in rows:
        if row["id"] in seen:
            duplicates.append(row["id"])
        seen.add(row["id"])
    if duplicates:
        issues.append("planetscale_official_listing_duplicate_ids:" + ",".join(sorted(set(duplicates))))
    if other_links:
        issues.append("planetscale_official_listing_non_greenhouse_links:" + ",".join(sorted(set(other_links))))
    if not rows:
        issues.append("planetscale_official_listing_has_no_current_openings")
    main = soup.select_one("main") or soup
    controls = [
        normalized_title(node.get_text(" ", strip=True))
        for node in main.select("button,a")
        if re.search(r"\b(?:load more|show more|next page|previous page|next|previous)\b",
                     normalized_title(node.get_text(" ", strip=True)), re.I)
    ]
    filters = [node.get("name") or node.get("id") or "select" for node in main.select("select")]
    search = bool(main.select('input[type="search"], input[placeholder*="Search" i]'))
    if controls:
        issues.append("planetscale_official_listing_pagination_controls_present")
    if filters or search:
        issues.append("planetscale_official_listing_filter_controls_present")
    return {
        "url": response_url,
        "page_title": page_title,
        "h1": h1,
        "section_heading": "Current openings",
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "count": len(rows),
        "pagination_controls": controls,
        "filter_controls": filters,
        "keyword_search_available": search,
        "other_listing_links": other_links,
        "issues": issues,
    }


def parse_board_page(html: str, response_url: str = BOARD_PAGE_URL) -> dict:
    """Read the company-scoped Greenhouse board page as a secondary membership check."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != DETAIL_HOST
            or parsed.path.rstrip("/") != BOARD_PAGE_PATH):
        raise ValueError("PlanetScale Greenhouse board page came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    page_title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if page_title != "Jobs at PlanetScale":
        raise ValueError("PlanetScale Greenhouse board page identity changed")
    count_node = next(
        (node for node in soup.select("h2")
         if re.fullmatch(r"\d+\s+jobs", normalized_title(node.get_text(" ", strip=True)), re.I)),
        None,
    )
    count_label = normalized_title(count_node.get_text(" ", strip=True)) if count_node else None
    count_value = int(re.match(r"(\d+)", count_label).group(1)) if count_label else None
    rows, issues = [], []
    for anchor in soup.select('a[href*="/planetscale/jobs/"]'):
        target = urljoin(response_url, anchor.get("href", ""))
        match = _valid_detail_url(target)
        if match is None:
            issues.append(f"planetscale_board_page_invalid_job_link:{target}")
            continue
        identity = match.group(1)
        title_node = anchor.select_one("p.body--medium") or anchor.find("p")
        location_node = anchor.select_one("p.body__secondary") or (
            anchor.find_all("p")[1] if len(anchor.find_all("p")) > 1 else None
        )
        title = normalized_title(title_node.get_text(" ", strip=True) if title_node else "")
        location = normalized_title(location_node.get_text(" ", strip=True) if location_node else "")
        if not title:
            issues.append(f"planetscale_board_page_card_title_missing:{identity}")
        rows.append({"id": identity, "url": target, "title": title, "location": location})
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        issues.append("planetscale_board_page_duplicate_ids")
    if count_value is None:
        issues.append("planetscale_board_page_count_label_missing")
    elif count_value != len(ids):
        issues.append("planetscale_board_page_count_mismatch")
    if not rows:
        issues.append("planetscale_board_page_has_no_job_links")
    main = soup.select_one("main") or soup
    pagination = [
        normalized_title(node.get_text(" ", strip=True))
        for node in main.select("button,a")
        if re.search(r"\b(?:load more|next page|previous page|next|previous)\b",
                     normalized_title(node.get_text(" ", strip=True)), re.I)
    ]
    if pagination:
        issues.append("planetscale_board_page_pagination_controls_present")
    return {
        "url": response_url,
        "page_title": page_title,
        "count_label": count_label,
        "count": len(rows),
        "count_value": count_value,
        "rows": rows,
        "job_ids": ids,
        "departments": [
            normalized_title(node.get_text(" ", strip=True))
            for node in soup.select("h3")
            if normalized_title(node.get_text(" ", strip=True))
        ],
        "search_available": bool(soup.select('input[placeholder*="Search" i]')),
        "department_filter_available": bool(soup.select("select, .select__input")),
        "pagination_controls": pagination,
        "issues": issues,
    }


def parse_board_api(payload: dict, request_url: str = BOARD_API_URL) -> dict:
    """Validate the public company-scoped Greenhouse feed and retain every source field."""
    parsed = urlsplit(request_url)
    if (parsed.scheme != "https" or parsed.netloc != "boards-api.greenhouse.io"
            or parsed.path != BOARD_API_PATH or parse_qs(parsed.query) != {"content": ["true"]}):
        raise ValueError("PlanetScale Greenhouse jobs API came from an unexpected URL")
    if not isinstance(payload, dict):
        raise ValueError("PlanetScale Greenhouse jobs API response is not an object")
    jobs, meta = payload.get("jobs"), payload.get("meta")
    if not isinstance(jobs, list) or not isinstance(meta, dict):
        raise ValueError("PlanetScale Greenhouse jobs API response is missing jobs or meta")
    total = meta.get("total")
    if not isinstance(total, int) or total < 0:
        raise ValueError("PlanetScale Greenhouse jobs API meta.total is invalid")
    rows, issues, seen = [], [], set()
    for source_row in jobs:
        if not isinstance(source_row, dict):
            issues.append("planetscale_board_api_row_not_object")
            continue
        identity = str(source_row.get("id", ""))
        if not re.fullmatch(r"\d+", identity):
            issues.append("planetscale_board_api_row_missing_numeric_id")
            continue
        if identity in seen:
            issues.append(f"planetscale_board_api_duplicate_id:{identity}")
            continue
        seen.add(identity)
        source_url = str(source_row.get("absolute_url", ""))
        match = _valid_detail_url(source_url)
        location = source_row.get("location")
        location_name = location.get("name") if isinstance(location, dict) else None
        title = normalized_title(source_row.get("title"))
        content = source_row.get("content")
        departments = source_row.get("departments")
        offices = source_row.get("offices")
        if (match is None or match.group(1) != identity
                or source_row.get("company_name") != "PlanetScale"
                or not title or not isinstance(location_name, str) or not location_name.strip()):
            issues.append(f"planetscale_board_api_row_identity_or_fields_missing:{identity}")
            continue
        if not isinstance(content, str) or len(normalized_body_text(text_html(content))) < 100:
            issues.append(f"planetscale_board_api_description_empty_or_truncated:{identity}")
        if not isinstance(departments, list) or not all(
                isinstance(item, dict) and item.get("name") for item in departments):
            issues.append(f"planetscale_board_api_departments_missing:{identity}")
        if not isinstance(offices, list) or not all(
                isinstance(item, dict) and item.get("name") for item in offices):
            issues.append(f"planetscale_board_api_offices_missing:{identity}")
        if not isinstance(source_row.get("first_published"), str):
            issues.append(f"planetscale_board_api_first_published_missing:{identity}")
        if not isinstance(source_row.get("updated_at"), str):
            issues.append(f"planetscale_board_api_updated_at_missing:{identity}")
        rows.append({
            "id": identity,
            "url": source_url,
            "title": title,
            "location": location_name.strip(),
            "requisition_id": source_row.get("requisition_id"),
            "first_published": source_row.get("first_published"),
            "updated_at": source_row.get("updated_at"),
            "application_deadline": source_row.get("application_deadline"),
            "language": source_row.get("language"),
            "departments": departments,
            "offices": offices,
            "content": content,
            "raw": dict(source_row),
        })
    if total != len(jobs):
        issues.append("planetscale_board_api_meta_total_does_not_match_rows")
    if len(rows) != len(jobs):
        issues.append("planetscale_board_api_retained_rows_do_not_match_response")
    if not rows:
        issues.append("planetscale_board_api_has_no_jobs")
    return {
        "url": request_url,
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "count": len(rows),
        "total": total,
        "meta": meta,
        "issues": list(dict.fromkeys(issues)),
    }


def reconcile_membership(official: dict, board_api: dict, board_page: dict | None) -> dict:
    official_ids = list(official.get("job_ids") or [])
    api_ids = list(board_api.get("job_ids") or [])
    page_ids = list(board_page.get("job_ids") or []) if board_page else []
    official_set, api_set, page_set = set(official_ids), set(api_ids), set(page_ids)
    missing_from_api = [identity for identity in official_ids if identity not in api_set]
    api_only = [identity for identity in api_ids if identity not in official_set]
    missing_from_page = [identity for identity in official_ids if identity not in page_set]
    page_only = [identity for identity in page_ids if identity not in official_set]
    counts_match = (len(official_ids) == len(api_ids) == len(page_ids) == len(official_set))
    sets_match = official_set == api_set == page_set
    complete = bool(
        board_page is not None and counts_match and sets_match
        and len(api_ids) == len(api_set) and len(page_ids) == len(page_set)
        and not missing_from_api and not api_only and not missing_from_page and not page_only
    )
    return {
        "complete": complete,
        "official_count": len(official_ids),
        "board_api_count": len(api_ids),
        "board_page_count": len(page_ids),
        "official_ids_in_order": official_ids,
        "board_api_ids_in_order": api_ids,
        "board_page_ids_in_order": page_ids,
        "official_order_matches_board_api": official_ids == api_ids,
        "official_order_matches_board_page": official_ids == page_ids,
        "missing_from_board_api": missing_from_api,
        "board_api_only": api_only,
        "missing_from_board_page": missing_from_page,
        "board_page_only": page_only,
        "duplicate_official_ids": sorted({x for x in official_ids if official_ids.count(x) > 1}),
        "duplicate_board_api_ids": sorted({x for x in api_ids if api_ids.count(x) > 1}),
        "duplicate_board_page_ids": sorted({x for x in page_ids if page_ids.count(x) > 1}),
    }


def parse_salary(description_text: str) -> dict | None:
    """Keep the published salary range; never invent a currency or amount."""
    text = description_text or ""
    label = SALARY_LABEL.search(text)
    if label is None:
        return None
    tail = text[label.end():label.end() + 180]
    match = SALARY_RANGE.match(tail)
    if match is None:
        return {
            "raw": text[label.start():label.start() + 220].strip(),
            "label": normalized_title(label.group(0)).rstrip(":"),
            "min": None,
            "max": None,
            "currency": None,
            "currency_source": "not_stated",
            "period": None,
            "note": "Salary-range label present but no numeric range parsed",
            "source_heading": "Total Compensation and Pay Transparency"
            if "Total Compensation and Pay Transparency" in text[:label.start()] else None,
        }
    min_value = int(match.group("min").replace(",", ""))
    max_value = int(match.group("max").replace(",", ""))
    currency = (match.group("currency") or "").upper() or None
    currency_source = "explicit_code" if currency else "not_stated"
    if currency is None and match.group("us"):
        currency, currency_source = "USD", "US_prefix"
    raw_end = label.end() + match.end()
    return {
        "raw": text[label.start():raw_end].strip(),
        "label": normalized_title(label.group(0)).rstrip(":"),
        "min": min_value,
        "max": max_value,
        "currency": currency,
        "currency_source": currency_source,
        "amount_symbol": match.group("dollar1") or match.group("dollar2"),
        "period": "year",
        "period_basis": "Employer publishes a base-salary range under Total Compensation; no other period is stated.",
        "source_heading": "Total Compensation and Pay Transparency"
        if "Total Compensation and Pay Transparency" in text[:label.start()] else None,
    }


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-planetscale-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-planetscale-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    if len(blocks) == 1 and blocks[0].name == "div":
        blocks = [node for node in blocks[0].contents if isinstance(node, Tag)]
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in HEADING_TAGS:
            heading = normalized_title(block.get_text(" ", strip=True))
        if heading:
            if current is not None:
                current["text"] = text_html(current["html"])
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    if not sections:
        sections.append({"heading": None, "html": "", "text": ""})
    return sections


def classify_sections(sections: list[dict]) -> tuple[list[dict], list[dict]]:
    responsibilities, qualifications = [], []
    for section in sections:
        heading = (section.get("heading") or "").lower().replace("’", "'").replace("‘", "'")
        if not heading:
            continue
        item = {
            "heading": section["heading"],
            "html": section["html"],
            "text": section["text"],
        }
        if any(marker in heading for marker in RESPONSIBILITY_MARKERS):
            responsibilities.append(item)
        elif any(marker in heading for marker in QUALIFICATION_MARKERS):
            qualifications.append(item)
    return responsibilities, qualifications


def parse_detail_page(html: str, expected_row: dict, expected_url: str) -> dict:
    """Parse one exact company-linked Greenhouse detail and compare it with the feed body."""
    identity = str(expected_row.get("id", ""))
    match = _valid_detail_url(expected_url)
    if match is None or match.group(1) != identity:
        raise ValueError("PlanetScale representative detail URL is not an exact exposed job link")
    soup = BeautifulSoup(html, "lxml")
    page_title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "PlanetScale" not in page_title or "Job Application" not in page_title:
        raise ValueError("PlanetScale detail page identity changed")
    title_node = soup.select_one("h1")
    location_node = soup.select_one(".job__location")
    description_node = soup.select_one(".job__description")
    if title_node is None or location_node is None or description_node is None:
        raise ValueError("PlanetScale detail page is missing its title, location, or description")
    actual_title = normalized_title(title_node.get_text(" ", strip=True))
    actual_location = normalized_title(location_node.get_text(" ", strip=True))
    description_html = description_node.decode_contents()
    description_text = text_html(description_html)
    expected_text = text_html(expected_row.get("content") or "")
    sections = description_sections(description_html)
    responsibilities, qualifications = classify_sections(sections)
    links = [
        {"text": normalized_title(anchor.get_text(" ", strip=True)),
         "url": urljoin(expected_url, anchor.get("href", ""))}
        for anchor in description_node.select("a[href]")
    ]
    attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
    apply_controls = [
        {"tag": node.name, "text": normalized_title(node.get_text(" ", strip=True)),
         "href": urljoin(expected_url, node.get("href")) if node.get("href") else None}
        for node in soup.select("a,button")
        if re.search(r"\bapply\b", normalized_title(node.get_text(" ", strip=True)), re.I)
        or re.search(r"\bapply\b", node.get("href") or "", re.I)
    ]
    form_inside_description = bool(description_node.select_one("form,input,textarea,select"))
    return {
        "url": expected_url,
        "page_title": page_title,
        "title": actual_title,
        "location": actual_location,
        "title_matches": actual_title == normalized_title(expected_row.get("title")),
        "location_matches": actual_location == normalized_title(expected_row.get("location")),
        "description_html": description_html,
        "description_text": description_text,
        "description_matches_api_exact": description_text == expected_text,
        "description_matches_api_normalized": (
            normalized_body_text(description_text) == normalized_body_text(expected_text)
        ),
        "description_html_sha256": sha256(description_html.encode()).hexdigest(),
        "description_text_sha256": sha256(description_text.encode()).hexdigest(),
        "description_normalized_chars": len(normalized_body_text(description_text)),
        "description_fnv1a64": fnv1a64(normalized_body_text(description_text)),
        "description_sections": sections,
        "responsibilities": responsibilities,
        "qualifications": qualifications,
        "description_links": links,
        "role_attachments": attachments,
        "salary": parse_salary(description_text),
        "apply_controls": apply_controls,
        "has_application_form": soup.select_one("form") is not None,
        "application_form_inside_description": form_inside_description,
    }


def _source_text(value: str) -> str:
    """Undo the single HTML-escape layer used by Greenhouse's content field."""
    if re.search(r"&(?:amp;)*lt;(?:p|div|h[1-6]|ul|ol|section|article)\b", value or "", re.I):
        return unescape(value)
    return value or ""


def _changed_fields(initial_rows: list[dict], final_rows: list[dict]) -> dict[str, list[str]]:
    initial = {row["id"]: row for row in initial_rows}
    final = {row["id"]: row for row in final_rows}
    fields = ("title", "location", "requisition_id", "first_published", "updated_at",
              "application_deadline", "departments", "offices", "content")
    changed = {}
    for identity in sorted(set(initial) & set(final)):
        names = [name for name in fields if initial[identity].get(name) != final[identity].get(name)]
        if names:
            changed[identity] = names
    return changed


class PlanetScale(Adapter):
    """PlanetScale's own 12-card careers listing, hydrated from its exact Greenhouse posts."""

    VERIFICATION_REVISION = "planetscale.official-careers+greenhouse.jobs.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.board_by_id: dict[str, dict] = {}

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_listing(response.text, str(response.url))
        parsed.update({
            "status": response.status_code,
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _board_page(self):
        started = time.perf_counter()
        response = await self.http.get(BOARD_PAGE_URL)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_board_page(response.text, str(response.url))
        parsed.update({
            "status": response.status_code,
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _board_api(self):
        started = time.perf_counter()
        response = await self.http.get(BOARD_API_URL)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_board_api(response.json(), str(response.url))
        parsed.update({
            "status": response.status_code,
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    def _make_job(self, card: dict, row: dict, index: int):
        description_html = _source_text(row["content"])
        description_text = text_html(description_html)
        sections = description_sections(description_html)
        responsibilities, qualifications = classify_sections(sections)
        locations = [{"name": row["location"], "source": "greenhouse.location.name"}]
        for office in row["offices"]:
            if not isinstance(office, dict):
                continue
            name = office.get("name")
            if name and all(item.get("name") != name for item in locations):
                locations.append({
                    "name": name,
                    "location": office.get("location"),
                    "source": "greenhouse.offices",
                    "id": office.get("id"),
                })
        departments = [
            department.get("name")
            for department in row["departments"]
            if isinstance(department, dict) and department.get("name")
        ]
        job = self.job(
            {
                "official_listing": card,
                "board_api": row["raw"],
                "source_order": index,
                "employer_context": {
                    "board_company_name": "PlanetScale",
                    "corporate_footer_legal_name": "PlanetScale, Inc.",
                },
            },
            title=card["title"],
            url=card["url"],
            job_id=row["id"],
            requisition_id=row["requisition_id"],
            apply_url=card["url"],
            description=description_html,
            method="planetscale.official_careers_link+greenhouse.job_post.content",
            description_url=card["url"],
            locations=locations,
            department="; ".join(departments) if departments else None,
            employment_type=None,
            workplace_type=None,
            date_posted=row["first_published"],
            date_posted_raw=row["first_published"],
            date_updated=row["updated_at"],
            valid_through=row["application_deadline"] or None,
            salary=parse_salary(description_text),
            qualifications=qualifications or None,
            responsibilities=responsibilities or None,
            employer="PlanetScale",
            language=row["language"],
        )
        job.description_text = description_text
        job.raw["detail"] = {
            "source": "exact Greenhouse detail page",
            "source_url": card["url"],
            "api_content_sha256": sha256(row["content"].encode()).hexdigest(),
            "description_normalized_chars": len(normalized_body_text(description_text)),
            "description_fnv1a64": fnv1a64(normalized_body_text(description_text)),
            "description_sections": sections,
            "salary": parse_salary(description_text),
            "employment_field": "hidden",
            "pay_ranges_field": [],
            "application_questions_excluded": True,
        }
        return job

    async def _detail(self, job):
        started = time.perf_counter()
        response = await self.http.get(job.url)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != job.url:
            raise ValueError(f"PlanetScale detail redirected away from its exact exposed URL: {job.url}")
        row = self.board_by_id[job.job_id]
        parsed = parse_detail_page(response.text, row, job.url)
        if not parsed["title_matches"]:
            raise ValueError(f"PlanetScale detail title differs from the company listing: {job.job_id}")
        if not parsed["location_matches"]:
            raise ValueError(f"PlanetScale detail location differs from the company listing: {job.job_id}")
        if not parsed["description_matches_api_normalized"]:
            raise ValueError(f"PlanetScale detail body differs from the board API content: {job.job_id}")
        if parsed["application_form_inside_description"]:
            raise ValueError(f"PlanetScale application form leaked into the description: {job.job_id}")
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.salary = parsed["salary"]
        job.responsibilities = parsed["responsibilities"] or None
        job.qualifications = parsed["qualifications"] or None
        job.raw["detail"] = {
            "source": "exact Greenhouse detail page",
            "source_url": job.url,
            "http_status": response.status_code,
            "request_seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "title_matches": parsed["title_matches"],
            "location_matches": parsed["location_matches"],
            "api_content_matches_detail_exact": parsed["description_matches_api_exact"],
            "api_content_matches_detail_normalized": parsed["description_matches_api_normalized"],
            "description_html_sha256": parsed["description_html_sha256"],
            "description_text_sha256": parsed["description_text_sha256"],
            "description_normalized_chars": parsed["description_normalized_chars"],
            "description_fnv1a64": parsed["description_fnv1a64"],
            "description_sections": parsed["description_sections"],
            "description_links": parsed["description_links"],
            "role_attachments": parsed["role_attachments"],
            "apply_controls": parsed["apply_controls"],
            "has_application_form": parsed["has_application_form"],
            "application_form_inside_description": False,
            "salary": parsed["salary"],
            "employment_field": "hidden",
            "pay_ranges_field": [],
        }
        return job

    async def run(self):
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_ENTRY,
            "board_page_url": BOARD_PAGE_URL,
            "board_api_url": BOARD_API_URL,
            "listing_authority": (
                "PlanetScale's own server-rendered careers page publishes the exact 12 job cards "
                "and their individual Greenhouse links; that page defines membership."
            ),
            "request_shapes": {
                "official_listing": {"method": "GET", "url": OFFICIAL_ENTRY,
                                     "response": "server-rendered HTML with 12 exact job links"},
                "board_page": {"method": "GET", "url": BOARD_PAGE_URL,
                               "response": "company-scoped Greenhouse board HTML; 12 jobs"},
                "board_api": {"method": "GET", "url": BOARD_API_URL,
                              "response": "company-scoped Greenhouse jobs JSON; content=true"},
                "detail": {"method": "GET", "template": DETAIL_BASE + "/<job_id>",
                           "response": "server-rendered HTML with div.job__description.body"},
            },
            "detail_method": (
                "Fetch each exact company-linked Greenhouse detail URL and require its "
                ".job__description body to match the board API content for that same ID."
            ),
            "listing_filters_applied": False,
            "listing_pagination": "Official page has no filters, search, load-more, or pagination controls.",
            "dates_mapping": {
                "date_posted": "Greenhouse first_published (explicit publication timestamp)",
                "date_updated": "Greenhouse updated_at",
                "valid_through": "application_deadline, null when absent",
                "never_used": "request time, page render time, or template dates",
            },
            "salary_mapping": (
                "Numeric range is parsed only from the employer's explicit 'base salary range' / "
                "'salary range' statement; raw text is retained and currency stays null when not stated."
            ),
            "scope": "Exactly the 12 individual Greenhouse job IDs linked by planetScale.com/careers.",
        })
        initial_started = time.perf_counter()
        try:
            official = await self._official_entry()
            self.result.pages = 1
            board_api = await self._board_api()
            try:
                board_page = await self._board_page()
            except Exception as exc:
                board_page = None
                self.result.errors.append(
                    f"PlanetScale board page request failed: {type(exc).__name__}: {exc}"
                )
        except Exception as exc:
            self.result.errors.append(
                f"PlanetScale source request failed: {type(exc).__name__}: {exc}"
            )
            self.result.listing_complete = False
            self.result.timings["initial_source_collection_seconds"] = max(
                0.0, time.perf_counter() - initial_started
            )
            self.result.metadata["source_collection_wall_seconds"] = self.result.timings[
                "initial_source_collection_seconds"
            ]
            return self.result
        initial_seconds = max(0.0, time.perf_counter() - initial_started)
        self.result.timings["initial_source_collection_seconds"] = initial_seconds

        for issue in official["issues"] + board_api["issues"] + (board_page["issues"] if board_page else []):
            self.result.errors.append(issue)
        reconciliation = reconcile_membership(official, board_api, board_page)
        if not reconciliation["complete"]:
            self.result.errors.append("planetscale_membership_mismatch")
        self.result.expected_total = official["count"]
        self.result.metadata.update({
            "initial_official_listing": {k: v for k, v in official.items() if k != "rows"},
            "initial_official_rows": official["rows"],
            "initial_board_page": {k: v for k, v in board_page.items() if k != "rows"} if board_page else None,
            "initial_board_page_rows": board_page["rows"] if board_page else [],
            "initial_board_api": {k: v for k, v in board_api.items() if k not in ("rows", "meta")},
            "initial_board_api_meta": board_api["meta"],
            "membership_reconciliation": reconciliation,
            "independent_board_page_visible_count": board_page["count_value"] if board_page else None,
            "independent_board_page_job_ids": board_page["job_ids"] if board_page else [],
            "board_api_row_fields": sorted(board_api["rows"][0]["raw"]) if board_api["rows"] else [],
            "official_card_fields": sorted(official["rows"][0]) if official["rows"] else [],
        })

        self.board_by_id = {row["id"]: row for row in board_api["rows"]}
        jobs = []
        for index, card in enumerate(official["rows"], start=1):
            row = self.board_by_id.get(card["id"])
            if row is None:
                self.result.errors.append(f"planetscale_official_job_missing_from_board_api:{card['id']}")
                continue
            if normalized_title(card["title"]) != row["title"]:
                self.result.errors.append(f"planetscale_title_mismatch:{card['id']}")
            if normalized_title(card["location"]) != normalized_title(row["location"]):
                self.result.errors.append(f"planetscale_location_mismatch:{card['id']}")
            jobs.append(self._make_job(card, row, index))
        if len(jobs) != official["count"] or len(jobs) != board_api["count"]:
            self.result.errors.append("planetscale_retained_rows_do_not_match_company_listing_count")
        for job in jobs:
            self.add(job)

        if jobs:
            await self.hydrate(jobs, self._detail)

        detail_failures = [
            {"id": job.job_id, "url": job.url, "warnings": list(job.warnings)}
            for job in self.result.jobs if not job.description_verified
        ]
        if detail_failures:
            self.result.errors.append("planetscale_unverified_descriptions")
        self.result.metadata["detail_failures"] = detail_failures
        self.result.metadata["detail_request_count"] = len(jobs)

        final_started = time.perf_counter()
        final_check = {"membership_complete": False, "source_fields_unchanged": False}
        try:
            final_official = await self._official_entry()
            final_board_api = await self._board_api()
            try:
                final_board_page = await self._board_page()
            except Exception as exc:
                final_board_page = None
                self.result.errors.append(
                    f"PlanetScale final board page check failed: {type(exc).__name__}: {exc}"
                )
            final_reconciliation = reconcile_membership(
                final_official, final_board_api, final_board_page
            )
            changed = _changed_fields(board_api["rows"], final_board_api["rows"])
            initial_ids = set(board_api["job_ids"])
            final_ids = set(final_board_api["job_ids"])
            new_ids = sorted(final_ids - initial_ids)
            removed_ids = sorted(initial_ids - final_ids)
            order_changed = final_official["job_ids"] != official["job_ids"]
            source_fields_unchanged = not changed and not new_ids and not removed_ids
            final_check = {
                "official_count": final_official["count"],
                "official_ids": final_official["job_ids"],
                "board_api_count": final_board_api["count"],
                "board_api_ids": final_board_api["job_ids"],
                "board_page_count": final_board_page["count"] if final_board_page else None,
                "board_page_ids": final_board_page["job_ids"] if final_board_page else [],
                "membership_complete": final_reconciliation["complete"],
                "membership_reconciliation": final_reconciliation,
                "order_changed": order_changed,
                "new_ids": new_ids,
                "removed_ids": removed_ids,
                "changed_fields_by_id": changed,
                "source_fields_unchanged": source_fields_unchanged,
                "official": {k: v for k, v in final_official.items() if k != "rows"},
                "board_api": {k: v for k, v in final_board_api.items() if k not in ("rows", "meta")},
            }
            if not final_reconciliation["complete"]:
                self.result.errors.append("planetscale_final_membership_mismatch")
            if new_ids or removed_ids:
                self.result.errors.append("planetscale_final_membership_ids_changed")
            if changed:
                self.result.errors.append("planetscale_final_source_fields_changed")
            if order_changed:
                self.result.warnings.append("planetscale_official_listing_order_changed")
            if final_board_page is None:
                self.result.errors.append("planetscale_final_board_page_unavailable")
        except Exception as exc:
            self.result.errors.append(
                f"PlanetScale final source recheck failed: {type(exc).__name__}: {exc}"
            )
            final_check["error"] = f"{type(exc).__name__}: {exc}"
        final_seconds = max(0.0, time.perf_counter() - final_started)
        self.result.timings["final_source_recheck_seconds"] = final_seconds
        self.result.timings["source_collection_wall_seconds"] = initial_seconds + final_seconds
        self.result.timings["source_total_seconds"] = (
            initial_seconds + final_seconds
            + self.result.timings.get("detail_phase_seconds", 0.0)
        )
        self.result.metadata["final_source_recheck"] = final_check
        self.result.metadata["source_collection_wall_seconds"] = initial_seconds + final_seconds

        complete = bool(
            self.result.expected_total == len(self.result.jobs)
            and reconciliation["complete"]
            and final_check.get("membership_complete")
            and final_check.get("source_fields_unchanged")
            and not detail_failures
            and not self.result.errors
        )
        self.result.listing_complete = complete
        return self.result
