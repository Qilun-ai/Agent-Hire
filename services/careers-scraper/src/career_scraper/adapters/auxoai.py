"""auxoAI's user-supplied Zoho Recruit board and the public Zoho endpoints it uses.

Journey observed 2026-11-08 (round 3):

* The ledger entry ``https://auxo.ai/careers`` (and ``https://auxo.ai/``) still
  returns ``ConnectTimeout`` from this network: DNS resolves but TCP never
  connects.  No official-entry HTML was obtained in any round.
* The user confirmed on 2026-11-08 that auxoAI's job board is
  ``https://auxoai.zohorecruit.com/jobs/Careers``.  It is treated as the
  company-published listing for this review.  Its page title is
  ``Jobs at AuxoAI Engineering Pvt. Ltd.`` and its embedded ``meta`` object
  names org ``AuxoAI`` (website ``www.auxoai.com``), ``page_name`` ``Careers``
  and ``source`` ``CareerSite``.
* The hosted listing is client-rendered: the static HTML contains no visible
  job cards, but embeds the full membership in ``input#jobs`` (55 rows at
  observation) plus ``input#meta``.  The page's own public endpoint
  ``GET https://auxoai.zohorecruit.com/recruit/v2/public/Job_Openings?pagename=Careers&source=CareerSite``
  returns the same 55 rows with ``$url`` detail links.  There is no pagination:
  the endpoint rejects ``page``/``limit``/``offset`` with HTTP 400, and a
  headless-browser visit showed all 55 cards on one page behind the
  Job Type / Industry / City / Country filters (counts sum to 55 each).
* Each card links to
  ``https://auxoai.zohorecruit.com/jobs/Careers/<id>/<title-slug>?source=CareerSite``.
  The exact public detail payload
  ``GET .../recruit/v2/public/Job_Openings/<id>?pagename=Careers&source=CareerSite``
  carries the full HTML ``Job_Description`` plus ``$appy_url``, ``Salary``,
  ``Zip_Code`` and the ISO ``Date_Opened``.  The listing payload's
  ``Job_Description`` is the same body as plain text (within a few rendering
  characters); the detail HTML is the authoritative formatted body and is
  what is stored.  The server-rendered detail page embeds the identical
  object in ``var jobs``.
* Two rows are visibly ``Position filled`` (``Is_Locked`` true, detail pages
  say ``No longer accepting applications``).  They are still public postings
  on the board, so they are collected with the closed state preserved.
"""
from __future__ import annotations

from datetime import date
import json
import re
import time
import unicodedata
from urllib.parse import parse_qs, quote, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://auxo.ai/careers"
LEDGER_ENTRY = OFFICIAL_ENTRY
HOSTED_LISTING_URL = "https://auxoai.zohorecruit.com/jobs/Careers"
HOST = "auxoai.zohorecruit.com"
LISTING_API_URL = (
    "https://auxoai.zohorecruit.com/recruit/v2/public/Job_Openings"
    "?pagename=Careers&source=CareerSite"
)
DETAIL_API_TEMPLATE = (
    "https://auxoai.zohorecruit.com/recruit/v2/public/Job_Openings/{job_id}"
    "?pagename=Careers&source=CareerSite"
)
DETAIL_PAGE_TEMPLATE = (
    "https://auxoai.zohorecruit.com/jobs/Careers/{job_id}/{slug}?source=CareerSite"
)
EMPLOYER_NAME = "AuxoAI Engineering Pvt. Ltd."
EMPLOYER_ORG_NAME = "AuxoAI"
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
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "div", "dl", "dt", "dd", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
RESPONSIBILITY_TOKENS = (
    "responsibilit", "what you'll do", "what you’ll do", "what you will do",
    "key duties", "duties", "your role",
)
QUALIFICATION_TOKENS = (
    "requirement", "qualification", "skills", "experience", "what you'll need",
    "what you’ll need", "what we're looking for", "what we’re looking for",
    "who you are", "nice to have", "preferred", "what we expect",
)


ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\ufeff]")


