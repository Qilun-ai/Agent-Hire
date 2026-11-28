"""Yellow.ai's own career page and the company-scoped Zoho Recruit listing it embeds.

Journey observed 2026-11-08:

* The ledger URL ``https://yellow.ai/careers/`` currently returns HTTP 404.  The
  live official page linked from the site sitemap is
  ``https://yellow.ai/career/`` (``/careers`` resolves there, the trailing-slash
  ledger form does not).
* That page publishes one Zoho Recruit embed:
  ``rec_embed_js.load({widget_id:"rec_job_listing_div", page_name:"Careers",
  source:"CareerSite", site:"https://careers.yellow.ai", ...})``.
* The hosted listing is ``https://careers.yellow.ai/jobs/Careers``.  Its own
  page bundle calls
  ``GET https://careers.yellow.ai/recruit/v2/public/Job_Openings?pagename=Careers&source=CareerSite``
  and renders the one returned row.  There is no pagination, no load-more
  control and no server-side page parameter; the endpoint rejects extra params.
* Each card links to
  ``https://careers.yellow.ai/jobs/Careers/<id>/<title-slug>?source=CareerSite``.
  The exact public detail payload is
  ``GET .../recruit/v2/public/Job_Openings/<id>?pagename=Careers&source=CareerSite``
  and carries the full HTML ``Job_Description`` plus ``$appy_url``.  The
  server-rendered detail page embeds the same object in ``var jobs`` and renders
  it into ``#cw-rich-description``.

At observation time the only published row (``157454000000803145``, "GTM
recruiter") is visibly marked ``Position filled`` and its detail page says
``No longer accepting applications``.  It is still a public posting on the
company's official careers journey, so it is collected with the closed state
preserved instead of being dropped or turned into a zero-job listing.
"""
from __future__ import annotations

from datetime import date
import json
import re
import time
import unicodedata
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://yellow.ai/career/"
LEDGER_ENTRY = "https://yellow.ai/careers/"
HOSTED_LISTING_URL = "https://careers.yellow.ai/jobs/Careers"
LISTING_API_URL = (
    "https://careers.yellow.ai/recruit/v2/public/Job_Openings"
    "?pagename=Careers&source=CareerSite"
)
DETAIL_API_TEMPLATE = (
    "https://careers.yellow.ai/recruit/v2/public/Job_Openings/{job_id}"
    "?pagename=Careers&source=CareerSite"
)
DETAIL_PAGE_TEMPLATE = (
    "https://careers.yellow.ai/jobs/Careers/{job_id}/{slug}?source=CareerSite"
)
EMPLOYER_NAME = "Yellow.ai"
JOB_PATH = re.compile(r"^/jobs/Careers/(\d+)/([^/]+)$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
US_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
WHITESPACE = re.compile(r"\s+")
PUNCTUATION_SPACE = re.compile(r"\s+([,;:!?.])")
HEADING_RESPONSIBILITIES = re.compile(
    r"what you.?ll do|what you will do|responsibilit|your role|key duties", re.I
)
HEADING_QUALIFICATIONS = re.compile(
    r"what we expect|requirements|qualifications|who you are|what you.?ll need|"
    r"what we.?re looking for|skills|experience", re.I
)
JOB_EMBED_SCRIPT = re.compile(r"var jobs = JSON\.parse\('((?:\\.|[^'\\])*)'\);", re.S)


def normalized_text(value) -> str:
    """NFC, collapsed whitespace and the browser's no-space-before-punctuation form."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = WHITESPACE.sub(" ", text).strip()
    return PUNCTUATION_SPACE.sub(r"\1", text)


def normalized_body_text(html: str | None) -> str:
    return normalized_text(text_html(html))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def canonical_date(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if ISO_DATE.fullmatch(text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    match = US_DATE.fullmatch(text)
    if match:
        month, day, year = (int(part) for part in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def title_slug(title: str) -> str:
    """The visible card's path form: preserve title case, replace separators."""
    return re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]+", "-", title or "")).strip("-")


