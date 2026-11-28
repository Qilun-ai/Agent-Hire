"""MathWorks' own careers search (AEM) and its server-rendered job details.

The published journey inspected from MathWorks' own pages is:

    /company/jobs/opportunities.html
      -> iframe /company/jobs/opportunities/aem_iframe
      -> form GET /company/jobs/opportunities/search
      -> ?page=<n> (20 rows/page) and ?display=max
      -> /company/jobs/opportunities/<numeric-id>-<slug>

No third-party ATS is published by that journey.  Job details expose a
schema.org JobPosting JSON-LD block plus the rendered description body.
"""
from __future__ import annotations

from hashlib import sha256
from html import unescape
import json
import math
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .. import access
from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.mathworks.com/company/jobs/opportunities.html"
SEARCH_PATH = "/company/jobs/opportunities/search"
IFRAME_PATH = "/company/jobs/opportunities/aem_iframe"
DETAIL_PATH = re.compile(r"^/company/jobs/opportunities/(\d+)-([a-z0-9-]+)$", re.I)
OFFICIAL_HOSTS = {"www.mathworks.com", "mathworks.com", "in.mathworks.com"}
PAGE_SIZE = 20
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class SourceBlocked(RuntimeError):
    """The official source answered with bot protection or rate limiting."""

    def __init__(self, url: str, status: int | None, message: str):
        super().__init__(message)
        self.url = url
        self.status = status


def normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def normalized_key(value: str | None) -> str:
    return normalized_text(value).casefold()


def _official_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc.lower() in OFFICIAL_HOSTS


def _require_official(url: str, path: str) -> None:
    parts = urlsplit(url)
    if not _official_url(url) or parts.path.rstrip("/") != path.rstrip("/"):
        raise ValueError(f"MathWorks source URL is not the inspected official route: {url}")


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def decode_schema_html(value: str | None) -> str:
    """The JSON-LD description is HTML-escaped, and parts of it are escaped twice."""
    decoded = value or ""
    for _ in range(2):
        unescaped = unescape(decoded)
        if unescaped == decoded:
            break
        decoded = unescaped
    return decoded


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    _require_official(response_url, "/company/jobs/opportunities.html")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    h1 = [normalized_text(node.get_text(" ", strip=True)) for node in soup.select("h1")]
    iframe = soup.select_one("iframe#careers_search")
    if iframe is None:
        raise ValueError("MathWorks official careers page no longer exposes its careers_search iframe")
    iframe_url = urljoin(response_url, iframe.get("src", ""))
    if not _official_url(iframe_url) or urlsplit(iframe_url).path.rstrip("/") != IFRAME_PATH:
        raise ValueError("MathWorks careers iframe URL changed away from the inspected official route")
    if "MathWorks" not in title or "career" not in title.lower():
        raise ValueError("MathWorks official careers page identity changed")
    canonical = soup.select_one('link[rel="canonical"]')
    return {
        "url": response_url,
        "page_title": title,
        "h1": h1,
        "iframe_id": iframe.get("id"),
        "iframe_title": iframe.get("title"),
        "iframe_url": iframe_url,
        "canonical": urljoin(response_url, canonical.get("href")) if canonical and canonical.get("href") else None,
    }


def parse_iframe(html: str, response_url: str) -> dict:
    _require_official(response_url, IFRAME_PATH)
    soup = BeautifulSoup(html, "lxml")
    form = soup.select_one("form#careers_search")
    if form is None:
        raise ValueError("MathWorks careers iframe no longer exposes its careers_search form")
    action = urljoin(response_url, form.get("action", ""))
    if not _official_url(action) or urlsplit(action).path.rstrip("/") != SEARCH_PATH:
        raise ValueError("MathWorks careers iframe search action changed away from the official listing")
    inputs = []
    for node in form.select("input,select"):
        inputs.append({
            "name": node.get("name"),
            "type": node.get("type"),
            "id": node.get("id"),
            "value": node.get("value"),
        })
    return {
        "url": response_url,
        "form_id": form.get("id"),
        "method": (form.get("method") or "get").lower(),
        "search_url": action,
        "form_inputs": inputs,
    }


