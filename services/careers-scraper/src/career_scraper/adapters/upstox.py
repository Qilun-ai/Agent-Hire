"""Upstox's own careers page, its published jobs API and its Darwinbox detail records.

The ledger entry ``https://upstox.com/careers/`` is Upstox's Next.js marketing
site.  The page server-renders and then hydrates its "Upstox Careers" listing
from ``GET https://service.upstox.com/content/open/v1/jobs/``.  That payload
holds 58 Darwinbox recruitment rows; the page's own mapper only publishes rows
whose ``post_on_careers_page`` is ``1`` (13 at inspection time) and the rendered
grid says "Showing 13 of 13 jobs".  The cards carry generated teaser text, not
the description, and their action opens the company tenant page
``https://upstox.darwinbox.in/ms/candidatev2/main/careers/jobDetails/<id>``.
The exact full description is the ``message.job[0].jd`` field returned by that
tenant's public ``GET /ms/candidateapi/job/<id>?companyId=main`` detail request.
Only the 13 IDs published on Upstox's own page are ever requested.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import unescape
import json
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html

OFFICIAL_ENTRY = "https://upstox.com/careers/"
ENTRY_HOSTS = frozenset({"upstox.com", "www.upstox.com"})
LISTING_API = "https://service.upstox.com/content/open/v1/jobs/"
LISTING_HOST = "service.upstox.com"
DETAIL_HOST = "upstox.darwinbox.in"
DETAIL_API_TEMPLATE = f"https://{DETAIL_HOST}/ms/candidateapi/job/{{identity}}?companyId=main"
DETAIL_PAGE_TEMPLATE = f"https://{DETAIL_HOST}/ms/candidatev2/main/careers/jobDetails/{{identity}}?from=all"
COMPANY_CONFIG_API = (
    f"https://{DETAIL_HOST}/ms/candidateapi/getCompanyConfig?is_preview=false&companyId=main"
)
APPLY_ROUTE = f"https://{DETAIL_HOST}/ms/candidatev2/main/auth/login"
COMPANY_ID = "main"
TENANT_ID = "238"
EXPECTED_COMPANY_NAME = "UPSTOX"
JOB_ID_RE = re.compile(r"^[0-9a-f]{14}$")
IST = timezone(timedelta(hours=5, minutes=30))
# The public Darwinbox JSON route answers a browser user agent and rejects
# library user agents with 403.  No CAPTCHA or challenge page is served for
# these reads; the candidate application flow (not exercised here) uses Turnstile.
PUBLIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
FLIGHT_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', re.S)
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
LEAF_BLOCKS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "td", "th"})
CONTAINER_BLOCKS = frozenset({"html", "body", "div", "section", "article", "main", "ul", "ol", "table", "tbody", "thead", "tr"})
KNOWN_HEADINGS = {
    "about upstox", "about us", "about the role", "about the job", "job description",
    "the role", "role overview", "who you are", "what you'll own", "what you will own",
    "what you'll do", "what you will do", "what we're looking for", "what we’re looking for",
    "what we are looking for", "key responsibilities", "responsibilities", "key duties",
    "qualifications", "requirements", "who should apply", "must have", "good to have",
    "nice to have", "what you'll bring", "what you bring", "skills", "experience",
    "education", "why this role rocks", "why join us", "benefits", "perks",
}
RESPONSIBILITY_TOKENS = (
    "responsibilit", "what you'll own", "what you will own", "what you'd own",
    "what you would own", "what you'll do", "what you will do", "what you'd do",
    "key duties",
)
QUALIFICATION_TOKENS = (
    "qualif", "requirement", "who you are", "what we're looking for", "what we are looking for",
    "must have", "good to have", "nice to have", "required", "what you'll bring",
    "what you bring", "what you'll need", "what you need", "skills", "education",
    "who should apply",
)
LABEL_LINE = re.compile(
    r"^(?:job\s+title|title|role|company|location|work(?:place)?\s+arrangement|experience|"
    r"reports\s+to|function|department|employment\s+type|employee\s+type|qualification)\s*:",
    re.I,
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalize_visible_text(value: str | None) -> str:
    """NFC/collapsed visible text, with no space before punctuation."""
    text = unicodedata.normalize("NFC", value or "").replace("\u00a0", " ")
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_visible_text(value: str | None) -> str:
    """Whitespace-free visible text: stable against inline-anchor spacing quirks."""
    text = unicodedata.normalize("NFC", value or "")
    return re.sub(r"[\s\u00a0\u200b\u200c\u200d\ufeff]+", "", text)


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def decoded_description(raw: str | None) -> str:
    """Decode the escaped HTML wrapper Darwinbox stores in ``jd`` exactly once."""
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


def compact_body_text(html: str | None) -> str:
    return compact_visible_text(text_html(decoded_description(html)))


def job_detail_api_url(identity) -> str:
    if not JOB_ID_RE.fullmatch(str(identity)):
        raise ValueError(f"Upstox job ID is not the observed 14-hex form: {identity!r}")
    return DETAIL_API_TEMPLATE.format(identity=identity)


def detail_page_url(identity) -> str:
    if not JOB_ID_RE.fullmatch(str(identity)):
        raise ValueError(f"Upstox job ID is not the observed 14-hex form: {identity!r}")
    return DETAIL_PAGE_TEMPLATE.format(identity=identity)


def _flight_payload(html: str) -> str:
    chunks = FLIGHT_CHUNK.findall(html or "")
    if not chunks:
        raise ValueError("Upstox careers page has no Next.js flight payload")
    return "".join(json.loads(chunk) for chunk in chunks)


def _balanced_object(text: str, marker: str) -> dict:
    start = text.find(marker)
    if start < 0:
        raise ValueError(f"Upstox careers payload has no {marker} object")
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:index + 1])
    raise ValueError("Upstox careers payload object is not closed")


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Validate the company entry page and its server-rendered job cards."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc not in ENTRY_HOSTS
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Upstox careers entry redirected away from its official URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    headings = [normalized_title(node.get_text(" ", strip=True)) for node in soup.select("h1")]
    if "Upstox" not in title or "Careers" not in title or "Upstox Careers" not in headings:
        raise ValueError("Upstox careers page identity changed (title/h1)")
    page_text = soup.get_text(" ", strip=True)
    open_positions = re.search(r"(\d+)\s+open positions", page_text, re.I)
    showing = re.search(r"Showing\s+(\d+)\s+of\s+(\d+)\s+jobs", page_text, re.I)
    filters = [normalized_title(label.get_text(" ", strip=True)) for label in soup.select("label span")]

    payload = _balanced_object(_flight_payload(html), '{"initialJobs"')
    initial_jobs = payload.get("initialJobs")
    if not isinstance(initial_jobs, list) or not initial_jobs:
        raise ValueError("Upstox careers page has no server-rendered initialJobs")
    rows, seen = [], set()
    for row in initial_jobs:
        if not isinstance(row, dict) or not JOB_ID_RE.fullmatch(str(row.get("id", ""))):
            raise ValueError("Upstox server-rendered job card is missing its exact job ID")
        identity = str(row["id"])
        if identity in seen:
            raise ValueError(f"Upstox server-rendered job card repeats ID {identity}")
        if not normalized_title(row.get("title")):
            raise ValueError(f"Upstox server-rendered job card {identity} has no title")
        seen.add(identity)
        rows.append(row)
    visible_total = int(showing.group(2)) if showing else None
    if open_positions and int(open_positions.group(1)) != len(rows):
        raise ValueError("Upstox open-positions count differs from its rendered cards")
    if showing and (int(showing.group(1)) != len(rows) or visible_total != len(rows)):
        raise ValueError("Upstox 'Showing X of Y jobs' differs from its rendered cards")
    cards = soup.select("main h3")
    if len(cards) != len(rows):
        raise ValueError("Upstox rendered card grid count differs from initialJobs")
    return {
        "url": response_url,
        "title": title,
        "h1": "Upstox Careers",
        "open_positions": int(open_positions.group(1)) if open_positions else None,
        "visible_showing": int(showing.group(1)) if showing else None,
        "visible_total": visible_total,
        "filters": filters,
        "card_titles": [normalized_title(row["title"]) for row in rows],
        "rows": rows,
        "job_ids": [str(row["id"]) for row in rows],
        "count": len(rows),
    }


def parse_listing_api(payload) -> dict:
    """Validate the page's own jobs payload and split published vs unpublished rows."""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("Upstox jobs API did not return success:true")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("Upstox jobs API has no data array")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Upstox jobs API row is not an object")
        identity = str(row.get("job_id", ""))
        if not JOB_ID_RE.fullmatch(identity):
            raise ValueError(f"Upstox jobs API row has no exact job_id: {identity!r}")
        if identity in seen:
            raise ValueError(f"Upstox jobs API repeats job_id {identity}")
        if not normalized_title(row.get("job_title")):
            raise ValueError(f"Upstox jobs API row {identity} has no job_title")
        if row.get("post_on_careers_page") not in (0, 1):
            raise ValueError(f"Upstox jobs API row {identity} has an unexpected careers flag")
        seen.add(identity)
    published = [row for row in rows if row.get("post_on_careers_page") == 1]
    unpublished = [row for row in rows if row.get("post_on_careers_page") != 1]
    fields = sorted({key for row in rows for key in row})
    return {
        "rows": published,
        "all_rows": rows,
        "job_ids": [str(row["job_id"]) for row in published],
        "excluded_ids": [str(row["job_id"]) for row in unpublished],
        "careers_count": len(published),
        "total_records": len(rows),
        "excluded_count": len(unpublished),
        "field_names": fields,
        "meta_data": payload.get("metaData"),
        "request_id": payload.get("requestId"),
    }