def detail_page_url(job_id: str, title: str) -> str:
    return DETAIL_PAGE_TEMPLATE.format(job_id=quote(str(job_id), safe=""), slug=title_slug(title))


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "yellow.ai"
        or parsed.path.rstrip("/") != "/career"
    ):
        raise ValueError("Yellow.ai official careers entry is not the inspected /career/ page")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "career" not in title.lower():
        raise ValueError("Yellow.ai official careers page title no longer identifies careers")
    scripts = "\n".join(script.get_text() for script in soup.find_all("script"))
    if "rec_embed_js.load" not in scripts:
        raise ValueError("Yellow.ai official careers page no longer loads the Zoho Recruit embed")
    values = {}
    for key in ("widget_id", "page_name", "source", "site", "empty_job_msg"):
        match = re.search(rf'{key}\s*:\s*"([^"]*)"', scripts)
        if match:
            values[key] = match.group(1)
    if values.get("widget_id") != "rec_job_listing_div":
        raise ValueError("Yellow.ai Zoho Recruit widget id changed")
    if values.get("site") != "https://careers.yellow.ai":
        raise ValueError("Yellow.ai Zoho Recruit site changed")
    if values.get("source") != "CareerSite" or values.get("page_name") != "Careers":
        raise ValueError("Yellow.ai Zoho Recruit source/page changed")
    if soup.select_one("#rec_job_listing_div") is None:
        raise ValueError("Yellow.ai Zoho Recruit target div is missing")
    return {
        "url": response_url,
        "title": title,
        "widget_id": values["widget_id"],
        "page_name": values["page_name"],
        "source": values["source"],
        "site": values["site"],
        "empty_job_msg": values.get("empty_job_msg"),
        "embed_script": "https://static.zohocdn.com/recruit/embed_careers_site/javascript/v1.1/embed_jobs.js",
        "hosted_listing_url": HOSTED_LISTING_URL,
    }


def _parse_hidden_json(soup: BeautifulSoup, input_id: str):
    node = soup.select_one(f"input#{input_id}")
    if node is None or not node.get("value"):
        raise ValueError(f"Yellow.ai hosted listing is missing input#{input_id}")
    try:
        return json.loads(node["value"])
    except ValueError as exc:
        raise ValueError(f"Yellow.ai hosted listing input#{input_id} is not valid JSON") from exc


def _card_from_node(card: Tag, response_url: str) -> dict:
    anchor = card.select_one("h3 a[href]")
    if anchor is None:
        raise ValueError("Yellow.ai job card has no title link")
    target = urljoin(response_url, anchor.get("href", ""))
    parts = urlsplit(target)
    match = JOB_PATH.fullmatch(parts.path)
    if (
        parts.scheme != "https"
        or parts.netloc != "careers.yellow.ai"
        or match is None
        or parse_qs(parts.query) != {"source": ["CareerSite"]}
    ):
        raise ValueError(f"Yellow.ai job card link is not the exact hosted listing route: {target}")
    identity, slug = match.groups()
    paragraphs = card.select("p.cw-bw")
    if not paragraphs:
        raise ValueError(f"Yellow.ai job card {identity} has no location/preview paragraph")
    location = normalized_text(paragraphs[0].get_text(" ", strip=True))
    preview = normalized_text(paragraphs[-1].get_text(" ", strip=True))
    position = card.select_one("span.pos-fill")
    employment = card.select_one(".cw-full-time")
    opened = card.select_one(".search-date-opened")
    return {
        "id": identity,
        "slug": slug,
        "url": target,
        "title": normalized_text(anchor.get_text(" ", strip=True)),
        "location": location,
        "preview": preview,
        "employment_label": normalized_text(employment.get_text(" ", strip=True)) if employment else None,
        "date_label": normalized_text(opened.get_text(" ", strip=True)) if opened else None,
        "position_filled": position is not None,
        "position_label": normalized_text(position.get_text(" ", strip=True)) if position else None,
        "card_html": str(card),
    }


