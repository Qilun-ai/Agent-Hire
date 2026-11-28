"""Unacademy's official careers page and its exact Darwinbox tenant board.

The ledger entry ``https://unacademy.com/careers`` is Unacademy's own careers
page.  Its single job-listing control, ``View all job openings``, links to
``https://unacademy.darwinbox.in/ms/candidate/careers``, which enters the
company's own Darwinbox candidate board (tenant ``unacademy``, companyId
``main``, company name ``Unacademy Group``, tenant id ``78``).  That board's
listing request is ``POST /ms/candidateapi/job/alljobs?companyId=main`` with
``{companyId, page, sort_option, limit}``; exact details are
``GET /ms/candidateapi/job/{id}?companyId=main``.  Only the published board is
requested; no guessed slug, parent-wide board, or aggregator is used.
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import unescape
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html

OFFICIAL_ENTRY = "https://unacademy.com/careers"
OFFICIAL_HOSTS = frozenset({"unacademy.com", "www.unacademy.com"})
BOARD_HOST = "unacademy.darwinbox.in"
BOARD_ENTRY = f"https://{BOARD_HOST}/ms/candidate/careers"
BOARD_HOME = f"https://{BOARD_HOST}/ms/candidatev2/main/careers/home"
BOARD_LISTING = f"https://{BOARD_HOST}/ms/candidatev2/main/careers/allJobs"
API_BASE = f"https://{BOARD_HOST}/ms/candidateapi"
ALLJOBS_API = f"{API_BASE}/job/alljobs?companyId=main"
CONFIG_API = f"{API_BASE}/getCompanyConfig?is_preview=false&companyId=main"
FILTERS_API = f"{API_BASE}/job/newfilters?companyId=main"
APPLICATION_ROUTE = f"https://{BOARD_HOST}/ms/candidatev2/main/auth/login"
COMPANY_ID = "main"
SORT_OPTION = "new"
PAGE_LIMIT = 10
EXPECTED_COMPANY_NAME = "Unacademy Group"
EXPECTED_TENANT_ID = "78"
JOB_ID_RE = re.compile(r"^[0-9a-f]{14}$")
MAIN_BUNDLE_RE = re.compile(r"main\.[a-f0-9]+\.js")
LEGACY_BUNDLE_RE = re.compile(r"main\.[a-f0-9]+\.js")
IST = timezone(timedelta(hours=5, minutes=30))
CONTAINER_TAGS = frozenset({"div", "section", "article", "main", "td", "th", "tr", "tbody", "thead", "tfoot"})
BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "pre", "table"})
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
RESPONSIBILITY_TOKENS = (
    "responsibilit",
    "what you will do",
    "what you'll do",
    "what you will own",
    "what you'll own",
    "key duties",
    "the role:",
)
QUALIFICATION_TOKENS = (
    "qualif",
    "requirement",
    "what we're looking for",
    "must have",
    "who should apply",
    "good to have",
    "preferred skills",
    "preferred experience",
    "expected candidate profile",
    "key capabilities",
    "what you'll bring",
    "required skills",
    "required knowledge",
    "required competencies",
    "required certification",
    "required work experience",
    "educational qualifications",
)
PLACEHOLDER_DESCRIPTION = "please enter job description"


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalize_visible_text(value: str | None) -> str:
    """Comparable visible text: NFC, collapsed whitespace, no space before punctuation."""
    text = unicodedata.normalize("NFC", value or "").replace("\u00a0", " ")
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def decoded_description(raw: str | None) -> str:
    """Mirror the candidate detail component's one-level HTML decode of ``jd``."""
    value = raw or ""
    block = r"(?:p|div|section|article|ul|ol|h[1-6]|table|tbody|tr|td|th|blockquote|pre)"
    start = re.compile(rf"^\s*&(?:amp;)*lt;{block}(?:\s|&(?:amp;)*gt;)", re.I)
    end = re.compile(rf"&(?:amp;)*lt;/{block}&(?:amp;)*gt;\s*$", re.I)
    for _ in range(2):
        if not (start.search(value) and end.search(value)):
            break
        value = unescape(value)
    return value


def normalized_body_text(html: str | None) -> str:
    return normalize_visible_text(text_html(decoded_description(html)))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def job_detail_api_url(identity: str) -> str:
    if not JOB_ID_RE.fullmatch(str(identity)):
        raise ValueError(f"Unacademy job ID is not a 14-char hex ID: {identity!r}")
    return f"{API_BASE}/job/{identity}?companyId=main"


def detail_page_url(identity: str) -> str:
    if not JOB_ID_RE.fullmatch(str(identity)):
        raise ValueError(f"Unacademy job ID is not a 14-char hex ID: {identity!r}")
    return f"https://{BOARD_HOST}/ms/candidatev2/main/careers/jobDetails/{identity}"


def epoch_to_ist_date(value):
    if value in (None, ""):
        return None
    try:
        seconds = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=IST).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Unacademy official careers entry left its inspected HTTPS route")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Careers at Unacademy" not in title:
        raise ValueError("Unacademy official careers page title changed")
    links = []
    for anchor in soup.select("a[href]"):
        href = urljoin(response_url, anchor.get("href") or "")
        if href.rstrip("/") == BOARD_ENTRY.rstrip("/"):
            links.append({"text": normalized_title(anchor.get_text(" ", strip=True)), "href": href})
    if len(links) != 1:
        raise ValueError("Unacademy official careers page no longer publishes exactly one Darwinbox board link")
    if links[0]["text"] and "view all job openings" not in links[0]["text"].lower():
        raise ValueError("Unacademy official board link text changed")
    canonical = soup.select_one('link[rel="canonical"]')
    return {
        "url": response_url,
        "title": title,
        "canonical": canonical.get("href") if canonical else None,
        "board_link": links[0]["href"],
        "board_link_text": links[0]["text"],
    }