def parse_company_config(payload) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), dict):
        raise ValueError("Upstox Darwinbox company config has no message object")
    company = payload["message"].get("company")
    if not isinstance(company, dict):
        raise ValueError("Upstox Darwinbox company config has no company object")
    return {
        "company_name": company.get("company_name"),
        "subdomain": company.get("subdomain"),
        "tenant_id": str(company.get("tenant_id")) if company.get("tenant_id") is not None else None,
        "all_jobs_count": company.get("allJobsCount"),
        "group_company_site": company.get("group_company_site"),
        "new_careers": company.get("new_careers"),
        "recruitment_enabled": company.get("recruitment_enabled"),
    }


def parse_detail_api(payload, identity, listing_row: dict | None = None) -> dict:
    """Unwrap ``GET job/{id}`` and return its single Darwinbox job object."""
    if not isinstance(payload, dict):
        raise ValueError("Upstox detail API response is not an object")
    if payload.get("status") != "success":
        raise ValueError(f"Upstox detail API status is not success: {payload.get('status')!r}")
    message = payload.get("message")
    jobs = message.get("job") if isinstance(message, dict) else None
    if not isinstance(jobs, list) or not jobs:
        raise ValueError(f"Upstox detail API response for {identity} has no job object")
    row = jobs[0]
    if not isinstance(row, dict) or str(row.get("id")) != str(identity):
        raise ValueError(f"Upstox detail API response ID differs from requested ID {identity}")
    title = normalized_title(row.get("designation_display_name") or row.get("title"))
    if not title:
        raise ValueError(f"Upstox detail API response {identity} has no title")
    if listing_row is not None:
        listed = normalized_title(listing_row.get("job_title"))
        if listed and listed != title:
            raise ValueError(f"Upstox detail title differs from listed row for {identity}: {title!r} != {listed!r}")
        code = row.get("internal_job_code")
        if code and listing_row.get("job_code") and str(code) != str(listing_row["job_code"]):
            raise ValueError(f"Upstox detail job code differs from listed row for {identity}")
    return row