def _row_fields(text: str) -> dict:
    parts = [normalized_text(part) for part in (text or "").split("|")]
    parts += [""] * (3 - len(parts))
    return {
        "location": parts[0] or None,
        "team": parts[1] or None,
        "career_stage": parts[2] or None,
    }


def parse_search_page(html: str, response_url: str) -> dict:
    parts = urlsplit(response_url)
    if not _official_url(response_url) or parts.path.rstrip("/") != SEARCH_PATH:
        raise ValueError(f"MathWorks search response came from an unexpected route: {response_url}")
    query = parse_qs(parts.query)
    display = (query.get("display") or [None])[0]
    page_value = (query.get("page") or ["1"])[0]
    page = int(page_value) if str(page_value).isdigit() else 1
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    table = soup.select_one("table#search_results_table")
    if table is None:
        raise ValueError("MathWorks search page no longer exposes its search results table")
    if "MathWorks Careers" not in title:
        raise ValueError("MathWorks search page identity changed")

    rows, issues, seen = [], [], set()
    for tr in table.select("tbody tr"):
        desc_cell = tr.select_one("td.search_result_desc")
        if desc_cell is None:
            continue
        result_id = (desc_cell.get("id") or "").removeprefix("result_")
        title_link = desc_cell.select_one("div.search_title a[id^=title_]")
        if not result_id.isdigit() or title_link is None:
            issues.append("search_row_missing_result_id_or_title_link")
            continue
        href = title_link.get("href") or ""
        detail_url = urljoin(response_url, href)
        detail_parts = urlsplit(detail_url)
        match = DETAIL_PATH.fullmatch(detail_parts.path)
        if match is None or not _official_url(detail_url) or match[1] != result_id:
            issues.append(f"search_row_detail_link_mismatch:{result_id}")
            continue
        if result_id in seen:
            issues.append(f"search_duplicate_result_id:{result_id}")
            continue
        seen.add(result_id)
        checkbox = tr.select_one('input.job_posting_checkbox[name="job_posting_ids[]"]')
        if checkbox is None or (checkbox.get("value") or "") != result_id:
            issues.append(f"search_row_checkbox_mismatch:{result_id}")
        field_text = normalized_text(
            desc_cell.select_one("div.additional_field").get_text(" ", strip=True)
            if desc_cell.select_one("div.additional_field") else ""
        )
        fields = _row_fields(field_text)
        rows.append({
            "id": result_id,
            "slug": match[2],
            "title": normalized_text(title_link.get_text(" ", strip=True)),
            "url": detail_url.split("?")[0],
            "listing_href": href,
            "detail_path": detail_parts.path,
            "location": fields["location"],
            "team": fields["team"],
            "career_stage": fields["career_stage"],
            "additional_field_text": field_text,
            "summary": normalized_text(
                desc_cell.select_one("div.search_highlight").get_text(" ", strip=True)
                if desc_cell.select_one("div.search_highlight") else ""
            ),
            "row_html": str(tr),
        })

    footer = soup.select_one("#search_result_footer nav.pagination_controls")
    footer_text = normalized_text(footer.get_text(" ", strip=True)) if footer else ""
    max_link = soup.select_one("a#max_link_bottom")
    total = None
    if max_link is not None:
        digits = re.sub(r"[^\d]", "", max_link.get_text(" ", strip=True))
        total = int(digits) if digits else None
    if total is None:
        match = re.search(r"\bof\s+([\d,]+)", footer_text)
        total = int(match.group(1).replace(",", "")) if match else None
    next_link = footer.select_one('a[aria-label="next"]') if footer else None
    previous_link = footer.select_one('a[aria-label="previous"]') if footer else None

    def page_link(anchor):
        if anchor is None:
            return None
        classes = " ".join(anchor.get("class") or [])
        # The server-rendered first page marks "next" aria-disabled until JS
        # initializes, but still publishes the valid ?page=2 href.  The
        # genuinely unusable previous link also carries inactive/disabled.
        if re.search(r"\b(?:inactive|disabled)\b", classes):
            return None
        return urljoin(response_url, anchor.get("href", ""))

    next_url = page_link(next_link)
    previous_url = page_link(previous_link)
    max_url = urljoin(response_url, max_link.get("href")) if max_link and max_link.get("href") else None
    if total is not None and display != "max":
        expected = min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))
        if len(rows) != expected:
            issues.append(f"search_page_row_count_mismatch:{len(rows)}!={expected}")
    if display == "max" and total is not None and len(rows) != total:
        issues.append(f"search_max_row_count_mismatch:{len(rows)}!={total}")
    return {
        "url": response_url,
        "page_title": title,
        "page": page,
        "display": display,
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "count": len(rows),
        "total": total,
        "page_size": len(rows),
        "pagination_text": footer_text,
        "next_url": next_url,
        "previous_url": previous_url,
        "max_url": max_url,
        "issues": issues,
    }