def parse_board_entry(html: str, response_url: str = BOARD_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname != BOARD_HOST
            or parsed.path.rstrip("/") != "/ms/candidate/careers"):
        raise ValueError("Unacademy published Darwinbox entry left its inspected tenant route")
    soup = BeautifulSoup(html, "lxml")
    base = soup.base.get("href") if soup.base else None
    scripts = [node.get("src") or "" for node in soup.select("script[src]")]
    bundle = next((src for src in scripts if LEGACY_BUNDLE_RE.fullmatch(src)), None)
    has_root = soup.select_one("app-root") is not None
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    og_node = soup.select_one('meta[property="og:title"]')
    og_title = normalized_title(og_node.get("content")) if og_node else None
    if base == "/ms/candidate/" and bundle is not None and has_root:
        kind = "legacy_candidate_shell"
    elif base is None and bundle is None and EXPECTED_COMPANY_NAME in (title, og_title):
        # Scripted HTTP receives Darwinbox's crawler/OG shell; the browser
        # observation in companies/unacademy/browser-reference-worker.json
        # proves the candidatev2 app shell for the same tenant.
        kind = "og_shell"
    else:
        raise ValueError("Unacademy published Darwinbox entry no longer exposes its inspected candidate shell")
    return {
        "url": response_url,
        "kind": kind,
        "title": title,
        "og_title": og_title,
        "base_href": base,
        "main_bundle": urljoin(response_url, bundle) if bundle else None,
        "script_count": len(scripts),
    }