def _iter_leaf_blocks(node: Tag):
    for child in node.children:
        if not isinstance(child, Tag):
            continue
        if child.name in LEAF_BLOCKS or child.name in CONTAINER_BLOCKS:
            nested = child.find_all(list((LEAF_BLOCKS | CONTAINER_BLOCKS) - {"html", "body"}))
            has_text = any(block is not child and block.get_text(" ", strip=True) for block in nested)
            if has_text:
                yield from _iter_leaf_blocks(child)
            else:
                text = normalized_title(child.get_text(" ", strip=True))
                if text:
                    yield child, text


def _is_bold(node: Tag) -> bool:
    if node.name in HEADING_TAGS:
        return True
    if node.find(["strong", "b"]):
        return True
    for span in node.find_all("span"):
        style = (span.get("style") or "").replace(" ", "").lower()
        if "font-weight:700" in style or "font-weight:bold" in style:
            if span.get_text(" ", strip=True):
                return True
    return False


def _heading_key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(":").replace("’", "'").lower()


def description_sections(description_html: str | None) -> list[dict]:
    """Split the publisher's body into labelled blocks where headings are visible."""
    soup = BeautifulSoup(description_html or "", "lxml")
    sections: list[dict] = []
    current = {"heading": None, "paragraphs": []}
    for node, text in _iter_leaf_blocks(soup):
        key = _heading_key(text)
        heading_like = not LABEL_LINE.match(text) and (
            node.name in HEADING_TAGS or key in KNOWN_HEADINGS or (
                _is_bold(node) and len(text) <= 90 and not text.rstrip().endswith((".", "!", "?"))
            )
        )
        if heading_like:
            if current["heading"] is not None or current["paragraphs"]:
                sections.append(current)
            current = {"heading": normalized_title(text).rstrip(":"), "paragraphs": []}
        else:
            current["paragraphs"].append(text)
    if current["heading"] is not None or current["paragraphs"]:
        sections.append(current)
    for section in sections:
        section["text"] = "\n".join(section.pop("paragraphs")).strip()
    return [section for section in sections if section["heading"] or section["text"]]