def _parse_job_details(soup: BeautifulSoup) -> tuple[dict, list]:
    details, raw = {}, []
    seen = set()
    for ul in soup.select("ul.job_details_list"):
        for li in ul.select("li"):
            text = normalized_text(li.get_text(" ", strip=True))
            if not text or text in seen:
                continue
            seen.add(text)
            key, separator, value = text.partition(":")
            if not separator:
                raw.append({"text": text})
                continue
            key, value = normalized_text(key), normalized_text(value)
            details.setdefault(key, value)
            raw.append({"key": key, "value": value, "text": text})
    return details, raw


def _description_region(soup: BeautifulSoup) -> tuple[str, str | None]:
    section = soup.select_one("#careersContent")
    column = section.select_one("div.col-md-8") if section else None
    if column is None:
        return "", "careersContentMainColumnMissing"
    start = None
    for heading in column.select("h2"):
        if normalized_key(heading.get_text(" ", strip=True)) == "job summary":
            start = heading
            break
    if start is None:
        return "", "jobSummaryHeadingMissing"
    nodes = [start]
    for sibling in start.next_siblings:
        if not isinstance(sibling, Tag):
            continue
        text = normalized_text(sibling.get_text(" ", strip=True))
        if "join our talent network" in text.lower() or text.lower() == "share job":
            break
        nodes.append(sibling)
    fragment = "".join(str(node) for node in nodes)
    cleaned = BeautifulSoup(f"<div data-mathworks-description='1'>{fragment}</div>", "lxml")
    root = cleaned.select_one("[data-mathworks-description]")
    for node in root.select("script,style,noscript,button,.button"):
        node.decompose()
    for anchor in root.select("a"):
        if normalized_key(anchor.get_text(" ", strip=True)) in {"apply", "apply now"}:
            anchor.decompose()
    return str(root), None


def _walk_sections(root: Tag) -> list[dict]:
    sections, current = [], None

    def append(node):
        nonlocal current
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(node)

    def walk(node):
        nonlocal current
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            if child.name in HEADING_TAGS:
                if current is not None and (current["html"] or current["heading"]):
                    current["text"] = text_html(current["html"])
                    sections.append(current)
                current = {"heading": normalized_text(child.get_text(" ", strip=True)), "html": "", "text": ""}
            elif child.name in {"div", "section"} and child.find(HEADING_TAGS):
                walk(child)
            else:
                append(child)

    walk(root)
    if current is not None and (current["html"] or current["heading"]):
        current["text"] = text_html(current["html"])
        sections.append(current)
    return sections


def description_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(f"<div data-mathworks-sections='1'>{html or ''}</div>", "lxml")
    root = soup.select_one("[data-mathworks-sections]")
    sections = _walk_sections(root) if root else []
    if not sections:
        sections = [{"heading": None, "html": html or "", "text": text_html(html or "")}]
    else:
        for section in sections:
            if section["html"] and not section["text"]:
                section["text"] = text_html(section["html"])
    return sections


