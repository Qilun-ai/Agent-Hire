"""Redis's first-party careers listing and its exact published job pages.

Observed live journey (2026-11-08, see ``companies/redis/journey-research.md``):

- ``https://redis.io/careers/`` 301-redirects to the Redis-branded careers page
  ``https://redis.io/company/careers/``.
- That page links ``View current openings`` to the complete first-party listing
  ``https://redis.io/company/careers/current-job-openings/``. The listing renders
  12 cards and a ``Load More`` button; clicking it twice reveals all 32 cards.
- The listing UI requests ``https://redis.io/api/careers/feed/`` with
  ``start``/``end`` (exclusive, page size 12) plus optional ``location``,
  ``department``, ``search`` and ``region`` filters. With no filters it returns
  ``{"items": [...], "total": 32}``. This is the company's own listing and the
  membership authority; the filter controls are not applied.
- Each item carries ``_id`` (``ab-<uuid>``), ``title``, ``department``,
  ``location``, ``pathname`` and ``compensationSummary``. The detail route is
  ``/company/careers/<title-slug>-<uuid>/``.
- Each detail page carries exactly one schema.org ``JobPosting`` JSON-LD document
  with the full HTML description, ``datePosted``, ``employmentType``,
  ``identifier.value`` (the uuid), ``hiringOrganization`` and either
  ``jobLocation`` or ``jobLocationType``/``applicantLocationRequirements``.
  The visible description text equals the JSON-LD description text.
- The detail page embeds Redis's Ashby application form for the same uuid. The
  Ashby board API is fetched only as an enrichment/cross-check for the exact
  redis.io ids (same 32 ids currently); it is never used to add or remove
  membership. The apply URL comes from the exact matching Ashby job.
"""
from __future__ import annotations

import json
import re
import time
from hashlib import sha256
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import Result, text_html


OFFICIAL_ENTRY = "https://redis.io/careers/"
ENTRY_FINAL = "https://redis.io/company/careers/"
LISTING_URL = "https://redis.io/company/careers/current-job-openings/"
FEED_API = "https://redis.io/api/careers/feed/"
ASHBY_BOARD_API = "https://api.ashbyhq.com/posting-api/job-board/redis?includeCompensation=true"
PAGE_SIZE = 12
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
JOB_PATH_RE = re.compile(
    r"^/company/careers/(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)-"
    r"(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/$",
    re.I,
)
ASHBY_JOB_RE = re.compile(r"^https://jobs\.ashbyhq\.com/Redis/(?P<uuid>[0-9a-f-]{36})$", re.I)
ASHBY_APPLY_RE = re.compile(
    r"^https://jobs\.ashbyhq\.com/Redis/(?P<uuid>[0-9a-f-]{36})/application$", re.I
)

LOCATION_FILTER_OPTIONS = [
    "All locations", "All Americas", "Canada", "HQ (San Francisco)", "Mexico",
    "United States", "All EMEA", "Bulgaria", "Germany", "Israel", "London, England",
    "United Arab Emirates", "All APAC", "India", "Austin, TX", "Vancouver",
]
DEPARTMENT_FILTER_OPTIONS = [
    "All departments", "CTO", "Customer Org", "General & Administrative",
    "Marketing", "Product", "R&D", "Sales",
]


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _require_redis_url(url: str, path: str, label: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "redis.io"
            or parsed.path.rstrip("/") != path.rstrip("/")):
        raise ValueError(f"{label} left the official Redis route: {url}")


