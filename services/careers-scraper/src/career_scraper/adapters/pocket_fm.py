"""Pocket FM's current official careers journey and the exact Lever board it publishes.

Observed live on 2026-11-08 (see ``companies/pocket-fm/journey-research.md``):

- The ledger URL ``https://www.pocketfm.com/careers`` 301s to
  ``https://pocketfm.com/careers`` and that route 307s to the consumer homepage.
  Its response still identifies ``x-pathname: /careers``; the careers route no
  longer publishes a listing.
- The official Pocket FM product page ``https://pocketfm.com/sherpa`` publishes
  a footer link titled exactly ``Careers`` to
  ``https://jobs.lever.co/pocketfm``.  That board is titled
  ``Pocket Entertainment Corp`` and is the company-scoped listing of record.
- The board's own HTML renders every posting in one page (14 rows, no pagination
  and no filter controls).  Its public JSON endpoint
  ``https://api.lever.co/v0/postings/pocketfm?mode=json`` returns the same 14
  postings with the complete body sections.
- Each posting detail page is the exact company-linked URL
  ``https://jobs.lever.co/pocketfm/<uuid>``.  The rendered role body equals
  ``descriptionBody`` + every ``lists[]`` section + ``additional`` exactly
  (14/14 during research).  The page's salary line, AI-hiring disclaimer and
  Apply CTA are separate DOM sections and are not part of the role body.
- A second first-party link on ``https://sherpa.pocketfm.com/`` points to
  ``https://pocketfm.darwinbox.in/ms/candidate/careers``; in headless Chromium
  that official portal rendered "We Have 0 Open Jobs".  It publishes no
  postings and is recorded as an observed secondary portal only.

The adapter never guesses an ATS slug, never enumerates another company's
board, and never promotes Lever's ``createdAt`` record-creation timestamp to a
posting date.  All canonical date fields stay null.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html


LEDGER_ENTRY_URL = "https://www.pocketfm.com/careers"
PUBLISHER_URL = "https://pocketfm.com/sherpa"
BOARD_URL = "https://jobs.lever.co/pocketfm"
BOARD_API_URL = "https://api.lever.co/v0/postings/pocketfm?mode=json"
BOARD_EMPLOYER = "Pocket Entertainment Corp"
BOARD_SLUG = "pocketfm"
SECONDARY_OFFICIAL_PORTAL = "https://pocketfm.darwinbox.in/ms/candidate/careers"
SECONDARY_OFFICIAL_PORTAL_LINKED_FROM = "https://sherpa.pocketfm.com/"
JOB_PATH = re.compile(
    r"^/pocketfm/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
ATS_HOST_RE = re.compile(
    r"(?:jobs\.lever\.co|boards\.greenhouse\.io|jobs\.ashbyhq\.com|"
    r"careers\.kula\.ai|myworkdayjobs\.com|jobs\.smartrecruiters\.com|"
    r"\.darwinbox\.in|\.keka\.com|\.zohorecruit\.com)",
    re.I,
)
HEADING_RE = re.compile(
    r"^(?:about(?: the)? (?:role|team|pocket fm|us)|job summary|role overview|"
    r"key responsibilities|responsibilit(?:y|ies)|responsabilities|"
    r"required qualifications(?: and skills)?|preferred qualifications|qualifications|requirements|"
    r"what (?:will|you) (?:you )?do|what you(?:'|’)?ll do|what we(?:'|’)?re looking for|"
    r"what we are looking for|nice to have|ideal candidate|who you are|scope of work|"
    r"skills(?: and experience)?|experience|required deliverables|key deliverables|"
    r"what success looks like|benefits|why (?:join|pocket fm)|location|compensation)\s*:?\s*$",
    re.I,
)
RESPONSIBILITY_RE = re.compile(
    r"responsibilit|responsabilities|what (?:will|you) (?:you )?do|"
    r"what you(?:'|’)?ll do|scope of work|key duties",
    re.I,
)
QUALIFICATION_RE = re.compile(
    r"qualification|requirement|what you(?:'|’)?ll need|what we(?:'|’)?re looking for|"
    r"what we are looking for|who you are|skills|experience|must have|good to have|"
    r"nice to have|bonus points|ideal candidate",
    re.I,
)


class DetailVerificationError(ValueError):
    """The company-linked detail page does not match the listing's body."""


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def compact(value: str | None) -> str:
    return re.sub(r"[\s\ufeff]", "", unicodedata.normalize("NFC", value or ""))