def _paragraphs(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "lxml")
    values = []
    for node in soup.select("p,li"):
        text = normalized_text(node.get_text(" ", strip=True))
        if len(text) >= 40 and text not in values:
            values.append(text)
    return values


def _salary(value) -> dict | None:
    if not isinstance(value, dict) or value.get("@type") != "MonetaryAmount":
        return None
    quantity = value.get("value")
    result = {
        "currency": value.get("currency"),
        "unit": None,
        "value": None,
        "min": None,
        "max": None,
        "source": value,
    }
    if isinstance(quantity, dict):
        result.update({
            "value": quantity.get("value"),
            "min": quantity.get("minValue"),
            "max": quantity.get("maxValue"),
            "unit": quantity.get("unitText"),
        })
    elif quantity is not None:
        result["value"] = quantity
    return result


def _locations(value) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        address = item.get("address") or {}
        if isinstance(address, dict):
            parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            ]
            label = ", ".join(str(part) for part in parts if part)
        else:
            label = str(address)
        if label and label not in result:
            result.append(label)
    return result


def parse_detail_page(html: str, response_url: str) -> dict:
    parts = urlsplit(response_url)
    if not _official_url(response_url):
        raise ValueError(f"MathWorks detail response came from an unexpected host: {response_url}")
    match = DETAIL_PATH.fullmatch(parts.path)
    if match is None:
        raise ValueError(f"MathWorks detail response came from an unexpected route: {response_url}")
    job_id, slug = match.groups()
    soup = BeautifulSoup(html, "lxml")
    schema_node = soup.select_one('script[type="application/ld+json"]')
    if schema_node is None or not schema_node.string:
        raise ValueError(f"MathWorks detail {job_id} no longer exposes JobPosting JSON-LD")
    try:
        schema = json.loads(schema_node.string)
    except ValueError as exc:
        raise ValueError(f"MathWorks detail {job_id} JSON-LD is not valid JSON") from exc
    if isinstance(schema, list):
        schema = next((item for item in schema if item.get("@type") == "JobPosting"), None)
    if not isinstance(schema, dict) or schema.get("@type") != "JobPosting":
        raise ValueError(f"MathWorks detail {job_id} JSON-LD is not a JobPosting")
    identifier = schema.get("identifier") or {}
    requisition = normalized_text(identifier.get("value")) if isinstance(identifier, dict) else ""
    if not requisition.startswith(f"{job_id}-"):
        raise ValueError(f"MathWorks detail {job_id} identifier does not match its listing id: {requisition!r}")
    title = normalized_text(schema.get("title"))
    h1 = soup.select_one("#careersContent h1") or soup.select_one("h1")
    visible_title = normalized_text(h1.get_text(" ", strip=True)) if h1 else ""
    if not title or (visible_title and normalized_key(title) != normalized_key(visible_title)):
        raise ValueError(f"MathWorks detail {job_id} title mismatch: {title!r} != {visible_title!r}")
    description_html, region_issue = _description_region(soup)
    if not description_html:
        raise ValueError(f"MathWorks detail {job_id} rendered description region is missing")
    schema_description_html = decode_schema_html(schema.get("description"))
    schema_description_text = text_html(schema_description_html)
    description_text = text_html(description_html)
    if len(description_text) < 100:
        raise ValueError(f"MathWorks detail {job_id} rendered description is empty or truncated")
    sections = description_sections(description_html)
    headings = [section["heading"] for section in sections if section.get("heading")]
    rendered_paragraphs = _paragraphs(description_html)
    schema_paragraphs = _paragraphs(schema_description_html)
    rendered_text_normalized = normalized_text(description_text)
    schema_text_normalized = normalized_text(schema_description_text)
    schema_missing = [
        paragraph for paragraph in schema_paragraphs
        if paragraph not in rendered_text_normalized
    ][:20]
    rendered_missing = [
        paragraph for paragraph in rendered_paragraphs
        if paragraph not in schema_text_normalized
    ][:20]
    details, details_raw = _parse_job_details(soup)
    apply_button = soup.select_one("#apply_now_button[data-url]")
    apply_url = urljoin(response_url, apply_button.get("data-url")) if apply_button else None
    if apply_url is None:
        apply_link = soup.select_one('a[href*="/company/jobs/apply/"]')
        apply_url = urljoin(response_url, apply_link.get("href")) if apply_link else response_url
    location_text = details.get("Location")
    locations = [location_text] if location_text else _locations(schema.get("jobLocation"))
    team = details.get("Team")
    salary = _salary(schema.get("baseSalary"))
    salary_text = details.get("Salary Range")
    if salary is not None and salary_text:
        salary["text"] = salary_text
    date_posted = schema.get("datePosted")
    valid_through = schema.get("validThrough")
    if not isinstance(date_posted, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_posted):
        date_posted = None
    if not isinstance(valid_through, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", valid_through):
        valid_through = None
    responsibility_sections = [
        section for section in sections
        if section.get("heading") and "responsibilit" in section["heading"].lower()
    ]
    qualification_sections = [
        section for section in sections
        if section.get("heading") and "qualif" in section["heading"].lower()
    ]
    return {
        "job_id": job_id,
        "slug": slug,
        "title": title,
        "requisition_id": requisition,
        "date_posted": date_posted,
        "date_posted_raw": schema.get("datePosted"),
        "valid_through": valid_through,
        "employment_type": schema.get("employmentType"),
        "employer": (schema.get("hiringOrganization") or {}).get("name") if isinstance(schema.get("hiringOrganization"), dict) else None,
        "job_locations": _locations(schema.get("jobLocation")),
        "locations": locations,
        "team": team,
        "details": details,
        "details_raw": details_raw,
        "salary": salary,
        "salary_text": salary_text,
        "apply_url": apply_url,
        "description_html": description_html,
        "description_text": description_text,
        "description_sections": sections,
        "description_headings": headings,
        "description_normalized_chars": len(normalized_text(description_text)),
        "description_sha256": sha256(description_html.encode()).hexdigest(),
        "description_fnv1a64": fnv1a64(normalized_text(description_text)),
        "schema_description_html": schema_description_html,
        "schema_description_text": schema_description_text,
        "schema_description_sha256": sha256(schema_description_html.encode()).hexdigest(),
        "schema_rendered_overlap": {
            "schema_paragraphs": len(schema_paragraphs),
            "rendered_paragraphs": len(rendered_paragraphs),
            "schema_paragraphs_missing_from_rendered": schema_missing,
            "rendered_paragraphs_missing_from_schema": rendered_missing,
            "schema_text_chars": len(schema_text_normalized),
            "rendered_text_chars": len(rendered_text_normalized),
        },
        "description_region_issue": region_issue,
        "responsibilities": responsibility_sections,
        "qualifications": qualification_sections,
        "schema": schema,
        "has_talent_network_in_description": "join our talent network" in description_text.lower(),
    }


class MathWorksIndia(Adapter):
    """Read only MathWorks' own careers iframe, search pages and job details."""

    VERIFICATION_REVISION = "mathworks-india.aem-search-jsonld-dom.1"

    async def _get(self, url: str, **kwargs):
        started = time.perf_counter()
        attempts = getattr(self, "_access_attempts", None)
        if attempts is None:
            attempts = self._access_attempts = {}
        try:
            response = await self.http.get(url, headers=BROWSER_HEADERS, **kwargs)
            if response.status_code in (403, 429):
                return await self._escalate(url, response.status_code, started, attempts)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            status_match = re.search(r"HTTP (\d{3})", message)
            status = int(status_match.group(1)) if status_match else None
            if status in (403, 429):
                return await self._escalate(url, status, started, attempts)
            raise
        return response, max(0.0, time.perf_counter() - started)

    async def _escalate(self, url, status, started, attempts):
        """Retry a blocked MathWorks route inside a warmed stealth session.

        The entry and iframe are visited first so the same cookie jar serves
        the search/detail request; a bare request to the search route is what
        Akamai rejects.
        """
        page = None
        if access.stealth_enabled():
            _, _, page = await access.browser_fetch(
                url, warm_urls=(OFFICIAL_ENTRY, f"https://www.mathworks.com{IFRAME_PATH}"),
            )
        attempts[url] = access.attempt_metadata(page, url=url)
        if page is not None and page.ok:
            self.result.warnings.append(f"stealth_escalation_resolved:{url}")
            return access.as_response(page), max(0.0, time.perf_counter() - started)
        raise SourceBlocked(url, status, f"HTTP {status}: still blocked after stealth escalation")

    async def _collect_listing(self, label: str) -> dict:
        started = time.perf_counter()
        rows, pages, issues, seen = [], [], [], set()
        total = None
        page_url = f"https://www.mathworks.com{SEARCH_PATH}"
        page_number = 1
        while page_url and page_number <= 1000:
            if not self.page_guard():
                issues.append("mathworks_page_budget_exhausted")
                break
            response, request_seconds = await self._get(page_url)
            parsed = parse_search_page(response.text, str(response.url))
            parsed["request_seconds"] = request_seconds
            parsed["response_sha256"] = sha256(response.content).hexdigest()
            parsed["response_bytes"] = len(response.content)
            if total is None:
                total = parsed["total"]
            elif parsed["total"] is not None and parsed["total"] != total:
                issues.append(f"mathworks_listing_total_changed:{total}!={parsed['total']}")
            issues.extend(parsed["issues"])
            if total is not None and total > 0:
                expected_pages = math.ceil(total / PAGE_SIZE)
                if page_number > expected_pages:
                    issues.append(f"mathworks_listing_page_beyond_advertised_total:{page_number}>{expected_pages}")
                    break
                if parsed["count"] == 0:
                    issues.append(f"mathworks_listing_empty_page:{page_number}")
                    break
            for row in parsed["rows"]:
                if row["id"] in seen:
                    issues.append(f"mathworks_listing_duplicate_id:{row['id']}")
                    continue
                seen.add(row["id"])
                rows.append(row)
            pages.append({
                "page": parsed["page"],
                "url": parsed["url"],
                "status": response.status_code,
                "request_seconds": request_seconds,
                "response_sha256": parsed["response_sha256"],
                "response_bytes": parsed["response_bytes"],
                "row_count": parsed["count"],
                "job_ids": parsed["job_ids"],
                "total": parsed["total"],
                "pagination_text": parsed["pagination_text"],
                "next_url": parsed["next_url"],
                "max_url": parsed["max_url"],
            })
            next_url = parsed["next_url"]
            if next_url is None:
                break
            next_parts = urlsplit(next_url)
            next_query = parse_qs(next_parts.query)
            next_page = (next_query.get("page") or [""])[0]
            if not next_page.isdigit() or int(next_page) <= page_number:
                issues.append(f"mathworks_listing_non_advancing_next_link:{next_url}")
                break
            if total is not None and total > 0 and int(next_page) > math.ceil(total / PAGE_SIZE):
                issues.append(f"mathworks_listing_next_page_beyond_advertised_total:{next_page}")
                break
            page_url = next_url
            page_number = int(next_page)
        if total is not None and len(rows) != total:
            issues.append(f"mathworks_listing_total_mismatch:{len(rows)}!={total}")
        if total == 0 and not rows:
            issues.append("mathworks_listing_exposed_zero_rows")
        listing_complete = bool(total is not None and len(rows) == total and not issues)
        expected_pages = math.ceil(total / PAGE_SIZE) if total else None
        return {
            "label": label,
            "rows": rows,
            "job_ids": [row["id"] for row in rows],
            "total": total,
            "expected_pages": expected_pages,
            "pages": pages,
            "issues": list(dict.fromkeys(issues)),
            "listing_complete": listing_complete,
            "request_seconds": max(0.0, time.perf_counter() - started),
        }

    def _stub(self, row: dict):
        return self.job(
            {"listing": row},
            title=row["title"],
            url=row["url"],
            job_id=row["id"],
            apply_url=None,
            description="",
            method="mathworks.official_job_detail.pending",
            description_url=row["url"],
            locations=[row["location"]] if row.get("location") else [],
            department=row.get("team"),
            experience=row.get("career_stage"),
            employer="MathWorks",
            language="en",
        )

    async def _detail(self, job):
        row = job.raw["listing"]
        response, request_seconds = await self._get(row["url"])
        detail = parse_detail_page(response.text, str(response.url))
        if detail["job_id"] != row["id"]:
            raise ValueError(f"MathWorks detail id {detail['job_id']} does not match listing id {row['id']}")
        detail["request_seconds"] = request_seconds
        detail["final_url"] = str(response.url)
        detail["status"] = response.status_code
        detail["response_sha256"] = sha256(response.content).hexdigest()
        detail["response_bytes"] = len(response.content)
        detail["apply_url_resolved"] = detail["apply_url"]
        new_job = self.job(
            {"listing": row, "detail": detail},
            title=detail["title"],
            url=row["url"],
            job_id=row["id"],
            requisition_id=detail["requisition_id"],
            apply_url=detail["apply_url"],
            description=detail["description_html"],
            method="mathworks.official_job_detail.rendered_body",
            description_url=row["url"],
            locations=detail["locations"] or ([row["location"]] if row.get("location") else []),
            department=detail["team"] or row.get("team"),
            employment_type=detail["employment_type"],
            workplace_type=None,
            date_posted=detail["date_posted"],
            date_posted_raw=detail["date_posted_raw"],
            date_updated=None,
            valid_through=detail["valid_through"],
            salary=detail["salary"],
            qualifications=detail["qualifications"],
            responsibilities=detail["responsibilities"],
            employer=detail["employer"] or "MathWorks",
            language="en",
            experience=row.get("career_stage"),
        )
        new_job.description_text = detail["description_text"]
        new_job.raw["detail"] = detail
        new_job.raw["schema_description_html"] = detail["schema_description_html"]
        new_job.company_scope_verified = self.company.scope == "company"
        return new_job

    async def run(self):
        started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": f"https://www.mathworks.com{SEARCH_PATH}",
            "listing_authority": "MathWorks' own careers page iframe and server-rendered search route.",
            "detail_method": "Exact /company/jobs/opportunities/<id>-<slug> page; rendered body plus JobPosting JSON-LD.",
            "filter_scope": "No country, seniority, keyword or date filter applied.",
            "ats_usage": "None: the inspected official journey publishes no third-party ATS link, embed or redirect.",
            "access_attempts": {},
        })
        try:
            entry_response, entry_seconds = await self._get(OFFICIAL_ENTRY)
            entry = parse_official_entry(entry_response.text, str(entry_response.url))
            entry.update({
                "status": entry_response.status_code,
                "request_seconds": entry_seconds,
                "response_sha256": sha256(entry_response.content).hexdigest(),
                "response_bytes": len(entry_response.content),
                "final_url": str(entry_response.url),
            })
            iframe_response, iframe_seconds = await self._get(entry["iframe_url"])
            iframe = parse_iframe(iframe_response.text, str(iframe_response.url))
            iframe.update({
                "status": iframe_response.status_code,
                "request_seconds": iframe_seconds,
                "response_sha256": sha256(iframe_response.content).hexdigest(),
                "response_bytes": len(iframe_response.content),
                "final_url": str(iframe_response.url),
            })
            initial = await self._collect_listing("initial")
        except SourceBlocked as exc:
            self.result.metadata["access_attempts"] = getattr(self, "_access_attempts", {})
            self.result.errors.append(f"mathworks_official_source_blocked:{exc.status}:{exc.url}")
            self.result.metadata["source_blocked"] = {
                "url": exc.url,
                "status": exc.status,
                "message": str(exc),
                "stopped_on_bot_protection": True,
            }
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
            return self.result
        except Exception as exc:
            self.result.errors.append(f"mathworks_source_request_failed:{type(exc).__name__}:{exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
            return self.result

        self.result.metadata["initial_official_entry"] = entry
        self.result.metadata["initial_iframe"] = iframe
        self.result.metadata["initial_listing"] = {key: value for key, value in initial.items() if key != "rows"}
        self.result.expected_total = initial["total"]
        self.result.errors.extend(initial["issues"])

        stubs = [self._stub(row) for row in initial["rows"]]
        hydrated = await self.hydrate(stubs, self._detail)
        failed_jobs, verified_jobs = [], []
        for job in hydrated:
            if job.description_verified:
                verified_jobs.append(job)
            else:
                job.description_unavailable = True
                job.description_method = "mathworks.official_job_detail.unavailable"
                job.description_source_url = job.url
                failed_jobs.append({
                    "id": job.job_id,
                    "url": job.url,
                    "reason": "detail_not_verified",
                    "warnings": list(job.warnings),
                })
            self.add(job)
        self.result.metadata["detail_hydration"] = self.result.metadata.get("details", {})
        self.result.metadata["failed_jobs"] = failed_jobs
        self.result.metadata["verified_job_ids"] = [job.job_id for job in verified_jobs]

        final = None
        final_matches = False
        final_started = time.perf_counter()
        try:
            final = await self._collect_listing("final")
        except SourceBlocked as exc:
            self.result.errors.append(f"mathworks_final_source_blocked:{exc.status}:{exc.url}")
            self.result.metadata["final_source_blocked"] = {"url": exc.url, "status": exc.status, "message": str(exc)}
        except Exception as exc:
            self.result.errors.append(f"mathworks_final_source_request_failed:{type(exc).__name__}:{exc}")
        final_seconds = max(0.0, time.perf_counter() - final_started)
        self.result.timings["final_recheck_seconds"] = final_seconds

        changed_fields = {}
        if final is not None:
            initial_by_id = {row["id"]: row for row in initial["rows"]}
            final_by_id = {row["id"]: row for row in final["rows"]}
            for job_id in initial["job_ids"]:
                if job_id in final_by_id and initial_by_id[job_id] != final_by_id[job_id]:
                    changed_fields[job_id] = sorted(
                        key for key in set(initial_by_id[job_id]) | set(final_by_id[job_id])
                        if initial_by_id[job_id].get(key) != final_by_id[job_id].get(key)
                    )
            same_ids = initial["job_ids"] == final["job_ids"]
            same_total = initial["total"] == final["total"]
            final_matches = bool(
                initial["listing_complete"] and final["listing_complete"]
                and same_ids and same_total and not changed_fields and not final["issues"]
            )
            self.result.metadata["final_listing"] = {key: value for key, value in final.items() if key != "rows"}
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["job_ids"],
                "final_ids": final["job_ids"],
                "initial_total": initial["total"],
                "final_total": final["total"],
                "initial_pages": len(initial["pages"]),
                "final_pages": len(final["pages"]),
                "same_ids_and_order": same_ids,
                "same_total": same_total,
                "all_listing_fields_match": not changed_fields,
                "changed_fields_by_job_id": changed_fields,
                "new_job_ids": [i for i in final["job_ids"] if i not in initial["job_ids"]],
                "removed_job_ids": [i for i in initial["job_ids"] if i not in final["job_ids"]],
                "complete": final_matches,
            }
            if not same_ids or not same_total:
                self.result.errors.append("mathworks_listing_membership_changed_during_collection")
            if changed_fields:
                self.result.errors.append("mathworks_listing_fields_changed_during_collection")
            self.result.errors.extend(final["issues"])
        else:
            self.result.errors.append("mathworks_final_listing_recheck_missing")

        self.result.listing_complete = bool(
            final_matches
            and initial["total"] is not None
            and len(self.result.jobs) == initial["total"]
            and len(verified_jobs) == initial["total"]
        )
        self.result.metadata["job_ids"] = [job.job_id for job in self.result.jobs]
        self.result.metadata["current_native_membership_reconciled"] = final_matches
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
        return self.result