def _flight_positions(html: str) -> list[dict]:
    """Read the initial ``positions`` array from the Next.js flight payload."""
    marker = html.find('"positions":')
    if marker < 0:
        marker = html.find('\\"positions\\":')
    if marker < 0:
        raise ValueError("Redis listing no longer publishes its initial positions payload")
    push = html.rfind('self.__next_f.push([1,"', 0, marker)
    if push < 0:
        raise ValueError("Redis listing positions payload is not inside a Next.js flight chunk")
    start = push + len('self.__next_f.push([1,"')
    index, escaped = start, False
    while index < len(html):
        char = html[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            break
        index += 1
    else:
        raise ValueError("Redis listing flight chunk is not terminated")
    decoded = json.loads('"' + html[start:index] + '"')
    position = decoded.find('"positions":')
    if position < 0:
        raise ValueError("Redis listing flight chunk no longer contains positions")
    array_start = decoded.find("[", position)
    depth, in_string, escaped = 0, False, False
    end = None
    for cursor, char in enumerate(decoded[array_start:], array_start):
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
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    end = cursor + 1
                    break
    if end is None:
        raise ValueError("Redis listing positions array is not balanced")
    value = json.loads(decoded[array_start:end])
    if not isinstance(value, list):
        raise ValueError("Redis listing positions payload is not an array")
    return value


def parse_official_entry(html: str, response_url: str = ENTRY_FINAL) -> dict:
    _require_redis_url(response_url, "/company/careers", "Redis official careers entry")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "Redis Careers" not in title:
        raise ValueError(f"Redis official careers entry title changed: {title!r}")
    if "/company/careers/current-job-openings/" not in html:
        raise ValueError("Redis official careers entry no longer links its current-job-openings page")
    featured = []
    seen = set()
    for position in _flight_positions(html):
        if not isinstance(position, dict):
            raise ValueError("Redis official careers entry position is not an object")
        pathname = str(position.get("pathname") or "")
        match = JOB_PATH_RE.fullmatch(pathname)
        if match is None:
            raise ValueError(f"Redis official careers entry has an uninspected position path: {pathname!r}")
        identity = match.group("uuid").lower()
        if identity in seen:
            continue
        seen.add(identity)
        featured.append({
            "uuid": identity,
            "path": pathname,
            "title": normalized(position.get("title")),
            "department": position.get("department"),
            "location": position.get("location"),
            "region": position.get("region"),
        })
    return {
        "url": response_url,
        "title": title,
        "listing_url": LISTING_URL,
        "featured_jobs": featured,
        "featured_count": len(featured),
    }


def parse_listing_page(html: str, response_url: str = LISTING_URL) -> dict:
    _require_redis_url(response_url, "/company/careers/current-job-openings", "Redis job listing")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "current job openings" not in title.casefold():
        raise ValueError(f"Redis job listing title changed: {title!r}")
    positions = _flight_positions(html)
    rows = []
    seen = set()
    for position in positions:
        if not isinstance(position, dict):
            raise ValueError("Redis listing position is not an object")
        pathname = position.get("pathname")
        match = JOB_PATH_RE.fullmatch(str(pathname or ""))
        if match is None:
            raise ValueError(f"Redis listing has an uninspected position path: {pathname!r}")
        identity = match.group("uuid").lower()
        if identity in seen:
            raise ValueError(f"Redis listing repeats position uuid {identity}")
        seen.add(identity)
        title_value = normalized(position.get("title"))
        if not title_value:
            raise ValueError(f"Redis listing position {identity} has no title")
        rows.append({
            "uuid": identity,
            "path": pathname,
            "title": title_value,
            "department": position.get("department"),
            "location": position.get("location"),
            "region": position.get("region"),
            "compensationSummary": position.get("compensationSummary"),
        })
    return {
        "url": response_url,
        "title": title,
        "positions": rows,
        "position_ids": [row["uuid"] for row in rows],
        "count": len(rows),
        "page_size": PAGE_SIZE,
        "pagination": "Load More button (client-rendered; 12 cards per request)",
        "location_filter_options": LOCATION_FILTER_OPTIONS,
        "department_filter_options": DEPARTMENT_FILTER_OPTIONS,
        "search_placeholder": "Search open positions",
    }


def parse_feed_page(value: dict, start: int, end: int) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Redis careers feed response is not an object")
    items = value.get("items")
    total = value.get("total")
    if not isinstance(items, list) or not isinstance(total, int) or total < 0:
        raise ValueError("Redis careers feed response is missing items/total")
    if start < 0 or end <= start:
        raise ValueError("Redis careers feed page range is invalid")
    expected = max(0, min(end, total) - start)
    issues = []
    if len(items) != expected:
        issues.append(f"feed_page_row_count:{len(items)}!={expected}")
    rows = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Redis careers feed row is not an object")
        raw_id = str(item.get("_id") or "")
        if not raw_id.startswith("ab-"):
            raise ValueError(f"Redis careers feed row has an unexpected id: {raw_id!r}")
        identity = raw_id[3:]
        if not UUID_RE.fullmatch(identity):
            raise ValueError(f"Redis careers feed row has an invalid uuid: {identity!r}")
        pathname = str(item.get("pathname") or "")
        match = JOB_PATH_RE.fullmatch(pathname)
        if match is None or match.group("uuid").lower() != identity.lower():
            raise ValueError(
                f"Redis careers feed row {identity} has an uninspected pathname: {pathname!r}")
        if identity.lower() in seen:
            raise ValueError(f"Redis careers feed repeats uuid {identity}")
        seen.add(identity.lower())
        title = normalized(item.get("title"))
        if not title:
            raise ValueError(f"Redis careers feed row {identity} has no title")
        rows.append({
            "id": raw_id,
            "uuid": identity.lower(),
            "path": pathname,
            "title": title,
            "department": item.get("department"),
            "location": item.get("location"),
            "compensationSummary": item.get("compensationSummary"),
            "shortDescription": item.get("shortDescription"),
        })
    return {
        "start": start,
        "end": end,
        "total": total,
        "expected": expected,
        "items": rows,
        "count": len(rows),
        "issues": issues,
    }


def parse_ashby_board(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
        raise ValueError("Redis Ashby board response is missing jobs")
    rows = {}
    for job in value["jobs"]:
        if not isinstance(job, dict):
            raise ValueError("Redis Ashby board row is not an object")
        identity = str(job.get("id") or "").lower()
        if not UUID_RE.fullmatch(identity):
            raise ValueError(f"Redis Ashby board row has an invalid id: {identity!r}")
        job_url = str(job.get("jobUrl") or "")
        apply_url = str(job.get("applyUrl") or "")
        job_match = ASHBY_JOB_RE.fullmatch(job_url)
        apply_match = ASHBY_APPLY_RE.fullmatch(apply_url)
        if job_match is None or job_match.group("uuid").lower() != identity:
            raise ValueError(f"Redis Ashby board row {identity} has an unexpected jobUrl: {job_url!r}")
        if apply_match is None or apply_match.group("uuid").lower() != identity:
            raise ValueError(f"Redis Ashby board row {identity} has an unexpected applyUrl: {apply_url!r}")
        if identity in rows:
            raise ValueError(f"Redis Ashby board repeats id {identity}")
        rows[identity] = job
    return {
        "count": len(rows),
        "ids": list(rows),
        "rows": rows,
        "apiVersion": value.get("apiVersion"),
    }


def extract_sections(description_html: str) -> dict:
    soup = BeautifulSoup(description_html or "", "lxml")
    root = soup.body or soup
    sections: list[dict] = []
    current = {"heading": None, "paragraphs": [], "bullets": []}
    sections.append(current)
    for node in root.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        if node.name.lower() in {"h1", "h2", "h3", "h4"}:
            heading = normalized(node.get_text(" ", strip=True))
            if heading:
                current = {"heading": heading, "paragraphs": [], "bullets": []}
                sections.append(current)
            continue
        if node.name.lower() == "li":
            value = normalized(node.get_text(" ", strip=True))
            if value:
                current["bullets"].append(value)
            continue
        if node.find_parent("li") is not None:
            continue
        value = normalized(node.get_text(" ", strip=True))
        if not value:
            continue
        # Some Redis descriptions use a short plain or bold paragraph as a
        # section heading (for example "<p>What you'll do:</p>").
        strong = node.find("strong")
        bold_only = (
            strong is not None and len(value) <= 90
            and normalized(strong.get_text(" ", strip=True)) == value
        )
        if len(value) <= 90 and (
            value.endswith((":", "?")) or bold_only
            or re.match(r"^(?:what|why|who|how|your|the role|the team|about|benefits|extra|requirements|qualifications|responsibilities|nice to have|we give back|our)\b", value, re.I)
        ):
            current = {"heading": value, "paragraphs": [], "bullets": []}
            sections.append(current)
            continue
        bullet = re.match(r"^[\u25cf\u2022\u2023\u25aa\u2013-]\s*(.+)$", value)
        if bullet:
            current["bullets"].append(bullet.group(1).strip())
            continue
        current["paragraphs"].append(value)
    sections = [section for section in sections if section["paragraphs"] or section["bullets"]]

    def collect(patterns: list[str]) -> list[str]:
        values = []
        for section in sections:
            heading = (section["heading"] or "").casefold()
            if any(re.search(pattern, heading, re.I) for pattern in patterns):
                values.extend(section["bullets"] or section["paragraphs"])
        return values

    return {
        "sections": sections,
        "responsibilities": collect([
            r"\bwhat (?:you['’]?ll|you will) do\b", r"\bresponsibilit", r"\byour role\b",
            r"\bthe role\b", r"\bwhat you['’]?ll be doing\b", r"\bwhat type of work\b",
        ]),
        "qualifications": collect([
            r"\bwhat (?:you['’]?ll|you will) need\b", r"\bwhat will you need\b",
            r"\bwhat (?:you['’]?ll|you will) bring\b", r"\bwhat you['’]?ll bring\b",
            r"\brequirements?\b", r"\bqualifications?\b", r"\bwho you are\b",
            r"\babout you\b", r"\bmust have\b", r"\byou should have\b",
            r"\bexperience and strengths\b", r"\byour experience\b",
        ]),
        "nice_to_have": collect([
            r"\bnice to have\b", r"\bbonus\b", r"\bpreferred\b",
            r"\bextra great if you have\b", r"\bif you have\b",
        ]),
    }


def parse_detail_page(html: str, response_url: str, listing_row: dict) -> dict:
    parsed_url = urlsplit(response_url)
    match = JOB_PATH_RE.fullmatch(parsed_url.path)
    if parsed_url.scheme != "https" or parsed_url.hostname != "redis.io" or match is None:
        raise ValueError(f"Redis detail page left its official route: {response_url}")
    identity = match.group("uuid").lower()
    if identity != listing_row["uuid"].lower():
        raise ValueError(f"Redis detail uuid {identity} differs from listing uuid {listing_row['uuid']}")
    soup = BeautifulSoup(html, "lxml")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None
    if canonical_url != response_url:
        raise ValueError(f"Redis detail canonical URL changed: {canonical_url!r}")
    postings = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            value = json.loads(node.string or node.get_text() or "")
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("@type") == "JobPosting":
            postings.append(value)
    if len(postings) != 1:
        raise ValueError(
            f"Redis detail page {identity} does not carry exactly one JobPosting document ({len(postings)})")
    posting = postings[0]
    title = normalized(posting.get("title"))
    if title != normalized(listing_row.get("title")):
        raise ValueError(
            f"Redis detail {identity} title {title!r} differs from listing title "
            f"{listing_row.get('title')!r}")
    identifier = (posting.get("identifier") or {}).get("value")
    if str(identifier or "").lower() != identity:
        raise ValueError(f"Redis detail {identity} identifier changed: {identifier!r}")
    employer = (posting.get("hiringOrganization") or {}).get("name")
    if employer != "Redis":
        raise ValueError(f"Redis detail {identity} has an unexpected hiring organization: {employer!r}")
    if posting.get("url") != response_url:
        raise ValueError(f"Redis detail {identity} JSON-LD url changed: {posting.get('url')!r}")
    date_posted = posting.get("datePosted")
    if not isinstance(date_posted, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_posted):
        raise ValueError(f"Redis detail {identity} has no usable datePosted: {date_posted!r}")
    description_html = posting.get("description") or ""
    description_text = text_html(description_html)
    if len(description_text.strip()) < 100:
        raise ValueError(f"Redis detail {identity} description is missing or implausibly short")

    h1 = soup.select_one("main h1") or soup.select_one("h1")
    if h1 is None or normalized(h1.get_text(" ", strip=True)) != title:
        raise ValueError(f"Redis detail {identity} visible h1 does not match the JSON-LD title")
    location_node = h1.parent.select_one("span") if h1.parent else None
    visible_location = normalized(location_node.get_text(" ", strip=True)) if location_node else None
    listing_location = normalized(listing_row.get("location"))
    if visible_location and listing_location and visible_location != listing_location:
        raise ValueError(
            f"Redis detail {identity} visible location {visible_location!r} differs from "
            f"listing location {listing_location!r}")
    embed = soup.select_one("#ashby_embed")
    if embed is None:
        raise ValueError(f"Redis detail {identity} no longer exposes its Ashby application embed")
    description_node = embed.find_previous_sibling("div")
    if description_node is None:
        raise ValueError(f"Redis detail {identity} no longer exposes a visible description block")
    visible_text = description_node.get_text("\n", strip=True)
    body_matches = normalized(visible_text) == normalized(description_text)
    sections = extract_sections(description_html)
    salary_text = salary_from_description(description_text)
    apply_anchor = soup.select_one('a[href="#ashby_embed"]')
    return {
        "uuid": identity,
        "url": response_url,
        "title": title,
        "canonical_url": canonical_url,
        "posting": posting,
        "description_html": description_html,
        "description_text": description_text,
        "description_sha256": sha256(normalized(description_text).encode()).hexdigest(),
        "visible_description_text": visible_text,
        "visible_description_sha256": sha256(normalized(visible_text).encode()).hexdigest(),
        "visible_body_matches_jsonld": body_matches,
        "visible_location": visible_location,
        "headings": [section["heading"] for section in sections["sections"] if section["heading"]],
        "sections": sections["sections"],
        "responsibilities": sections["responsibilities"],
        "qualifications": sections["qualifications"],
        "nice_to_have": sections["nice_to_have"],
        "salary_from_description": salary_text,
        "employer": employer,
        "date_posted": date_posted,
        "employment_type": posting.get("employmentType"),
        "job_location": posting.get("jobLocation"),
        "job_location_type": posting.get("jobLocationType"),
        "applicant_location_requirements": posting.get("applicantLocationRequirements"),
        "base_salary": posting.get("baseSalary"),
        "ashby_embed": True,
        "apply_anchor": apply_anchor.get("href") if apply_anchor else None,
    }


SALARY_KEYWORD_RE = re.compile(r"\b(?:salary|compensation)\s+range\b", re.I)


def salary_from_description(description_text: str) -> dict | None:
    """Retain the exact published salary sentence; do not infer missing values."""
    lines = (description_text or "").splitlines()
    for index, line in enumerate(lines):
        if not SALARY_KEYWORD_RE.search(line):
            continue
        candidate = line.strip()
        lookahead = index + 1
        while (not re.search(r"[.!?](?:\s|$)", candidate)
               and lookahead < len(lines) and lookahead <= index + 3):
            candidate = f"{candidate} {lines[lookahead].strip()}".strip()
            lookahead += 1
        for sentence in re.split(r"(?<=[.!?])\s+", candidate):
            if not SALARY_KEYWORD_RE.search(sentence):
                continue
            sentence = normalized(sentence)
            amounts = [
                {"currency": currency, "value": value}
                for currency, value in re.findall(r"([$€£])\s?([\d][\d,]*)", sentence)
            ]
            return {"source": "description_text", "text": sentence, "amounts": amounts}
    return None


def _employment_label(value: str | None) -> str | None:
    return {
        "FULL_TIME": "Full-time", "PART_TIME": "Part-time", "CONTRACTOR": "Contract",
        "TEMPORARY": "Temporary", "INTERN": "Internship", "VOLUNTEER": "Volunteer",
        "PER_DIEM": "Per diem", "OTHER": "Other",
    }.get((value or "").upper(), value)


def _workplace_label(value: str | None) -> str | None:
    return {"Remote": "Remote", "Hybrid": "Hybrid", "OnSite": "On-site"}.get(value or "", value)


class Redis(Adapter):
    """Collect only the exact jobs published by redis.io's own careers listing."""

    VERIFICATION_REVISION = "redis.first-party-feed-and-jsonld-details.2026-11-08"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.failed_details: dict[str, str] = {}
        self._ashby: dict | None = None

    async def _feed_snapshot(self, label: str) -> tuple[list[dict], int, list[dict]]:
        rows: list[dict] = []
        pages: list[dict] = []
        total = None
        start = 0
        while total is None or start < total:
            if not self.page_guard():
                raise ValueError(f"Redis {label} listing page budget exhausted")
            query = urlencode({
                "start": start, "end": start + PAGE_SIZE,
                "location": "", "department": "", "search": "",
            })
            url = f"{FEED_API}?{query}"
            response = await self.http.get(url)
            if str(response.url) != url:
                raise ValueError(f"Redis {label} careers feed page redirected away: {response.url}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError(f"Redis {label} careers feed page is not JSON") from exc
            page = parse_feed_page(payload, start, start + PAGE_SIZE)
            if total is None:
                total = page["total"]
            elif page["total"] != total:
                raise ValueError(
                    f"Redis {label} careers feed total changed from {total} to {page['total']}")
            if page["issues"]:
                raise ValueError(f"Redis {label} careers feed page {start}: {page['issues']}")
            rows.extend(page["items"])
            pages.append({
                "label": label, "url": url, "start": start,
                "end": start + PAGE_SIZE, "rows": page["count"], "total": page["total"],
            })
            if not page["items"]:
                break
            start += PAGE_SIZE
        if total is None:
            raise ValueError(f"Redis {label} careers feed returned no pages")
        if len(rows) != total:
            raise ValueError(f"Redis {label} careers feed reconciled {len(rows)} rows, total {total}")
        ids = [row["uuid"] for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Redis {label} careers feed repeats a uuid")
        return rows, total, pages

    async def _ashby_snapshot(self) -> dict:
        response = await self.http.get(ASHBY_BOARD_API)
        if str(response.url) != ASHBY_BOARD_API:
            raise ValueError(f"Redis Ashby board redirected away: {response.url}")
        try:
            value = response.json()
        except ValueError as exc:
            raise ValueError("Redis Ashby board is not JSON") from exc
        return parse_ashby_board(value)

    def _apply_fields(self, job, parsed: dict, ashby_row: dict | None) -> None:
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "redis.official_detail.jobposting_jsonld"
        job.description_source_url = parsed["url"]
        job.apply_url = ashby_row.get("applyUrl") if ashby_row else None
        job.employment_type = _employment_label(parsed.get("employment_type"))
        job.workplace_type = _workplace_label((ashby_row or {}).get("workplaceType"))
        job.date_posted = parsed.get("date_posted")
        job.date_posted_raw = {
            "jsonld_datePosted": parsed.get("date_posted"),
            "ashby_publishedAt": (ashby_row or {}).get("publishedAt"),
        }
        job.salary = parsed.get("base_salary")
        compensation = (ashby_row or {}).get("compensation") or {}
        if job.salary is None and any(compensation.get(key) for key in (
                "compensationTierSummary", "scrapeableCompensationSalarySummary",
                "compensationTiers", "summaryComponents")):
            job.salary = compensation
        salary_text = parsed.get("salary_from_description")
        if job.salary is None:
            job.salary = salary_text
        elif salary_text and isinstance(job.salary, dict):
            job.salary = {
                **job.salary,
                "description_text": salary_text["text"],
                "description_amounts": salary_text["amounts"],
            }
        job.qualifications = parsed.get("qualifications") or None
        job.responsibilities = parsed.get("responsibilities") or None
        job.employer = parsed.get("employer")
        job.team = (ashby_row or {}).get("team")
        job.locations = [{
            "location": job.raw["listing_item"].get("location"),
            "source": "redis.io_careers_feed",
            "job_location": parsed.get("job_location"),
            "job_location_type": parsed.get("job_location_type"),
            "applicant_location_requirements": parsed.get("applicant_location_requirements"),
            "ashby_address": (ashby_row or {}).get("address"),
            "ashby_secondary_locations": (ashby_row or {}).get("secondaryLocations"),
        }]
        job.raw["detail"] = {
            "canonical_url": parsed["canonical_url"],
            "response_sha256": job.raw.get("detail_response_sha256"),
            "description_sha256": parsed["description_sha256"],
            "description_chars": len(normalized(parsed["description_text"])),
            "visible_description_sha256": parsed["visible_description_sha256"],
            "visible_description_chars": len(normalized(parsed["visible_description_text"])),
            "visible_body_matches_jsonld": parsed["visible_body_matches_jsonld"],
            "visible_location": parsed["visible_location"],
            "date_posted": parsed.get("date_posted"),
            "employment_type": parsed.get("employment_type"),
            "job_location": parsed.get("job_location"),
            "job_location_type": parsed.get("job_location_type"),
            "applicant_location_requirements": parsed.get("applicant_location_requirements"),
            "base_salary": parsed.get("base_salary"),
            "headings": parsed["headings"],
            "sections": parsed["sections"],
            "responsibilities": parsed["responsibilities"],
            "qualifications": parsed["qualifications"],
            "nice_to_have": parsed["nice_to_have"],
            "salary_from_description": parsed["salary_from_description"],
            "ashby_embed": parsed["ashby_embed"],
            "apply_anchor": parsed["apply_anchor"],
            "jsonld": parsed["posting"],
        }
        if ashby_row is not None:
            ashby_body = text_html(ashby_row.get("descriptionHtml") or "")
            job.raw["ashby_board"] = {
                key: value for key, value in ashby_row.items()
                if key not in {"descriptionHtml", "descriptionPlain"}
            }
            job.raw["ashby_board"]["description_sha256"] = sha256(
                normalized(ashby_body).encode()).hexdigest()
            job.raw["ashby_board"]["description_matches_redis"] = (
                normalized(ashby_body) == normalized(parsed["description_text"]))
            if not job.raw["ashby_board"]["description_matches_redis"]:
                job.warnings.append("ashby_description_differs")
            published = str(ashby_row.get("publishedAt") or "")
            if published[:10] and published[:10] != parsed.get("date_posted"):
                job.warnings.append("ashby_published_date_differs")
        if not parsed["visible_body_matches_jsonld"]:
            job.warnings.append("visible_description_mismatch")
            job.warnings.append("description_not_full_field")
            self.result.errors.append(
                f"Redis detail {job.job_id} visible body differs from JSON-LD description")
        if not job.apply_url:
            job.warnings.append("apply_url_missing")

    async def run(self) -> Result:
        if self.company.slug != "redis":
            raise ValueError("Redis adapter requires the Redis company")
        if self.source.url != LISTING_URL:
            raise ValueError(f"Redis adapter only starts at {LISTING_URL}")
        collection_started = time.monotonic()

        entry_started = time.monotonic()
        entry = await self.http.get(OFFICIAL_ENTRY)
        entry_meta = parse_official_entry(entry.text, str(entry.url))
        if entry_meta["listing_url"] != LISTING_URL:
            raise ValueError("Redis careers entry no longer links the inspected listing URL")
        listing = await self.http.get(LISTING_URL)
        listing_meta = parse_listing_page(listing.text, str(listing.url))
        entry_listing_seconds = time.monotonic() - entry_started

        feed_started = time.monotonic()
        initial_rows, total, initial_pages = await self._feed_snapshot("initial")
        feed_seconds = time.monotonic() - feed_started
        if total == 0:
            self.result.expected_total = 0
            self.result.listing_complete = False
            self.result.errors.append("redis_official_listing_returned_zero_jobs")
            return self.result
        initial_ids = [row["uuid"] for row in initial_rows]
        listing_ids = listing_meta["position_ids"]
        if (len(listing_ids) > len(initial_ids)
                or listing_ids != initial_ids[:len(listing_ids)]):
            raise ValueError(
                "Redis listing's initial rendered positions differ from the first feed page: "
                f"{listing_ids} != {initial_ids[:len(listing_ids)]}")
        featured_ids = [row["uuid"] for row in entry_meta["featured_jobs"]]
        missing_featured = [identity for identity in featured_ids if identity not in initial_ids]
        if missing_featured:
            self.result.errors.append(
                f"redis_entry_featured_jobs_not_in_listing:{missing_featured}")

        ashby_started = time.monotonic()
        ashby = None
        ashby_error = None
        try:
            ashby = await self._ashby_snapshot()
            self._ashby = ashby
        except Exception as exc:  # noqa: BLE001 - recorded as a source failure
            ashby_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(f"redis_ashby_board_crosscheck_failed:{ashby_error}")
        ashby_seconds = time.monotonic() - ashby_started
        ashby_ids = ashby["ids"] if ashby else []
        missing_in_ashby = [identity for identity in initial_ids if identity not in set(ashby_ids)]
        extra_in_ashby = [identity for identity in ashby_ids if identity not in set(initial_ids)]
        if missing_in_ashby or extra_in_ashby:
            self.result.warnings.append(
                f"redis_ashby_membership_differs:missing={missing_in_ashby}:extra={extra_in_ashby}")

        self.result.expected_total = total
        for row in initial_rows:
            job = self.job(
                {
                    "listing_item": row,
                    "listing_page": {
                        "api": FEED_API, "page_size": PAGE_SIZE,
                        "initial_total": total,
                    },
                    "source_ui": {
                        "listing_url": LISTING_URL,
                        "pagination": "Load More",
                        "location_filter_options": LOCATION_FILTER_OPTIONS,
                        "department_filter_options": DEPARTMENT_FILTER_OPTIONS,
                    },
                },
                title=row["title"],
                url="https://redis.io" + row["path"],
                job_id=row["uuid"],
                locations=[row["location"]] if row.get("location") else [],
                department=row.get("department"),
            )
            job.record_identity = f"redis:{row['uuid']}"
            self.add(job)

        async def detail(job):
            try:
                response = await self.http.get(job.url)
                digest = sha256(response.content).hexdigest()
                parsed = parse_detail_page(
                    response.text, str(response.url), job.raw["listing_item"])
                ashby_row = (self._ashby or {}).get("rows", {}).get(job.job_id)
                job.raw["detail_response_sha256"] = digest
                self._apply_fields(job, parsed, ashby_row)
                job.validate()
                return job
            except Exception as exc:
                self.failed_details[job.job_id] = f"{type(exc).__name__}: {exc}"
                raise

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        unverified = [job.job_id for job in self.result.jobs if not job.description_verified]
        if unverified:
            self.result.errors.append(f"redis_descriptions_unverified:{unverified}")

        final_started = time.monotonic()
        final_complete = True
        final_rows: list[dict] = []
        final_pages: list[dict] = []
        try:
            final_rows, final_total, final_pages = await self._feed_snapshot("final")
            if final_total != total:
                final_complete = False
                self.result.errors.append(
                    f"redis_listing_total_changed_during_run:{total}->{final_total}")
            final_ids = [row["uuid"] for row in final_rows]
            if final_ids != initial_ids:
                final_complete = False
                self.result.errors.append("redis_listing_membership_changed_during_run")
            changed = [
                identity for identity, before in zip(initial_ids, initial_rows)
                for after in final_rows
                if after["uuid"] == identity and after != before
            ]
            if changed:
                final_complete = False
                self.result.errors.append(f"redis_listing_fields_changed_during_run:{changed}")
        except Exception as exc:  # noqa: BLE001 - final recheck failure is recorded
            final_complete = False
            self.result.errors.append(
                f"redis_final_listing_recheck_failed:{type(exc).__name__}: {exc}")
        final_seconds = time.monotonic() - final_started

        self.result.listing_complete = bool(
            final_complete and len(self.result.jobs) == total and not unverified
            and not any(error.startswith("redis_official_listing") for error in self.result.errors)
        )
        self.result.metadata.update({
            "official_journey": {
                "official_entry_url": OFFICIAL_ENTRY,
                "entry_final_url": entry_meta["url"],
                "entry_title": entry_meta["title"],
                "listing_url": LISTING_URL,
                "listing_title": listing_meta["title"],
                "feed_api": FEED_API,
                "feed_page_size": PAGE_SIZE,
                "pagination": listing_meta["pagination"],
                "location_filter_options": listing_meta["location_filter_options"],
                "department_filter_options": listing_meta["department_filter_options"],
                "search_placeholder": listing_meta["search_placeholder"],
                "filters_applied": False,
                "region_filter_observation": (
                    "UI region options request region=EMEA/Americas/APAC; observed totals "
                    "12/13/4=29 because Austin, TX (1) and Vancouver (2) are outside the "
                    "region mapping. The unfiltered feed still returns all 32."
                ),
            },
            "entry_featured_jobs": entry_meta["featured_jobs"],
            "entry_featured_count": entry_meta["featured_count"],
            "initial_listing_positions": listing_meta["positions"],
            "initial_feed_pages": initial_pages,
            "final_feed_pages": final_pages,
            "membership_reconciliation": {
                "expected_total": total,
                "feed_rows": len(initial_rows),
                "jobs_collected": len(self.result.jobs),
                "initial_ids": initial_ids,
                "final_ids": [row["uuid"] for row in final_rows],
                "final_recheck_complete": final_complete,
                "initial_ssr_positions_match_feed_prefix": True,
                "entry_featured_ids": featured_ids,
                "entry_featured_missing": missing_featured,
                "ashby_board_ids": ashby_ids,
                "missing_in_ashby": missing_in_ashby,
                "extra_in_ashby": extra_in_ashby,
                "complete": self.result.listing_complete,
            },
            "ashby_crosscheck": {
                "url": ASHBY_BOARD_API,
                "error": ashby_error,
                "count": ashby["count"] if ashby else 0,
                "ids_match": set(ashby_ids) == set(initial_ids),
                "missing_in_ashby": missing_in_ashby,
                "extra_in_ashby": extra_in_ashby,
                "descriptions_match": sum(
                    bool(job.raw.get("ashby_board", {}).get("description_matches_redis"))
                    for job in self.result.jobs),
                "dates_match": sum(
                    bool(job.raw.get("ashby_board"))
                    and not any(w == "ashby_published_date_differs" for w in job.warnings)
                    for job in self.result.jobs),
            },
            "detail_stats": self.result.metadata.get("details", {}),
            "failed_details": self.failed_details,
        })
        self.result.timings.update({
            "redis_entry_and_listing_seconds": round(entry_listing_seconds, 6),
            "redis_feed_collection_seconds": round(feed_seconds, 6),
            "redis_ashby_crosscheck_seconds": round(ashby_seconds, 6),
            "redis_final_recheck_seconds": round(final_seconds, 6),
            "redis_collection_wall_seconds": round(time.monotonic() - collection_started, 6),
        })
        return self.result
