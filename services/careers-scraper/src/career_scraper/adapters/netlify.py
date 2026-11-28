"""Netlify's own careers page, its embedded Greenhouse jobs, and exact job routes."""
from __future__ import annotations

from hashlib import sha256
from html import unescape as html_unescape
import json
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.netlify.com/careers/"
GREENHOUSE_BOARD = "https://job-boards.greenhouse.io/netlify"
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/netlify/jobs?content=true"
DETAIL_HOST = "job-boards.greenhouse.io"
BOARD_TOKEN = "netlify"
EXPECTED_COMPANY = "Netlify"
EXPECTED_LEGAL_NAME = "Netlify, Inc."
JOB_LINK_PATH = re.compile(r"^/netlify/jobs/(\d+)/?$")
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
RESPONSIBILITY_HEADINGS = (
    "what you'll do", "what you will do", "what you’ll do", "what you will be doing",
    "responsibilit", "day to day", "day-to-day", "your impact", "the work",
)
QUALIFICATION_HEADINGS = (
    "what you'll bring", "what you will bring", "what you’ll bring", "what we're looking for",
    "what we’re looking for", "what you'll need", "what you will need", "what you’ll need",
    "qualif", "requirements", "this role is a great fit if", "who you are", "you'll have",
    "you will have", "nice to have", "bonus points",
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def rendered_text(html: str | None) -> str:
    """Visible inline text with browser-like whitespace for body comparisons."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def canonical_body(value: str | None) -> str:
    """Whitespace-insensitive canonical text used for rendered body hashes.

    HTML block text and browser innerText differ only in incidental whitespace
    (for example a space before an inline element). Removing all whitespace
    keeps every visible character and punctuation mark while making the two
    representations comparable.
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", value or ""))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def decode_astro_value(value):
    """Decode Astro's devalue-style ``[type, payload]`` props without eval."""
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        kind, payload = value
        if kind == 0:
            return decode_astro_value(payload)
        if kind == 1:
            if not isinstance(payload, list):
                raise ValueError("Astro props array payload is not a list")
            return [decode_astro_value(item) for item in payload]
        raise ValueError(f"Astro props contain an unsupported value type: {kind}")
    if isinstance(value, list):
        return [decode_astro_value(item) for item in value]
    if isinstance(value, dict):
        return {key: decode_astro_value(item) for key, item in value.items()}
    return value


def _json_ld_organization(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        graph = value.get("@graph") if isinstance(value, dict) else None
        candidates = graph if isinstance(graph, list) else [value]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "Organization":
                return item
    raise ValueError("Netlify official careers page no longer publishes its Organization JSON-LD")


def _validate_exact_url(response_url: str) -> None:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "www.netlify.com"
            or parts.path != "/careers/" or parts.query or parts.fragment):
        raise ValueError("Netlify official careers entry redirected away from its inspected URL")


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Read the current official listing and every job object embedded in it."""
    _validate_exact_url(response_url)
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    heading = soup.select_one("h1")
    heading_text = normalized_title(heading.get_text(" ", strip=True)) if heading else ""
    if title != "Careers at Netlify" or heading_text != "Careers at Netlify":
        raise ValueError("Netlify official careers page title or heading changed")

    organization = _json_ld_organization(html)
    if organization.get("legalName") != EXPECTED_LEGAL_NAME or organization.get("name") != EXPECTED_COMPANY:
        raise ValueError("Netlify official careers page organization identity changed")

    islands = soup.select('astro-island[component-url*="JobSearch"]')
    if len(islands) != 1:
        raise ValueError("Netlify official careers page no longer exposes one JobSearch island")
    island = islands[0]
    component_url = island.get("component-url") or ""
    if not component_url.startswith("/_astro/JobSearch") or not component_url.endswith(".js"):
        raise ValueError("Netlify official careers page published an unexpected JobSearch module")
    try:
        props = json.loads(island.get("props") or "")
    except ValueError as exc:
        raise ValueError("Netlify JobSearch island props are not valid JSON") from exc
    decoded = decode_astro_value(props)
    jobs = decoded.get("jobs") if isinstance(decoded, dict) else None
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Netlify JobSearch island no longer embeds a non-empty jobs list")

    result_title = soup.select_one(".job-search-results-title")
    result_count = soup.select_one(".job-search-results-count")
    result_title_text = normalized_title(result_title.get_text(" ", strip=True)) if result_title else ""
    result_count_text = normalized_title(result_count.get_text(" ", strip=True)) if result_count else ""
    count_match = re.fullmatch(r"(\d+)\s+Results?", result_count_text, re.I)
    if result_title_text != "Showing All Openings" or count_match is None:
        raise ValueError("Netlify official listing is no longer showing its unfiltered result total")
    visible_total = int(count_match.group(1))
    if visible_total != len(jobs):
        raise ValueError("Netlify official listing result count does not match its embedded job objects")

    table_rows = []
    for row in soup.select("table.job-search-table tbody tr"):
        anchor = row.select_one('a[href*="job-boards.greenhouse.io/netlify/jobs/"]')
        cells = row.select("td")
        if anchor is None or len(cells) != 2:
            table_rows.append({"invalid": True, "html": str(row)})
            continue
        table_rows.append({
            "title": normalized_title(anchor.get_text(" ", strip=True)),
            "url": anchor.get("href"),
            "category": normalized_title(cells[0].get_text(" ", strip=True).removeprefix("Category ")),
            "location": normalized_title(cells[1].get_text(" ", strip=True).removeprefix("Location ")),
        })
    if len(table_rows) != len(jobs):
        raise ValueError("Netlify official listing table row count does not match its embedded jobs")

    filters = {
        "keyword_search_available": soup.select_one("#job-search-keywords") is not None,
        "category_select_available": soup.select_one("#job-search-category") is not None,
        "categories": [
            normalized_title(option.get_text(" ", strip=True))
            for option in soup.select("#job-search-category option")
        ],
        "default_keyword": "",
        "default_category": "All Categories",
        "pagination_controls": [
            normalized_title(node.get_text(" ", strip=True))
            for node in soup.select(".job-search button, .job-search a")
            if re.search(r"\b(?:next|previous|load more)\b", node.get_text(" ", strip=True), re.I)
        ],
        "client_show_all_control_present": soup.select_one(".job-search-load-more-btn") is not None,
    }

    rows, issues = [], []
    for index, source_row in enumerate(jobs):
        row = parse_embedded_job(source_row)
        table = table_rows[index]
        if not isinstance(table, dict) or table.get("invalid"):
            issues.append(f"netlify_table_row_invalid:{row['id']}")
        else:
            if table["title"] != row["title"]:
                issues.append(f"netlify_table_title_mismatch:{row['id']}")
            if table["url"] != row["url"]:
                issues.append(f"netlify_table_url_mismatch:{row['id']}")
            if table["category"] != row["department"]:
                issues.append(f"netlify_table_department_mismatch:{row['id']}")
            if table["location"] not in row["locations"]:
                issues.append(f"netlify_table_location_mismatch:{row['id']}")
        if row["id"] in {existing["id"] for existing in rows}:
            issues.append(f"netlify_duplicate_embedded_job:{row['id']}")
            continue
        row["table_row"] = table
        rows.append(row)

    return {
        "url": response_url,
        "page_title": title,
        "heading": heading_text,
        "legal_name": organization.get("legalName"),
        "component_url": component_url,
        "component_props_keys": sorted(props),
        "embedded_job_count": len(jobs),
        "visible_result_count": visible_total,
        "visible_result_title": result_title_text,
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "table_rows": table_rows,
        "filters": filters,
        "issues": issues,
        "source_fields": sorted({key for row in jobs for key in row}),
    }


def parse_embedded_job(source_row) -> dict:
    if not isinstance(source_row, dict):
        raise ValueError("Netlify embedded JobSearch row is not an object")
    identity = str(source_row.get("id", ""))
    if not re.fullmatch(r"\d+", identity):
        raise ValueError("Netlify embedded job has no numeric Greenhouse id")
    title = normalized_title(source_row.get("title"))
    absolute_url = str(source_row.get("absolute_url") or "")
    parts = urlsplit(absolute_url)
    match = JOB_LINK_PATH.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.netloc != DETAIL_HOST or match is None
            or match[1] != identity):
        raise ValueError(f"Netlify embedded job URL is not its exact Greenhouse route: {identity}")
    if source_row.get("company_name") != EXPECTED_COMPANY:
        raise ValueError(f"Netlify embedded job company_name changed: {identity}")
    location = source_row.get("location")
    location_name = normalized_title(location.get("name")) if isinstance(location, dict) else ""
    if not location_name:
        raise ValueError(f"Netlify embedded job location is missing: {identity}")
    departments = source_row.get("departments")
    if not isinstance(departments, list) or not departments:
        raise ValueError(f"Netlify embedded job departments are missing: {identity}")
    department_names = [
        normalized_title(item.get("name")) for item in departments
        if isinstance(item, dict) and item.get("name")
    ]
    if not department_names:
        raise ValueError(f"Netlify embedded job has no named department: {identity}")
    offices = source_row.get("offices")
    if not isinstance(offices, list):
        raise ValueError(f"Netlify embedded job offices are missing: {identity}")
    office_locations = [
        normalized_title(item.get("location") or item.get("name")) for item in offices
        if isinstance(item, dict) and (item.get("location") or item.get("name"))
    ]
    locations = list(dict.fromkeys([location_name] + office_locations))
    content = source_row.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"Netlify embedded job content is missing: {identity}")
    description_html = html_unescape(content).strip()
    description_text = text_html(description_html)
    if len(description_text) < 100:
        raise ValueError(f"Netlify embedded job content is empty or truncated: {identity}")
    first_published = source_row.get("first_published")
    updated_at = source_row.get("updated_at")
    requisition_id = source_row.get("requisition_id")
    return {
        "id": identity,
        "title": title,
        "url": absolute_url,
        "apply_url": absolute_url,
        "location": location_name,
        "locations": locations,
        "department": department_names[0],
        "department_names": department_names,
        "requisition_id": requisition_id,
        "date_posted": first_published or None,
        "date_posted_raw": first_published,
        "date_updated": updated_at or None,
        "valid_through": source_row.get("application_deadline") or None,
        "description_html": description_html,
        "description_text": description_text,
        "source_row": source_row,
        "posting_kind": (
            "talent_community_intake"
            if "talent community" in title.casefold() or "talent community" in description_text.casefold()[:500]
            else "job"
        ),
    }


def parse_greenhouse_api(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Netlify Greenhouse API response is not an object")
    jobs = payload.get("jobs")
    meta = payload.get("meta")
    total = meta.get("total") if isinstance(meta, dict) else None
    if not isinstance(jobs, list) or not all(isinstance(row, dict) for row in jobs):
        raise ValueError("Netlify Greenhouse API response is missing its jobs array")
    if not isinstance(total, int) or total != len(jobs):
        raise ValueError("Netlify Greenhouse API meta total does not match its jobs array")
    rows, seen, issues = [], set(), []
    for source_row in jobs:
        identity = str(source_row.get("id", ""))
        if identity in seen:
            issues.append(f"netlify_greenhouse_api_duplicate_id:{identity}")
            continue
        row = parse_embedded_job(source_row)
        seen.add(identity)
        rows.append(row)
    return {
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "total": total,
        "issues": issues,
        "source_fields": sorted({key for row in jobs for key in row}),
    }


def parse_board_page(html: str, response_url: str = GREENHOUSE_BOARD) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != DETAIL_HOST or parts.path.rstrip("/") != "/netlify":
        raise ValueError("Netlify Greenhouse board redirected away from the company board")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if title != "Jobs at Netlify":
        raise ValueError("Netlify Greenhouse board title changed")
    text = soup.get_text(" ", strip=True)
    count_match = re.search(r"\b(\d+)\s+jobs?\b", text, re.I)
    if count_match is None:
        raise ValueError("Netlify Greenhouse board no longer publishes its job count")
    rows = []
    for anchor in soup.select('a[href*="job-boards.greenhouse.io/netlify/jobs/"]'):
        href = anchor.get("href")
        parts = urlsplit(href or "")
        match = JOB_LINK_PATH.fullmatch(parts.path)
        if parts.scheme != "https" or parts.netloc != DETAIL_HOST or match is None:
            continue
        rows.append({
            "id": match[1],
            "title": normalized_title(anchor.get_text(" ", strip=True)),
            "url": href,
        })
    ids = [row["id"] for row in rows]
    return {
        "url": response_url,
        "title": title,
        "count_text": int(count_match.group(1)),
        "rows": rows,
        "job_ids": ids,
        "issues": [] if len(ids) == len(set(ids)) == int(count_match.group(1)) else ["netlify_board_count_or_duplicate_mismatch"],
    }


def parse_detail_page(html: str, job: dict, response_url: str) -> dict:
    identity = str(job["id"])
    parts = urlsplit(response_url)
    match = JOB_LINK_PATH.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.netloc != DETAIL_HOST or match is None
            or match[1] != identity):
        raise ValueError("Netlify exact detail URL changed its job identity")
    soup = BeautifulSoup(html, "lxml")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    expected_page_title = f"Job Application for {job['title']} at Netlify"
    page_title_template_variant = None
    if normalized_title(page_title) != normalized_title(expected_page_title):
        if page_title == "page_title":
            # Greenhouse has been observed serving this literal upstream template
            # placeholder while still rendering the exact H1, body and form.
            page_title_template_variant = "upstream Greenhouse template placeholder 'page_title'"
        else:
            raise ValueError("Netlify exact detail page title changed")
    heading = soup.select_one("h1")
    actual_title = normalized_title(heading.get_text(" ", strip=True)) if heading else ""
    if actual_title != job["title"]:
        raise ValueError("Netlify exact detail heading does not match its embedded listing title")
    location_node = soup.select_one(".job__location")
    location = normalized_title(location_node.get_text(" ", strip=True)) if location_node else ""
    if location != job["location"]:
        raise ValueError("Netlify exact detail location does not match its embedded listing")
    description = soup.select_one(".job__description")
    if description is None:
        raise ValueError("Netlify exact detail description block is missing")
    description_html = str(description)
    description_text = text_html(description_html)
    if description_text != job["description_text"]:
        raise ValueError("Netlify exact detail body does not match the official listing's embedded content")
    form = soup.select_one("form#application-form")
    if form is None:
        raise ValueError("Netlify exact detail application form is missing")
    form_action = form.get("action")
    expected_action = f"/netlify/jobs/{identity}"
    if form_action != expected_action:
        raise ValueError("Netlify exact detail application form action changed")
    apply_control = next(
        (node for node in soup.select("button, a")
         if normalized_title(node.get_text(" ", strip=True)).casefold() == "apply"),
        None,
    )
    if apply_control is None:
        raise ValueError("Netlify exact detail page no longer publishes its Apply control")

    links, attachments = [], []
    for anchor in description.select("a[href]"):
        link = {"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
        links.append(link)
        if ATTACHMENT_SUFFIX.search(anchor.get("href") or ""):
            attachments.append(link)
    sections = description_sections(description.decode_contents())
    canonical = canonical_body(description_text)
    return {
        "job_id": identity,
        "url": response_url,
        "page_title": page_title,
        "page_title_template_variant": page_title_template_variant,
        "title": actual_title,
        "location": location,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(rendered_text(description_html)),
        "description_canonical_chars": len(canonical),
        "description_canonical_sha256": sha256(canonical.encode()).hexdigest(),
        "description_fnv1a64": fnv1a64(canonical),
        "matches_embedded_content": True,
        "description_sections": sections,
        "description_links": links,
        "role_attachments": attachments,
        "application_form_id": form.get("id"),
        "application_form_action": form_action,
        "apply_control": normalized_title(apply_control.get_text(" ", strip=True)),
        "application_form_on_same_page": True,
    }


def description_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(f'<div data-netlify-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-netlify-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    # Greenhouse wraps the detail body in one unclassed div; unwrap only that
    # structural wrapper so the published headings remain section boundaries.
    while len(blocks) == 1 and blocks[0].name == "div" and not blocks[0].get("class"):
        blocks = [node for node in blocks[0].contents if isinstance(node, Tag)]
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in HEADING_TAGS:
            heading = normalized_title(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    else:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def extract_salary(description_html: str) -> dict | None:
    """Retain an explicitly published hiring-range paragraph verbatim."""
    soup = BeautifulSoup(description_html or "", "lxml")
    for paragraph in soup.find_all("p"):
        text = normalized_title(paragraph.get_text(" ", strip=True))
        lower = text.casefold()
        if "hiring range for this role is targeted at" not in lower and "salary range for this role" not in lower:
            continue
        matches = re.findall(r"\$[\d,]+(?:\s*-\s*\$[\d,]+)?(?:\s*CAD)?", text, re.I)
        return {
            "source_field": "job__description compensation paragraph",
            "source_text": text,
            "stated_ranges": matches,
            "basis_note": "Published hiring range text, retained verbatim without conversion or inference.",
        }
    return None


def _select_sections(sections: list[dict], terms: tuple[str, ...]) -> list[dict] | None:
    selected = [
        section for section in sections
        if section.get("heading") and any(term in section["heading"].casefold() for term in terms)
    ]
    return selected or None


def _source_metadata(item: dict, response) -> dict:
    return {
        "request_url": item["url"],
        "final_url": item["final_url"],
        "status": item["status"],
        "content_type": item["content_type"],
        "seconds": item["elapsed_seconds"],
        "response_bytes": item["response_bytes"],
        "response_sha256": item["response_sha256"],
        "fetched_at": item["fetched_at"],
        "redirects": item.get("redirects", []),
    }


class Netlify(Adapter):
    """Collect only the jobs published by Netlify's own careers page and linked job routes."""

    VERIFICATION_REVISION = "netlify.official-astro-jobsearch.company-greenhouse.1"

    async def _official_snapshot(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        elapsed = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update({
            "status": response.status_code,
            "seconds": elapsed,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _board_snapshot(self):
        started = time.perf_counter()
        response = await self.http.get(GREENHOUSE_BOARD)
        elapsed = max(0.0, time.perf_counter() - started)
        parsed = parse_board_page(response.text, str(response.url))
        parsed.update({
            "status": response.status_code,
            "seconds": elapsed,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _api_snapshot(self):
        started = time.perf_counter()
        response = await self.http.get(GREENHOUSE_API)
        elapsed = max(0.0, time.perf_counter() - started)
        parsed = parse_greenhouse_api(response.json())
        parsed.update({
            "status": response.status_code,
            "seconds": elapsed,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _component_asset(self, component_url: str):
        started = time.perf_counter()
        response = await self.http.get("https://www.netlify.com" + component_url)
        elapsed = max(0.0, time.perf_counter() - started)
        text = response.text
        return {
            "url": str(response.url),
            "status": response.status_code,
            "seconds": elapsed,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "client_filtering": "job-search-form" in text and "filter(" in text,
            "client_show_all_threshold_10": "slice(0,10)" in text,
            "client_load_more_label": "Show all " in text,
            "server_fetch_calls": bool(re.search(r"\bfetch\s*\(", text)),
        }

    def _make_job(self, row: dict, api_row: dict | None) -> object:
        detail = None
        job = self.job(
            {
                "listing": dict(row["source_row"]),
                "normalized_listing": row,
                "company_listing_row": row.get("table_row"),
                "greenhouse_api_row": dict(api_row["source_row"]) if api_row else None,
                "detail": detail,
                "dates": {
                    "first_published": row["date_posted_raw"],
                    "updated_at": row["date_updated"],
                    "application_deadline": row["valid_through"],
                    "posting_date_mapping": "first_published",
                    "updated_date_mapping": "updated_at",
                },
                "application_route": {
                    "job_url": row["url"],
                    "apply_url": row["apply_url"],
                    "application_form_on_same_page": True,
                    "application_form_id": None,
                    "application_form_action": None,
                    "separate_apply_url": None,
                },
                "posting_kind": row["posting_kind"],
                "location_source": {
                    "location": row["source_row"].get("location"),
                    "offices": row["source_row"].get("offices"),
                },
                "employer_source": {
                    "company_name": row["source_row"].get("company_name"),
                    "legal_name": EXPECTED_LEGAL_NAME,
                },
            },
            title=row["title"],
            url=row["url"],
            job_id=row["id"],
            requisition_id=row["requisition_id"],
            apply_url=row["apply_url"],
            description=row["description_html"],
            method="netlify.official-careers.astro-JobSearch.jobs[].content",
            description_url=OFFICIAL_ENTRY,
            locations=row["locations"],
            department=row["department"],
            employment_type=None,
            workplace_type="Remote" if "remote" in row["location"].casefold() else None,
            date_posted=row["date_posted"],
            date_posted_raw=row["date_posted_raw"],
            date_updated=row["date_updated"],
            valid_through=row["valid_through"],
            salary=extract_salary(row["description_html"]),
            employer=EXPECTED_LEGAL_NAME,
            language=row["source_row"].get("language"),
        )
        sections = description_sections(row["description_html"])
        job.responsibilities = _select_sections(sections, RESPONSIBILITY_HEADINGS)
        job.qualifications = _select_sections(sections, QUALIFICATION_HEADINGS)
        job.description_text = row["description_text"]
        job.raw["description_sections"] = sections
        job.raw["posting_kind"] = row["posting_kind"]
        return job

    async def _detail(self, job):
        started = time.perf_counter()
        response = await self.http.get(job.url)
        fetch_seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_detail_page(response.text, job.raw["normalized_listing"], str(response.url))
        parse_seconds = max(0.0, time.perf_counter() - started) - fetch_seconds
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "netlify.greenhouse.job__description"
        job.description_source_url = str(response.url)
        job.apply_url = job.url
        job.raw["detail"] = {
            **parsed,
            "source_response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "http_status": response.status_code,
            "http_seconds": fetch_seconds,
            "local_parse_seconds": max(0.0, parse_seconds),
        }
        job.raw["application_route"].update({
            "application_form_id": parsed["application_form_id"],
            "application_form_action": parsed["application_form_action"],
            "separate_apply_url": None,
        })
        timings = self.result.metadata.setdefault("detail_source_timings", {})
        timings[str(job.job_id)] = {
            "http_seconds": fetch_seconds,
            "local_parse_seconds": max(0.0, parse_seconds),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "http_status": response.status_code,
            "final_url": str(response.url),
        }
        self.result.timings["detail_http_seconds_sum"] = (
            self.result.timings.get("detail_http_seconds_sum", 0.0) + fetch_seconds
        )
        self.result.timings["detail_local_parse_seconds_sum"] = (
            self.result.timings.get("detail_local_parse_seconds_sum", 0.0) + max(0.0, parse_seconds)
        )
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_ENTRY,
            "board_reconciliation_url": GREENHOUSE_BOARD,
            "api_reconciliation_url": GREENHOUSE_API,
            "listing_authority": (
                "Netlify's own /careers/ page embeds the complete Greenhouse job objects in its "
                "JobSearch island; the visible table, result count and embedded IDs are reconciled."
            ),
            "detail_method": (
                "Each exact job-boards.greenhouse.io/netlify/jobs/<id> page is fetched and its "
                ".job__description body must equal the official listing's embedded content."
            ),
            "request_scope": (
                "Only www.netlify.com/careers/ and the company-scoped netlify Greenhouse routes "
                "published/embedded there; no other board or aggregator is queried."
            ),
        })
        try:
            initial = await self._official_snapshot()
            board = await self._board_snapshot()
            api = await self._api_snapshot()
            asset = await self._component_asset(initial["component_url"])
        except Exception as exc:
            self.result.errors.append(f"Netlify source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        self.result.pages = 1
        self.result.expected_total = initial["embedded_job_count"]
        self.result.metadata["initial_official_listing"] = {
            key: value for key, value in initial.items() if key != "rows"
        }
        self.result.metadata["initial_board_listing"] = {
            key: value for key, value in board.items() if key != "rows"
        }
        self.result.metadata["initial_api_listing"] = {
            key: value for key, value in api.items() if key != "rows"
        }
        self.result.metadata["jobsearch_component_asset"] = asset
        self.result.errors.extend(initial["issues"])
        self.result.errors.extend(board["issues"])
        self.result.errors.extend(api["issues"])

        if board["job_ids"] and set(board["job_ids"]) != set(initial["job_ids"]):
            self.result.errors.append("netlify_company_board_and_official_page_id_sets_differ")
        if board["count_text"] != initial["embedded_job_count"]:
            self.result.errors.append("netlify_company_board_count_differs_from_official_page")
        if api["job_ids"] and set(api["job_ids"]) != set(initial["job_ids"]):
            self.result.errors.append("netlify_greenhouse_api_and_official_page_id_sets_differ")
        if api["total"] != initial["embedded_job_count"]:
            self.result.errors.append("netlify_greenhouse_api_total_differs_from_official_page")

        api_by_id = {row["id"]: row for row in api["rows"]}
        for row in initial["rows"]:
            api_row = api_by_id.get(row["id"])
            if api_row is None:
                self.result.errors.append(f"netlify_embedded_job_missing_from_greenhouse_api:{row['id']}")
                continue
            for field in ("title", "url", "requisition_id", "date_posted", "date_updated",
                          "valid_through", "description_html", "department_names", "locations"):
                if row[field] != api_row[field]:
                    self.result.errors.append(f"netlify_api_field_mismatch:{row['id']}:{field}")
            if row["source_row"].get("company_name") != api_row["source_row"].get("company_name"):
                self.result.errors.append(f"netlify_api_company_name_mismatch:{row['id']}")

        jobs = []
        for row in initial["rows"]:
            job = self._make_job(row, api_by_id.get(row["id"]))
            jobs.append(job)
            self.add(job)
            if self.limit():
                break
        if len(jobs) != initial["embedded_job_count"]:
            self.result.errors.append("netlify_retained_rows_do_not_match_official_listing_count")

        hydrated = await self.hydrate(jobs, self._detail)

        final = None
        try:
            final = await self._official_snapshot()
        except Exception as exc:
            self.result.errors.append(f"Netlify final listing recheck failed: {type(exc).__name__}: {exc}")
        membership = {
            "initial_ids": initial["job_ids"],
            "final_ids": final["job_ids"] if final else [],
            "official_page_count": initial["embedded_job_count"],
            "official_table_row_count": len(initial["table_rows"]),
            "visible_result_count": initial["visible_result_count"],
            "greenhouse_board_count": board["count_text"],
            "greenhouse_board_ids": board["job_ids"],
            "greenhouse_api_total": api["total"],
            "greenhouse_api_ids": api["job_ids"],
            "company_board_id_set_matches": set(board["job_ids"]) == set(initial["job_ids"]),
            "greenhouse_api_id_set_matches": set(api["job_ids"]) == set(initial["job_ids"]),
            "initial_final_id_order_matches": bool(final and final["job_ids"] == initial["job_ids"]),
            "initial_final_rows_match": bool(
                final and all(
                    left["source_row"] == right["source_row"]
                    for left, right in zip(initial["rows"], final["rows"])
                )
            ),
            "new_ids": sorted(set(final["job_ids"]) - set(initial["job_ids"])) if final else [],
            "removed_ids": sorted(set(initial["job_ids"]) - set(final["job_ids"])) if final else [],
            "pagination": (
                "No pagination or load-more control on the current 5-row official listing; the "
                "published component only slices at 10 for a Show all control, and the server HTML "
                "embeds every job object."
            ),
            "filters": initial["filters"],
            "complete": bool(
                final
                and final["job_ids"] == initial["job_ids"]
                and len(initial["rows"]) == initial["embedded_job_count"] == api["total"] == board["count_text"]
                and set(board["job_ids"]) == set(api["job_ids"]) == set(initial["job_ids"])
            ),
        }
        self.result.metadata["membership_reconciliation"] = membership
        if not membership["complete"]:
            self.result.errors.append("netlify_membership_reconciliation_incomplete")
        if final is not None:
            self.result.metadata["final_official_listing"] = {
                key: value for key, value in final.items() if key != "rows"
            }
        self.result.listing_complete = bool(
            membership["complete"]
            and len(hydrated) == len(jobs) == initial["embedded_job_count"]
            and not self.result.errors
        )
        self.result.metadata["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result
