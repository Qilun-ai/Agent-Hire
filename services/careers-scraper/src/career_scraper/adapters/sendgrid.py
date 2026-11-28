"""SendGrid's reviewed official careers journey and Twilio attribution scope.

SendGrid's ledger URL ``https://sendgrid.com/en-us/careers`` is current and
official, but SendGrid has been absorbed into Twilio and no longer publishes a
SendGrid-only job board.  The ledger URL returns a 301 to Twilio's SendGrid
product page::

    https://sendgrid.com/en-us/careers
      -> 301 https://www.twilio.com/en-us/sendgrid

That product page links to Twilio careers.  The canonical SendGrid careers path
``https://sendgrid.com/careers`` redirects to the parent-wide Eightfold board
``https://jobs.twilio.com/careers``.

The user's 2026-11-08 scope decision for absorbed companies makes this parent
board acceptable only for postings explicitly attributed to SendGrid:

* a structured field (legal employer, business unit, brand/team/organization)
  whose value names SendGrid, or
* when no such field exists, an explicit SendGrid company name in the job title
  or in the role's own hiring-business header.

Roles that merely mention SendGrid as a product or skill are excluded.  In the
reviewed snapshot the parent board has 127 rows; structured fields are only
Twilio-wide ``business_unit`` values (S&M/R&D/G&A), no title contains SendGrid,
and exactly one role's description header says it is "within Twilio SendGrid".
One sales role lists SendGrid as one of three products and is excluded.

The adapter enumerates the complete parent board, fetches every posted row's
detail and apply payload, applies the rule to every row, and collects only the
attributed postings.  It never guesses an ATS slug and never treats a keyword
search as scope proof.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from hashlib import sha256
import html as html_module
import json
import math
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter


OFFICIAL_ENTRY = "https://sendgrid.com/en-us/careers"
PRODUCT_PAGE = "https://www.twilio.com/en-us/sendgrid"
CANONICAL_ENTRY = "https://sendgrid.com/careers"
CANONICAL_HOP = "https://www.twilio.com/en-us/company/jobs"
TWILIO_CAREERS = "https://www.twilio.com/en-us/careers"
BOARD_URL = "https://jobs.twilio.com/careers"
BOARD_HOST = "jobs.twilio.com"
BOARD_DOMAIN = "twilio.com"
BOARD_TITLE = "Careers at Twilio"
BOARD_SEARCH_API = "https://jobs.twilio.com/api/pcsx/search"
POSITION_API = "https://jobs.twilio.com/api/pcsx/position_details"
APPLY_API = "https://jobs.twilio.com/api/apply/v2/jobs"
BOARD_SEARCH_PATH = "/api/pcsx/search"
POSITION_PATH = "/api/pcsx/position_details"
APPLY_PATH_PREFIX = "/api/apply/v2/jobs/"
JOB_PATH = re.compile(r"^/careers/job/(\d+)$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PAGE_SIZE = 10  # The observed UI asks for 10; larger `num` values are ignored.
INSPECTED_PARENT_BOARD_JOBS = 127
INSPECTED_PARENT_BOARD_PAGES = 13
INSPECTED_PARENT_BOARD_DEPARTMENTS = 22
INSPECTED_PARENT_BOARD_LOCATIONS = 14
INSPECTED_KEYWORD_IDS = ("1099556138460", "1099554230881")
SCOPE_DECISION_DATE = "2026-11-08"
SCOPE_RULE = (
    "Absorbed-company membership (user decision 2026-11-08): a parent-board "
    "posting belongs to SendGrid only when a structured legal-employer / "
    "business-unit / brand / team / organization field names SendGrid, or, when "
    "no such field exists, the job title or the role's own hiring-business header "
    "explicitly names SendGrid. Product/skill mentions are excluded."
)
SENDGRID_RE = re.compile(r"\bsend\s*grid\b", re.I)
STRUCTURED_KEY_RE = re.compile(
    r"(legal[_\s-]*employer|business[_\s-]*unit|brand|team|organi[sz]ation|"
    r"org[_\s-]*unit|division|company|employer)",
    re.I,
)
EXCLUDED_STRUCTURED_KEYS = {
    "companyName", "companyLogo", "company_url", "company_logo",
    "jobDescription", "job_description", "description", "description_html",
}
HEADER_END_RE = re.compile(
    r"\b(Responsibilities|What you(?:'|’)ll do|In this role|The role|Your role|"
    r"Your responsibilities|What you will do)\b",
    re.I,
)
ATTRIBUTION_PATTERNS = (
    re.compile(
        r"\b(?:within|at|part\s+of|join(?:ing)?|on)\s+(?:the\s+)?"
        r"(?:twilio\s+)?send\s*grid(?:\s+(?:team|business|division|"
        r"org(?:anization)?|unit))?\b",
        re.I,
    ),
    re.compile(
        r"\b(?:twilio\s+)?send\s*grid(?:'s|’s)?\s+"
        r"(?:team|business|division|org(?:anization)?|unit|email\s+platform)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:hiring\s+(?:business|team|org(?:anization)?|unit))\s*[:\-]\s*"
        r"(?:twilio\s+)?send\s*grid\b",
        re.I,
    ),
    re.compile(
        r"\b(?:twilio\s+)?send\s*grid\s+is\s+(?:hiring|looking\s+for)\b",
        re.I,
    ),
)
PRODUCT_CONTEXT_RE = re.compile(
    r"(?:(?:products?|portfolio|suite|platforms?|communications|segment)[^.]{0,140}"
    r"\bsend\s*grid\b)|(?:\bsend\s*grid\b[^.]{0,140}"
    r"(?:products?|portfolio|suite|platforms?|segment|communications))",
    re.I,
)


def normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def description_text(value: str | None) -> str:
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return normalized_text(soup.get_text(" "))


def redirect_hops(response) -> list[dict]:
    return [
        {
            "status": hop.status_code,
            "url": str(hop.request.url),
            "location": hop.headers.get("location"),
        }
        for hop in response.history
    ]


def _unique_links(soup: BeautifulSoup, predicate) -> list[dict]:
    rows, seen = [], set()
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or not predicate(href):
            continue
        text = normalized_text(anchor.get_text(" ", strip=True))
        key = (text, href)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"text": text, "href": href})
    return rows


def parse_official_entry(html: str, response_url: str, history: list[dict]) -> dict:
    """Validate the exact ledger URL -> SendGrid product page journey."""
    if response_url.rstrip("/") != PRODUCT_PAGE:
        raise ValueError(f"SendGrid official entry ended outside the SendGrid product page: {response_url}")
    first = history[0] if history else {}
    if (first.get("url") != OFFICIAL_ENTRY or first.get("status") != 301
            or first.get("location") != PRODUCT_PAGE):
        raise ValueError(f"SendGrid official entry redirect changed: {history!r}")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalized_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "SendGrid" not in title or "Twilio" not in title:
        raise ValueError(f"SendGrid product page identity changed: {title!r}")
    careers_links = _unique_links(
        soup, lambda href: href.rstrip("/").endswith("/en-us/careers")
    )
    jobs_links = _unique_links(
        soup, lambda href: href.startswith(("http://jobs.twilio.com/", "https://jobs.twilio.com/"))
    )
    if not careers_links or not jobs_links:
        raise ValueError("SendGrid product page no longer publishes both careers and jobs links")
    return {
        "url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "entry_status": 301,
        "status": 200,
        "title": title,
        "history": history,
        "careers_links": careers_links,
        "jobs_links": jobs_links,
    }


def parse_canonical_entry(html: str, response_url: str, history: list[dict]) -> dict:
    """Validate the canonical SendGrid careers path -> parent Twilio board."""
    if response_url.rstrip("/") != BOARD_URL:
        raise ValueError(f"SendGrid canonical careers path ended outside the Twilio board: {response_url}")
    if not history:
        raise ValueError("SendGrid canonical careers path no longer redirects")
    first = history[0]
    if (first.get("url") != CANONICAL_ENTRY or first.get("status") != 301
            or first.get("location") != CANONICAL_HOP):
        raise ValueError(f"SendGrid canonical careers redirect changed: {history!r}")
    if not any(hop.get("location") == "https://jobs.twilio.com" for hop in history):
        raise ValueError(f"SendGrid canonical careers path no longer reaches jobs.twilio.com: {history!r}")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalized_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "Careers at Twilio" not in title:
        raise ValueError(f"SendGrid canonical careers target is not the Twilio board: {title!r}")
    return {
        "url": CANONICAL_ENTRY,
        "final_url": response_url,
        "entry_status": 301,
        "status": 200,
        "title": title,
        "history": history,
    }


def parse_twilio_careers_page(html: str, response_url: str) -> dict:
    """Validate the Twilio careers marketing page's published board link."""
    if response_url.rstrip("/") != TWILIO_CAREERS:
        raise ValueError(f"Twilio careers page redirected unexpectedly: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalized_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "Careers" not in title or "Twilio" not in title:
        raise ValueError(f"Twilio careers page identity changed: {title!r}")
    links = _unique_links(
        soup, lambda href: href.startswith(("http://jobs.twilio.com/", "https://jobs.twilio.com/"))
    )
    if not any(href.split("?")[0].rstrip("/") == BOARD_URL for href in (row["href"] for row in links)):
        raise ValueError("Twilio careers page no longer links to jobs.twilio.com/careers")
    return {
        "url": TWILIO_CAREERS,
        "final_url": response_url,
        "title": title,
        "jobs_links": links,
    }


def _embedded_pcsx_config(html: str) -> dict:
    for block in re.findall(r"<code[^>]*>(.*?)</code>", html or "", re.S):
        text = html_module.unescape(block)
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("configs"), dict):
            return value
    raise ValueError("Twilio board no longer embeds its PCSX configuration")