def normalized_text(value) -> str:
    """NFC, collapsed whitespace and the browser's no-space-before-punctuation form."""
    text = unicodedata.normalize("NFC", str(value or ""))
    # Zero-width characters are invisible rendering artifacts in the board's
    # HTML (e.g. a \u200b inside one posting's body); browsers do not show them.
    text = ZERO_WIDTH.sub("", text)
    text = WHITESPACE.sub(" ", text).strip()
    return PUNCTUATION_SPACE.sub(r"\1", text)


def normalized_body_text(html: str | None) -> str:
    return normalized_text(text_html(html))


def rendered_body_text(html: str | None) -> str:
    """Read text like a browser: no space at inline tag boundaries, breaks at blocks."""
    soup = BeautifulSoup(f'<div data-auxoai-text="1">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-auxoai-text]")
    if root is None:
        return ""
    parts: list[str] = []

    def walk(node):
        for child in node.children:
            if isinstance(child, NavigableString):
                parts.append(str(child))
            elif isinstance(child, Tag):
                if child.name in ("script", "style", "noscript"):
                    continue
                if child.name == "br":
                    parts.append("\n")
                elif child.name in BLOCK_TAGS:
                    parts.append("\n")
                    walk(child)
                    parts.append("\n")
                else:
                    walk(child)

    walk(root)
    value = "".join(parts).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def canonical_date(value) -> str | None:
    """Accept the board's ISO detail date and the listing API's US date."""
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
    return re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]+", "-", title or "")).strip("-")


def detail_page_url(job_id: str, title: str) -> str:
    return DETAIL_PAGE_TEMPLATE.format(job_id=quote(str(job_id), safe=""), slug=title_slug(title))


def valid_job_url(value, expected_id: str | None = None) -> bool:
    if not isinstance(value, str):
        return False
    parts = urlsplit(value)
    match = JOB_PATH.fullmatch(parts.path)
    return bool(
        parts.scheme == "https"
        and parts.netloc == HOST
        and match is not None
        and (expected_id is None or match.group(1) == str(expected_id))
        and parse_qs(parts.query) == {"source": ["CareerSite"]}
    )


def preferred_job_url(row: dict, card: dict | None = None) -> str:
    """Prefer the exact URL emitted by the board's own listing API row."""
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


def location_record(row: dict) -> dict:
    city = normalized_text(row.get("City")) or None
    state = normalized_text(row.get("State")) or None
    country = normalized_text(row.get("Country")) or None
    zip_code = normalized_text(row.get("Zip_Code")) or None
    formatted = ", ".join(part for part in (city, state, country) if part)
    return {"city": city, "state": state, "country": country,
            "zip_code": zip_code, "formatted": formatted or None}


def workplace_type(row: dict) -> str | None:
    if row.get("Remote_Job") is True:
        return "Remote"
    if normalized_text(row.get("City")) == "Remote":
        return "Remote"
    return None


def _parse_hidden_json(soup: BeautifulSoup, input_id: str):
    node = soup.select_one(f"input#{input_id}")
    if node is None or not node.get("value"):
        raise ValueError(f"auxoAI hosted listing is missing input#{input_id}")
    try:
        return json.loads(node["value"])
    except ValueError as exc:
        raise ValueError(f"auxoAI hosted listing input#{input_id} is not valid JSON") from exc


