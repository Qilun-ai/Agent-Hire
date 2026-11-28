"""OpenAI's own careers search page and the detail pages it links to.

Membership is read from https://openai.com/careers/search/ exactly as rendered
(and server-rendered) by OpenAI.  Each listing row links to an OpenAI detail
page and to the exact Ashby application URL OpenAI publishes for that job.  The
full role body comes from the OpenAI detail page's ``main div.prose`` blocks;
the Ashby board is never enumerated.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://openai.com/careers/search/"
CAREERS_ORIGIN = "https://openai.com"
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
JOB_PATH = re.compile(r"^/careers/([^/?#]+)/?$", re.I)
ASHBY_APPLY = re.compile(
    r"^https://jobs\.ashbyhq\.com/openai/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/application$",
    re.I,
)
ASHBY_PAGE = re.compile(
    r"^/openai/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?(?:application)?$",
    re.I,
)
MULTI_LOCATION = re.compile(r"^(\d+) locations?$", re.I)
SALARY = re.compile(r"\$([\d,.]+)\s*([KMB]?)\s*(?:[–-]\s*\$([\d,.]+)\s*([KMB]?))?", re.I)

# Country / state / territory suffixes used by OpenAI's multi-location detail
# subheads.  These let a comma in "Washington, DC" stay inside one location
# while "San Francisco, Seattle" remains two locations.
REGION_SUFFIXES = {
    "Japan", "South Korea", "Australia", "France", "Sweden", "DC", "UK",
    "Ireland", "Germany", "Spain", "UAE", "Switzerland", "India", "Italy",
    "Netherlands", "Canada", "Brazil", "NL", "Mexico", "England", "NY", "CA",
    "WA", "TX", "USA", "United States",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
RESPONSIBILITY_TOKENS = (
    "what you'll do", "what you will do", "what you’ll do", "responsibilities",
    "in this role", "your role", "day to day", "what you'll be doing",
    "what you will be doing", "the work",
)
QUALIFICATION_TOKENS = (
    "what we're looking for", "what we’re looking for", "qualifications",
    "requirements", "must-have", "must have", "preferred qualifications",
    "who you are", "what you'll bring", "what you will bring", "what you bring",
    "skills and experience", "experience",
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_visible_text(value: str | None) -> str:
    """Remove extraction-only spaces before punctuation, preserving newlines."""
    return re.sub(r"[ \t]+([,.;:!?])", r"\1", value or "")


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def split_locations(raw: str) -> list[str]:
    """Split an OpenAI detail subhead suffix into its exact location labels."""
    tokens = [
        token.strip()
        for token in re.split(r",\s*(?:and\s+)?|\s+and\s+", raw or "")
        if token.strip()
    ]
    locations: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            index + 1 < len(tokens)
            and tokens[index + 1] in REGION_SUFFIXES
            and token not in REGION_SUFFIXES
        ):
            locations.append(f"{token}, {tokens[index + 1]}")
            index += 2
        else:
            locations.append(token)
            index += 1
    return locations


def _amount(value: str, suffix: str) -> int:
    number = float(value.replace(",", ""))
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(
        (suffix or "").upper(), 1
    )
    return int(round(number * multiplier))


def parse_salary(raw: str | None) -> dict | None:
    if not raw:
        return None
    value = normalized_title(raw)
    match = SALARY.search(value)
    if match is None:
        return {"raw": value, "currency": "USD", "period": "year",
                "min": None, "max": None, "notes": []}
    minimum = _amount(match.group(1), match.group(2))
    maximum = _amount(match.group(3), match.group(4)) if match.group(3) else None
    notes = [
        part.strip()
        for part in re.split(r"\s*[+•]\s*", value)
        if part.strip() and "$" not in part
    ]
    return {
        "raw": value,
        "currency": "USD",
        "period": "year",
        "min": minimum,
        "max": maximum,
        "notes": notes,
    }


def block_heading(block: Tag) -> str | None:
    if block.name in HEADING_TAGS:
        value = normalized_title(block.get_text(" ", strip=True))
        return value or None
    if block.name != "p":
        return None
    strongs = block.find_all(["strong", "b"], recursive=False)
    if len(strongs) != 1:
        return None
    strong_text = normalized_title(strongs[0].get_text(" ", strip=True))
    block_text = normalized_title(block.get_text(" ", strip=True))
    if strong_text and strong_text == block_text and len(block_text) <= 120:
        return block_text
    return None


def description_sections(prose_html: list[str]) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(
        '<div data-openai-body="true">' + "".join(prose_html or []) + "</div>",
        "lxml",
    )
    root = soup.select_one("[data-openai-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections: list[dict] = []
    current: dict | None = None

    def finish():
        nonlocal current
        if current is None:
            return
        current["text"] = clean_visible_text(text_html(current["html"]))
        sections.append(current)
        current = None

    for block in blocks:
        heading = block_heading(block)
        if heading:
            finish()
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    finish()
    if not sections:
        sections.append({"heading": None, "html": "", "text": ""})
    return sections


def _section_matches(section: dict, tokens: tuple[str, ...], text_window: int = 240) -> bool:
    heading = (section.get("heading") or "").lower()
    if heading and any(token in heading for token in tokens):
        return True
    body = (section.get("text") or "").lower()[:text_window]
    return any(token in body for token in tokens)


def _choose_sections(sections: list[dict], tokens: tuple[str, ...]) -> list[dict] | None:
    chosen = [
        {"heading": section["heading"], "text": section["text"]}
        for section in sections
        if _section_matches(section, tokens) and (section.get("text") or "").strip()
    ]
    return chosen or None


def _extract_json_object(html: str, marker: str) -> dict | None:
    index = html.find(marker)
    if index < 0:
        return None
    start = html.find("{", index)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(html)):
        char = html[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(html[start:position + 1])
    raise ValueError("Ashby posting JSON object is not terminated")


def mapped_employment_type(value: str | None) -> str | None:
    if not value:
        return None
    mapping = {
        "fulltime": "Full-time",
        "parttime": "Part-time",
        "contract": "Contract",
        "intern": "Internship",
        "temporary": "Temporary",
    }
    return mapping.get(value.replace("_", "").replace("-", "").lower(), value)


def ashby_salary(json_ld: dict, posting: dict) -> dict | None:
    base = json_ld.get("baseSalary")
    raw = (posting.get("scrapeableCompensationSalarySummary")
           or posting.get("compensationTierSummary"))
    if isinstance(base, dict):
        currency = base.get("currency") or "USD"
        value = base.get("value") if isinstance(base.get("value"), dict) else {}
        minimum = value.get("minValue")
        maximum = value.get("maxValue")
        unit = value.get("unitText") or "YEAR"
        if minimum is not None or maximum is not None:
            return {
                "raw": raw or normalized_title(
                    f"{currency} {minimum or ''} - {maximum or ''} {unit}"
                ),
                "currency": currency,
                "period": "year" if str(unit).upper() == "YEAR" else str(unit).lower(),
                "min": int(minimum) if minimum is not None else None,
                "max": int(maximum) if maximum is not None else None,
                "notes": [],
                "source": "ashby_schema_org_base_salary",
            }
    return parse_salary(raw)


def parse_ashby_application(html: str, expected_row: dict, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    path_match = ASHBY_PAGE.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "jobs.ashbyhq.com"
        or path_match is None
        or path_match.group(1).lower() != expected_row["job_id"]
    ):
        raise ValueError("Ashby application response is not the exact company-linked job URL")
    soup = BeautifulSoup(html, "lxml")
    posting = _extract_json_object(html, '"posting":{')
    if not isinstance(posting, dict):
        raise ValueError("Ashby application page has no posting object")
    json_ld = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            value = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and value.get("@type") == "JobPosting":
            json_ld = value
            break
    if json_ld is None:
        raise ValueError("Ashby application page has no JobPosting JSON-LD")
    issues = []
    if str(posting.get("id", "")).lower() != expected_row["job_id"]:
        issues.append("ashby_posting_id_does_not_match_listing")
    ashby_title = normalized_title(posting.get("title"))
    if ashby_title != expected_row["title"]:
        issues.append("ashby_title_does_not_match_listing")
    if posting.get("isListed") is False:
        issues.append("ashby_posting_is_not_listed")
    if posting.get("isConfidential") is True:
        issues.append("ashby_posting_is_confidential")
    description_html = posting.get("descriptionHtml") or json_ld.get("description") or ""
    compensation_html = posting.get("compensationPhilosophyHtml") or ""
    compensation_summary = (
        posting.get("compensationTierSummary")
        or posting.get("scrapeableCompensationSalarySummary")
    )
    fallback_html = description_html
    if compensation_summary:
        fallback_html += (
            f"<p><strong>Compensation</strong></p><p>{compensation_summary}</p>"
            + compensation_html
        )
    date_posted_raw = json_ld.get("datePosted")
    date_posted = None
    if isinstance(date_posted_raw, str):
        date_match = re.match(r"^(\d{4}-\d{2}-\d{2})", date_posted_raw)
        date_posted = date_match.group(1) if date_match else None
    deadline = posting.get("applicationDeadline")
    valid_through = None
    if isinstance(deadline, str):
        deadline_match = re.match(r"^(\d{4}-\d{2}-\d{2})", deadline)
        valid_through = deadline_match.group(1) if deadline_match else None
    return {
        "url": response_url,
        "title": ashby_title,
        "job_id": str(posting.get("id", "")).lower(),
        "department": posting.get("departmentName") or posting.get("departmentExternalName"),
        "team_names": posting.get("teamNames"),
        "location_name": posting.get("locationName"),
        "secondary_location_names": posting.get("secondaryLocationNames") or [],
        "workplace_type": posting.get("workplaceType") or json_ld.get("jobLocationType"),
        "employment_type_raw": posting.get("employmentType") or json_ld.get("employmentType"),
        "employment_type": mapped_employment_type(
            posting.get("employmentType") or json_ld.get("employmentType")
        ),
        "date_posted": date_posted,
        "date_posted_raw": date_posted_raw,
        "valid_through": valid_through,
        "application_deadline_raw": deadline,
        "compensation_summary": compensation_summary,
        "compensation_tiers": posting.get("compensationTiers"),
        "compensation_philosophy_html": compensation_html,
        "salary": ashby_salary(json_ld, posting),
        "description_html": description_html,
        "fallback_description_html": fallback_html,
        "is_listed": posting.get("isListed"),
        "is_confidential": posting.get("isConfidential"),
        "hiring_organization": (
            json_ld.get("hiringOrganization", {}).get("name")
            if isinstance(json_ld.get("hiringOrganization"), dict) else None
        ),
        "legal_entity_texting_consent": posting.get("legalEntityNameForTextingConsent"),
        "job_location": json_ld.get("jobLocation"),
        "applicant_location_requirements": json_ld.get("applicantLocationRequirements"),
        "json_ld_base_salary": json_ld.get("baseSalary"),
        "issues": issues,
    }


def parse_listing(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "openai.com"
        or parsed.path.rstrip("/") != "/careers/search"
    ):
        raise ValueError("OpenAI careers listing redirected away from its official search URL")
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main")
    if main is None:
        raise ValueError("OpenAI careers listing has no main element")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""

    advertised_values: list[int] = []
    for node in main.find_all(string=re.compile(r"\d[\d,]*\s+jobs", re.I)):
        value = normalized_title(str(node))
        match = re.fullmatch(r"([\d,]+)\s+jobs", value, re.I)
        if match:
            advertised_values.append(int(match.group(1).replace(",", "")))
    advertised = advertised_values[0] if advertised_values else None
    issues: list[str] = []
    if advertised is None:
        issues.append("openai_advertised_job_count_missing")
    elif len(set(advertised_values)) != 1:
        issues.append("openai_advertised_job_count_ambiguous")

    rows: list[dict] = []
    seen_hrefs: set[str] = set()
    seen_ids: set[str] = set()
    for anchor in main.select('a[href^="/careers/"]'):
        href = anchor.get("href") or ""
        if href.rstrip("/") in ("/careers", "/careers/search"):
            continue
        path_match = JOB_PATH.fullmatch(href)
        if path_match is None or path_match.group(1).lower() == "search":
            issues.append(f"openai_listing_unexpected_career_href:{href}")
            continue
        title_node = anchor.select_one("h2")
        title = normalized_title(title_node.get_text(" ", strip=True)) if title_node else ""
        spans = [
            normalized_title(span.get_text(" ", strip=True))
            for span in anchor.select("span")
            if normalized_title(span.get_text(" ", strip=True))
        ]
        if not title or len(spans) < 2:
            issues.append(f"openai_listing_row_fields_missing:{href}")
            continue
        team, location_label = spans[0], spans[1]
        apply_url = None
        node = anchor
        for _ in range(5):
            node = node.parent
            if node is None or node.name in ("main", "body", "html"):
                break
            candidate = node.select_one('a[href*="jobs.ashbyhq.com/openai/"]')
            if candidate:
                apply_url = candidate.get("href")
                break
        apply_match = ASHBY_APPLY.fullmatch(apply_url or "")
        if apply_match is None:
            issues.append(f"openai_listing_apply_url_missing_or_unexpected:{href}")
            continue
        job_id = apply_match.group(1).lower()
        if href in seen_hrefs:
            issues.append(f"openai_listing_duplicate_href:{href}")
            continue
        if job_id in seen_ids:
            issues.append(f"openai_listing_duplicate_ashby_id:{job_id}")
            continue
        seen_hrefs.add(href)
        seen_ids.add(job_id)
        rows.append({
            "href": href,
            "url": urljoin(CAREERS_ORIGIN, href),
            "slug": path_match.group(1),
            "title": title,
            "team": team,
            "location_label": location_label,
            "apply_url": apply_url,
            "job_id": job_id,
        })

    if advertised is not None and len(rows) != advertised:
        issues.append("openai_advertised_count_does_not_match_visible_rows")
    if not rows:
        issues.append("openai_listing_has_no_exposed_job_rows")

    buttons = [b.get_text(" ", strip=True) for b in main.select("button")]
    pagination = [
        b.get_text(" ", strip=True)
        for b in main.select("button,a")
        if re.search(r"\b(?:next|previous|load more|show more|page \d+)\b",
                     b.get_text(" ", strip=True), re.I)
    ]
    return {
        "rows": rows,
        "job_ids": [row["job_id"] for row in rows],
        "count": len(rows),
        "advertised_count": advertised,
        "page_title": page_title,
        "filters": {
            "team_control": next((b for b in buttons if b == "All teams"), None),
            "location_control": next((b for b in buttons if b == "All locations"), None),
            "job_search_input": bool(main.select_one('input[placeholder="Search"]')),
        },
        "pagination_controls": pagination,
        "issues": issues,
    }


def parse_detail(html: str, expected_row: dict, response_url: str) -> dict:
    expected = urlsplit(expected_row["url"])
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "openai.com"
        or parsed.path.rstrip("/") != expected.path.rstrip("/")
    ):
        raise ValueError("OpenAI job detail redirected away from its exact company URL")
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main")
    if main is None:
        raise ValueError("OpenAI job detail has no main element")

    title_node = main.select_one('[data-article-hero-copy-region="headline"]')
    subhead_node = main.select_one('[data-article-hero-copy-region="subhead"]')
    title = normalized_title(title_node.get_text(" ", strip=True)) if title_node else ""
    subhead = normalized_title(subhead_node.get_text(" ", strip=True)) if subhead_node else ""
    issues: list[str] = []
    if title != expected_row["title"]:
        issues.append("openai_detail_title_does_not_match_listing")

    apply_links = list(dict.fromkeys(
        link.get("href")
        for link in main.select('a[href*="jobs.ashbyhq.com/openai/"]')
        if link.get("href")
    ))
    apply_url = apply_links[0] if len(apply_links) == 1 else None
    apply_match = ASHBY_APPLY.fullmatch(apply_url or "")
    if apply_match is None:
        issues.append("openai_detail_apply_url_missing_or_ambiguous")
    elif apply_match.group(1).lower() != expected_row["job_id"]:
        issues.append("openai_detail_apply_id_does_not_match_listing")
    elif apply_url != expected_row["apply_url"]:
        issues.append("openai_detail_apply_url_does_not_match_listing")

    team = expected_row["team"]
    prefix = f"{team} - "
    location_raw = ""
    if not subhead.startswith(prefix):
        issues.append("openai_detail_team_subhead_prefix_changed")
    else:
        location_raw = subhead[len(prefix):].strip()
    location_label = expected_row["location_label"]
    location_notes: list[str] = []
    multi = MULTI_LOCATION.fullmatch(location_label)
    if multi:
        locations = split_locations(location_raw)
        expected_count = int(multi.group(1))
        if len(locations) != expected_count:
            deduped = list(dict.fromkeys(locations))
            if len(deduped) == expected_count:
                location_notes.append(
                    "duplicate_location_labels_removed:" + ",".join(
                        label for label in locations
                        if locations.count(label) > 1
                    )
                )
                locations = deduped
            else:
                issues.append(
                    f"openai_detail_location_count_mismatch:{len(locations)}!={expected_count}"
                )
    else:
        locations = [location_label]
        if normalized_title(location_raw) != normalized_title(location_label):
            issues.append("openai_detail_location_does_not_match_listing")

    prose_nodes = [
        node for node in main.select("div.prose")
        if node.find_parent("div", class_="prose") is None
    ]
    prose_html = [
        node.decode_contents() for node in prose_nodes
        if normalized_body_text(node.decode_contents())
    ]
    description_html = "\n".join(prose_html)
    description_text = clean_visible_text(text_html(description_html))
    if not prose_html:
        issues.append("openai_detail_has_no_description_prose")
    if len(description_text.strip()) < 100:
        issues.append("openai_detail_description_short_or_empty")
    if re.search(r"\bApply now\b|OpenAI © 2015", description_text):
        issues.append("openai_detail_description_contains_page_boilerplate")

    sections = description_sections(prose_html)
    compensation_raw = None
    for section in sections:
        if normalized_title(section.get("heading") or "").lower() == "compensation":
            for line in (section.get("text") or "").splitlines():
                if "$" in line and re.search(r"\d", line):
                    compensation_raw = normalized_title(line)
                    break
            if compensation_raw:
                break
    salary = parse_salary(compensation_raw)
    responsibilities = _choose_sections(sections, RESPONSIBILITY_TOKENS)
    qualifications = _choose_sections(sections, QUALIFICATION_TOKENS)

    return {
        "url": response_url,
        "title": title,
        "team": team,
        "subhead": subhead,
        "locations": locations,
        "location_notes": location_notes,
        "apply_url": apply_url,
        "apply_links": apply_links,
        "job_id": apply_match.group(1).lower() if apply_match else None,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(normalized_body_text(description_html)),
        "description_fnv1a64": fnv1a64(normalized_body_text(description_html)),
        "sections": sections,
        "compensation_raw": compensation_raw,
        "salary": salary,
        "responsibilities": responsibilities,
        "qualifications": qualifications,
        "issues": issues,
    }


def detail_page_reference(html: str, expected_row: dict, response_url: str) -> dict:
    """Check an exact company-linked detail page against its listing row."""
    parsed = parse_detail(html, expected_row, response_url)
    if parsed["issues"]:
        raise ValueError(
            "OpenAI detail page did not match its listing row: "
            + "; ".join(parsed["issues"])
        )
    return parsed


def _listing_row_hash(row: dict) -> str:
    value = json.dumps(row, sort_keys=True, ensure_ascii=False)
    return sha256(value.encode()).hexdigest()


class OpenAI(Adapter):
    """Read the complete OpenAI careers search listing and its exact details."""

    VERIFICATION_REVISION = "openai.com-careers-search-detail.1"

    def __init__(self, http, company, source, options, *,
                 detail_cache: dict | None = None,
                 cache_callback=None,
                 ashby_cache: dict | None = None,
                 ashby_cache_callback=None):
        super().__init__(http, company, source, options)
        self.detail_cache = detail_cache or {}
        self.cache_callback = cache_callback
        self.ashby_cache = ashby_cache or {}
        self.ashby_cache_callback = ashby_cache_callback
        self._blocked = False
        self._blocked_reason = None
        self._ashby_blocked = False
        self._ashby_blocked_reason = None

    async def _listing(self, label: str) -> dict:
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY, headers=BROWSER_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_listing(response.text, str(response.url))
        parsed.update(
            label=label,
            status=response.status_code,
            final_url=str(response.url),
            request_seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _detail(self, job, listing_row: dict):
        cached = self.detail_cache.get(job.job_id)
        if cached and cached.get("listing_row_hash") == _listing_row_hash(listing_row):
            return cached["parsed"], True
        started = time.perf_counter()
        response = await self.http.get(job.url, headers=BROWSER_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_detail(response.text, listing_row, str(response.url))
        parsed.update(
            status=response.status_code,
            final_url=str(response.url),
            request_seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        if self.cache_callback and not parsed["issues"]:
            self.cache_callback(job.job_id, {
                "listing_row_hash": _listing_row_hash(listing_row),
                "parsed": parsed,
            })
        return parsed, False

    async def _ashby_detail(self, job, listing_row: dict):
        cached = self.ashby_cache.get(job.job_id)
        if cached and cached.get("listing_row_hash") == _listing_row_hash(listing_row):
            return cached["parsed"], True
        started = time.perf_counter()
        response = await self.http.get(listing_row["apply_url"], headers=BROWSER_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_ashby_application(response.text, listing_row, str(response.url))
        parsed.update(
            status=response.status_code,
            final_url=str(response.url),
            request_seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        if self.ashby_cache_callback and not parsed["issues"]:
            self.ashby_cache_callback(job.job_id, {
                "listing_row_hash": _listing_row_hash(listing_row),
                "parsed": parsed,
            })
        return parsed, False

    def _ashby_summary(self, parsed: dict) -> dict:
        keys = (
            "title", "job_id", "department", "team_names", "location_name",
            "secondary_location_names", "workplace_type", "employment_type_raw",
            "employment_type", "date_posted", "date_posted_raw", "valid_through",
            "application_deadline_raw", "compensation_summary", "compensation_tiers",
            "salary", "is_listed", "is_confidential", "hiring_organization",
            "legal_entity_texting_consent", "job_location",
            "applicant_location_requirements", "json_ld_base_salary", "issues",
            "status", "final_url", "request_seconds", "response_sha256",
            "response_bytes",
        )
        return {key: parsed.get(key) for key in keys}

    async def _collect_ashby_metadata(self, jobs: list) -> dict:
        started = time.perf_counter()
        sem = asyncio.Semaphore(max(1, self.options.detail_concurrency or 2))
        stats = {"total": len(jobs), "checked": 0, "fetched_jobs": 0,
                 "reused_cached_jobs": 0, "failed": 0, "blocked": False,
                 "fallbacks_used": 0}
        failed: list[dict] = []
        request_seconds = 0.0

        async def one(job):
            nonlocal request_seconds
            async with sem:
                if self._ashby_blocked:
                    job.warnings.append("ashby_metadata_stopped_after_waf_or_rate_limit")
                    return
                try:
                    parsed, reused = await self._ashby_detail(job, job.raw["listing"])
                except FetchError as exc:
                    message = str(exc)
                    if "HTTP 403" in message or "HTTP 429" in message:
                        self._ashby_blocked = True
                        self._ashby_blocked_reason = message
                        self.result.errors.append(
                            f"OpenAI exact Ashby application source blocked: {message}"
                        )
                    failed.append({"id": job.job_id, "url": job.raw["listing"]["apply_url"],
                                   "reason": message})
                    job.warnings.append("ashby_metadata_fetch_failed")
                    stats["failed"] += 1
                    stats["checked"] += 1
                    self.result.validate_job(job)
                    return
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    failed.append({"id": job.job_id, "url": job.raw["listing"]["apply_url"],
                                   "reason": message})
                    job.warnings.append("ashby_metadata_fetch_failed")
                    stats["failed"] += 1
                    stats["checked"] += 1
                    self.result.validate_job(job)
                    return
                if reused:
                    stats["reused_cached_jobs"] += 1
                else:
                    stats["fetched_jobs"] += 1
                    request_seconds += float(parsed.get("request_seconds") or 0.0)

                job.raw["ashby_detail"] = self._ashby_summary(parsed)
                if parsed.get("date_posted") and not job.date_posted:
                    job.date_posted = parsed["date_posted"]
                    job.date_posted_raw = parsed.get("date_posted_raw")
                if parsed.get("employment_type"):
                    job.employment_type = parsed["employment_type"]
                if parsed.get("workplace_type"):
                    job.workplace_type = parsed["workplace_type"]
                if parsed.get("valid_through"):
                    job.valid_through = parsed["valid_through"]
                if parsed.get("hiring_organization"):
                    job.employer = parsed["hiring_organization"]
                if not job.locations:
                    ashby_locations = []
                    if parsed.get("location_name"):
                        ashby_locations.append(parsed["location_name"])
                    for location in parsed.get("secondary_location_names") or []:
                        if location and location not in ashby_locations:
                            ashby_locations.append(location)
                    if ashby_locations:
                        job.locations = ashby_locations
                salary = parsed.get("salary")
                if salary and (not job.salary or job.salary.get("min") is None):
                    job.salary = salary

                needed_fallback = (
                    not (job.description_text or "").strip()
                    or len((job.description_text or "").strip()) < 100
                    or job.description_unavailable
                )
                if needed_fallback:
                    fallback_html = parsed.get("fallback_description_html") or ""
                    fallback_text = text_html(fallback_html)
                    if len(fallback_text.strip()) >= 100 and not parsed["issues"]:
                        normal = normalized_body_text(fallback_html)
                        job.description_html = fallback_html
                        job.description_text = fallback_text
                        job.description_method = (
                            "exact Ashby application page fallback; OpenAI detail URL "
                            "redirected to the careers search page"
                        )
                        job.description_source_url = parsed.get("final_url")
                        job.description_unavailable = False
                        job.raw["detail"] = {
                            "fallback_source": "exact Ashby application page linked by the OpenAI listing row",
                            "description_sha256": sha256(fallback_text.encode()).hexdigest(),
                            "description_normalized_chars": len(normal),
                            "description_fnv1a64": fnv1a64(normal),
                            "description_sections": description_sections(
                                [parsed.get("fallback_description_html") or ""]
                            ),
                            "compensation_raw": parsed.get("compensation_summary"),
                            "apply_links": [parsed.get("final_url")],
                            "response_status": parsed.get("status"),
                            "response_sha256": parsed.get("response_sha256"),
                            "response_bytes": parsed.get("response_bytes"),
                            "request_seconds": parsed.get("request_seconds"),
                            "reused_cached_detail": parsed.get("reused_cached_detail", False),
                            "source_route": "exact Ashby application page published in the OpenAI listing row",
                        }
                        job.warnings = [
                            warning for warning in job.warnings
                            if warning not in (
                                "detail_fetch_failed", "short_or_missing_description",
                                "no_full_description_provenance",
                            )
                        ]
                        stats["fallbacks_used"] += 1
                        for item in self.result.metadata.get("failed_jobs", []):
                            if item.get("id") == job.job_id:
                                item["resolved"] = True
                                item["resolution"] = (
                                    "exact Ashby application page fallback for a broken "
                                    "OpenAI detail link"
                                )
                    else:
                        self.result.errors.append(
                            f"OpenAI detail unavailable and exact Ashby fallback incomplete: {job.job_id}"
                        )
                if parsed["issues"]:
                    job.warnings.append("ashby_metadata_issue")
                    failed.append({
                        "id": job.job_id,
                        "url": job.raw["listing"]["apply_url"],
                        "reason": "; ".join(parsed["issues"]),
                    })
                    if needed_fallback:
                        self.result.errors.append(
                            f"OpenAI detail and Ashby fallback identity issue {job.job_id}: "
                            + "; ".join(parsed["issues"])
                        )
                self.result.validate_job(job)
                stats["checked"] += 1

        await asyncio.gather(*(one(job) for job in jobs))
        unresolved = [
            item for item in self.result.metadata.get("failed_jobs", [])
            if not item.get("resolved")
        ]
        if self.result.metadata.get("detail_stats"):
            self.result.metadata["detail_stats"]["failed"] = len(unresolved)
        stats["blocked"] = self._ashby_blocked
        if self._ashby_blocked:
            stats["blocked_reason"] = self._ashby_blocked_reason
        seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["ashby_metadata_phase_seconds"] = seconds
        self.result.timings["ashby_request_seconds_sum"] = request_seconds
        self.result.metadata["ashby_stats"] = stats
        self.result.metadata["ashby_failed_jobs"] = failed
        return stats

    def _apply_detail(self, job, parsed: dict):
        job.title = parsed["title"]
        job.apply_url = parsed["apply_url"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "openai.com/careers detail main div.prose (all blocks)"
        job.description_source_url = parsed.get("final_url") or job.url
        job.locations = parsed["locations"]
        job.department = parsed["team"]
        job.employer = "OpenAI"
        job.language = "en"
        job.salary = parsed["salary"]
        job.responsibilities = parsed["responsibilities"]
        job.qualifications = parsed["qualifications"]
        normal = normalized_body_text(parsed["description_html"])
        job.raw["detail"] = {
            "subhead": parsed["subhead"],
            "description_sha256": sha256(parsed["description_text"].encode()).hexdigest(),
            "description_normalized_chars": len(normal),
            "description_fnv1a64": fnv1a64(normal),
            "description_sections": parsed["sections"],
            "compensation_raw": parsed["compensation_raw"],
            "apply_links": parsed["apply_links"],
            "response_status": parsed.get("status"),
            "response_sha256": parsed.get("response_sha256"),
            "response_bytes": parsed.get("response_bytes"),
            "request_seconds": parsed.get("request_seconds"),
            "reused_cached_detail": parsed.get("reused_cached_detail", False),
            "source_route": "OpenAI detail page linked from its own careers search row",
        }
        if parsed["issues"]:
            job.warnings.append("detail_identity_or_location_mismatch")

    async def _collect_details(self, jobs: list) -> dict:
        started = time.perf_counter()
        sem = asyncio.Semaphore(max(1, self.options.detail_concurrency or 2))
        stats = {"total": len(jobs), "checked": 0, "fetched_jobs": 0,
                 "reused_cached_jobs": 0, "failed": 0, "blocked": False}
        failed: list[dict] = []
        request_seconds = 0.0
        parse_seconds = 0.0

        async def one(job):
            nonlocal request_seconds, parse_seconds
            async with sem:
                if self._blocked:
                    job.warnings.append("collection_stopped_after_waf_or_rate_limit")
                    return
                try:
                    parsed, reused = await self._detail(job, job.raw["listing"])
                except FetchError as exc:
                    message = str(exc)
                    if "HTTP 403" in message or "HTTP 429" in message:
                        self._blocked = True
                        self._blocked_reason = message
                        self.result.errors.append(
                            f"OpenAI source blocked during detail collection: {message}"
                        )
                    failed.append({"id": job.job_id, "url": job.url, "reason": message})
                    job.warnings.append("detail_fetch_failed")
                    stats["failed"] += 1
                    stats["checked"] += 1
                    return
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    failed.append({"id": job.job_id, "url": job.url, "reason": message})
                    job.warnings.append("detail_fetch_failed")
                    stats["failed"] += 1
                    stats["checked"] += 1
                    return
                if reused:
                    stats["reused_cached_jobs"] += 1
                else:
                    stats["fetched_jobs"] += 1
                    request_seconds += float(parsed.get("request_seconds") or 0.0)
                parse_started = time.perf_counter()
                self._apply_detail(job, parsed)
                parse_seconds += max(0.0, time.perf_counter() - parse_started)
                if parsed["issues"]:
                    reason = "; ".join(parsed["issues"])
                    failed.append({"id": job.job_id, "url": job.url, "reason": reason})
                    self.result.errors.append(
                        f"OpenAI detail issue {job.job_id}: {reason}"
                    )
                self.result.validate_job(job)
                stats["checked"] += 1

        await asyncio.gather(*(one(job) for job in jobs))
        stats["blocked"] = self._blocked
        if self._blocked:
            stats["blocked_reason"] = self._blocked_reason
        seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["detail_phase_seconds"] = seconds
        self.result.timings["detail_request_seconds_sum"] = request_seconds
        self.result.timings["detail_parse_seconds_sum"] = parse_seconds
        self.result.metadata["detail_stats"] = stats
        self.result.metadata["failed_jobs"] = failed
        return stats

    def _compare_listings(self, initial: dict, final: dict) -> dict:
        initial_by_id = {row["job_id"]: row for row in initial["rows"]}
        final_by_id = {row["job_id"]: row for row in final["rows"]}
        changed_fields: dict[str, list[str]] = {}
        for identity in initial_by_id.keys() & final_by_id.keys():
            before, after = initial_by_id[identity], final_by_id[identity]
            changed = sorted(
                key for key in set(before) | set(after)
                if before.get(key) != after.get(key)
            )
            if changed:
                changed_fields[identity] = changed
        new_ids = [identity for identity in final["job_ids"] if identity not in initial_by_id]
        removed_ids = [identity for identity in initial["job_ids"] if identity not in final_by_id]
        return {
            "initial_ids": initial["job_ids"],
            "final_ids": final["job_ids"],
            "initial_count": initial["count"],
            "final_count": final["count"],
            "initial_advertised_count": initial["advertised_count"],
            "final_advertised_count": final["advertised_count"],
            "same_ids_and_order": initial["job_ids"] == final["job_ids"],
            "same_advertised_count": initial["advertised_count"] == final["advertised_count"],
            "all_row_fields_match": not changed_fields,
            "changed_fields_by_job_id": changed_fields,
            "new_job_ids": new_ids,
            "removed_job_ids": removed_ids,
            "complete": (
                initial["job_ids"] == final["job_ids"]
                and initial["advertised_count"] == final["advertised_count"]
                and not changed_fields
                and not final["issues"]
            ),
        }

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_ENTRY,
            "listing_authority": "OpenAI's own server-rendered careers search page and its exact job links.",
            "listing_membership_request": {
                "method": "GET",
                "url": OFFICIAL_ENTRY,
                "filters_applied": False,
                "scope": "OpenAI careers search rows only; Ashby is used only as the exact apply URL published per row.",
                "pagination": "The current search page renders all listing rows and exposes no next/previous/load-more control.",
            },
            "detail_method": "OpenAI detail page linked by the listing row; all main div.prose blocks retained.",
        })
        try:
            initial = await self._listing("initial")
        except Exception as exc:
            self.result.errors.append(f"OpenAI listing request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        self.result.expected_total = initial["advertised_count"]
        self.result.metadata["initial_listing"] = {k: v for k, v in initial.items() if k != "rows"}
        self.result.metadata["initial_job_ids"] = initial["job_ids"]
        if initial["issues"]:
            self.result.errors.extend(initial["issues"])
        if initial["advertised_count"] != initial["count"]:
            self.result.errors.append("openai_initial_advertised_count_mismatch")

        jobs = []
        for row in initial["rows"]:
            job = self.job(
                {"listing": row},
                title=row["title"],
                url=row["url"],
                job_id=row["job_id"],
                apply_url=row["apply_url"],
                employer="OpenAI",
                language="en",
            )
            jobs.append(job)

        stats = await self._collect_details(jobs)
        ashby_stats = None
        if not self._blocked:
            ashby_stats = await self._collect_ashby_metadata(jobs)

        final = None
        if not self._blocked:
            try:
                final = await self._listing("final")
            except Exception as exc:
                self.result.errors.append(
                    f"OpenAI final listing recheck failed: {type(exc).__name__}: {exc}"
                )
        if final is None:
            self.result.metadata["membership_reconciliation"] = {
                "complete": False,
                "reason": "final listing recheck unavailable",
            }
            self.result.errors.append("openai_listing_membership_not_reconciled")
        else:
            reconciliation = self._compare_listings(initial, final)
            self.result.metadata["final_listing"] = {
                k: v for k, v in final.items() if k != "rows"
            }
            self.result.metadata["final_job_ids"] = final["job_ids"]
            self.result.metadata["membership_reconciliation"] = reconciliation
            if final["issues"]:
                self.result.errors.extend(final["issues"])
            if not reconciliation["complete"]:
                self.result.errors.append("openai_listing_membership_changed_during_collection")

        self.result.jobs = jobs
        self.result.metadata["job_ids"] = [job.job_id for job in jobs]
        self.result.metadata["detail_collection_mode"] = (
            "Every exact OpenAI detail page linked from the listing is fetched and "
            "its full main div.prose body retained; the exact Ashby application URL "
            "published in the same listing row supplies structured type/date/place "
            "metadata and a same-job fallback where the OpenAI detail link is broken."
        )
        self.result.metadata["detail_stats"] = stats
        self.result.metadata["ashby_stats"] = ashby_stats
        self.result.listing_complete = bool(
            final is not None
            and self.result.metadata["membership_reconciliation"].get("complete")
            and len(jobs) == initial["count"]
            and not stats.get("blocked")
            and stats.get("failed") == 0
            and not self.result.errors
        )
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result