def parse_board_shell(html: str, response_url: str) -> dict:
    """Validate the parent board's identity and configuration (not membership)."""
    if response_url.rstrip("/") != BOARD_URL:
        raise ValueError(f"Twilio board shell came from an unexpected URL: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalized_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if BOARD_TITLE not in title:
        raise ValueError(f"Twilio board title changed: {title!r}")
    group = re.search(r"window\._EF_GROUP_ID\s*=\s*[\"']([^\"']+)[\"']", html or "")
    if not group or group.group(1) != BOARD_DOMAIN:
        raise ValueError("Twilio board group id changed")
    config = _embedded_pcsx_config(html)
    pcsx = config.get("configs", {}).get("pcsxConfig")
    search = pcsx.get("searchConfig") if isinstance(pcsx, dict) else None
    if not isinstance(search, dict):
        raise ValueError("Twilio board search configuration is missing")
    base = search.get("basePositionFq")
    smart = search.get("smartFilters")
    all_filters = search.get("allFilters")
    if not isinstance(base, str) or "position.system_id:greenhouse" not in base:
        raise ValueError(f"Twilio board position scope changed: {base!r}")
    if not isinstance(smart, list) or not isinstance(all_filters, list):
        raise ValueError("Twilio board filter configuration changed")
    return {
        "url": BOARD_URL,
        "final_url": response_url,
        "title": title,
        "group_id": group.group(1),
        "base_position_fq": base,
        "smart_filters": smart,
        "all_filters": all_filters,
        "sendgrid_in_config": "sendgrid" in json.dumps(config, ensure_ascii=False).lower(),
    }


def _error_is_empty(value) -> bool:
    if value in (None, "", {}, []):
        return True
    if isinstance(value, dict):
        return not (value.get("message") or value.get("body"))
    return False


def _parse_filters(filter_def: dict) -> dict:
    filters = {}
    for item in filter_def.get("smartFilters") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("filterName")
        if not isinstance(name, str) or not name:
            continue
        options = []
        for option in item.get("options") or []:
            if not isinstance(option, dict):
                continue
            options.append({
                "label": normalized_text(option.get("label")),
                "value": normalized_text(option.get("value")),
            })
        filters[name] = {
            "title": normalized_text(item.get("title")),
            "options": options,
        }
    return filters


def parse_board_search(payload: dict, requested_start: int) -> dict:
    """Parse one exact page of the Twilio board's own PCSX search response."""
    if not isinstance(payload, dict):
        raise ValueError("Twilio board search response is not an object")
    if payload.get("status") != 200:
        raise ValueError(f"Twilio board search returned status {payload.get('status')!r}")
    if not _error_is_empty(payload.get("error")):
        raise ValueError(f"Twilio board search returned an error: {payload.get('error')!r}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Twilio board search response is missing data")
    count = data.get("count")
    positions = data.get("positions")
    filter_def = data.get("filterDef")
    if not isinstance(count, int) or count < 0:
        raise ValueError("Twilio board search count changed")
    if not isinstance(positions, list):
        raise ValueError("Twilio board search positions changed")
    if not isinstance(filter_def, dict):
        raise ValueError("Twilio board search filter definition changed")
    if len(positions) > PAGE_SIZE:
        raise ValueError(f"Twilio board returned {len(positions)} rows for a {PAGE_SIZE}-row page")

    filters = _parse_filters(filter_def)
    facets = filter_def.get("facets") if isinstance(filter_def.get("facets"), dict) else {}
    filters_text = json.dumps({"filters": filters, "facets": facets}, ensure_ascii=False).lower()
    sendgrid_filter = "sendgrid" in filters_text

    rows, seen, duplicate_ids, issues = [], set(), [], []
    for row in positions:
        if not isinstance(row, dict):
            issues.append("twilio_board_row_not_object")
            continue
        identity = str(row.get("id", ""))
        if not re.fullmatch(r"\d+", identity):
            issues.append("twilio_board_row_missing_numeric_id")
            continue
        if identity in seen:
            duplicate_ids.append(identity)
            issues.append(f"twilio_board_duplicate_id:{identity}")
            continue
        position_url = row.get("positionUrl")
        match = JOB_PATH.fullmatch(position_url or "")
        if match is None or match.group(1) != identity:
            issues.append(f"twilio_board_row_position_url_mismatch:{identity}")
            continue
        title = normalized_text(row.get("name"))
        if not title:
            issues.append(f"twilio_board_row_missing_title:{identity}")
            continue
        if not isinstance(row.get("locations"), list):
            issues.append(f"twilio_board_row_locations_missing:{identity}")
        if not isinstance(row.get("department"), str) or not row.get("department").strip():
            issues.append(f"twilio_board_row_department_missing:{identity}")
        if not isinstance(row.get("creationTs"), int):
            issues.append(f"twilio_board_row_creation_missing:{identity}")
        seen.add(identity)
        rows.append(dict(row))

    expected_rows = min(PAGE_SIZE, max(0, count - requested_start))
    if len(positions) != expected_rows:
        issues.append(
            f"twilio_board_page_row_count_mismatch:{requested_start}:{len(positions)}!={expected_rows}"
        )
    return {
        "start": requested_start,
        "count": count,
        "page_size": PAGE_SIZE,
        "expected_rows": expected_rows,
        "rows": rows,
        "row_count": len(rows),
        "filters": filters,
        "facets": facets,
        "sendgrid_filter_present": sendgrid_filter,
        "sendgrid_titled_rows": [row for row in rows if SENDGRID_RE.search(normalized_text(row.get("name")))],
        "sendgrid_titled_count": sum(bool(SENDGRID_RE.search(normalized_text(row.get("name")))) for row in rows),
        "duplicate_ids": duplicate_ids,
        "issues": issues,
    }


def parse_keyword_search(payload: dict) -> dict:
    page = parse_board_search(payload, 0)
    data = payload.get("data") if isinstance(payload, dict) else {}
    results_meta = data.get("resultsMetaData") if isinstance(data, dict) else {}
    return {
        **page,
        "query": "SendGrid",
        "used_fuzz_search": bool((results_meta or {}).get("usedFuzzSearch")),
        "applied_filters": (data or {}).get("appliedFilters") or {},
        "keyword_match_titles": [normalized_text(row.get("name")) for row in page["rows"]],
        "sendgrid_titled_count": page["sendgrid_titled_count"],
    }


def hiring_header(text: str | None) -> str:
    """Return the role's own hiring-business header.

    The header is the leading description text before the first responsibilities
    section; it names the team/business the role sits in.  Cap at 1500 chars so
    a malformed document cannot turn a whole body into a "header".
    """
    value = normalized_text(text)
    match = HEADER_END_RE.search(value)
    end = match.start() if match else min(len(value), 1500)
    return value[:min(end, 1500)].strip()


def _mention_contexts(text: str, width: int = 90) -> list[str]:
    rows = []
    for match in SENDGRID_RE.finditer(text):
        rows.append(text[max(0, match.start() - width):min(len(text), match.end() + width)].strip())
    return rows


def _iter_structured_fields(value, prefix: str = "") -> list[tuple[str, object]]:
    """Yield public structured fields whose keys can attribute a legal employer."""
    rows: list[tuple[str, object]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in EXCLUDED_STRUCTURED_KEYS:
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            if STRUCTURED_KEY_RE.search(str(key)):
                if not isinstance(item, (dict, list)):
                    rows.append((path, item))
                elif isinstance(item, list) and all(not isinstance(x, (dict, list)) for x in item):
                    rows.append((path, item))
            if isinstance(item, (dict, list)):
                rows.extend(_iter_structured_fields(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, (dict, list)):
                rows.extend(_iter_structured_fields(item, f"{prefix}[{index}]"))
    return rows


def structured_fields(detail: dict, apply_payload: dict) -> dict:
    """Collect the structured the board publishes for employer/business attribution."""
    values: dict[str, object] = {}
    for path, value in _iter_structured_fields(detail, "detail"):
        values[path] = value
    for path, value in _iter_structured_fields(apply_payload, "apply"):
        values[path] = value
    # These top-level names are always audited even when their key casing changes.
    for prefix, payload in (("detail", detail), ("apply", apply_payload)):
        for key in ("business_unit", "department", "efcustomTextDepartmentObject",
                    "legal_employer", "brand", "team", "organization", "org_unit",
                    "division", "employer", "company", "companyName"):
            if key in payload and f"{prefix}.{key}" not in values:
                values[f"{prefix}.{key}"] = payload[key]
    return values


def classify_posting(detail: dict, apply_payload: dict | None = None) -> dict:
    """Apply the user's absorbed-company attribution rule to one parent posting."""
    apply_payload = apply_payload or {}
    title = normalized_text(detail.get("title") or detail.get("name") or apply_payload.get("name"))
    text = normalized_text(detail.get("description_text") or description_text(detail.get("description_html")))
    header = hiring_header(text)
    fields = structured_fields(detail, apply_payload)
    structured_matches = [
        {"field": path, "value": value}
        for path, value in fields.items()
        if SENDGRID_RE.search(json.dumps(value, ensure_ascii=False))
    ]
    title_match = bool(SENDGRID_RE.search(title))
    header_mention = bool(SENDGRID_RE.search(header))
    phrase = next(
        (match.group(0) for pattern in ATTRIBUTION_PATTERNS for match in pattern.finditer(header)),
        None,
    )
    product_context = bool(PRODUCT_CONTEXT_RE.search(header))
    full_mentions = len(SENDGRID_RE.findall(text))

    rule = None
    included = False
    exclusion = None
    if structured_matches:
        rule, included = "structured_field", True
    elif title_match:
        rule, included = "job_title", True
    elif header_mention and phrase:
        rule, included = "description_header", True
    elif header_mention:
        exclusion = "product_or_generic_mention_in_header"
    elif full_mentions:
        exclusion = "body_mention_only"
    else:
        exclusion = "no_sendgrid_attribution"

    reason = {
        "structured_field": "Structured attribution field names SendGrid.",
        "job_title": "Job title explicitly names SendGrid.",
        "description_header": (
            "Role's own hiring-business header explicitly names SendGrid"
            + (f" ({phrase!r})." if phrase else ".")
        ),
        "product_or_generic_mention_in_header": (
            "SendGrid appears in the header only as a product/brand mention, not as the hiring business."
        ),
        "body_mention_only": "SendGrid appears only below the hiring-business header.",
        "no_sendgrid_attribution": "No structured, title or header attribution to SendGrid.",
    }[rule or exclusion]
    return {
        "job_id": str(detail.get("job_id") or apply_payload.get("id") or ""),
        "title": title,
        "department": detail.get("department"),
        "business_unit": apply_payload.get("business_unit"),
        "included": included,
        "rule": rule,
        "exclusion": exclusion,
        "reason": reason,
        "structured_fields": fields,
        "structured_matches": structured_matches,
        "title_match": title_match,
        "header_mention": header_mention,
        "header_attribution_phrase": phrase,
        "header_product_context": product_context,
        "header_text": header,
        "full_text_sendgrid_mentions": full_mentions,
        "mention_contexts": _mention_contexts(text),
    }


def build_scope_audit(classifications: list[dict], *, parent_count: int,
                      keyword: dict | None = None) -> dict:
    """Summarise every row's rule outcome and the borderline exclusions."""
    included = [row for row in classifications if row["included"]]
    excluded = [row for row in classifications if not row["included"]]
    rule_counts = Counter(row["rule"] for row in included)
    exclusion_counts = Counter(row["exclusion"] for row in excluded)
    business_units = Counter(
        row["business_unit"] for row in classifications if row.get("business_unit")
    )
    title_hits = [row for row in classifications if row["title_match"]]
    structured_hits = [row for row in classifications if row["structured_matches"]]
    header_hits = [row for row in classifications if row["header_mention"]]
    product_exclusions = [
        row for row in excluded
        if row["exclusion"] == "product_or_generic_mention_in_header"
    ]
    body_exclusions = [row for row in excluded if row["exclusion"] == "body_mention_only"]
    return {
        "scope_rule": SCOPE_RULE,
        "scope_decision_date": SCOPE_DECISION_DATE,
        "parent_board_count": parent_count,
        "rows_classified": len(classifications),
        "included_total": len(included),
        "excluded_total": len(excluded),
        "rule_counts": {
            "structured_field": rule_counts.get("structured_field", 0),
            "job_title": rule_counts.get("job_title", 0),
            "description_header": rule_counts.get("description_header", 0),
        },
        "exclusion_counts": dict(exclusion_counts),
        "structured_field_values": dict(business_units),
        "structured_field_values_note": (
            "The only structured employer/business values on the parent board are Twilio-wide "
            "business_unit groups; none names SendGrid. No legal-employer, brand, team or "
            "organization field is published."
        ),
        "structured_field_hits": [
            {"job_id": row["job_id"], "title": row["title"], "matches": row["structured_matches"]}
            for row in structured_hits
        ],
        "title_hits": [{"job_id": row["job_id"], "title": row["title"]} for row in title_hits],
        "description_header_hits": [
            {
                "job_id": row["job_id"],
                "title": row["title"],
                "phrase": row["header_attribution_phrase"],
                "header_text": row["header_text"],
            }
            for row in included if row["rule"] == "description_header"
        ],
        "header_mentions_total": len(header_hits),
        "borderline_exclusions": [
            {
                "job_id": row["job_id"],
                "title": row["title"],
                "exclusion": row["exclusion"],
                "reason": row["reason"],
                "header_text": row["header_text"],
                "mentions": row["mention_contexts"],
            }
            for row in product_exclusions + body_exclusions
        ],
        "included_ids": [row["job_id"] for row in included],
        "excluded_ids": [row["job_id"] for row in excluded],
        "keyword_probe": keyword or {},
        "reconciliation": {
            "parent_board_rows": parent_count,
            "classified_rows": len(classifications),
            "included_rows": len(included),
            "excluded_rows": len(excluded),
            "included_plus_excluded": len(included) + len(excluded),
        },
    }


def parse_position_detail(payload: dict, expected_id: str) -> dict:
    """Parse one exact Twilio board detail payload as evidence."""
    if not isinstance(payload, dict) or payload.get("status") != 200:
        raise ValueError(f"Twilio position detail {expected_id} returned an unexpected status")
    if not _error_is_empty(payload.get("error")):
        raise ValueError(f"Twilio position detail {expected_id} returned an error")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"Twilio position detail {expected_id} is missing data")
    identity = str(data.get("id", ""))
    if identity != str(expected_id):
        raise ValueError(f"Twilio position detail id changed: {identity!r} != {expected_id!r}")
    title = normalized_text(data.get("name"))
    if not title:
        raise ValueError(f"Twilio position detail {expected_id} is missing its title")
    public_url = data.get("publicUrl")
    expected_url = f"https://jobs.twilio.com/careers/job/{expected_id}"
    if public_url != expected_url:
        raise ValueError(f"Twilio position detail {expected_id} public URL changed: {public_url!r}")
    position_url = data.get("positionUrl")
    if position_url != f"/careers/job/{expected_id}":
        raise ValueError(f"Twilio position detail {expected_id} position URL changed: {position_url!r}")
    description_html = data.get("jobDescription")
    if not isinstance(description_html, str):
        raise ValueError(f"Twilio position detail {expected_id} is missing its description")
    text = description_text(description_html)
    if len(text) < 100:
        raise ValueError(f"Twilio position detail {expected_id} description is empty or truncated")
    dates = data.get("efcustomTextPostDate")
    date_posted = None
    if isinstance(dates, list) and dates:
        candidate = dates[0]
        if isinstance(candidate, str) and ISO_DATE.fullmatch(candidate):
            date_posted = candidate
    return {
        "job_id": identity,
        "display_job_id": data.get("displayJobId"),
        "title": title,
        "url": expected_url,
        "apply_url": f"https://jobs.twilio.com/careers/apply?pid={expected_id}",
        "apply_url_source": "observed rendered Apply Now route for the same detail page",
        "department": data.get("department"),
        "locations": data.get("locations") or [],
        "standardized_locations": data.get("standardizedLocations") or [],
        "work_location_option": data.get("workLocationOption"),
        "ats_job_id": data.get("atsJobId"),
        "creation_ts": data.get("creationTs"),
        "date_posted": date_posted,
        "date_posted_raw": dates,
        "date_posted_source": "efcustomTextPostDate (the rendered Date posted value)" if date_posted else None,
        "description_html": description_html,
        "description_text": text,
        "description_chars": len(text),
        "description_sha256": sha256(text.encode()).hexdigest(),
        "description_fnv1a64": fnv1a64(text),
        "sendgrid_mentions": len(SENDGRID_RE.findall(text)),
        "twilio_sendgrid_phrase": bool(re.search(r"Twilio\s+SendGrid", text, re.I)),
        "raw_data": data,
    }


def parse_apply_v2(payload: dict, expected_id: str) -> dict:
    """Read the extra public fields the board's apply payload exposes."""
    if not isinstance(payload, dict):
        raise ValueError(f"Twilio apply payload {expected_id} is not an object")
    if str(payload.get("id", "")) != str(expected_id):
        raise ValueError(f"Twilio apply payload id changed: {payload.get('id')!r} != {expected_id!r}")
    return {
        "job_id": str(expected_id),
        "title": normalized_text(payload.get("name")),
        "business_unit": payload.get("business_unit"),
        "department": payload.get("department"),
        "ats_job_id": payload.get("ats_job_id"),
        "display_job_id": payload.get("display_job_id"),
        "type": payload.get("type"),
        "t_create": payload.get("t_create"),
        "t_update": payload.get("t_update"),
        "canonical_position_url": payload.get("canonicalPositionUrl"),
        "upload_resume_before_apply": payload.get("upload_resume_before_apply"),
        "raw_payload": payload,
    }


def _section(text: str, start: str, ends: tuple[str, ...]) -> str | None:
    start_match = re.search(rf"\b{start}\b", text, re.I)
    if not start_match:
        return None
    tail = text[start_match.end():]
    end_index = len(tail)
    for end in ends:
        match = re.search(rf"\b{end}\b", tail, re.I)
        if match and match.start() < end_index:
            end_index = match.start()
    value = tail[:end_index].strip()
    return value or None


def parse_salary(text: str) -> dict | None:
    compensation = _section(text, "Compensation", ("Applications", "Apply", "About Twilio"))
    if not compensation:
        return None
    ranges = re.findall(r"\$[\d,]+(?:\s*-\s*\$[\d,]+)?", compensation)
    return {
        "currency": "USD",
        "ranges": list(dict.fromkeys(ranges)),
        "text": compensation,
        "source": "description Compensation section",
    }


def parse_valid_through(text: str) -> tuple[str | None, str | None]:
    match = re.search(r"accepted\s+until\s+(\d{1,2})/(\d{1,2})/(\d{4})", text, re.I)
    if not match:
        return None, None
    month, day, year = match.groups()
    try:
        value = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    except ValueError:
        return None, match.group(0)
    return value, match.group(0)


class SendGrid(Adapter):
    """Collect parent-board postings explicitly attributed to SendGrid."""

    VERIFICATION_REVISION = "sendgrid.twilio-parent-board.attribution-scope.2"

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url), redirect_hops(response))
        parsed.update(
            status=response.status_code,
            final_status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _canonical_entry(self):
        started = time.perf_counter()
        response = await self.http.get(CANONICAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_canonical_entry(response.text, str(response.url), redirect_hops(response))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _twilio_careers(self):
        started = time.perf_counter()
        response = await self.http.get(TWILIO_CAREERS)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_twilio_careers_page(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _board_shell(self):
        started = time.perf_counter()
        response = await self.http.get(BOARD_URL)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_board_shell(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _search_pages(self, query: str, *, label: str):
        started = time.perf_counter()
        rows, pages, seen, duplicate_ids, issues = [], [], set(), [], []
        expected_count = None
        first_filters, first_facets = {}, {}
        keyword_meta = {}
        start = 0
        while True:
            if not self.page_guard():
                issues.append(f"twilio_{label}_page_budget_exhausted")
                break
            request_started = time.perf_counter()
            response = await self.http.request(
                "GET", BOARD_SEARCH_API,
                params={"domain": BOARD_DOMAIN, "query": query, "location": "", "start": start},
                headers={"Referer": BOARD_URL, "Accept": "application/json"},
            )
            request_seconds = max(0.0, time.perf_counter() - request_started)
            parts = urlsplit(str(response.url))
            if parts.scheme != "https" or parts.hostname != BOARD_HOST or parts.path != BOARD_SEARCH_PATH:
                raise ValueError(f"Twilio board search redirected outside its reviewed route: {response.url}")
            payload = response.json()
            page = parse_board_search(payload, start)
            issues.extend(page["issues"])
            duplicate_ids.extend(page.get("duplicate_ids") or [])
            if not first_filters:
                first_filters = page["filters"]
                first_facets = page["facets"]
            if query and not keyword_meta:
                keyword_meta = parse_keyword_search(payload)
            if expected_count is None:
                expected_count = page["count"]
            elif page["count"] != expected_count:
                issues.append(f"twilio_{label}_count_changed:{expected_count}->{page['count']}")
            for row in page["rows"]:
                identity = str(row["id"])
                if identity in seen:
                    duplicate_ids.append(identity)
                    issues.append(f"twilio_{label}_duplicate_id_across_pages:{identity}")
                    continue
                seen.add(identity)
                rows.append(row)
            pages.append({
                "start": start,
                "status": response.status_code,
                "count": page["count"],
                "row_count": page["row_count"],
                "job_ids": [str(row["id"]) for row in page["rows"]],
                "request_url": str(response.request.url),
                "final_url": str(response.url),
                "request_seconds": request_seconds,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
            })
            if expected_count is None or expected_count <= start + page["row_count"]:
                break
            if page["row_count"] == 0:
                issues.append(f"twilio_{label}_unexpected_empty_page:{start}")
                break
            start += PAGE_SIZE
        expected_pages = math.ceil(expected_count / PAGE_SIZE) if expected_count else 0
        if expected_pages and len(pages) != expected_pages:
            issues.append(f"twilio_{label}_page_count_mismatch:{len(pages)}!={expected_pages}")
        if expected_count is not None and len(rows) != expected_count:
            issues.append(f"twilio_{label}_total_does_not_match_rows:{expected_count}!={len(rows)}")
        return {
            "query": query,
            "rows": rows,
            "count": expected_count,
            "pages": pages,
            "pages_count": len(pages),
            "expected_pages": expected_pages,
            "row_count": len(rows),
            "filters": first_filters,
            "facets": first_facets,
            "sendgrid_filter_present": "sendgrid" in json.dumps(
                {"filters": first_filters, "facets": first_facets}, ensure_ascii=False
            ).lower(),
            "used_fuzz_search": keyword_meta.get("used_fuzz_search") if keyword_meta else None,
            "applied_filters": keyword_meta.get("applied_filters") if keyword_meta else None,
            "keyword_match_titles": keyword_meta.get("keyword_match_titles") if keyword_meta else None,
            "duplicate_ids": list(dict.fromkeys(duplicate_ids)),
            "issues": list(dict.fromkeys(issues)),
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def _detail(self, job_id: str, label: str = ""):
        started = time.perf_counter()
        response = await self.http.request(
            "GET", POSITION_API,
            params={"position_id": job_id, "domain": BOARD_DOMAIN, "hl": "en"},
            headers={"Referer": f"{BOARD_URL}/job/{job_id}", "Accept": "application/json"},
        )
        seconds = max(0.0, time.perf_counter() - started)
        parts = urlsplit(str(response.url))
        if parts.scheme != "https" or parts.hostname != BOARD_HOST or parts.path != POSITION_PATH:
            raise ValueError(f"Twilio position detail redirected outside its reviewed route: {response.url}")
        parsed = parse_position_detail(response.json(), job_id)
        parsed.update({
            "label": label,
            "requested_url": str(response.request.url),
            "final_url": str(response.url),
            "http_status": response.status_code,
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _apply_v2(self, job_id: str):
        started = time.perf_counter()
        response = await self.http.request(
            "GET", f"{APPLY_API}/{job_id}",
            params={"domain": BOARD_DOMAIN},
            headers={"Referer": f"{BOARD_URL}/job/{job_id}", "Accept": "application/json"},
        )
        seconds = max(0.0, time.perf_counter() - started)
        parts = urlsplit(str(response.url))
        if parts.scheme != "https" or parts.hostname != BOARD_HOST or not parts.path.startswith(APPLY_PATH_PREFIX):
            raise ValueError(f"Twilio apply payload redirected outside its reviewed route: {response.url}")
        parsed = parse_apply_v2(response.json(), job_id)
        parsed.update({
            "requested_url": str(response.request.url),
            "final_url": str(response.url),
            "http_status": response.status_code,
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _row_payloads(self, row: dict):
        """Fetch and parse one row's full detail and apply payloads."""
        job_id = str(row["id"])
        detail = await self._detail(job_id, "membership_scan")
        apply_payload = await self._apply_v2(job_id)
        return job_id, detail, apply_payload

    def _job_from_posting(self, detail: dict, apply_payload: dict, classification: dict):
        text = detail["description_text"]
        responsibilities = _section(text, "Responsibilities", ("Qualifications", "What We Offer"))
        qualifications = _section(text, "Qualifications", ("Location", "What We Offer", "Compensation"))
        salary = parse_salary(text)
        valid_through, valid_through_raw = parse_valid_through(text)
        workplace = {
            "remote_local": "remote",
            "remote": "remote",
            "hybrid": "hybrid",
            "onsite": "onsite",
        }.get(detail.get("work_location_option"), detail.get("work_location_option"))
        raw = {
            "board_row": detail.get("board_row"),
            "position_detail": detail.get("raw_data") or {},
            "apply_payload": apply_payload.get("raw_payload") or {},
            "scope_attribution": classification,
            "employer": "Twilio",
            "employer_source": (
                "Twilio board branding companyName; the parent board publishes no explicit "
                "legal-employer field"
            ),
            "team": "Twilio SendGrid",
            "team_source": "role description hiring-business header",
            "posting_type": apply_payload.get("type"),
            "ats_job_id": detail.get("ats_job_id"),
            "display_job_id": detail.get("display_job_id"),
            "valid_through_raw": valid_through_raw,
            "salary_source": "description Compensation section",
        }
        job = self.job(
            raw=raw,
            title=detail["title"],
            url=detail["url"],
            job_id=detail["job_id"],
            description=detail["description_html"],
            method="api_pcsx_position_details.jobDescription (full public field)",
            description_url=(
                f"{POSITION_API}?position_id={detail['job_id']}&domain={BOARD_DOMAIN}&hl=en"
            ),
            requisition_id=detail.get("display_job_id"),
            apply_url=detail["apply_url"],
            locations=detail.get("locations") or [],
            department=detail.get("department"),
            team="Twilio SendGrid",
            employment_type=None,
            workplace_type=workplace,
            date_posted=detail.get("date_posted"),
            date_posted_raw=detail.get("date_posted_raw"),
            date_updated=None,
            valid_through=valid_through,
            salary=salary,
            qualifications=qualifications,
            responsibilities=responsibilities,
            experience=None,
            education=None,
            employer="Twilio",
            language="en",
        )
        job.description_text = text
        job.company_scope_verified = True
        job.raw["company_scope_verified_reason"] = classification["reason"]
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_final_url": PRODUCT_PAGE,
            "canonical_entry_url": CANONICAL_ENTRY,
            "listing_url": BOARD_URL,
            "scope_rule": SCOPE_RULE,
            "scope_decision_date": SCOPE_DECISION_DATE,
            "listing_authority": (
                "SendGrid's official careers URL redirects to Twilio's SendGrid product page; "
                "the product page publishes Twilio careers links and the canonical SendGrid "
                "careers path redirects to jobs.twilio.com/careers. The board is Twilio-wide; "
                "membership is the user's 2026-11-08 absorbed-company attribution rule."
            ),
            "detail_method": (
                "Every parent-board row's /api/pcsx/position_details jobDescription field; "
                "the rendered #job-description-container text is independently compared."
            ),
            "request_scope": (
                "Official SendGrid redirects, the exact Twilio product/careers pages, the "
                "Twilio board's own PCSX search/detail endpoints, and the board's public apply "
                "payloads. No guessed ATS slug, no aggregator and no keyword-search shortcut "
                "is used for membership."
            ),
            "collection_policy": (
                "Enumerate all parent-board rows, classify each with the round-3 attribution "
                "rule, and collect only the attributed postings with their full descriptions."
            ),
        })

        try:
            self.page_guard()
            official = await self._official_entry()
        except Exception as exc:
            self.result.errors.append(f"sendgrid_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["sendgrid_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result
        self.result.metadata["official_entry"] = official

        canonical = None
        try:
            self.page_guard()
            canonical = await self._canonical_entry()
        except Exception as exc:
            self.result.warnings.append(f"sendgrid_canonical_entry_failed: {type(exc).__name__}: {exc}")
        if canonical:
            self.result.metadata["canonical_entry"] = canonical

        careers = None
        try:
            self.page_guard()
            careers = await self._twilio_careers()
        except Exception as exc:
            self.result.warnings.append(f"sendgrid_twilio_careers_failed: {type(exc).__name__}: {exc}")
        if careers:
            self.result.metadata["twilio_careers_page"] = careers

        try:
            self.page_guard()
            shell = await self._board_shell()
        except Exception as exc:
            self.result.errors.append(f"sendgrid_board_shell_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["sendgrid_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result
        self.result.metadata["board_shell"] = shell

        try:
            parent = await self._search_pages("", label="parent")
        except Exception as exc:
            self.result.errors.append(f"sendgrid_parent_board_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["sendgrid_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        try:
            keyword = await self._search_pages("SendGrid", label="keyword")
        except Exception as exc:
            self.result.warnings.append(f"sendgrid_keyword_probe_failed: {type(exc).__name__}: {exc}")
            keyword = {
                "query": "SendGrid", "rows": [], "count": None, "pages": [],
                "pages_count": 0, "expected_pages": 0, "row_count": 0,
                "issues": ["keyword_probe_unavailable"], "seconds": 0.0,
            }

        parent_departments = sorted({
            normalized_text(row.get("department")) for row in parent["rows"]
            if normalized_text(row.get("department"))
        })
        parent_locations = set()
        for row in parent["rows"]:
            for location in row.get("locations") or []:
                if isinstance(location, str) and location.strip():
                    parent_locations.add(location.strip())
        parent_view = {
            "url": BOARD_URL,
            "title": shell["title"],
            "group_id": shell["group_id"],
            "base_position_fq": shell["base_position_fq"],
            "smart_filters": shell["smart_filters"],
            "all_filters": shell["all_filters"],
            "sendgrid_in_config": shell["sendgrid_in_config"],
            "count": parent["count"],
            "row_count": parent["row_count"],
            "pages_count": parent["pages_count"],
            "expected_pages": parent["expected_pages"],
            "page_size": PAGE_SIZE,
            "last_page_rows": parent["pages"][-1]["row_count"] if parent["pages"] else 0,
            "unique_job_ids": [str(row["id"]) for row in parent["rows"]],
            "department_count": len(parent_departments),
            "departments": parent_departments,
            "location_count": len(parent_locations),
            "locations": sorted(parent_locations),
            "facet_location_count": len(parent["filters"].get("job_location", {}).get("options") or []),
            "department_option_count": len(parent["filters"].get("custom_department", {}).get("options") or []),
            "seniority_option_count": len(parent["filters"].get("seniority", {}).get("options") or []),
            "work_location_option_count": len(parent["filters"].get("work_location_option", {}).get("options") or []),
            "filters": parent["filters"],
            "facets": parent["facets"],
            "sendgrid_filter_present": parent["sendgrid_filter_present"] or shell["sendgrid_in_config"],
            "sendgrid_titled_count": sum(
                bool(SENDGRID_RE.search(normalized_text(row.get("name")))) for row in parent["rows"]
            ),
            "sendgrid_titled_rows": [
                {"job_id": str(row["id"]), "title": normalized_text(row.get("name"))}
                for row in parent["rows"]
                if SENDGRID_RE.search(normalized_text(row.get("name")))
            ],
            "duplicate_ids": parent["duplicate_ids"],
            "issues": parent["issues"],
            "pages": parent["pages"],
            "seconds": parent["seconds"],
        }
        keyword_view = {
            "url": f"{BOARD_URL}?query=SendGrid",
            "query": keyword["query"],
            "count": keyword["count"],
            "row_count": keyword["row_count"],
            "pages_count": keyword["pages_count"],
            "used_fuzz_search": keyword.get("used_fuzz_search"),
            "applied_filters": keyword.get("applied_filters") or {},
            "job_ids": [str(row["id"]) for row in keyword["rows"]],
            "titles": [normalized_text(row.get("name")) for row in keyword["rows"]],
            "sendgrid_titled_count": sum(
                bool(SENDGRID_RE.search(normalized_text(row.get("name")))) for row in keyword["rows"]
            ),
            "issues": keyword["issues"],
            "pages": keyword["pages"],
            "seconds": keyword["seconds"],
        }
        self.result.metadata["parent_board"] = parent_view
        self.result.metadata["keyword_probe"] = keyword_view

        detail_started = time.perf_counter()
        semaphore = asyncio.Semaphore(max(1, self.options.detail_concurrency))
        details: dict[str, dict] = {}
        applies: dict[str, dict] = {}
        failures: list[dict] = []
        stop = {"reason": None}

        async def one(row: dict):
            job_id = str(row["id"])
            async with semaphore:
                if stop["reason"]:
                    return {
                        "job_id": job_id,
                        "reason": f"not_fetched_after_challenge: {stop['reason']}",
                    }
                if not self.page_guard():
                    return {"job_id": job_id, "reason": "sendgrid_membership_scan_page_budget_exhausted"}
                try:
                    detail = await self._detail(job_id, "membership_scan")
                except Exception as exc:
                    reason = f"detail: {type(exc).__name__}: {exc}"
                    if any(code in reason for code in ("HTTP 429", "HTTP 403", "HTTP 405")):
                        stop["reason"] = reason
                    return {"job_id": job_id, "reason": reason}
                try:
                    apply_payload = await self._apply_v2(job_id)
                except Exception as exc:
                    reason = f"apply: {type(exc).__name__}: {exc}"
                    if any(code in reason for code in ("HTTP 429", "HTTP 403", "HTTP 405")):
                        stop["reason"] = reason
                    return {
                        "job_id": job_id,
                        "reason": reason,
                        "detail": detail,
                    }
                return {"job_id": job_id, "detail": detail, "apply": apply_payload}

        tasks = [one(row) for row in parent["rows"]]
        for value in await asyncio.gather(*tasks):
            if value.get("detail"):
                details[value["job_id"]] = value["detail"]
            if value.get("apply"):
                applies[value["job_id"]] = value["apply"]
            if value.get("reason"):
                failures.append({"job_id": value["job_id"], "reason": value["reason"]})
                self.result.warnings.append(
                    f"sendgrid_membership_scan_failed:{value['job_id']}: {value['reason']}"
                )
        self.result.timings["sendgrid_detail_seconds"] = max(
            0.0, time.perf_counter() - detail_started
        )
        if stop["reason"]:
            self.result.errors.append(f"sendgrid_stopped_on_challenge: {stop['reason']}")

        classifications: list[dict] = []
        for row in parent["rows"]:
            job_id = str(row["id"])
            detail = details.get(job_id)
            if not detail:
                continue
            apply_payload = applies.get(job_id) or {}
            detail["board_row"] = row
            classification = classify_posting(detail, apply_payload)
            classification["apply_business_unit"] = apply_payload.get("business_unit")
            classification["apply_department"] = apply_payload.get("department")
            classifications.append(classification)

        audit_started = time.perf_counter()
        audit = build_scope_audit(
            classifications,
            parent_count=parent["count"] if parent["count"] is not None else parent["row_count"],
            keyword=keyword_view,
        )
        audit["rows_classified_from_details"] = len(details)
        audit["rows_with_apply_payload"] = len(applies)
        audit["scan_failures"] = failures
        audit["rows"] = [
            {
                "job_id": row["job_id"],
                "title": row["title"],
                "department": row.get("department"),
                "business_unit": row.get("business_unit"),
                "included": row["included"],
                "rule": row["rule"],
                "exclusion": row["exclusion"],
                "title_match": row["title_match"],
                "header_mention": row["header_mention"],
                "sendgrid_mentions": row["full_text_sendgrid_mentions"],
            }
            for row in classifications
        ]
        self.result.metadata["scope_audit"] = audit
        self.result.timings["sendgrid_scope_audit_seconds"] = max(
            0.0, time.perf_counter() - audit_started
        )

        if parent["issues"] or parent["count"] != parent["row_count"]:
            self.result.errors.append("sendgrid_parent_board_reconciliation_failed")
        if keyword.get("count") is not None and keyword["count"] != 2:
            self.result.warnings.append(f"sendgrid_keyword_count_changed:{keyword['count']}")
        if failures:
            self.result.errors.append(f"sendgrid_membership_scan_failures:{len(failures)}")

        included = [row for row in classifications if row["included"]]
        for classification in included:
            job_id = classification["job_id"]
            detail = details[job_id]
            apply_payload = applies.get(job_id) or {}
            detail["board_row"] = next(
                (row for row in parent["rows"] if str(row["id"]) == job_id), None
            )
            job = self._job_from_posting(detail, apply_payload, classification)
            self.add(job)
            # Adapter.add() sets this from Company.scope; SendGrid is a division of
            # Twilio, but this record's SendGrid membership was positively verified
            # by the round-3 attribution rule.
            job.company_scope_verified = True

        self.result.expected_total = len(included) if not failures else None
        self.result.listing_complete = (
            not self.result.errors
            and len(details) == parent["row_count"]
            and len(applies) == parent["row_count"]
            and len(classifications) == parent["row_count"]
            and len(self.result.jobs) == len(included)
            and all(job.description_verified for job in self.result.jobs)
        )
        self.result.metadata["job_ids"] = [job.job_id for job in self.result.jobs]
        self.result.metadata["collected_jobs"] = [
            {
                "job_id": job.job_id,
                "requisition_id": job.requisition_id,
                "title": job.title,
                "url": job.url,
                "apply_url": job.apply_url,
                "date_posted": job.date_posted,
                "description_chars": len(job.description_text),
                "description_sha256": job.description_sha256,
                "scope_rule": (job.raw.get("scope_attribution") or {}).get("rule"),
            }
            for job in self.result.jobs
        ]
        self.result.metadata["membership_reconciliation"] = {
            "parent_board_advertised": parent["count"],
            "parent_board_rows": parent["row_count"],
            "parent_board_pages": parent["pages_count"],
            "details_fetched": len(details),
            "apply_payloads_fetched": len(applies),
            "rows_classified": len(classifications),
            "attempted_postings_mentioning_sendgrid": sum(
                row["full_text_sendgrid_mentions"] > 0 for row in classifications
            ),
            "included_total": len(included),
            "collected_jobs": len(self.result.jobs),
            "excluded_total": len(classifications) - len(included),
            "rule_counts": audit["rule_counts"],
        }
        self.result.timings["sendgrid_source_collection_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result