def compact_hash(value: str | None) -> str:
    return hashlib.sha256(compact(value).encode("utf-8")).hexdigest()


def _assert_https_host(url: str, allowed: str, label: str) -> None:
    parsed = urlsplit(str(url))
    if parsed.scheme != "https" or parsed.hostname != allowed:
        raise ValueError(f"{label} left its inspected company HTTPS host: {url}")


def validate_ledger_entry(steps: list[dict]) -> dict:
    """Validate the saved redirect chain for the ledger URL.

    The current site sends ``www.pocketfm.com/careers`` -> ``pocketfm.com/careers``
    -> ``/``.  A 200 careers document remains acceptable; a chain that ends on
    the homepage is recorded as a changed entry, not as a job listing.
    """
    if not isinstance(steps, list) or not steps:
        raise ValueError("Pocket FM ledger-entry redirect chain is empty")
    first = steps[0]
    if first.get("url") != LEDGER_ENTRY_URL or not isinstance(first.get("status"), int):
        raise ValueError("Pocket FM ledger entry did not start at its inspected URL")
    statuses = [int(step.get("status") or 0) for step in steps]
    if statuses[-1] not in (200, 301, 302, 307, 308):
        raise ValueError(f"Pocket FM ledger entry ended with an unexpected HTTP status: {statuses}")
    for step in steps[:-1]:
        if not step.get("location"):
            raise ValueError("Pocket FM ledger entry redirect has no Location header")
    final_url = steps[-1].get("url")
    _assert_https_host(final_url, "pocketfm.com", "Pocket FM ledger entry")
    homepage = urlsplit(final_url).path in ("", "/")
    return {
        "requested_url": LEDGER_ENTRY_URL,
        "statuses": statuses,
        "redirect_chain": [
            {"url": step.get("url"), "status": step.get("status"), "location": step.get("location")}
            for step in steps
        ],
        "final_url": final_url,
        "final_status": statuses[-1],
        "redirected_to_homepage": homepage,
        "careers_document_present": not homepage and statuses[-1] == 200,
    }