def _role_sections(sections: list[dict], tokens: tuple[str, ...]) -> list[dict]:
    """Pick labelled role sections; absorb unlabelled sub-headings of a matched section."""
    picked = []
    peers = RESPONSIBILITY_TOKENS + QUALIFICATION_TOKENS
    for index, section in enumerate(sections):
        heading = (section.get("heading") or "").replace("’", "'").lower()
        if not heading or not any(token in heading for token in tokens):
            continue
        parts = [section["text"]]
        if not section["text"]:
            for following in sections[index + 1:]:
                next_heading = (following.get("heading") or "").replace("’", "'").lower()
                if next_heading in KNOWN_HEADINGS or (
                        next_heading and any(token in next_heading for token in peers)):
                    break
                text = following["text"]
                if following.get("heading"):
                    text = f"{following['heading']}: {text}".strip()
                if text:
                    parts.append(text)
        picked.append({"heading": section["heading"], "text": "\n".join(part for part in parts if part)})
    return picked


def _iso_to_ist_date(value) -> str | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(IST).date().isoformat()


def _epoch_to_ist_date(value) -> str | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text
        try:
            value = int(float(text))
        except ValueError:
            return None
    try:
        return datetime.fromtimestamp(int(value), tz=IST).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _listing_timestamp_to_ist_date(value) -> str | None:
    """The page's ``DD-MM-YYYY HH:mm:ss`` created timestamp, interpreted as IST."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})(?:\s+(\d{2}):(\d{2}):(\d{2}))?", value.strip())
    if not match:
        return None
    day, month, year, hour, minute, second = match.groups()
    parsed = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0), tzinfo=IST)
    return parsed.date().isoformat()


def _locations(detail_row: dict, listing_row: dict | None = None) -> list[dict]:
    """All public office locations; the tenant collapses multiples to 'Multiple locations'."""
    cleaned = []
    for key in ("tool_tip_locations", "officelocation_show_arr_list", "officelocations_without_area"):
        values = detail_row.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            text = re.sub(r"\s+", " ", str(value or "")).replace("\r", "").strip()
            if text and text.lower() != "multiple locations" and text not in cleaned:
                cleaned.append(text)
        if cleaned:
            break
    if not cleaned and listing_row is not None:
        for value in listing_row.get("location") or []:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            if text and text not in cleaned:
                cleaned.append(text)
    return [{"location": value, "address": None} for value in cleaned]


def _experience(detail_row: dict) -> dict | None:
    raw = detail_row.get("experience")
    minimum = detail_row.get("experience_from_num")
    maximum = detail_row.get("experience_to_num")
    if not raw and minimum is None and maximum is None:
        return None

    def years(months):
        return round(months / 12, 2) if isinstance(months, (int, float)) else None

    return {
        "raw": raw,
        "minimum_years": years(minimum),
        "maximum_years": years(maximum),
        "source": "darwinbox.experience / experience_from_num(months) / experience_to_num(months)",
    }


def _salary(detail_row: dict) -> dict | None:
    fields = {key: detail_row.get(key) for key in
              ("salary_currency", "salary_min", "salary_max", "salary_range", "salary_timeframe")}
    if not any(detail_row.get(key) not in (None, "", 0) for key in ("salary_min", "salary_max", "salary_range")):
        return None
    return fields


def _attachment_links(description_html: str) -> list[dict]:
    soup = BeautifulSoup(description_html or "", "lxml")
    links = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if ATTACHMENT_SUFFIX.search(href):
            links.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return links


def _description_links(description_html: str) -> list[dict]:
    soup = BeautifulSoup(description_html or "", "lxml")
    return [{"url": (anchor.get("href") or "").strip(), "label": anchor.get_text(" ", strip=True) or None}
            for anchor in soup.select("a[href]")]


class Upstox(Adapter):
    """Read only Upstox's own careers page and the exact jobs it publishes there."""

    VERIFICATION_REVISION = "upstox.official-careers.jobs-api+darwinbox-candidateapi.1"
    RETRYABLE = ("HTTP 403", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.retry_events: list[dict] = []
        self.blocked = False

    async def _retry(self, request, *, attempts: int = 4, label: str = ""):
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
                    await self._sleep(1.5 * attempt)
        raise last

    @staticmethod
    async def _sleep(seconds: float):
        import asyncio
        await asyncio.sleep(seconds)

    async def _entry(self, label: str):
        started = time.perf_counter()
        response = await self._retry(lambda: self.http.get(OFFICIAL_ENTRY), label=f"official entry {label}")
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != OFFICIAL_ENTRY:
            raise ValueError("Upstox official entry redirected away from its inspected URL")
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(
            status=response.status_code, seconds=round(seconds, 6),
            response_bytes=len(response.content), response_sha256=sha256(response.content).hexdigest(),
        )
        return parsed

    async def _listing(self, label: str):
        started = time.perf_counter()
        response = await self._retry(lambda: self.http.get(LISTING_API), label=f"jobs API {label}")
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != LISTING_API or urlsplit(str(response.url)).netloc != LISTING_HOST:
            raise ValueError("Upstox jobs API redirected away from its observed source")
        parsed = parse_listing_api(response.json())
        parsed.update(
            url=str(response.url), status=response.status_code, seconds=round(seconds, 6),
            response_bytes=len(response.content), response_sha256=sha256(response.content).hexdigest(),
        )
        return parsed

    async def _company_config(self):
        started = time.perf_counter()
        try:
            response = await self._retry(
                lambda: self.http.get(COMPANY_CONFIG_API, headers=PUBLIC_HEADERS),
                label="darwinbox company config", attempts=3,
            )
            parsed = parse_company_config(response.json())
            parsed.update(url=str(response.url), status=response.status_code,
                          seconds=round(max(0.0, time.perf_counter() - started), 6),
                          response_bytes=len(response.content),
                          response_sha256=sha256(response.content).hexdigest())
            return parsed, None
        except (FetchError, ValueError) as exc:
            return None, f"{type(exc).__name__}: {exc}"

    async def _detail(self, identity: str):
        started = time.perf_counter()
        response = await self._retry(
            lambda: self.http.get(job_detail_api_url(identity), headers=PUBLIC_HEADERS),
            label=f"detail {identity}",
        )
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != job_detail_api_url(identity):
            raise ValueError(f"Upstox detail API for {identity} redirected away from its source")
        payload = response.json()
        meta = {
            "url": str(response.url), "status": response.status_code,
            "request_seconds": round(seconds, 6), "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "request_headers": dict(PUBLIC_HEADERS),
        }
        return payload, meta

    def _make_job(self, listing_row: dict, card: dict, detail_row: dict | None,
                  detail_meta: dict | None, detail_error: str | None):
        identity = str(listing_row["job_id"])
        raw_jd = (detail_row or {}).get("jd") or ""
        description_html = decoded_description(raw_jd)
        description_text = text_html(description_html)
        normalized = normalize_visible_text(description_text)
        compact = compact_visible_text(description_text)
        sections = description_sections(description_html)
        responsibilities = _role_sections(sections, RESPONSIBILITY_TOKENS)
        qualifications = _role_sections(sections, QUALIFICATION_TOKENS)
        if detail_error:
            description_status = "detail_unavailable"
        elif not description_html.strip():
            description_status = "publisher_empty"
        elif len(normalized) < 100:
            description_status = "source_short"
        else:
            description_status = "full"

        created_on = (detail_row or {}).get("created_on")
        posted_on = (detail_row or {}).get("posted_on")
        listing_created = listing_row.get("job_created_timestamp")
        card_posted = card.get("postedDate")
        date_posted = _iso_to_ist_date(created_on) or _listing_timestamp_to_ist_date(listing_created)
        if card_posted and date_posted and _iso_to_ist_date(card_posted) != date_posted:
            self.result.errors.append(f"Upstox posted date differs between card and detail for {identity}")
        job_posting = (detail_row or {}).get("job_posting") if isinstance((detail_row or {}).get("job_posting"), dict) else {}
        careers = job_posting.get("careers") if isinstance(job_posting.get("careers"), dict) else {}
        created_ist = _listing_timestamp_to_ist_date(listing_created)
        if created_ist and date_posted and created_ist != date_posted:
            self.result.errors.append(f"Upstox listing created timestamp and detail created_on differ for {identity}")

        source_ui = {
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_api_url": LISTING_API,
            "detail_api_url": job_detail_api_url(identity),
            "detail_page_url": detail_page_url(identity),
            "apply_url": detail_page_url(identity),
            "application_control": "Apply Now",
            "application_route_observed": APPLY_ROUTE,
            "application_requires_sign_in": True,
            "application_turnstile_present": True,
            "application_submitted": False,
            "employer": listing_row.get("group_company") or EXPECTED_COMPANY_NAME,
            "company_id": COMPANY_ID,
            "tenant_id": TENANT_ID,
            "listing_membership_field": "post_on_careers_page == 1",
            "company_card_is_generated_teaser": True,
            "listing_locations": listing_row.get("location"),
            "company_card": card,
            "date_fields": {
                "job_created_timestamp": listing_created,
                "created_on": created_on,
                "posted_on": posted_on,
                "job_posting_on": (detail_row or {}).get("job_posting_on"),
                "job_posting": job_posting,
                "date_posted_mapping": (
                    "listing card postedDate / Darwinbox created_on (Asia/Kolkata date) -> date_posted"
                ),
                "date_updated_mapping": "posted_on (date-only Unix seconds, Asia/Kolkata) -> date_updated",
                "detail_ui_label": "Job Snapshot > Updated Date",
                "note": "The detail page shows the updated date, not the original posted date; the card shows 'Posted <created date>'.",
            },
            "salary_source_fields": {
                key: (detail_row or {}).get(key) for key in
                ("salary_currency", "salary_min", "salary_max", "salary_range", "salary_timeframe")
            },
            "description_status": description_status,
            "description_normalized_chars": len(normalized),
            "description_normalized_sha256": sha256(normalized.encode()).hexdigest() if normalized else None,
            "description_compact_chars": len(compact),
            "description_compact_fnv1a64": fnv1a64(compact),
            "description_fnv1a64": fnv1a64(normalized),
            "description_sha256": sha256(description_html.encode()).hexdigest() if description_html else None,
            "description_sections": sections,
            "role_attachments": _attachment_links(description_html),
            "description_links": _description_links(description_html),
            "detail_request": detail_meta,
            "detail_error": detail_error,
        }
        raw = {
            "listing": listing_row,
            "company_card": card,
            "detail": detail_row,
            "source_ui": source_ui,
        }
        job = self.job(
            raw,
            title=normalized_title((detail_row or {}).get("designation_display_name") or listing_row.get("job_title")),
            url=detail_page_url(identity),
            job_id=identity,
            requisition_id=(detail_row or {}).get("internal_job_code") or listing_row.get("job_code"),
            apply_url=detail_page_url(identity),
            description=description_html,
            method="upstox.official_careers.darwinbox_candidateapi.job.jd",
            description_url=job_detail_api_url(identity) if detail_row is not None else LISTING_API,
            locations=_locations(detail_row or {}, listing_row),
            department=(detail_row or {}).get("department") or listing_row.get("department"),
            employment_type=(detail_row or {}).get("emp_type") or listing_row.get("employee_type"),
            workplace_type=card.get("locationType"),
            experience=_experience(detail_row or {}),
            salary=_salary(detail_row or {}),
            date_posted=date_posted,
            date_posted_raw={"created_on": created_on, "job_created_timestamp": listing_created,
                             "card_postedDate": card_posted},
            date_updated=_epoch_to_ist_date(posted_on),
            valid_through=_epoch_to_ist_date(careers.get("to")),
            employer=listing_row.get("group_company") or EXPECTED_COMPANY_NAME,
            language="en",
            responsibilities=responsibilities or None,
            qualifications=qualifications or None,
        )
        job.description_text = description_text
        job.record_identity = f"upstox:{identity}"
        if description_status in ("publisher_empty", "detail_unavailable"):
            job.description_unavailable = True
        if description_status == "publisher_empty":
            job.warnings.append("publisher_empty_description")
        elif description_status == "source_short":
            job.warnings.append("source_short_description")
        elif description_status == "detail_unavailable":
            job.warnings.append("detail_fetch_failed")
        return job

    async def run(self):
        started = self.result._started_clock
        if self.company.slug != "upstox" or self.company.careers_url.rstrip("/") != OFFICIAL_ENTRY.rstrip("/"):
            raise ValueError("Upstox dedicated flow must start from its official HTTPS /careers/ entry")
        if not self.page_guard():
            return self.result

        source_started = time.perf_counter()
        try:
            entry = await self._entry("initial")
        except (FetchError, ValueError) as exc:
            self.result.errors.append(f"Upstox official entry check failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.expected_total = None
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": OFFICIAL_ENTRY,
                "membership_observable_via_http": False,
            })
            self.result.timings["official_entry_phase_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6)
            return self.result

        try:
            listing = await self._listing("initial")
        except (FetchError, ValueError) as exc:
            self.result.errors.append(f"Upstox jobs API check failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.expected_total = None
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": OFFICIAL_ENTRY,
                "official_entry_observation": {k: v for k, v in entry.items() if k != "rows"},
                "listing_api_url": LISTING_API,
                "membership_observable_via_http": False,
            })
            self.result.timings["official_entry_phase_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6)
            return self.result

        config, config_error = await self._company_config()
        source_seconds = max(0.0, time.perf_counter() - source_started)

        initial_ids = entry["job_ids"]
        api_ids = listing["job_ids"]
        if initial_ids != api_ids:
            self.result.errors.append(
                "Upstox rendered cards and careers-published API rows differ (IDs or order)"
            )
        if entry["visible_total"] is not None and entry["visible_total"] != listing["careers_count"]:
            self.result.errors.append("Upstox visible job total differs from careers-published API rows")
        if config is not None:
            if config.get("company_name") != EXPECTED_COMPANY_NAME:
                self.result.errors.append("Upstox Darwinbox company config identity changed")
            if config.get("tenant_id") != TENANT_ID:
                self.result.errors.append("Upstox Darwinbox tenant id changed")
            if config.get("group_company_site") not in (OFFICIAL_ENTRY, OFFICIAL_ENTRY.rstrip("/")):
                self.result.errors.append("Upstox Darwinbox config points at a different company site")
            if config.get("all_jobs_count") != listing["careers_count"]:
                self.result.errors.append("Upstox Darwinbox allJobsCount differs from careers-published rows")

        rows_by_id = {str(row["job_id"]): row for row in listing["rows"]}
        cards_by_id = {str(row["id"]): row for row in entry["rows"]}
        detail_started = time.perf_counter()
        for identity in api_ids:
            row = rows_by_id[identity]
            card = cards_by_id.get(identity, {})
            detail_row = None
            detail_meta = None
            detail_error = None
            if not self.blocked:
                try:
                    payload, detail_meta = await self._detail(identity)
                    detail_row = parse_detail_api(payload, identity, row)
                    if not normalized_body_text(detail_row.get("jd")):
                        self.result.errors.append(f"Upstox detail {identity} has an empty jd field")
                except (FetchError, ValueError) as exc:
                    detail_error = f"{type(exc).__name__}: {exc}"
                    self.result.errors.append(f"Upstox exact detail {identity} failed: {detail_error}")
                    if any(token in detail_error for token in ("HTTP 403", "HTTP 429")):
                        self.blocked = True
            else:
                detail_error = "skipped_after_blocked_detail_request"
                self.result.warnings.append(f"detail_skipped_after_block:{identity}")
            self.add(self._make_job(row, card, detail_row, detail_meta, detail_error))
        detail_seconds = max(0.0, time.perf_counter() - detail_started)

        final_started = time.perf_counter()
        final_ok = True
        final_ids: list[str] = []
        final_visible_total = None
        final_careers_count = None
        try:
            final_entry = await self._entry("final")
            final_listing = await self._listing("final")
            final_ids = final_entry["job_ids"]
            final_visible_total = final_entry["visible_total"]
            final_careers_count = final_listing["careers_count"]
            if final_ids != initial_ids:
                self.result.errors.append("Upstox rendered job membership changed between initial and final checks")
                final_ok = False
            if final_listing["job_ids"] != api_ids:
                self.result.errors.append("Upstox careers-published API membership changed between initial and final checks")
                final_ok = False
            if final_visible_total is not None and final_visible_total != final_careers_count:
                self.result.errors.append("Upstox final visible count differs from published API rows")
                final_ok = False
        except (FetchError, ValueError) as exc:
            self.result.errors.append(f"Upstox final listing recheck failed: {type(exc).__name__}: {exc}")
            final_ok = False
        final_seconds = max(0.0, time.perf_counter() - final_started)

        total = listing["careers_count"]
        membership_complete = bool(
            final_ok and len(initial_ids) == len(api_ids) == total
            and entry["visible_total"] == total
        )
        self.result.expected_total = total
        self.result.listing_complete = (
            membership_complete and len(self.result.jobs) == total
            and all(job.description_verified for job in self.result.jobs)
            and not self.blocked
        )
        if total == 0 and not self.result.errors:
            self.result.warnings.append("official_careers_page_currently_lists_zero_jobs")
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_observation": {k: v for k, v in entry.items() if k != "rows"},
            "listing_api_url": LISTING_API,
            "listing_request_shape": (
                "GET https://service.upstox.com/content/open/v1/jobs/ with no query parameters; "
                "the page's mapper keeps rows whose post_on_careers_page === 1."
            ),
            "membership_authority": (
                "Upstox's own /careers/ page: server-rendered 'Upstox Careers' grid plus its "
                "service.upstox.com jobs payload filtered by post_on_careers_page == 1; cross-checked "
                "against the same tenant's Darwinbox allJobsCount and jobDetails links."
            ),
            "listing_filter_behavior": (
                "Department/Location/Location Type/Work Type checkboxes filter the 13 rendered cards "
                "client-side via 'Show Results'; the unfiltered default and the API request carry no "
                "filter parameters, and this flow applies none."
            ),
            "ui_pagination": (
                "No pagination control; the page reports 'Showing 13 of 13 jobs' and renders all 13 "
                "cards in one grid."
            ),
            "listing_record_counts": {
                "all_api_records": listing["total_records"],
                "careers_published": listing["careers_count"],
                "not_published_on_careers_page": listing["excluded_count"],
                "excluded_ids": listing["excluded_ids"],
            },
            "listing_row_fields": listing["field_names"],
            "company_config": config,
            "company_config_error": config_error,
            "tenant_id": TENANT_ID,
            "company_id": COMPANY_ID,
            "employer": EXPECTED_COMPANY_NAME,
            "initial_ids": initial_ids,
            "collected_ids": [str(job.job_id) for job in self.result.jobs],
            "final_ids": final_ids,
            "final_careers_count": final_careers_count,
            "final_visible_total": final_visible_total,
            "membership_changed": bool(final_ok and final_ids != initial_ids),
            "description_source": (
                "Each exact careers-published ID's Darwinbox message.job[0].jd, decoded once from its "
                "escaped HTML wrapper; the company card's generated teaser text is never used."
            ),
            "company_card_teaser_ignored": True,
            "representative_detail_ids_inspected": [api_ids[0], api_ids[-1]] if api_ids else [],
            "empty_description_ids": [str(job.job_id) for job in self.result.jobs if job.description_unavailable],
            "detail_request_headers": dict(PUBLIC_HEADERS),
            "date_semantics": {
                "date_posted": "card postedDate / Darwinbox created_on (Asia/Kolkata date)",
                "date_updated": "posted_on / job_posting_on (date-only Unix seconds, Asia/Kolkata); detail 'Updated Date'",
                "valid_through": "job_posting.careers.to when present; absent for the collected rows",
                "created_on_retained_raw": True,
                "detail_ui_label": "Updated Date",
            },
            "application_route": {
                "detail_page_apply_control": "Apply Now",
                "observed_route": APPLY_ROUTE,
                "requires_sign_in": True,
                "turnstile_present": True,
                "submitted": False,
                "note": "Clicking Apply Now on the public jobDetails page opens the candidate sign-in form.",
            },
            "blocked_detail_requests": self.blocked,
            "retry_events": self.retry_events,
            "membership_observable_via_http": True,
            "source_collection_wall_seconds": round(source_seconds, 6),
            "collection_wall_seconds": round(max(0.0, time.perf_counter() - started), 6),
        })
        self.result.timings["official_entry_phase_seconds"] = round(source_seconds, 6)
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        self.result.timings["final_recheck_phase_seconds"] = round(final_seconds, 6)
        return self.result