def parse_hosted_listing(html: str, response_url: str = HOSTED_LISTING_URL) -> dict:
    """Parse the client-rendered board page's embedded membership (input#jobs/meta)."""
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != HOST
        or parsed.path.rstrip("/") != "/jobs/Careers"
    ):
        raise ValueError("auxoAI hosted listing response came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    meta = _parse_hidden_json(soup, "meta")
    hidden_rows = _parse_hidden_json(soup, "jobs")
    if not isinstance(meta, dict) or not isinstance(hidden_rows, list):
        raise ValueError("auxoAI hosted listing embedded objects have unexpected types")
    org = meta.get("org_info") if isinstance(meta.get("org_info"), dict) else {}
    issues = []
    if org.get("company_name") != EMPLOYER_ORG_NAME:
        issues.append("hosted_listing_employer_changed")
    if meta.get("page_name") != "Careers" or meta.get("source") != "CareerSite":
        issues.append("hosted_listing_page_or_source_changed")
    expected_list_url = meta.get("list_url")
    if expected_list_url and expected_list_url.rstrip("/") != HOSTED_LISTING_URL.rstrip("/"):
        issues.append("hosted_listing_list_url_changed")

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
        if canonical_date(row.get("Date_Opened")) is None:
            issues.append(f"hidden_listing_date_unparseable:{identity}")

    # The static response is client-rendered: no server-side cards exist in
    # this HTML.  Visible membership is reconciled separately against the
    # listing API (and by the independent browser reference); the hidden
    # payload is the page's own embedded membership.
    cards = [node for node in soup.select("div.cw-filter-joblist")]
    if cards:
        issues.append("hosted_listing_static_cards_present_unexpected")
    pagination = [
        normalized_text(node.get_text(" ", strip=True))
        for node in soup.select("button, a")
        if re.search(r"\b(next|previous|load more)\b", node.get_text(" ", strip=True), re.I)
    ]
    filter_text = ""
    filter_node = soup.select_one(".cw-job-listing")
    if filter_node is not None:
        filter_text = normalized_text(filter_node.get_text(" ", strip=True))
    return {
        "rows": [],
        "hidden_rows": hidden_rows,
        "hidden_by_id": hidden_by_id,
        "count": len(hidden_rows),
        "visible_card_count": len(cards),
        "cards_rendered_in_static_html": bool(cards),
        "hidden_count": len(hidden_rows),
        "ids": list(hidden_by_id),
        "hidden_ids": list(hidden_by_id),
        "page_title": normalized_text(soup.title.get_text(" ", strip=True) if soup.title else ""),
        "meta": meta,
        "employer": org.get("company_name"),
        "employer_legal_title": normalized_text(soup.title.get_text(" ", strip=True) if soup.title else ""),
        "filter_text": filter_text,
        "pagination_controls": pagination,
        "issues": issues,
    }


def parse_listing_api(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("code") != "success":
        raise ValueError(f"auxoAI listing API changed: {value!r}"[:500])
    data = value.get("data")
    info = value.get("info")
    if not isinstance(data, list) or not isinstance(info, dict):
        raise ValueError("auxoAI listing API response is missing data or info")
    if info.get("page_name") != "Careers":
        raise ValueError("auxoAI listing API page_name changed")
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
        raise ValueError(f"auxoAI detail API changed for {expected_id}: {value!r}"[:500])
    data = value.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError(f"auxoAI detail API did not return exactly one row for {expected_id}")
    row = data[0]
    if str(row.get("id", "")) != str(expected_id):
        raise ValueError(f"auxoAI detail API returned the wrong job id for {expected_id}")
    if not normalized_text(row.get("Posting_Title") or row.get("Job_Opening_Name")):
        raise ValueError(f"auxoAI detail API row {expected_id} has no title")
    description = row.get("Job_Description")
    if not isinstance(description, str) or len(normalized_body_text(description)) < 100:
        raise ValueError(f"auxoAI detail API row {expected_id} has an empty description")
    if canonical_date(row.get("Date_Opened")) is None:
        raise ValueError(f"auxoAI detail API row {expected_id} has an unparseable Date_Opened")
    apply_url = row.get("$appy_url")
    if isinstance(apply_url, str):
        parts = urlsplit(apply_url)
        match = JOB_PATH.fullmatch(parts.path)
        query = parse_qs(parts.query)
        if not (
            parts.scheme == "https" and parts.netloc == HOST and match is not None
            and match.group(1) == str(expected_id)
            and query.get("$apply") == ["true"] and query.get("source") == ["CareerSite"]
        ):
            raise ValueError(f"auxoAI detail API row {expected_id} has no exact hosted apply URL")
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
        raise ValueError(f"auxoAI detail page {expected_id} has no embedded jobs payload")
    try:
        rows = json.loads(_unescape_job_string(match.group(1)))
    except ValueError as exc:
        raise ValueError(f"auxoAI detail page {expected_id} embedded jobs payload is invalid JSON") from exc
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError(f"auxoAI detail page {expected_id} embedded payload is not one job")
    row = rows[0]
    if str(row.get("id", "")) != str(expected_id):
        raise ValueError(f"auxoAI detail page {expected_id} embedded the wrong job")
    return row


def description_sections(html: str) -> list[dict]:
    """Split the employer's rich text into its visible headings and body text.

    Headings in the board's HTML are ``<p>`` blocks whose entire text is one
    ``<b>``/``<strong>`` run (optionally with a ``<br>``), plus real heading
    tags.  Everything else stays attached to the preceding heading.
    """
    soup = BeautifulSoup(f'<div data-auxoai-body="1">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-auxoai-body]")
    if root is None:
        return []
    for span in root.select('span[id="spandesc"], span[id="spanreq"]'):
        span.unwrap()
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    sections: list[dict] = []
    current: dict | None = None
    for block in root.contents:
        if isinstance(block, NavigableString):
            if not str(block).strip():
                continue
            if current is None:
                current = {"heading": None, "html": "", "text": ""}
            current["html"] += str(block)
            continue
        if not isinstance(block, Tag):
            continue
        heading = ""
        if block.name in heading_tags:
            heading = normalized_text(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find(["strong", "b"])
            full_text = normalized_text(block.get_text(" ", strip=True))
            strong_text = normalized_text(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text
        elif block.name == "div":
            first = block.find(heading_tags)
            if first is not None and normalized_text(first.get_text(" ", strip=True)) == normalized_text(
                    block.get_text(" ", strip=True)) and len(normalized_text(first.get_text())) <= 120:
                heading = normalized_text(first.get_text(" ", strip=True))
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        sections.append(current)
    for section in sections:
        if section["html"]:
            section["text"] = rendered_body_text(section["html"])
        section["text"] = section["text"].strip()
    return sections


NUMBERED_SUBHEADING = re.compile(r"^\d+[.)]\s")


def select_sections(sections: list[dict], tokens: tuple[str, ...]) -> list[dict] | None:
    """Return matched sections plus their numbered subsections.

    The board's bodies put a bare ``Responsibilities`` heading immediately
    before numbered ``1. ...`` subheadings, so the parent heading itself has
    no text; the numbered items are the section's actual content.
    """
    selected: list[dict] = []
    in_block = False
    for section in sections:
        heading = section.get("heading") or ""
        matched = bool(heading) and any(token in heading.lower() for token in tokens)
        numbered = bool(heading) and bool(NUMBERED_SUBHEADING.match(heading))
        if matched:
            in_block = True
        elif in_block and not numbered and heading:
            in_block = False
        if (matched or (in_block and numbered)) and section.get("text"):
            selected.append({"heading": section["heading"], "text": section["text"]})
    return selected or None


def section_text(sections: list[dict], pattern: re.Pattern) -> str | None:
    for section in sections:
        if section.get("heading") and pattern.search(section["heading"]):
            return section.get("text") or None
    return None


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


class AuxoAI(Adapter):
    """Collect every posting on auxoAI's user-supplied Zoho Recruit board."""

    VERIFICATION_REVISION = "auxoai.zoho-recruit-careers.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.official_entry_probe = None
        self.listing = None
        self.listing_api = None
        self.started = None

    async def _probe_ledger_entry(self) -> None:
        """Record the ledger entry's reachability; never gates board membership."""
        started = time.perf_counter()
        try:
            response = await self.http.get(LEDGER_ENTRY)
            self.official_entry_probe = {
                "url": LEDGER_ENTRY,
                "status": response.status_code,
                "final_url": str(response.url),
                "seconds": max(0.0, time.perf_counter() - started),
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - recorded exactly, board is separate
            self.official_entry_probe = {
                "url": LEDGER_ENTRY,
                "status": None,
                "final_url": None,
                "seconds": max(0.0, time.perf_counter() - started),
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def run(self):
        self.started = time.perf_counter()
        collection_started = time.perf_counter()
        if self.company.careers_url not in (OFFICIAL_ENTRY, HOSTED_LISTING_URL):
            self.result.errors.append(
                "ValueError: auxoAI flow is gated to its ledger official entry "
                f"{OFFICIAL_ENTRY} or its user-supplied Zoho board {HOSTED_LISTING_URL}; "
                f"received {self.company.careers_url}"
            )
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - self.started)
            self.result.finish()
            return self.result

        await self._probe_ledger_entry()
        if self.official_entry_probe and self.official_entry_probe.get("error"):
            self.result.warnings.append(
                f"ledger_official_entry_unreachable:{self.official_entry_probe['error']}"
            )

        listing_response = await self.http.get(HOSTED_LISTING_URL)
        if listing_response.status_code != 200:
            raise ValueError(f"auxoAI hosted listing returned HTTP {listing_response.status_code}")
        self.listing = parse_hosted_listing(listing_response.text, str(listing_response.url))

        api_response = await self.http.get(LISTING_API_URL)
        self.listing_api = parse_listing_api(api_response.json())

        collection_seconds = max(0.0, time.perf_counter() - collection_started)
        self.result.timings["source_collection_seconds"] = collection_seconds
        self.result.timings["source_collection_wall_seconds"] = collection_seconds
        self.result.pages = 1
        self.result.expected_total = self.listing_api["count"]

        issues = list(self.listing["issues"]) + list(self.listing_api["issues"])
        if self.listing["count"] != self.listing_api["count"]:
            issues.append("hidden_listing_and_api_count_mismatch")
        if set(self.listing["ids"]) != set(self.listing_api["ids"]):
            issues.append("hidden_listing_and_api_id_membership_mismatch")
        if self.listing["ids"] != self.listing_api["ids"]:
            issues.append("hidden_listing_and_api_order_mismatch")
        if issues:
            self.result.errors.extend(issues)
            self.result.listing_complete = False
        else:
            self.result.listing_complete = True

        hidden_by_id = self.listing.get("hidden_by_id") or {}
        for row in self.listing_api["rows"]:
            identity = str(row["id"])
            hidden_row = hidden_by_id.get(identity)
            title = normalized_text(row.get("Posting_Title") or row.get("Job_Opening_Name"))
            card = card_from_hidden_row(hidden_row, (self.listing_api.get("keys") or {}).get("_closed_jobs")) if hidden_row else None
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
                "hidden_listing_row": hidden_row,
                "listing_card": card,
                "detail_api_url": detail_url,
                "listing_api_url": LISTING_API_URL,
                "position_filled": bool(row.get("Is_Locked")),
                "position_label": (card or {}).get("position_label"),
                "publish": row.get("Publish"),
                "keep_on_career_site": row.get("Keep_on_Career_Site"),
                "industry": row.get("Industry"),
                "currency": row.get("Currency"),
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
            "official_entry_url": OFFICIAL_ENTRY,
            "ledger_entry_probe": self.official_entry_probe,
            "listing_url": HOSTED_LISTING_URL,
            "listing_api_url": LISTING_API_URL,
            "listing_page_title": self.listing["page_title"],
            "listing_employer": self.listing["employer"],
            "employer_legal_name": EMPLOYER_NAME,
            "visible_listing_rows": self.listing["count"],
            "hidden_listing_rows": self.listing["hidden_count"],
            "api_listing_rows": self.listing_api["count"],
            "listing_ids": self.listing_api["ids"],
            "listing_order": self.listing_api["ids"],
            "pagination": "No next/previous/load-more controls in the rendered board; one hosted listing page, one hidden payload and one API response; the endpoint rejects page/limit/offset with HTTP 400.",
            "filter_text": self.listing["filter_text"],
            "api_facets": self.listing_api["facets"],
            "api_keys": self.listing_api["keys"],
            "membership_reconciliation": {
                "hidden_embedded_rows": self.listing["hidden_count"],
                "api_rows": self.listing_api["count"],
                "complete": self.result.listing_complete,
                "issues": issues,
            },
            "position_filled_rows": [
                str(row["id"]) for row in self.listing_api["rows"] if row.get("Is_Locked") is True
            ],
            "scope_note": "Board URL supplied by the user on 2026-11-08; auxo.ai itself is unreachable from this network (ledger entry probe recorded above).",
        })
        self.result.finish()
        return self.result

    async def detail(self, job):
        started = time.perf_counter()
        listing_row = job.raw["listing_row"]
        detail_url = job.raw["detail_api_url"]
        response = await self.http.get(detail_url)
        if response.status_code != 200:
            raise ValueError(f"auxoAI detail API returned HTTP {response.status_code}")
        detail_row = parse_detail_api(response.json(), job.job_id)
        api_seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["detail_api_seconds_sum"] = self.result.timings.get("detail_api_seconds_sum", 0.0) + api_seconds

        list_text = normalized_body_text(listing_row.get("Job_Description"))
        detail_text = normalized_body_text(detail_row.get("Job_Description"))
        job.raw["listing_vs_detail_description_chars"] = {
            "listing_normalized_chars": len(list_text),
            "detail_normalized_chars": len(detail_text),
            "char_difference": len(detail_text) - len(list_text),
        }
        # The listing payload carries the same body as plain text; the
        # detail HTML is authoritative.  Only flag a real divergence.
        if list_text and list_text[:120] not in detail_text:
            self.result.errors.append(f"listing_vs_detail_description_mismatch:{job.job_id}")
            job.warnings.append("detail_listing_mismatch")

        list_date = canonical_date(listing_row.get("Date_Opened"))
        detail_date = canonical_date(detail_row.get("Date_Opened"))
        if list_date != detail_date:
            self.result.errors.append(f"listing_vs_detail_date_mismatch:{job.job_id}")

        hidden_row = job.raw.get("hidden_listing_row") or {}
        if hidden_row:
            if normalized_text(hidden_row.get("Posting_Title")) != normalized_text(detail_row.get("Posting_Title")):
                self.result.errors.append(f"hidden_listing_vs_detail_title_mismatch:{job.job_id}")
            if canonical_date(hidden_row.get("Date_Opened")) != detail_date:
                self.result.errors.append(f"hidden_listing_vs_detail_date_mismatch:{job.job_id}")

        sections = description_sections(detail_row["Job_Description"])
        job.title = normalized_text(detail_row.get("Posting_Title") or detail_row.get("Job_Opening_Name")) or job.title
        job.apply_url = detail_row.get("$appy_url")
        job.description_html = detail_row["Job_Description"]
        job.description_text = detail_text
        job.description_method = "auxoai.zoho_recruit_public_job_detail"
        job.description_source_url = detail_url
        job.date_posted = detail_date
        job.date_posted_raw = {
            "listing": listing_row.get("Date_Opened"),
            "hidden_listing": hidden_row.get("Date_Opened"),
            "detail": detail_row.get("Date_Opened"),
        }
        job.employment_type = detail_row.get("Job_Type")
        job.employer = EMPLOYER_NAME
        job.locations = [location_record(detail_row)] if location_text(detail_row) else []
        job.workplace_type = workplace_type(detail_row)
        job.salary = detail_row.get("Salary")
        job.experience = detail_row.get("Work_Experience")
        job.department = None
        job.responsibilities = select_sections(sections, RESPONSIBILITY_TOKENS)
        job.qualifications = select_sections(sections, QUALIFICATION_TOKENS)
        job.raw["detail_row"] = detail_row
        job.raw["detail_api_url"] = detail_url
        job.raw["description_sections"] = sections
        job.raw["zip_code"] = detail_row.get("Zip_Code")
        job.raw["industry"] = detail_row.get("Industry")
        job.raw["currency"] = detail_row.get("Currency")
        job.raw["remote_job"] = detail_row.get("Remote_Job")
        job.raw["publish"] = detail_row.get("Publish")
        job.raw["keep_on_career_site"] = detail_row.get("Keep_on_Career_Site")
        job.raw["is_locked"] = detail_row.get("Is_Locked")
        job.raw["position_filled"] = bool(job.raw.get("position_filled") or detail_row.get("Is_Locked"))
        job.raw["read_only_job"] = detail_row.get("$read_only_job")

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