def parse_publisher_page(html: str, response_url: str = PUBLISHER_URL) -> dict:
    """Return the exact Careers links published by the official Pocket FM page."""
    _assert_https_host(response_url, "pocketfm.com", "Pocket FM careers publisher")
    if urlsplit(response_url).path.rstrip("/") != "/sherpa":
        raise ValueError(f"Pocket FM careers publisher has an unexpected path: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "pocket fm" not in title.lower():
        raise ValueError("Pocket FM careers publisher title no longer names Pocket FM")
    careers = []
    for anchor in soup.select("a[href]"):
        label = normalized_title(anchor.get_text(" ", strip=True))
        href = anchor.get("href", "").strip()
        if label.lower() == "careers":
            careers.append({"text": label, "href": href})
    if len(careers) != 1:
        raise ValueError(
            f"Pocket FM careers publisher no longer exposes exactly one Careers link: {careers!r}")
    link = careers[0]
    if link["href"] != BOARD_URL:
        raise ValueError(f"Pocket FM Careers link changed from its inspected Lever board: {link['href']}")
    ats_links = sorted({
        href for href in (a.get("href", "").strip() for a in soup.select("a[href]"))
        if ATS_HOST_RE.search(href)
    })
    return {
        "url": response_url,
        "title": title,
        "careers_links": careers,
        "ats_links": ats_links,
        "board_url": link["href"],
        "issues": [] if ats_links == [BOARD_URL] else [f"additional_ats_links:{ats_links}"],
    }


def parse_listing(html: str, response_url: str = BOARD_URL) -> dict:
    """Parse the company-scoped Lever board HTML and its visible pagination controls."""
    _assert_https_host(response_url, "jobs.lever.co", "Pocket FM Lever board")
    if urlsplit(response_url).path.rstrip("/") != "/pocketfm":
        raise ValueError(f"Pocket FM Lever board has an unexpected path: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if normalized_title(title) != BOARD_EMPLOYER:
        raise ValueError(f"Pocket FM Lever board employer title changed: {title!r}")
    rows, seen, issues = [], set(), []
    for posting in soup.select("div.posting"):
        anchor = posting.select_one("a.posting-title")
        if anchor is None:
            issues.append("posting_missing_title_anchor")
            continue
        href = anchor.get("href", "").strip()
        parsed = urlsplit(href)
        match = JOB_PATH.fullmatch(parsed.path)
        if (parsed.scheme != "https" or parsed.hostname != "jobs.lever.co"
                or match is None or parsed.query or parsed.fragment):
            issues.append(f"posting_has_unexpected_url:{href}")
            continue
        identity = match.group(1)
        if identity in seen:
            issues.append(f"duplicate_posting_id:{identity}")
            continue
        heading = anchor.select_one("h5")
        row_title = normalized_title(heading.get_text(" ", strip=True) if heading else "")
        if not row_title:
            issues.append(f"posting_missing_title:{identity}")
            continue
        categories = [normalized_title(node.get_text(" ", strip=True))
                      for node in anchor.select(".posting-category")]
        workplace_node = anchor.select_one(".workplaceTypes")
        workplace = normalized_title(workplace_node.get_text(" ", strip=True).rstrip("—- ")) if workplace_node else None
        commitment = next((node.get_text(" ", strip=True) for node in anchor.select(".commitment")), None)
        location = next((node.get_text(" ", strip=True) for node in anchor.select(".location")), None)
        rows.append({
            "id": identity,
            "url": href,
            "title": row_title,
            "categories": categories,
            "commitment": normalized_title(commitment),
            "location": normalized_title(location),
            "workplace": normalized_title(workplace),
            "visible_text": normalized_title(posting.get_text(" ", strip=True)),
        })
        seen.add(identity)
    if not rows:
        issues.append("pocketfm_board_has_no_exposed_postings")
    buttons = [normalized_title(node.get_text(" ", strip=True))
               for node in soup.select("button,a")]
    pagination = [label for label in buttons if re.search(r"load more|show more|next|previous|page \d", label, re.I)]
    return {
        "url": response_url,
        "title": title,
        "rows": rows,
        "count": len(rows),
        "pagination_controls": pagination,
        "filter_controls": [label for label in buttons
                            if re.search(r"department|location|team|work type|search", label, re.I)][:20],
        "issues": issues,
    }


def parse_board_payload(payload, snapshot_name: str = "listing") -> list[dict]:
    """Validate the exact public Lever board response and return its rows."""
    if not isinstance(payload, list):
        raise ValueError(f"Pocket FM Lever {snapshot_name} response is not a postings array")
    rows, seen = [], set()
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError(f"Pocket FM Lever {snapshot_name} row is not an object")
        identity = str(row.get("id") or "")
        if not JOB_PATH.fullmatch(f"/pocketfm/{identity}"):
            raise ValueError(f"Pocket FM Lever {snapshot_name} row has an invalid posting id: {identity!r}")
        if identity in seen:
            raise ValueError(f"Pocket FM Lever {snapshot_name} repeats posting id {identity}")
        title = normalized_title(row.get("text"))
        if not title:
            raise ValueError(f"Pocket FM Lever {snapshot_name} row {identity} has no title")
        if row.get("hostedUrl") != f"{BOARD_URL}/{identity}" or row.get("applyUrl") != f"{BOARD_URL}/{identity}/apply":
            raise ValueError(f"Pocket FM Lever {snapshot_name} row {identity} changed its hosted/apply URL")
        categories = row.get("categories")
        if not isinstance(categories, dict) or not normalized_title(categories.get("location")):
            raise ValueError(f"Pocket FM Lever {snapshot_name} row {identity} has no location")
        if not isinstance(row.get("descriptionBody"), str) or not row["descriptionBody"].strip():
            raise ValueError(f"Pocket FM Lever {snapshot_name} row {identity} has no descriptionBody")
        if not isinstance(row.get("lists", []), list):
            raise ValueError(f"Pocket FM Lever {snapshot_name} row {identity} has a non-list lists field")
        if row.get("createdAt") is not None and not isinstance(row["createdAt"], int):
            raise ValueError(f"Pocket FM Lever {snapshot_name} row {identity} has a non-integer createdAt")
        if row.get("salaryRange") is not None and not isinstance(row["salaryRange"], dict):
            raise ValueError(f"Pocket FM Lever {snapshot_name} row {identity} has an invalid salaryRange")
        seen.add(identity)
        rows.append(dict(row))
    return rows


def compose_description(row: dict) -> str:
    """Rebuild the exact role body rendered by the company-linked Lever detail page."""
    parts = [row.get("opening") or "", row.get("descriptionBody") or row.get("description") or ""]
    for section in row.get("lists") or []:
        parts.append(f"<h2>{escape(section.get('text') or '')}</h2><ul>{section.get('content') or ''}</ul>")
    parts.append(row.get("additional") or "")
    parts.append(row.get("salaryDescription") or "")
    return "\n".join(part for part in parts if part and part.strip())


def _heading_from_block(block: Tag) -> str:
    if block.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return normalized_title(block.get_text(" ", strip=True)).rstrip(":?？").strip()
    if block.name != "p":
        return ""
    text = normalized_title(block.get_text(" ", strip=True))
    if not text or len(text) > 120:
        return ""
    strong = block.find("strong", recursive=False)
    if strong is not None and normalized_title(strong.get_text(" ", strip=True)) == text:
        return text.rstrip(":?？").strip()
    candidate = text.rstrip(":?？").strip()
    if HEADING_RE.match(candidate) or HEADING_RE.match(text):
        return candidate
    return ""


def _flatten_blocks(blocks: list[Tag]) -> list[Tag]:
    """Unwrap Lever's attribute-less outer description wrapper(s)."""
    flattened = []
    for block in blocks:
        if block.name == "div" and not block.get("class") and not block.get("id"):
            flattened.extend(_flatten_blocks([child for child in block.contents if isinstance(child, Tag)]))
        else:
            flattened.append(block)
    return flattened


def description_sections(html: str) -> list[dict]:
    """Split the employer-authored HTML into heading + exact body sections."""
    soup = BeautifulSoup(f'<div data-pocketfm-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-pocketfm-body]")
    blocks = _flatten_blocks([node for node in (root.contents if root else []) if isinstance(node, Tag)])
    sections, current = [], None
    for block in blocks:
        heading = _heading_from_block(block)
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
        section["text"] = text_html(section["html"]).strip()
        section["html_sha256"] = hashlib.sha256(section["html"].encode()).hexdigest()
        section["text_sha256"] = hashlib.sha256(section["text"].encode()).hexdigest()
    return sections


def classify_sections(sections: list[dict]) -> dict:
    responsibilities, qualifications, other = [], [], []
    for section in sections:
        heading = section.get("heading") or ""
        if heading and RESPONSIBILITY_RE.search(heading):
            responsibilities.append(section)
        elif heading and QUALIFICATION_RE.search(heading):
            qualifications.append(section)
        else:
            other.append(section)
    return {
        "responsibilities": responsibilities or None,
        "qualifications": qualifications or None,
        "other_sections": other or None,
    }


def job_metadata(row: dict) -> dict:
    """Stable normalized fields for one validated board row (dates intentionally null)."""
    categories = row.get("categories") or {}
    raw_locations = [categories.get("location")] + list(categories.get("allLocations") or [])
    locations = []
    for value in raw_locations:
        value = normalized_title(value)
        if value and value not in locations:
            locations.append(value)
    return {
        "locations": locations,
        "department": categories.get("department"),
        "team": categories.get("team"),
        "employment_type": categories.get("commitment"),
        "workplace_type": row.get("workplaceType"),
        "employer": BOARD_EMPLOYER,
        "salary": row.get("salaryRange"),
        "date_posted": None,
        "date_posted_raw": None,
        "date_updated": None,
        "valid_through": None,
        "raw_listing_created_at": row.get("createdAt"),
        "raw_country": row.get("country"),
    }


def parse_detail_page(html: str, response_url: str) -> dict:
    """Parse a company-linked Lever detail page into role and non-role DOM sections."""
    _assert_https_host(response_url, "jobs.lever.co", "Pocket FM Lever detail")
    soup = BeautifulSoup(html or "", "lxml")
    title = ""
    for heading in soup.select("div.posting-header h2, div.posting-header h1"):
        title = normalized_title(heading.get_text(" ", strip=True))
        break
    if not title:
        raise ValueError("Pocket FM Lever detail page has no posting title")
    categories = {}
    for node in soup.select("div.posting-categories .posting-category, div.posting-categories .workplaceTypes"):
        classes = set(node.get("class") or [])
        value = normalized_title(node.get_text(" ", strip=True)).rstrip("/ ").strip()
        if "location" in classes:
            categories["location"] = value
        elif "department" in classes:
            categories["department"] = value
        elif "commitment" in classes:
            categories["commitment"] = value
        elif "workplaceTypes" in classes:
            categories["workplace"] = value
    content_sections = []
    for section in soup.select("div.section-wrapper > div.section"):
        classes = set(section.get("class") or [])
        if "posting-header" in classes or "last-section-apply" in classes:
            continue
        if section.get("data-qa") in {"ai-disclaimer", "salary-range"}:
            continue
        content_sections.append(section)
    if not content_sections:
        raise ValueError("Pocket FM Lever detail page has no role-content section")
    content_html = "\n".join(str(section) for section in content_sections)
    ai_nodes = soup.select('[data-qa="ai-disclaimer"]')
    salary_nodes = soup.select('[data-qa="salary-range"]')
    apply_urls = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"/apply(?:/)?$", urlsplit(href).path, re.I) or re.search(r"apply for this job", anchor.get_text(" ", strip=True), re.I):
            if href not in apply_urls:
                apply_urls.append(href)
    return {
        "title": title,
        "categories": categories,
        "content_html": content_html,
        "description_text": text_html(content_html).strip(),
        "description_compact_sha256": compact_hash(text_html(content_html)),
        "description_compact_chars": len(compact(text_html(content_html))),
        "ai_disclaimer_present": bool(ai_nodes),
        "ai_disclaimer_text": normalized_title(" ".join(node.get_text(" ", strip=True) for node in ai_nodes)) or None,
        "ai_disclaimer_sha256": compact_hash(" ".join(node.get_text(" ", strip=True) for node in ai_nodes)) if ai_nodes else None,
        "salary_display": normalized_title(" ".join(node.get_text(" ", strip=True) for node in salary_nodes)) or None,
        "apply_urls": apply_urls,
    }


class PocketFM(Adapter):
    """Follow Pocket FM's exact first-party Careers link to its Lever board."""

    VERIFICATION_REVISION = "pocketfm.official-sherpa-footer-link.lever-exact-ids.1"

    async def _no_redirect_get(self, url: str):
        """Fetch one hop without following redirects and retain identical HTTP evidence."""
        request = self.http.client.build_request("GET", url)
        response = await self.http.client.send(request, follow_redirects=False)
        if self.http.output:
            digest = hashlib.sha256(response.content).hexdigest()
            target = self.http.output / "responses" / f"{digest}.gz"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(gzip.compress(response.content))
            record = {
                "url": str(response.request.url),
                "final_url": str(response.url),
                "method": "GET",
                "request_body": None,
                "status": response.status_code,
                "sha256": digest,
                "content_type": response.headers.get("content-type"),
                "redirects": [],
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "manual_redirect_hop": True,
            }
            with (self.http.output / "requests.jsonl").open("a") as handle:
                handle.write(json.dumps(record) + "\n")
        await response.aclose()
        return response

    async def _ledger_entry(self) -> dict:
        started = time.perf_counter()
        steps = []
        url = LEDGER_ENTRY_URL
        try:
            for _ in range(4):
                response = await self._no_redirect_get(url)
                step = {
                    "url": url,
                    "status": response.status_code,
                    "location": response.headers.get("location"),
                    "seconds": round(max(0.0, time.perf_counter() - started), 6),
                    "response_sha256": hashlib.sha256(response.content).hexdigest(),
                }
                steps.append(step)
                location = response.headers.get("location")
                if not location:
                    break
                url = str(response.url.join(location))
        except Exception as exc:
            raise FetchError(f"Pocket FM ledger-entry request failed: {type(exc).__name__}: {exc}") from exc
        summary = validate_ledger_entry(
            [{"url": step["url"], "status": step["status"], "location": step["location"]} for step in steps])
        summary["steps"] = steps
        summary["seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        return summary

    async def run(self):
        started = time.perf_counter()

        ledger = await self._ledger_entry()
        self.result.metadata["ledger_entry"] = ledger
        if ledger["redirected_to_homepage"]:
            self.result.warnings.append("ledger_careers_url_redirects_to_homepage")

        publisher_started = time.perf_counter()
        publisher_response = await self.http.get(PUBLISHER_URL)
        publisher = parse_publisher_page(publisher_response.text, str(publisher_response.url))
        publisher["status"] = publisher_response.status_code
        publisher["sha256"] = hashlib.sha256(publisher_response.content).hexdigest()
        publisher["bytes"] = len(publisher_response.content)
        publisher["seconds"] = round(max(0.0, time.perf_counter() - publisher_started), 6)
        self.result.metadata["publisher"] = publisher
        if publisher["issues"]:
            self.result.errors.extend(publisher["issues"])

        listing_started = time.perf_counter()
        listing_response = await self.http.get(BOARD_URL)
        listing = parse_listing(listing_response.text, str(listing_response.url))
        listing["status"] = listing_response.status_code
        listing["sha256"] = hashlib.sha256(listing_response.content).hexdigest()
        listing["bytes"] = len(listing_response.content)
        listing["seconds"] = round(max(0.0, time.perf_counter() - listing_started), 6)
        if listing["issues"]:
            self.result.errors.extend(listing["issues"])

        api_started = time.perf_counter()
        payload = await self.http.json(BOARD_API_URL)
        api_rows = parse_board_payload(payload)
        api_seconds = round(max(0.0, time.perf_counter() - api_started), 6)
        if not isinstance(payload, list):
            raise ValueError("Pocket FM Lever API response is not a postings array")

        listing_ids = [row["id"] for row in listing["rows"]]
        api_ids = [row["id"] for row in api_rows]
        if len(listing_ids) != len(set(listing_ids)):
            self.result.errors.append("pocketfm_listing_repeats_ids")
        if set(listing_ids) != set(api_ids):
            self.result.errors.append(
                f"pocketfm_listing_api_membership_mismatch:{len(listing_ids)}!={len(api_ids)}")
        if len(listing_ids) != len(api_rows):
            self.result.errors.append("pocketfm_listing_api_count_mismatch")
        if listing["pagination_controls"]:
            self.result.errors.append(f"pocketfm_unreviewed_pagination:{listing['pagination_controls']}")
        by_api = {row["id"]: row for row in api_rows}
        membership_rows = []
        for row in listing["rows"]:
            api_row = by_api.get(row["id"])
            if api_row is None:
                self.result.errors.append(f"pocketfm_listing_id_missing_from_api:{row['id']}")
                continue
            if normalized_title(api_row.get("text")) != row["title"]:
                self.result.errors.append(f"pocketfm_listing_title_mismatch:{row['id']}")
            api_location = normalized_title((api_row.get("categories") or {}).get("location"))
            if normalized_title(row["location"]) != api_location:
                self.result.errors.append(f"pocketfm_listing_location_mismatch:{row['id']}")
            membership_rows.append({"id": row["id"], "title": row["title"], "location": row["location"]})
        self.result.expected_total = len(api_rows)
        self.result.pages = 1
        self.result.listing_complete = (
            len(api_rows) == len(listing["rows"]) > 0
            and set(listing_ids) == set(api_ids)
            and not any(error.startswith("pocketfm_") for error in self.result.errors)
        )
        self.result.metadata["official_journey"] = {
            "ledger_entry_url": LEDGER_ENTRY_URL,
            "ledger_entry_final_url": ledger["final_url"],
            "ledger_entry_redirected_to_homepage": ledger["redirected_to_homepage"],
            "publisher_url": PUBLISHER_URL,
            "publisher_title": publisher["title"],
            "publisher_careers_links": publisher["careers_links"],
            "publisher_ats_links": publisher["ats_links"],
            "board_url": BOARD_URL,
            "board_api_url": BOARD_API_URL,
            "board_employer": BOARD_EMPLOYER,
            "listing_rows": listing["count"],
            "api_rows": len(api_rows),
            "pagination": "none: the board renders all postings in one HTML response; no next/load-more controls",
            "filter_controls": listing["filter_controls"],
            "secondary_official_portal": {
                "url": SECONDARY_OFFICIAL_PORTAL,
                "linked_from": SECONDARY_OFFICIAL_PORTAL_LINKED_FROM,
                "observed_rendered_text": "We Have 0 Open Jobs",
                "observed_with": "playwright chromium headless",
                "membership_used": False,
                "reason": "Official secondary portal linked from a Pocket FM subdomain renders zero open jobs; no postings to collect.",
            },
            "membership_rows": membership_rows,
            "scope_reason": (
                "The ledger URL now redirects to the consumer homepage; the official "
                "pocketfm.com/sherpa footer publishes a Careers link to jobs.lever.co/pocketfm "
                "(Pocket Entertainment Corp), whose 14 published postings are the company-scoped membership."
            ),
        }
        self.result.metadata["listing"] = {
            "url": BOARD_URL,
            "status": listing["status"],
            "bytes": listing["bytes"],
            "sha256": listing["sha256"],
            "seconds": listing["seconds"],
            "count": listing["count"],
            "pagination_controls": listing["pagination_controls"],
            "filter_controls": listing["filter_controls"],
        }
        self.result.metadata["api"] = {
            "url": BOARD_API_URL,
            "count": len(api_rows),
            "seconds": api_seconds,
        }
        self.result.timings.update({
            "pocketfm_ledger_entry_seconds": ledger["seconds"],
            "pocketfm_publisher_seconds": publisher["seconds"],
            "pocketfm_listing_seconds": listing["seconds"],
            "pocketfm_api_seconds": api_seconds,
        })

        jobs = []
        for listing_row in listing["rows"]:
            api_row = by_api[listing_row["id"]]
            meta = job_metadata(api_row)
            sections = description_sections(compose_description(api_row))
            classified = classify_sections(sections)
            job = self.job(
                api_row,
                title=api_row["text"],
                url=api_row["hostedUrl"],
                job_id=api_row["id"],
                apply_url=api_row.get("applyUrl"),
                description=compose_description(api_row),
                method="pocketfm.lever.detail_page_role_sections",
                description_url=f"{BOARD_URL}/{api_row['id']}",
                locations=meta["locations"],
                department=meta["department"],
                team=meta["team"],
                employment_type=meta["employment_type"],
                workplace_type=meta["workplace_type"],
                employer=meta["employer"],
                salary=meta["salary"],
                date_posted=None,
                date_posted_raw=None,
                date_updated=None,
                valid_through=None,
                responsibilities=classified["responsibilities"],
                qualifications=classified["qualifications"],
            )
            job.description_text = text_html(job.description_html).strip()
            job.raw = {
                "listing": listing_row,
                "api": api_row,
                "sections": sections,
                "classified_sections": classified,
                "date_semantics": {
                    "date_posted": None,
                    "date_posted_raw": None,
                    "date_updated": None,
                    "valid_through": None,
                    "lever_createdAt_preserved_only_at": "raw.api.createdAt",
                    "lever_createdAt": api_row.get("createdAt"),
                    "source_page_displays_posting_date": False,
                },
            }
            self.add(job)
            jobs.append(job)
            if self.limit():
                break

        async def detail(job):
            detail_started = time.perf_counter()
            response = await self.http.get(job.url)
            page = parse_detail_page(response.text, str(response.url))
            seconds = round(max(0.0, time.perf_counter() - detail_started), 6)
            job.raw["detail_page"] = {
                "url": str(response.url),
                "status": response.status_code,
                "bytes": len(response.content),
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "seconds": seconds,
                "title": page["title"],
                "categories": page["categories"],
                "salary_display": page["salary_display"],
                "ai_disclaimer_present": page["ai_disclaimer_present"],
                "ai_disclaimer_text": page["ai_disclaimer_text"],
                "ai_disclaimer_sha256": page["ai_disclaimer_sha256"],
                "description_compact_sha256": page["description_compact_sha256"],
                "description_compact_chars": page["description_compact_chars"],
                "apply_urls": page["apply_urls"],
            }
            if page["title"] != job.title:
                raise DetailVerificationError(
                    f"Pocket FM detail title mismatch for {job.job_id}: {page['title']!r} != {job.title!r}")
            expected_hash = compact_hash(job.description_text)
            job.raw["detail_page"]["expected_description_compact_sha256"] = expected_hash
            job.raw["detail_page"]["description_matches_listing_api"] = (
                page["description_compact_sha256"] == expected_hash)
            if page["description_compact_sha256"] != expected_hash:
                raise DetailVerificationError(
                    f"Pocket FM detail body mismatch for {job.job_id}: "
                    f"{page['description_compact_sha256']} != {expected_hash}")
            expected_categories = job.raw["api"].get("categories") or {}
            if page["categories"].get("location") and expected_categories.get("location"):
                if normalized_title(page["categories"]["location"]) != normalized_title(expected_categories["location"]):
                    raise DetailVerificationError(
                        f"Pocket FM detail location mismatch for {job.job_id}")
            salary = dict(job.raw["api"].get("salaryRange") or {})
            if page["salary_display"]:
                salary["display"] = page["salary_display"]
            job.salary = salary or None
            if page["apply_urls"]:
                expected_apply = f"{BOARD_URL}/{job.job_id}/apply"
                if expected_apply not in page["apply_urls"]:
                    raise DetailVerificationError(
                        f"Pocket FM detail apply URL mismatch for {job.job_id}: {page['apply_urls']}")
                job.apply_url = expected_apply
            job.description_method = "pocketfm.lever.detail_page_role_sections"
            job.description_source_url = str(response.url)
            return job

        self.result.jobs = list(await self.hydrate(jobs, detail))
        self.result.metadata["details"] = self.result.metadata.get("details", {})
        self.result.metadata["date_semantics"] = {
            "canonical_date_fields": ["date_posted", "date_updated", "valid_through"],
            "all_null": all(
                job.date_posted is None and job.date_updated is None and job.valid_through is None
                for job in self.result.jobs),
            "lever_createdAt_preserved_count": sum(
                isinstance((job.raw.get("api") or {}).get("createdAt"), int) for job in self.result.jobs),
            "note": "Lever createdAt is a board record-creation timestamp; it is preserved at raw.api.createdAt and never promoted to a posting date.",
        }
        self.result.timings["pocketfm_collection_wall_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        return self.result