def parse_board_shell(html: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname != BOARD_HOST
            or parsed.path not in ("/ms/candidatev2/main/careers/home", "/ms/candidatev2/main/careers/allJobs")):
        raise ValueError(f"Unacademy board shell left its inspected Darwinbox route: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    base = soup.base.get("href") if soup.base else None
    scripts = [node.get("src") or "" for node in soup.select("script[src]")]
    bundle = next((src for src in scripts if MAIN_BUNDLE_RE.fullmatch(src)), None)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    og_node = soup.select_one('meta[property="og:title"]')
    og_title = normalized_title(og_node.get("content")) if og_node else None
    if base == "/ms/candidatev2/" and bundle is not None:
        kind = "candidatev2_app_shell"
    elif base is None and bundle is None and EXPECTED_COMPANY_NAME in (title, og_title):
        kind = "og_shell"
    else:
        raise ValueError("Unacademy board shell changed from its inspected candidatev2 app shell")
    return {
        "url": response_url,
        "kind": kind,
        "title": title,
        "og_title": og_title,
        "base_href": base,
        "main_bundle": urljoin(response_url, bundle) if bundle else None,
        "script_count": len(scripts),
    }


def parse_config(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError("Unacademy company config response is not successful")
    message = payload.get("message")
    company = message.get("company") if isinstance(message, dict) else None
    if not isinstance(company, dict):
        raise ValueError("Unacademy company config response has no company object")
    if company.get("company_name") != EXPECTED_COMPANY_NAME:
        raise ValueError("Unacademy company config name changed")
    if str(company.get("tenant_id")) != EXPECTED_TENANT_ID:
        raise ValueError("Unacademy company config tenant id changed")
    if company.get("new_careers") is not True or company.get("is_preview") is not False:
        raise ValueError("Unacademy company config no longer describes the published careers board")
    if company.get("recruitment_enabled") is not True:
        raise ValueError("Unacademy company config no longer has recruitment enabled")
    count = company.get("allJobsCount")
    if not isinstance(count, int) or count < 0:
        raise ValueError("Unacademy company config allJobsCount is not a non-negative integer")
    return {
        "company_name": company.get("company_name"),
        "tenant_id": str(company.get("tenant_id")),
        "all_jobs_count": count,
        "new_careers": company.get("new_careers"),
        "is_preview": company.get("is_preview"),
        "recruitment_enabled": company.get("recruitment_enabled"),
        "job_tile_permissions": company.get("job_tile_permissions"),
        "job_detail_permissions": company.get("job_detail_permissions"),
        "careers_filters": company.get("careers_filters"),
    }


def parse_filters(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError("Unacademy filter-options response is not successful")
    message = payload.get("message")
    if not isinstance(message, dict):
        raise ValueError("Unacademy filter-options response has no message object")
    result = {}
    for key in ("employee_type", "functional_area", "location", "department", "job_tags", "country", "capability"):
        values = message.get(key)
        if not isinstance(values, list):
            raise ValueError(f"Unacademy filter-options response has no {key} list")
        rows = []
        for row in values:
            if not isinstance(row, dict) or not normalized_title(row.get("name")):
                raise ValueError(f"Unacademy filter-options response has an unidentified {key} option")
            item = {"id": row.get("_id") or row.get("id"), "name": normalized_title(row.get("name"))}
            if key == "location" and row.get("country"):
                item["country"] = row.get("country")
            rows.append(item)
        result[key] = rows
    return result


def parse_alljobs_payload(payload: dict, label: str) -> tuple[list, int]:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError(f"Unacademy {label} listing response is not successful")
    rows = payload.get("data")
    counts = payload.get("job_counts")
    if not isinstance(rows, list):
        raise ValueError(f"Unacademy {label} listing response has no data array")
    if not isinstance(counts, int) or counts < 0:
        raise ValueError(f"Unacademy {label} listing job_counts is not a non-negative integer")
    ids = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"Unacademy {label} listing contains a non-object row")
        identity = str(row.get("id") or "")
        if not JOB_ID_RE.fullmatch(identity):
            raise ValueError(f"Unacademy {label} listing contains a missing or invalid job ID")
        if not normalized_title(row.get("title") or row.get("designation_name")):
            raise ValueError(f"Unacademy {label} listing contains an unidentified job")
        if not isinstance(row.get("jd"), str):
            raise ValueError(f"Unacademy {label} listing row {identity} has no string jd field")
        if not isinstance(row.get("posted_on"), int):
            raise ValueError(f"Unacademy {label} listing row {identity} has no integer posted_on")
        if not normalized_title(row.get("internal_job_code")):
            raise ValueError(f"Unacademy {label} listing row {identity} has no internal job code")
        ids.append(identity)
    if len(ids) != len(set(ids)):
        raise ValueError(f"Unacademy {label} listing repeats a job ID")
    if counts < len(rows):
        raise ValueError(f"Unacademy {label} listing job_counts is smaller than its page")
    return rows, counts


def parse_detail_payload(payload: dict, identity: str) -> dict:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError(f"Unacademy detail response for {identity} is not successful")
    message = payload.get("message")
    rows = message.get("job") if isinstance(message, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError(f"Unacademy detail response for {identity} has no single job object")
    row = rows[0]
    if str(row.get("id")) != str(identity):
        raise ValueError(f"Unacademy detail response ID differs from requested ID {identity}")
    if not isinstance(row.get("jd"), str):
        raise ValueError(f"Unacademy detail response for {identity} has no string jd field")
    if not isinstance(row.get("posted_on"), int):
        raise ValueError(f"Unacademy detail response for {identity} has no integer posted_on")
    return row


def description_status(raw_jd: str | None, description_text: str | None) -> str:
    normalized = normalize_visible_text(description_text)
    if not normalized:
        return "publisher_empty"
    if normalized.lower().strip(" .") == PLACEHOLDER_DESCRIPTION:
        return "publisher_placeholder"
    if len(normalized) < 100:
        return "source_short"
    return "full"


def _is_bold(node: Tag) -> bool:
    if node.name in ("b", "strong"):
        return True
    style = (node.get("style") or "").lower()
    return "font-weight" in style and any(token in style for token in ("bold", "bolder", "600", "700", "800", "900"))


def _iter_blocks(node: Tag):
    for child in node.children:
        if not isinstance(child, Tag):
            continue
        if child.name in CONTAINER_TAGS:
            if child.find(lambda tag: tag.name in CONTAINER_TAGS or tag.name in BLOCK_TAGS, recursive=True):
                yield from _iter_blocks(child)
            else:
                yield child
        elif child.name in BLOCK_TAGS:
            if child.name == "table":
                if child.find(lambda tag: tag.name in CONTAINER_TAGS or (tag.name in BLOCK_TAGS and tag.name != "table"), recursive=True):
                    yield from _iter_blocks(child)
                else:
                    yield child
            else:
                yield child


def _heading_and_body(block: Tag):
    if block.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        heading = normalized_title(block.get_text(" ", strip=True))
        return (heading or None), None
    if block.name not in ("p", "div"):
        return None, None
    bolds = [node for node in block.find_all(["b", "strong", "span"], recursive=True) if _is_bold(node)]
    if not bolds:
        return None, None
    first = bolds[0]
    bold_text = normalized_title(first.get_text(" ", strip=True))
    if not bold_text or len(bold_text) > 120:
        return None, None
    full_text = normalized_title(block.get_text(" ", strip=True))
    if not full_text.startswith(bold_text):
        return None, None
    rest = full_text[len(bold_text):].strip()
    if not rest:
        return bold_text, None
    sibling = first.next_sibling
    while sibling is not None and getattr(sibling, "name", None) is None and not str(sibling).strip():
        sibling = sibling.next_sibling
    has_break = sibling is not None and getattr(sibling, "name", None) == "br"
    known_heading = bold_text.endswith(":") or bold_text.lower().rstrip(" :") in {
        "about the role", "the role", "key responsibilities", "requirements",
        "qualifications", "who should apply", "what we're looking for",
        "what you'll own", "must have", "good to have",
    }
    if not (has_break or known_heading):
        return None, None
    clone = BeautifulSoup(str(block), "lxml").find(block.name)
    clone_bolds = [node for node in clone.find_all(["b", "strong", "span"], recursive=True) if _is_bold(node)]
    if not clone_bolds:
        return None, None
    clone_bold = clone_bolds[0]
    following = clone_bold.next_sibling
    clone_bold.extract()
    while following is not None and getattr(following, "name", None) is None and not str(following).strip():
        following = following.next_sibling
    if following is not None and getattr(following, "name", None) == "br":
        following.extract()
    return bold_text, clone


def description_sections(description_html: str | None) -> list[dict]:
    """Keep every employer-authored heading and the exact body below it."""
    html = decoded_description(description_html)
    if not normalize_visible_text(text_html(html)):
        return []
    soup = BeautifulSoup(f'<div data-unacademy-body="true">{html}</div>', "lxml")
    root = soup.select_one("[data-unacademy-body]")
    sections = []
    current = {"heading": None, "html": "", "text": ""}
    for block in _iter_blocks(root) if root is not None else []:
        heading, body = _heading_and_body(block)
        if heading:
            if current["html"].strip() or current["heading"] is not None:
                sections.append(current)
            current = {"heading": heading, "html": str(body) if body is not None else "", "text": ""}
        else:
            current["html"] += str(block)
    if current["html"].strip() or current["heading"] is not None or not sections:
        current["text"] = text_html(current["html"])
        sections.append(current)
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def _heading_matches(heading: str | None, tokens: tuple[str, ...]) -> bool:
    if not heading:
        return False
    value = heading.lower().replace("\u2019", "'")
    return any(token in value for token in tokens)


def _role_sections(sections: list[dict], match_tokens: tuple[str, ...], stop_tokens: tuple[str, ...]) -> list[dict]:
    selected = []
    for index, section in enumerate(sections):
        if not _heading_matches(section.get("heading"), match_tokens):
            continue
        if section.get("text", "").strip():
            selected.append(section)
            continue
        for following in sections[index + 1:]:
            heading = following.get("heading")
            if _heading_matches(heading, stop_tokens) or _heading_matches(heading, match_tokens):
                break
            if following.get("text", "").strip():
                selected.append(following)
    return selected


def _locations(listing_row: dict, detail_row: dict | None) -> list[dict]:
    values = []
    for row in (detail_row or {}, listing_row):
        for key in ("tool_tip_locations", "officelocation_show_arr_list"):
            candidate = row.get(key)
            if isinstance(candidate, list):
                values = [
                    normalized_title(value)
                    for value in candidate
                    if isinstance(value, str) and value.strip() and "multiple locations" not in value.lower()
                ]
                if values:
                    break
        if values:
            break
    if not values:
        for row in (detail_row or {}, listing_row):
            for key in ("officelocation_show_arr", "locations"):
                candidate = row.get(key)
                if (isinstance(candidate, str) and candidate.strip()
                        and "multiple locations" not in candidate.lower()):
                    values = [normalized_title(candidate)]
                    break
            if values:
                break
    unique = []
    seen = set()
    for value in values:
        clean = re.sub(r"\s+", " ", value.replace("\r", " ")).strip()
        if clean and clean not in seen:
            seen.add(clean)
            unique.append({"location": clean, "address": None})
    return unique


def _salary(listing_row: dict, detail_row: dict | None) -> dict | None:
    source = detail_row or listing_row
    fields = {
        key: source.get(key, listing_row.get(key))
        for key in ("salary_currency", "salary_min", "salary_max", "salary_range", "salary_timeframe")
    }
    if not any(str(fields[key] or "").strip() for key in ("salary_currency", "salary_min", "salary_max", "salary_range")):
        return None
    return {"source_fields": fields}


def _experience(listing_row: dict, detail_row: dict | None) -> dict | None:
    source = detail_row or listing_row
    value = source.get("experience") or listing_row.get("experience")
    if not isinstance(value, str) or not value.strip():
        return None
    return {
        "source_text": normalized_title(value),
        "from_months": source.get("experience_from_num", listing_row.get("experience_from_num")),
        "to_months": source.get("experience_to_num", listing_row.get("experience_to_num")),
    }


def _workplace_type(value) -> str | None:
    if value in (1, "1", True):
        return "Remote"
    if value in (0, "0", False):
        return "On-site"
    return None


def _title(listing_row: dict, detail_row: dict | None) -> str:
    for row in (detail_row or {}, listing_row):
        for key in ("designation_display_name", "title", "designation_name", "designation_title_name"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _attachment_links(description_html: str) -> list[dict]:
    soup = BeautifulSoup(description_html or "", "lxml")
    return [
        {"label": normalized_title(anchor.get_text(" ", strip=True)) or None, "url": anchor.get("href")}
        for anchor in soup.select("a[href]")
        if ATTACHMENT_SUFFIX.search(anchor.get("href") or "")
    ]


def _description_links(description_html: str) -> list[dict]:
    soup = BeautifulSoup(description_html or "", "lxml")
    return [
        {"label": normalized_title(anchor.get_text(" ", strip=True)) or None, "url": anchor.get("href")}
        for anchor in soup.select("a[href]")
    ]


class Unacademy(Adapter):
    """Follow Unacademy's official entry to its exact Darwinbox board only."""

    VERIFICATION_REVISION = "unacademy.official-careers.darwinbox-candidatev2.1"
    RETRYABLE = ("HTTP 403", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.retry_events = []
        self.blocked = False

    async def _retry(self, request, *, attempts: int = 3, label: str = ""):
        """Retry transient 403/429/5xx responses; never solve a challenge."""
        last = None
        for attempt in range(1, attempts + 1):
            try:
                return await request()
            except FetchError as exc:
                last = exc
                message = str(exc)
                if not any(token in message for token in self.RETRYABLE):
                    raise
                self.retry_events.append({"label": label, "attempt": attempt, "error": message})
                if attempt < attempts:
                    await asyncio.sleep(1.25 * attempt)
        raise last

    def _make_job(self, listing_row: dict, detail_row: dict | None, *, detail_meta: dict | None = None):
        identity = str(listing_row["id"])
        source = detail_row or listing_row
        raw_jd = source.get("jd")
        if raw_jd is None:
            raw_jd = listing_row.get("jd") or ""
        if detail_row is not None and (detail_row.get("jd") or "") != (listing_row.get("jd") or ""):
            self.result.errors.append(f"{identity}: listing and detail jd differ")
        description_html = decoded_description(raw_jd)
        description_text = text_html(description_html)
        normalized = normalize_visible_text(description_text)
        sections = description_sections(description_html)
        responsibilities = _role_sections(sections, RESPONSIBILITY_TOKENS, QUALIFICATION_TOKENS)
        qualifications = _role_sections(sections, QUALIFICATION_TOKENS, RESPONSIBILITY_TOKENS)
        status = description_status(raw_jd, description_text)
        if status == "publisher_empty":
            self.result.errors.append(f"{identity}: publisher jd is empty/blank; no role description is published")
        elif status == "publisher_placeholder":
            self.result.errors.append(
                f"{identity}: publisher jd is the template placeholder 'Please enter job description'"
            )
        elif status == "source_short":
            self.result.errors.append(f"{identity}: description is implausibly short ({len(normalized)} chars)")
        date_posted_raw = source.get("posted_on")
        if date_posted_raw is None:
            date_posted_raw = listing_row.get("posted_on")
        date_posted = epoch_to_ist_date(date_posted_raw)
        created_on = listing_row.get("created_on") or source.get("created_on")
        posting = listing_row.get("job_posting") if isinstance(listing_row.get("job_posting"), dict) else {}
        careers = posting.get("careers") if isinstance(posting.get("careers"), dict) else {}
        valid_through = epoch_to_ist_date(careers.get("to"))
        source_ui = {
            "official_entry_url": OFFICIAL_ENTRY,
            "board_entry_url": BOARD_ENTRY,
            "board_home_url": BOARD_HOME,
            "board_listing_url": BOARD_LISTING,
            "listing_api_url": ALLJOBS_API,
            "detail_api_url": job_detail_api_url(identity),
            "detail_page_url": detail_page_url(identity),
            "apply_url": detail_page_url(identity),
            "application_control": "Apply Now",
            "application_route_observed": APPLICATION_ROUTE,
            "application_requires_sign_in": True,
            "application_turnstile_present": True,
            "employer": EXPECTED_COMPANY_NAME,
            "tenant_id": EXPECTED_TENANT_ID,
            "company_id": COMPANY_ID,
            "job_id": identity,
            "internal_job_code": source.get("internal_job_code") or listing_row.get("internal_job_code"),
            "date_fields": {
                "posted_on": listing_row.get("posted_on"),
                "job_posting_on": (detail_row or {}).get("job_posting_on"),
                "created_on": created_on,
                "job_posting": listing_row.get("job_posting"),
                "date_posted_mapping": "posted_on / job_posting_on (Unix seconds, Asia/Kolkata date) -> date_posted",
                "valid_through_mapping": "job_posting.careers.to (Unix seconds, Asia/Kolkata date) -> valid_through",
                "detail_ui_label": "Updated Date",
                "note": "created_on is retained separately; the detail UI's Updated Date matches the posted_on date in Asia/Kolkata.",
            },
            "salary_source_fields": {
                key: source.get(key, listing_row.get(key))
                for key in ("salary_currency", "salary_min", "salary_max", "salary_range", "salary_timeframe")
            },
            "description_status": status,
            "raw_jd": raw_jd,
            "description_normalized_chars": len(normalized),
            "description_normalized_sha256": sha256(normalized.encode()).hexdigest() if normalized else None,
            "description_fnv1a64": fnv1a64(normalized),
            "description_sha256": sha256(description_html.encode()).hexdigest() if description_html else None,
            "description_sections": sections,
            "role_attachments": _attachment_links(description_html),
            "description_links": _description_links(description_html),
            "detail_request": detail_meta,
        }
        raw = {"listing": listing_row, "detail": detail_row, "source_ui": source_ui}
        job = self.job(
            raw,
            title=_title(listing_row, detail_row),
            url=detail_page_url(identity),
            job_id=identity,
            requisition_id=source.get("internal_job_code") or listing_row.get("internal_job_code"),
            apply_url=detail_page_url(identity),
            description=description_html,
            method="unacademy.official_careers.darwinbox_candidateapi.job.jd",
            description_url=job_detail_api_url(identity) if detail_row is not None else ALLJOBS_API,
            locations=_locations(listing_row, detail_row),
            department=source.get("department") or listing_row.get("department_name_only"),
            employment_type=source.get("emp_type") or listing_row.get("emp_type_name"),
            workplace_type=_workplace_type(source.get("is_remote", listing_row.get("is_remote"))),
            experience=_experience(listing_row, detail_row),
            salary=_salary(listing_row, detail_row),
            date_posted=date_posted,
            date_posted_raw=date_posted_raw,
            date_updated=None,
            valid_through=valid_through,
            employer=EXPECTED_COMPANY_NAME,
            language="en",
            responsibilities=responsibilities or None,
            qualifications=qualifications or None,
        )
        job.description_text = description_text
        job.record_identity = f"unacademy:{identity}"
        if status == "publisher_empty":
            job.description_unavailable = True
            job.warnings.append("publisher_empty_description")
        elif status == "publisher_placeholder":
            job.description_unavailable = True
            job.warnings.append("publisher_placeholder_description")
        elif status == "source_short":
            job.warnings.append("source_short_description")
        if detail_row is None:
            job.warnings.append("detail_fetch_failed")
        return job

    async def _post_listing(self, page: int, snapshot: str):
        body = {"companyId": COMPANY_ID, "page": page, "sort_option": SORT_OPTION, "limit": PAGE_LIMIT}
        started = time.perf_counter()
        response = await self._retry(
            lambda: self.http.request("POST", ALLJOBS_API, json=body),
            label=f"listing page {page} ({snapshot})",
        )
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != ALLJOBS_API:
            raise ValueError(f"Unacademy {snapshot} listing request redirected away from its source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Unacademy {snapshot} listing response is not JSON") from exc
        rows, counts = parse_alljobs_payload(payload, snapshot)
        meta = {
            "page": page,
            "snapshot": snapshot,
            "request_url": str(response.request.url),
            "final_url": str(response.url),
            "request_body": body,
            "status": response.status_code,
            "request_seconds": round(seconds, 6),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "job_counts": counts,
            "rows": len(rows),
            "ids": [str(row["id"]) for row in rows],
        }
        return payload, rows, counts, meta

    async def _get_detail(self, identity: str):
        started = time.perf_counter()
        response = await self._retry(
            lambda: self.http.get(job_detail_api_url(identity)),
            label=f"detail {identity}",
        )
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != job_detail_api_url(identity):
            raise ValueError(f"Unacademy detail request for {identity} redirected away from its source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Unacademy detail response for {identity} is not JSON") from exc
        row = parse_detail_payload(payload, identity)
        meta = {
            "url": str(response.request.url),
            "final_url": str(response.url),
            "status": response.status_code,
            "request_seconds": round(seconds, 6),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        }
        return row, meta

    async def run(self):
        started = self.result._started_clock
        if self.company.slug != "unacademy" or self.company.careers_url != OFFICIAL_ENTRY:
            raise ValueError("Unacademy dedicated flow must start from its official HTTPS /careers entry")
        if str(self.source.url) not in (BOARD_LISTING, OFFICIAL_ENTRY):
            raise ValueError("Unacademy source must be the exact company-published Darwinbox board")
        if not self.page_guard():
            return self.result

        entry_started = time.perf_counter()
        try:
            entry = await self._retry(lambda: self.http.get(OFFICIAL_ENTRY), label="official entry")
            entry_observation = parse_official_entry(entry.text, str(entry.url))
        except (FetchError, ValueError) as exc:
            self.result.errors.append(f"Unacademy official entry check failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.expected_total = None
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": OFFICIAL_ENTRY,
                "membership_observable_via_http": False,
            })
            self.result.timings["official_entry_phase_seconds"] = round(max(0.0, time.perf_counter() - entry_started), 6)
            return self.result
        entry_seconds = max(0.0, time.perf_counter() - entry_started)

        board_started = time.perf_counter()
        board_entry = None
        board_shells = {}
        try:
            response = await self._retry(lambda: self.http.get(BOARD_ENTRY), label="board entry")
            board_entry = {
                "url": str(response.url), "status": response.status_code,
                "bytes": len(response.content), "sha256": sha256(response.content).hexdigest(),
                "parsed": parse_board_entry(response.text, str(response.url)),
            }
        except (FetchError, ValueError) as exc:
            self.result.errors.append(f"Unacademy published board entry check failed: {type(exc).__name__}: {exc}")
        for key, url in (("home", BOARD_HOME), ("listing", BOARD_LISTING)):
            try:
                response = await self._retry(lambda: self.http.get(url), label=f"board shell {key}")
                board_shells[key] = {
                    "url": str(response.url), "status": response.status_code,
                    "bytes": len(response.content), "sha256": sha256(response.content).hexdigest(),
                    "parsed": parse_board_shell(response.text, str(response.url)),
                }
            except (FetchError, ValueError) as exc:
                self.result.errors.append(f"Unacademy board shell {key} check failed: {type(exc).__name__}: {exc}")
                board_shells[key] = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
        board_seconds = max(0.0, time.perf_counter() - board_started)

        config = None
        filters = None
        config_meta = filters_meta = None
        config_filters_started = time.perf_counter()
        try:
            config_started = time.perf_counter()
            config_response = await self._retry(lambda: self.http.get(CONFIG_API), label="company config")
            config_seconds = max(0.0, time.perf_counter() - config_started)
            config = parse_config(config_response.json())
            config_meta = {
                "url": str(config_response.url), "status": config_response.status_code,
                "request_seconds": round(config_seconds, 6), "bytes": len(config_response.content),
                "sha256": sha256(config_response.content).hexdigest(),
            }
        except (FetchError, ValueError) as exc:
            self.result.errors.append(f"Unacademy company config check failed: {type(exc).__name__}: {exc}")
        try:
            filters_started = time.perf_counter()
            filters_response = await self._retry(
                lambda: self.http.request("POST", FILTERS_API, json={"companyId": COMPANY_ID, "id": []}),
                label="filter options",
            )
            filters_seconds = max(0.0, time.perf_counter() - filters_started)
            filters_payload = filters_response.json()
            filters = parse_filters(filters_payload)
            filters_meta = {
                "url": str(filters_response.url), "status": filters_response.status_code,
                "request_seconds": round(filters_seconds, 6), "bytes": len(filters_response.content),
                "sha256": sha256(filters_response.content).hexdigest(),
                "raw_message": filters_payload.get("message"),
            }
        except (FetchError, ValueError) as exc:
            self.result.errors.append(f"Unacademy filter-options check failed: {type(exc).__name__}: {exc}")
        config_filters_seconds = max(0.0, time.perf_counter() - config_filters_started)

        listing_started = time.perf_counter()
        page_reconciliation = []
        try:
            _, initial_rows, initial_counts, initial_meta = await self._post_listing(1, "initial")
        except (FetchError, ValueError) as exc:
            self.result.errors.append(f"Unacademy listing failed: {type(exc).__name__}: {exc}")
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.pages = 1
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": OFFICIAL_ENTRY,
                "official_entry_observation": entry_observation,
                "board_entry_observation": board_entry,
                "board_shell_observations": board_shells,
                "company_config": config,
                "company_config_request": config_meta,
                "filter_options": filters,
                "filter_options_request": filters_meta,
                "membership_authority": (
                    "Unacademy official /careers page's 'View all job openings' link -> exact Darwinbox "
                    "tenant board; its POST /ms/candidateapi/job/alljobs?companyId=main listing request"
                ),
                "listing_api_url": ALLJOBS_API,
                "membership_observable_via_http": False,
                "collection_wall_seconds": round(max(0.0, time.perf_counter() - started), 6),
            })
            self.result.timings["official_entry_phase_seconds"] = round(entry_seconds, 6)
            self.result.timings["board_shell_phase_seconds"] = round(board_seconds, 6)
            self.result.timings["config_filters_phase_seconds"] = round(config_filters_seconds, 6)
            self.result.timings["listing_phase_seconds"] = round(max(0.0, time.perf_counter() - listing_started), 6)
            return self.result

        page_reconciliation.append(initial_meta)
        all_rows = list(initial_rows)
        seen = {str(row["id"]) for row in all_rows}
        total = initial_counts
        page = 2
        while len(all_rows) < total and page <= 100:
            if not self.page_guard():
                self.result.errors.append("Unacademy listing page budget exhausted")
                break
            try:
                _, rows, page_counts, meta = await self._post_listing(page, f"page{page}")
            except (FetchError, ValueError) as exc:
                self.result.errors.append(f"Unacademy listing page {page} failed: {type(exc).__name__}: {exc}")
                break
            page_reconciliation.append(meta)
            if page_counts != total:
                self.result.errors.append("Unacademy listing job_counts changed between pages")
                break
            if not rows:
                self.result.errors.append("Unacademy listing ended before job_counts was reached")
                break
            new_rows = [row for row in rows if str(row["id"]) not in seen]
            if not new_rows:
                self.result.errors.append("Unacademy listing repeated IDs before job_counts was reached")
                break
            for row in new_rows:
                seen.add(str(row["id"]))
            all_rows.extend(new_rows)
            page += 1

        pagination_probe = None
        if len(all_rows) == total:
            if not self.page_guard():
                self.result.errors.append("Unacademy pagination-probe page budget exhausted")
            else:
                try:
                    _, probe_rows, probe_counts, probe_meta = await self._post_listing(2, "initial-pagination-probe")
                    page_reconciliation.append(probe_meta)
                    pagination_probe = {
                        "page": 2,
                        "rows": len(probe_rows),
                        "job_counts": probe_counts,
                        "ids": [str(row["id"]) for row in probe_rows],
                    }
                    if probe_rows:
                        self.result.errors.append("Unacademy pagination probe page 2 unexpectedly returned rows")
                    if probe_counts != total:
                        self.result.errors.append("Unacademy pagination probe job_counts differs from page 1")
                except (FetchError, ValueError) as exc:
                    self.result.errors.append(
                        f"Unacademy pagination probe failed: {type(exc).__name__}: {exc}"
                    )

        initial_ids = [str(row["id"]) for row in initial_rows]
        initial_membership_ids = [str(row["id"]) for row in all_rows]
        if len(all_rows) != total:
            self.result.errors.append("Unacademy collected rows do not match advertised job_counts")
        if config is not None and config["all_jobs_count"] != total:
            self.result.errors.append("Unacademy company config allJobsCount differs from listing job_counts")
        listing_seconds = max(0.0, time.perf_counter() - listing_started)

        detail_started = time.perf_counter()
        for row in all_rows:
            identity = str(row["id"])
            detail_row = None
            detail_meta = None
            if self.blocked:
                self.result.errors.append(f"Unacademy exact detail {identity} skipped after a WAF/challenge response")
                self.add(self._make_job(row, None))
                continue
            try:
                detail_row, detail_meta = await self._get_detail(identity)
            except (FetchError, ValueError) as exc:
                self.result.errors.append(f"Unacademy exact detail {identity} failed: {type(exc).__name__}: {exc}")
                if "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
                    self.blocked = True
            if detail_row is not None:
                if normalized_title(detail_row.get("title")) != normalized_title(row.get("title")):
                    self.result.errors.append(f"Unacademy listing and detail title differ for {identity}")
            self.add(self._make_job(row, detail_row, detail_meta=detail_meta))
        detail_seconds = max(0.0, time.perf_counter() - detail_started)

        final_started = time.perf_counter()
        final_ok = True
        final_counts = None
        final_page_one_ids = []
        final_membership_ids = []
        final_probe = None
        unique_pages = max(1, page - 1)
        for final_page in range(1, unique_pages + 1):
            if not self.page_guard():
                self.result.errors.append("Unacademy final listing page budget exhausted")
                final_ok = False
                break
            try:
                _, rows, counts, meta = await self._post_listing(final_page, f"final-page{final_page}")
            except (FetchError, ValueError) as exc:
                self.result.errors.append(
                    f"Unacademy final listing page {final_page} failed: {type(exc).__name__}: {exc}"
                )
                final_ok = False
                break
            page_reconciliation.append(meta)
            if final_counts is None:
                final_counts = counts
            elif counts != final_counts:
                self.result.errors.append("Unacademy final listing job_counts changed between pages")
                final_ok = False
                break
            if final_page == 1:
                final_page_one_ids = [str(row["id"]) for row in rows]
            final_membership_ids.extend(str(row["id"]) for row in rows)
        if final_ok and len(all_rows) == total:
            if not self.page_guard():
                self.result.errors.append("Unacademy final pagination-probe page budget exhausted")
                final_ok = False
            else:
                try:
                    _, probe_rows, probe_counts, probe_meta = await self._post_listing(2, "final-pagination-probe")
                    page_reconciliation.append(probe_meta)
                    final_probe = {
                        "page": 2,
                        "rows": len(probe_rows),
                        "job_counts": probe_counts,
                        "ids": [str(row["id"]) for row in probe_rows],
                    }
                    if probe_rows:
                        self.result.errors.append("Unacademy final pagination probe page 2 unexpectedly returned rows")
                        final_ok = False
                    if probe_counts != total:
                        self.result.errors.append("Unacademy final pagination probe job_counts differs from page 1")
                        final_ok = False
                except (FetchError, ValueError) as exc:
                    self.result.errors.append(
                        f"Unacademy final pagination probe failed: {type(exc).__name__}: {exc}"
                    )
                    final_ok = False
        if final_ok and len(final_membership_ids) != total:
            self.result.errors.append("Unacademy final listing rows do not match advertised job_counts")
            final_ok = False
        membership_changed = bool(
            final_ok and (initial_membership_ids != final_membership_ids or final_counts != total)
        )
        if membership_changed:
            self.result.errors.append("Unacademy listing membership changed between initial and final snapshots")
        membership_complete = len(all_rows) == total and total >= 0 and not membership_changed
        final_listing_seconds = max(0.0, time.perf_counter() - final_started)

        self.result.expected_total = total
        self.result.listing_complete = membership_complete and final_ok and len(self.result.jobs) == total
        if total == 0 and not self.result.errors:
            self.result.warnings.append("official_board_currently_lists_zero_open_jobs")
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_observation": entry_observation,
            "board_entry_url": BOARD_ENTRY,
            "board_entry_observation": board_entry,
            "board_home_url": BOARD_HOME,
            "board_listing_url": BOARD_LISTING,
            "board_shell_observations": board_shells,
            "tenant_id": EXPECTED_TENANT_ID,
            "company_id": COMPANY_ID,
            "employer": EXPECTED_COMPANY_NAME,
            "company_config": config,
            "company_config_request": config_meta,
            "filter_options": filters,
            "filter_options_request": filters_meta,
            "filters_applied": False,
            "listing_api_url": ALLJOBS_API,
            "listing_request_shape": {
                "companyId": COMPANY_ID, "page": "UI page state",
                "sort_option": SORT_OPTION, "limit": PAGE_LIMIT,
            },
            "membership_authority": (
                "Unacademy official /careers page's 'View all job openings' link -> exact Darwinbox "
                "tenant board; its POST /ms/candidateapi/job/alljobs?companyId=main listing request"
            ),
            "listing_filter_behavior": (
                "Department, Location, Employee Type, Job Tag, Country and Experience filters are exposed "
                "by the board; the unfiltered listing request sends only companyId/page/sort_option/limit."
            ),
            "ui_pagination": (
                "The UI requests page=1 with limit=10 and shows all 7 current rows with no Load More control; "
                "a page=2 probe returns zero rows and the same job_counts."
            ),
            "initial_ids": initial_ids,
            "final_ids": final_page_one_ids,
            "collected_ids": [str(job.job_id) for job in self.result.jobs],
            "initial_membership_ids": initial_membership_ids,
            "final_membership_ids": final_membership_ids,
            "initial_job_counts": initial_counts,
            "final_job_counts": final_counts,
            "initial_pagination_probe": pagination_probe,
            "final_pagination_probe": final_probe,
            "listing_pages_fetched": len(page_reconciliation),
            "unique_listing_pages": unique_pages,
            "page_reconciliation": page_reconciliation,
            "membership_changed": membership_changed,
            "description_source": (
                "Each exact listed row's jd field, confirmed against GET job/{id} detail objects "
                "(message.job[0]); the escaped HTML wrapper is decoded once."
            ),
            "description_status_by_id": {
                str(job.job_id): job.raw["source_ui"]["description_status"] for job in self.result.jobs
            },
            "representative_detail_ids_inspected": (
                [str(all_rows[0]["id"]), str(all_rows[-1]["id"])] if all_rows else []
            ),
            "empty_description_ids": [
                str(job.job_id) for job in self.result.jobs if job.description_unavailable
            ],
            "date_semantics": {
                "date_posted": "posted_on / job_posting_on (Unix seconds, Asia/Kolkata date); detail UI 'Updated Date'",
                "date_updated": None,
                "valid_through": "job_posting.careers.to (Unix seconds, Asia/Kolkata date); not displayed by the detail UI",
                "created_on_retained_raw": True,
                "detail_ui_label": "Updated Date",
            },
            "application_route": {
                "detail_page_apply_control": "Apply Now",
                "observed_route": APPLICATION_ROUTE,
                "requires_sign_in": True,
                "turnstile_present": True,
                "submitted": False,
                "note": "The public detail page is the apply entry; Apply Now opens the candidate sign-in form.",
            },
            "membership_observable_via_http": True,
            "retry_events": self.retry_events,
            "waf_detail_blocked": self.blocked,
            "collection_wall_seconds": round(max(0.0, time.perf_counter() - started), 6),
        })
        self.result.timings["official_entry_phase_seconds"] = round(entry_seconds, 6)
        self.result.timings["board_shell_phase_seconds"] = round(board_seconds, 6)
        self.result.timings["config_filters_phase_seconds"] = round(config_filters_seconds, 6)
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["final_listing_phase_seconds"] = round(final_listing_seconds, 6)
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        return self.result