def parse_hosted_listing(html: str, response_url: str = HOSTED_LISTING_URL) -> dict:
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "careers.yellow.ai"
        or parsed.path.rstrip("/") != "/jobs/Careers"
    ):
        raise ValueError("Yellow.ai hosted listing response came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    meta = _parse_hidden_json(soup, "meta")
    hidden_rows = _parse_hidden_json(soup, "jobs")
    if not isinstance(meta, dict) or not isinstance(hidden_rows, list):
        raise ValueError("Yellow.ai hosted listing embedded objects have unexpected types")
    org = meta.get("org_info") if isinstance(meta.get("org_info"), dict) else {}
    issues = []
    if org.get("company_name") != EMPLOYER_NAME:
        issues.append("hosted_listing_employer_changed")
    if meta.get("page_name") != "Careers" or meta.get("source") != "CareerSite":
        issues.append("hosted_listing_page_or_source_changed")
    expected_list_url = meta.get("list_url")
    if expected_list_url and expected_list_url.rstrip("/") != HOSTED_LISTING_URL.rstrip("/"):
        issues.append("hosted_listing_list_url_changed")

    cards, seen = [], set()
    for card_node in soup.select("div.cw-filter-joblist"):
        try:
            card = _card_from_node(card_node, response_url)
        except ValueError as exc:
            issues.append(str(exc))
            continue
        if card["id"] in seen:
            issues.append(f"duplicate_visible_card:{card['id']}")
            continue
        seen.add(card["id"])
        cards.append(card)

    hidden_by_id = {}
    for row in hidden_rows:
        if not isinstance(row, dict):
            issues.append("hosted_listing_hidden_row_not_object")
            continue
        identity = str(row.get("id", ""))
        if not re.fullmatch(r"\d+", identity):
            issues.append("hosted_listing_hidden_row_missing_numeric_id")
            continue
        if identity in hidden_by_id:
            issues.append(f"duplicate_hidden_row:{identity}")
            continue
        hidden_by_id[identity] = row

    visible_ids = [card["id"] for card in cards]
    hidden_ids = list(hidden_by_id)
    cards_rendered = bool(cards)
    if cards_rendered:
        if set(visible_ids) != set(hidden_ids):
            issues.append("visible_and_hidden_listing_ids_mismatch")
        if len(cards) != len(hidden_rows):
            issues.append("visible_and_hidden_listing_counts_mismatch")
        for card in cards:
            row = hidden_by_id.get(card["id"])
            if row is None:
                continue
            if normalized_text(row.get("Posting_Title") or row.get("Job_Opening_Name")) != card["title"]:
                issues.append(f"visible_and_hidden_title_mismatch:{card['id']}")
            if canonical_date(row.get("Date_Opened")) is None:
                issues.append(f"hidden_listing_date_unparseable:{card['id']}")
            if card["date_label"] and canonical_date(card["date_label"]) is None:
                issues.append(f"visible_listing_date_unparseable:{card['id']}")
    else:
        # The static server response contains only the hidden row payload and
        # a JS template; the cards are rendered in the browser.  The independent
        # browser reference supplies the visible card count for this run.
        for row in hidden_rows:
            if canonical_date(row.get("Date_Opened")) is None:
                issues.append(f"hidden_listing_date_unparseable:{row.get('id')}")

    filter_text = ""
    filter_node = soup.select_one(".cw-job-listing .filter-by, .cw-job-listing #filter, .cw-job-listing .cw-filter")
    if filter_node is not None:
        filter_text = normalized_text(filter_node.get_text(" ", strip=True))
    pagination = [
        normalized_text(node.get_text(" ", strip=True))
        for node in soup.select("button, a")
        if re.search(r"\b(next|previous|load more|page)\b", node.get_text(" ", strip=True), re.I)
    ]
    return {
        "rows": cards,
        "hidden_rows": hidden_rows,
        "count": len(cards) if cards_rendered else len(hidden_rows),
        "visible_card_count": len(cards),
        "cards_rendered_in_static_html": cards_rendered,
        "hidden_count": len(hidden_rows),
        "ids": visible_ids if cards_rendered else hidden_ids,
        "hidden_ids": hidden_ids,
        "page_title": normalized_text(soup.title.get_text(" ", strip=True) if soup.title else ""),
        "meta": meta,
        "employer": org.get("company_name"),
        "filter_text": filter_text,
        "pagination_controls": pagination,
        "issues": issues,
    }


def parse_listing_api(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("code") != "success":
        raise ValueError(f"Yellow.ai listing API changed: {value!r}"[:500])
    data = value.get("data")
    info = value.get("info")
    if not isinstance(data, list) or not isinstance(info, dict):
        raise ValueError("Yellow.ai listing API response is missing data or info")
    if info.get("page_name") != "Careers":
        raise ValueError("Yellow.ai listing API page_name changed")
    rows, seen, issues = [], set(), []
    for row in data:
        if not isinstance(row, dict):
            issues.append("listing_row_not_object")
            continue
        identity = str(row.get("id", ""))
        if not re.fullmatch(r"\d+", identity):
            issues.append("listing_row_missing_numeric_id")
            continue
        if identity in seen:
            issues.append(f"duplicate_listing_id:{identity}")
            continue
        title = normalized_text(row.get("Posting_Title") or row.get("Job_Opening_Name"))
        description = row.get("Job_Description")
        if not title:
            issues.append(f"listing_row_missing_title:{identity}")
        if not isinstance(description, str) or len(normalized_text(description)) < 100:
            issues.append(f"listing_row_description_empty_or_truncated:{identity}")
        if canonical_date(row.get("Date_Opened")) is None:
            issues.append(f"listing_row_date_unparseable:{identity}")
        if row.get("Keep_on_Career_Site") is not True:
            issues.append(f"listing_row_not_kept_on_career_site:{identity}")
        if not valid_job_url(row.get("$url"), identity):
            issues.append(f"listing_row_source_url_invalid:{identity}")
        seen.add(identity)
        rows.append(dict(row))
    return {
        "rows": rows,
        "count": len(rows),
        "ids": [str(row["id"]) for row in rows],
        "page_name": info.get("page_name"),
        "fields": info.get("fields") or [],
        "facets": info.get("facets") or [],
        "keys": info.get("keys") or {},
        "issues": issues,
    }


def parse_detail_api(value: dict, expected_id: str) -> dict:
    if not isinstance(value, dict) or value.get("code") != "success":
        raise ValueError(f"Yellow.ai detail API changed for {expected_id}: {value!r}"[:500])
    data = value.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError(f"Yellow.ai detail API did not return exactly one row for {expected_id}")
    row = data[0]
    if str(row.get("id", "")) != str(expected_id):
        raise ValueError(f"Yellow.ai detail API returned the wrong job id for {expected_id}")
    if not normalized_text(row.get("Posting_Title") or row.get("Job_Opening_Name")):
        raise ValueError(f"Yellow.ai detail API row {expected_id} has no title")
    description = row.get("Job_Description")
    if not isinstance(description, str) or len(normalized_body_text(description)) < 100:
        raise ValueError(f"Yellow.ai detail API row {expected_id} has an empty description")
    if canonical_date(row.get("Date_Opened")) is None:
        raise ValueError(f"Yellow.ai detail API row {expected_id} has an unparseable Date_Opened")
    apply_url = row.get("$appy_url")
    if not isinstance(apply_url, str) or not apply_url.startswith("https://careers.yellow.ai/"):
        raise ValueError(f"Yellow.ai detail API row {expected_id} has no exact hosted apply URL")
    return row


def _unescape_job_string(value: str) -> str:
    value = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), value)
    value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), value)
    return re.sub(
        r"\\(.)",
        lambda m: {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(m.group(1), m.group(1)),
        value,
        flags=re.S,
    )


def parse_detail_page_embedded(html: str, expected_id: str) -> dict:
    """Read the server-rendered detail page's own ``var jobs`` payload."""
    match = JOB_EMBED_SCRIPT.search(html)
    if match is None:
        raise ValueError(f"Yellow.ai detail page {expected_id} has no embedded jobs payload")
    try:
        rows = json.loads(_unescape_job_string(match.group(1)))
    except ValueError as exc:
        raise ValueError(f"Yellow.ai detail page {expected_id} embedded jobs payload is invalid JSON") from exc
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError(f"Yellow.ai detail page {expected_id} embedded payload is not one job")
    row = rows[0]
    if str(row.get("id", "")) != str(expected_id):
        raise ValueError(f"Yellow.ai detail page {expected_id} embedded the wrong job")
    return row


def _bold_font_size(node: Tag, root: Tag) -> tuple[float | None, str | None]:
    parent = node
    while parent is not None and parent is not root:
        style = re.sub(r"\s+", "", parent.get("style", "")).lower()
        if "font-weight:700" in style or "font-weight:bold" in style:
            match = re.search(r"font-size:([0-9.]+)(pt|px)", style)
            return (float(match.group(1)), match.group(2)) if match else (None, None)
        parent = parent.parent
    return None, None


def _is_heading(node: Tag, root: Tag) -> bool:
    text = normalized_text(node.get_text(" ", strip=True))
    size, unit = _bold_font_size(node, root)
    if size is None:
        return False
    if unit == "pt":
        return size >= 12 and len(text) <= 120
    if unit == "px":
        return size >= 18 and len(text) <= 120
    return False


def description_sections(html: str) -> list[dict]:
    """Split the employer's rich text into its visible headings and body text."""
    soup = BeautifulSoup(f'<div data-yellow-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-yellow-body]")
    if root is None:
        return [{"heading": None, "text": normalized_body_text(html)}]
    leaves = [
        node
        for node in root.find_all("span")
        if node.find("span") is None and normalized_text(node.get_text(" ", strip=True))
    ]
    if not leaves:
        return [{"heading": None, "text": normalized_body_text(html)}]
    sections, current = [], {"heading": None, "parts": []}
    for leaf in leaves:
        if _is_heading(leaf, root):
            if current["heading"] is not None or current["parts"]:
                sections.append(current)
            current = {"heading": normalized_text(leaf.get_text(" ", strip=True)), "parts": []}
        else:
            current["parts"].append(normalized_text(leaf.get_text(" ", strip=True)))
    if current["heading"] is not None or current["parts"]:
        sections.append(current)
    result = [
        {"heading": section["heading"], "text": normalized_text(" ".join(section["parts"]))}
        for section in sections
    ]
    result = [
        section for section in result
        if section["heading"] is not None or section["text"]
    ]
    return result or [{"heading": None, "text": normalized_body_text(html)}]


def section_text(sections: list[dict], pattern: re.Pattern) -> str | None:
    for section in sections:
        if section.get("heading") and pattern.search(section["heading"]):
            return section.get("text") or None
    return None


def valid_job_url(value, expected_id: str | None = None) -> bool:
    if not isinstance(value, str):
        return False
    parts = urlsplit(value)
    match = JOB_PATH.fullmatch(parts.path)
    return bool(
        parts.scheme == "https"
        and parts.netloc in {"yellow.zohorecruit.in", "careers.yellow.ai"}
        and match is not None
        and (expected_id is None or match.group(1) == str(expected_id))
        and parse_qs(parts.query) == {"source": ["CareerSite"]}
    )


def preferred_job_url(row: dict, card: dict | None = None) -> str:
    """Prefer the exact URL emitted by the official page's own embed/API row."""
    source_url = row.get("$url")
    if valid_job_url(source_url, str(row.get("id", ""))):
        return source_url
    if card and card.get("url"):
        return card["url"]
    identity = str(row.get("id", ""))
    title = normalized_text(row.get("Posting_Title") or row.get("Job_Opening_Name"))
    return detail_page_url(identity, title)


def location_text(row: dict) -> str | None:
    parts = [normalized_text(row.get(key)) for key in ("City", "State", "Country") if normalized_text(row.get(key))]
    return ", ".join(parts) if parts else None


def card_from_hidden_row(row: dict, closed_label: str | None = None) -> dict:
    """Reconstruct the card fields the browser renders from the page's hidden row."""
    identity = str(row["id"])
    title = normalized_text(row.get("Posting_Title") or row.get("Job_Opening_Name"))
    return {
        "id": identity,
        "slug": title_slug(title),
        "url": detail_page_url(identity, title),
        "title": title,
        "location": location_text(row),
        "preview": normalized_text(row.get("Job_Description"))[:500],
        "employment_label": row.get("Job_Type"),
        "date_label": row.get("Date_Opened"),
        "position_filled": bool(row.get("Is_Locked")),
        "position_label": closed_label if row.get("Is_Locked") else None,
        "card_html": None,
        "derived_from_hidden_listing_row": True,
    }


class YellowAI(Adapter):
    VERIFICATION_REVISION = "yellow-ai.zoho-recruit-careers.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.official_entry = None
        self.listing = None
        self.listing_api = None
        self.started = None

    async def _probe_ledger_entry(self) -> None:
        try:
            response = await self.http.get(LEDGER_ENTRY)
            self.result.metadata["ledger_entry_status"] = response.status_code
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.result.metadata["ledger_entry_status"] = 404 if "404" in message else "error"
            self.result.metadata["ledger_entry_error"] = message

    async def run(self):
        self.started = time.perf_counter()
        collection_started = time.perf_counter()
        await self._probe_ledger_entry()

        official_response = await self.http.get(OFFICIAL_ENTRY)
        if official_response.status_code != 200:
            raise ValueError(f"Yellow.ai official careers entry returned HTTP {official_response.status_code}")
        self.official_entry = parse_official_entry(official_response.text, str(official_response.url))

        listing_response = await self.http.get(HOSTED_LISTING_URL)
        if listing_response.status_code != 200:
            raise ValueError(f"Yellow.ai hosted listing returned HTTP {listing_response.status_code}")
        self.listing = parse_hosted_listing(listing_response.text, str(listing_response.url))

        api_response = await self.http.get(LISTING_API_URL)
        self.listing_api = parse_listing_api(api_response.json())

        collection_seconds = max(0.0, time.perf_counter() - collection_started)
        self.result.timings["source_collection_seconds"] = collection_seconds
        self.result.timings["source_collection_wall_seconds"] = collection_seconds
        self.result.pages = 1
        self.result.expected_total = self.listing["count"]

        issues = list(self.listing["issues"]) + list(self.listing_api["issues"])
        if self.listing["count"] != self.listing_api["count"]:
            issues.append("visible_listing_and_api_count_mismatch")
        if set(self.listing["ids"]) != set(self.listing_api["ids"]):
            issues.append("visible_listing_and_api_id_membership_mismatch")
        if self.listing["hidden_count"] != self.listing_api["count"]:
            issues.append("hidden_listing_and_api_count_mismatch")
        if self.listing["hidden_ids"] and set(self.listing["hidden_ids"]) != set(self.listing_api["ids"]):
            issues.append("hidden_listing_and_api_id_membership_mismatch")
        if issues:
            self.result.errors.extend(issues)
            self.result.listing_complete = False
        else:
            self.result.listing_complete = True

        cards = {card["id"]: card for card in self.listing["rows"]}
        for row in self.listing_api["rows"]:
            identity = str(row["id"])
            card = cards.get(identity)
            title = normalized_text(row.get("Posting_Title") or row.get("Job_Opening_Name"))
            if card is None:
                card = card_from_hidden_row(row, (self.listing_api.get("keys") or {}).get("_closed_jobs"))
            # The official page's rendered card and the API's $url use the
            # company's default Zoho host; the hosted listing page card uses
            # careers.yellow.ai. Both are exact same-job links. Keep the
            # official-page URL as job.url and preserve the other in raw.
            url = preferred_job_url(row, card)
            detail_url = DETAIL_API_TEMPLATE.format(job_id=quote(identity, safe=""))
            job = self.job(
                row,
                title=title,
                url=url,
                job_id=identity,
                description="",
                method="pending",
                description_url=detail_url,
            )
            job.raw = {
                "listing_row": row,
                "listing_card": card,
                "detail_api_url": detail_url,
                "listing_api_url": LISTING_API_URL,
                "position_filled": bool(card and card.get("position_filled")) or bool(row.get("Is_Locked")),
                "position_label": (card or {}).get("position_label"),
                "date_format": "MM/dd/yyyy",
                "hosted_listing_card_url": (card or {}).get("url"),
                "official_embed_url": row.get("$url"),
            }
            self.add(job)

        representatives = set()
        if self.result.jobs:
            representatives.add(self.result.jobs[0].job_id)
            representatives.add(self.result.jobs[-1].job_id)
        for job in self.result.jobs:
            job.raw["representative_detail_page"] = job.job_id in representatives

        await self.hydrate(self.result.jobs, self.detail)
        verified = sum(job.description_verified for job in self.result.jobs)
        if len(self.result.jobs) != self.result.expected_total:
            self.result.listing_complete = False
            self.result.errors.append("collected_job_count_differs_from_listing_total")
        if verified != len(self.result.jobs):
            self.result.listing_complete = False
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry": self.official_entry,
            "listing_url": HOSTED_LISTING_URL,
            "listing_api_url": LISTING_API_URL,
            "listing_page_title": self.listing["page_title"],
            "listing_employer": self.listing["employer"],
            "visible_listing_rows": self.listing["count"],
            "hidden_listing_rows": self.listing["hidden_count"],
            "api_listing_rows": self.listing_api["count"],
            "listing_ids": self.listing_api["ids"],
            "listing_order": self.listing_api["ids"],
            "pagination": "No next/previous/load-more controls; one hosted listing page and one API response; extra page/limit params are rejected by the endpoint.",
            "filter_text": self.listing["filter_text"],
            "api_facets": self.listing_api["facets"],
            "api_keys": self.listing_api["keys"],
            "membership_reconciliation": {
                "visible_ui_rows": self.listing["count"],
                "hidden_embedded_rows": self.listing["hidden_count"],
                "api_rows": self.listing_api["count"],
                "complete": self.result.listing_complete,
                "issues": issues,
            },
            "position_filled_rows": [
                str(row["id"]) for row in self.listing_api["rows"] if row.get("Is_Locked") is True
            ],
            "ledger_entry_url": LEDGER_ENTRY,
            "ledger_entry_status": self.result.metadata.get("ledger_entry_status"),
            "official_entry_note": "The ledger's trailing-slash /careers/ URL returns 404; /careers redirects to the live /career/ page linked from the sitemap.",
        })
        self.result.finish()
        return self.result

    async def detail(self, job):
        started = time.perf_counter()
        listing_row = job.raw["listing_row"]
        detail_url = job.raw["detail_api_url"]
        response = await self.http.get(detail_url)
        if response.status_code != 200:
            raise ValueError(f"Yellow.ai detail API returned HTTP {response.status_code}")
        detail_row = parse_detail_api(response.json(), job.job_id)
        api_seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["detail_api_seconds_sum"] = self.result.timings.get("detail_api_seconds_sum", 0.0) + api_seconds

        list_text = normalized_body_text(listing_row.get("Job_Description"))
        detail_text = normalized_body_text(detail_row.get("Job_Description"))
        if list_text != detail_text:
            self.result.errors.append(f"listing_vs_detail_description_mismatch:{job.job_id}")
            job.warnings.append("detail_listing_mismatch")

        list_date = canonical_date(listing_row.get("Date_Opened"))
        detail_date = canonical_date(detail_row.get("Date_Opened"))
        if list_date != detail_date:
            self.result.errors.append(f"listing_vs_detail_date_mismatch:{job.job_id}")

        card = job.raw.get("listing_card") or {}
        if card:
            if normalized_text(card.get("title")) != normalized_text(detail_row.get("Posting_Title")):
                self.result.errors.append(f"listing_vs_detail_title_mismatch:{job.job_id}")
            if card.get("date_label") and canonical_date(card["date_label"]) != detail_date:
                self.result.errors.append(f"card_vs_detail_date_mismatch:{job.job_id}")

        sections = description_sections(detail_row["Job_Description"])
        job.title = normalized_text(detail_row.get("Posting_Title") or detail_row.get("Job_Opening_Name")) or job.title
        job.apply_url = detail_row.get("$appy_url")
        job.description_html = detail_row["Job_Description"]
        job.description_text = detail_text
        job.description_method = "yellow_ai.zoho_recruit_public_job_detail"
        job.description_source_url = detail_url
        job.date_posted = detail_date
        job.date_posted_raw = {
            "listing": listing_row.get("Date_Opened"),
            "detail": detail_row.get("Date_Opened"),
            "format": "MM/dd/yyyy",
        }
        job.employment_type = detail_row.get("Job_Type")
        job.employer = EMPLOYER_NAME
        job.locations = [location_text(detail_row)] if location_text(detail_row) else []
        job.department = None
        job.responsibilities = section_text(sections, HEADING_RESPONSIBILITIES)
        job.qualifications = section_text(sections, HEADING_QUALIFICATIONS)
        job.raw["detail_row"] = detail_row
        job.raw["detail_api_url"] = detail_url
        job.raw["description_sections"] = sections
        job.raw["work_mode"] = detail_row.get("Work_Mode")
        job.raw["remote_job"] = detail_row.get("Remote_Job")
        job.raw["zip_code"] = detail_row.get("Zip_Code")
        job.raw["industry"] = detail_row.get("Industry")
        job.raw["publish"] = detail_row.get("Publish")
        job.raw["keep_on_career_site"] = detail_row.get("Keep_on_Career_Site")
        job.raw["is_locked"] = detail_row.get("Is_Locked")
        job.raw["position_filled"] = bool(job.raw.get("position_filled") or detail_row.get("Is_Locked"))

        if job.raw.get("representative_detail_page"):
            page_started = time.perf_counter()
            page_response = await self.http.get(job.url)
            page_seconds = max(0.0, time.perf_counter() - page_started)
            self.result.timings["representative_detail_page_seconds"] = self.result.timings.get("representative_detail_page_seconds", 0.0) + page_seconds
            if page_response.status_code != 200:
                self.result.errors.append(f"representative_detail_page_http_{page_response.status_code}:{job.job_id}")
            else:
                embedded = parse_detail_page_embedded(page_response.text, job.job_id)
                embedded_text = normalized_body_text(embedded.get("Job_Description"))
                job.raw["detail_page"] = {
                    "url": job.url,
                    "status": page_response.status_code,
                    "embedded_title": normalized_text(embedded.get("Posting_Title")),
                    "embedded_date": canonical_date(embedded.get("Date_Opened")),
                    "description_normalized_chars": len(embedded_text),
                    "description_fnv1a64": fnv1a64(embedded_text),
                    "description_matches_detail_api": embedded_text == detail_text,
                    "title_matches_detail_api": normalized_text(embedded.get("Posting_Title")) == job.title,
                }
                if not job.raw["detail_page"]["description_matches_detail_api"]:
                    self.result.errors.append(f"detail_page_vs_api_description_mismatch:{job.job_id}")
                if not job.raw["detail_page"]["title_matches_detail_api"]:
                    self.result.errors.append(f"detail_page_vs_api_title_mismatch:{job.job_id}")
        return job
