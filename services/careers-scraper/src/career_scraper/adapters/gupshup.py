"""Gupshup's official careers journey (reviewed 2026-11-07).

The ledger URL ``https://www.gupshup.io/careers`` 301-redirects to the current
official careers page ``https://www.gupshup.ai/careers``.  That page is a
marketing page: it publishes no job cards, no job links, no listing API, no
filters, no pagination and no ATS iframe/script/link.  Its only hiring CTAs open
a WhatsApp chat.

This adapter therefore records the absence explicitly instead of returning a
successful zero-job listing, and it never falls back to a guessed ATS slug or an
aggregator.  If the official page later publishes a listing, the adapter fails
with ``unreviewed_careers_listing_detected`` so the new journey is inspected by
a human before membership is collected.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now


OFFICIAL_ENTRY = "https://www.gupshup.io/careers"
OFFICIAL_CAREERS = "https://www.gupshup.ai/careers"
EXPECTED_TITLE = "Careers at Gupshup | Shape the Future of Conversations"
EXPECTED_H1 = "Your Next Career Move Is Just a Message Away"
EXPECTED_PHRASE = "Join Gupshup's Global Team"

# Hosts that would indicate a real external job board/ATS embed.  None of these
# appears on the official careers page; the list exists so a future change is
# detected and reviewed rather than silently scraped.
ATS_HOST = re.compile(
    r"(?i)(?:^|\.)(?:"
    r"boards\.greenhouse\.io|job-boards\.greenhouse\.io|greenhouse\.io|"
    r"jobs\.lever\.co|lever\.co|"
    r"(?:[a-z0-9-]+\.)?myworkdayjobs\.com|workday\.com|"
    r"ashbyhq\.com|smartrecruiters\.com|"
    r"keka\.com|darwinbox\.com|darwinbox\.in|"
    r"zohorecruit\.com|zohorecruit\.in|"
    r"turbohire\.co|recruitee\.com|workable\.com|bamboohr\.com|freshteam\.com|"
    r"hirepro\.in|recruiterflow\.com|jazz\.co|applytojob\.com|jobvite\.com|"
    r"icims\.com|taleo\.net|oraclecloud\.com|successfactors\.com|"
    r"phenompeople\.com|eightfold\.ai"
    r")$"
)
JOB_PATH = re.compile(
    r"(?i)(?:^|/)(?:job|jobs|job-|position|positions|opening|openings|"
    r"vacanc|vacancy|apply)(?:/|$|[?#])"
)
CAREER_ROLE_PATH = re.compile(r"^/careers/[a-z0-9][a-z0-9-]{2,}/?$", re.I)
WHATSAPP_CTA = re.compile(r"(?i)^https?://(?:api\.whatsapp\.com/send|wa\.me/)")
PAGINATION_TEXT = re.compile(r"(?i)\b(?:next|previous|prev|load more|show more|page \d+)\b")
LOCALE_CAREERS = re.compile(r"^/(?:es|pt|en-AE)/careers/?$", re.I)


def normalize_text(value: str | None) -> str:
    value = (value or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", value).strip()


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def _count_job_postings(value) -> int:
    if isinstance(value, dict):
        types = value.get("@type")
        if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
            return 1
        return sum(_count_job_postings(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_job_postings(item) for item in value)
    return 0


def job_posting_schema_count(soup: BeautifulSoup) -> int:
    total = 0
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text() or ""
        try:
            total += _count_job_postings(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return total


def _absolute(response_url: str, href: str) -> str:
    return urljoin(response_url, (href or "").strip())


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def parse_careers_page(html: str, response_url: str = OFFICIAL_CAREERS) -> dict:
    """Parse the official careers page and describe every listing/ATS signal.

    Raises ``ValueError`` when the response is no longer the inspected Gupshup
    careers page, so a redirect or redesign is reported instead of treated as an
    empty listing.
    """
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or _host(response_url) not in {"www.gupshup.ai", "gupshup.ai"}
            or parts.path.rstrip("/") != "/careers"):
        raise ValueError(f"Gupshup careers entry redirected to an unexpected URL: {response_url}")

    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != EXPECTED_TITLE:
        raise ValueError(f"Gupshup careers page title changed: {title!r}")

    page_text = visible_text(html)
    body_text = normalize_text(page_text)
    h1_nodes = [normalize_text(node.get_text(" ", strip=True)) for node in soup.select("h1")]
    if EXPECTED_H1 not in h1_nodes:
        raise ValueError(f"Gupshup careers page h1 changed: {h1_nodes!r}")
    if EXPECTED_PHRASE not in body_text:
        raise ValueError("Gupshup careers page no longer contains its 'Join Gupshup's Global Team' identity phrase")

    anchors, listing_candidates, external_ats_links, whatsapp_ctas, mailto_links = [], [], [], [], []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if href.startswith("mailto:"):
            mailto_links.append(_absolute(response_url, href))
            continue
        if not href or href.startswith(("#", "javascript:", "tel:")):
            continue
        absolute = _absolute(response_url, href)
        item = {
            "text": normalize_text(anchor.get_text(" ", strip=True)),
            "href": href,
            "url": absolute,
        }
        anchors.append(item)
        host = _host(absolute)
        path = urlsplit(absolute).path
        if ATS_HOST.search(host):
            external_ats_links.append(item)
        elif JOB_PATH.search(path) or CAREER_ROLE_PATH.match(path):
            if not LOCALE_CAREERS.match(path):
                listing_candidates.append(item)
        if WHATSAPP_CTA.match(absolute):
            whatsapp_ctas.append(item)

    ats_embeds = []
    for tag in soup.select("iframe[src], script[src], form[action], link[href]"):
        attribute = next((name for name in ("src", "action", "href") if tag.get(name)), None)
        if attribute is None:
            continue
        absolute = _absolute(response_url, tag.get(attribute))
        if ATS_HOST.search(_host(absolute)):
            ats_embeds.append({"tag": tag.name, "attribute": attribute, "url": absolute})

    pagination_controls = []
    for element in soup.select("a,button"):
        text = normalize_text(element.get_text(" ", strip=True))
        if text and PAGINATION_TEXT.search(text):
            pagination_controls.append(text)

    hiring_contacts = [
        url for url in mailto_links
        if re.search(r"(?i)(career|job|hiring|recruit)", url)
    ]

    normalized = body_text
    return {
        "response_url": response_url,
        "title": title,
        "h1": h1_nodes,
        "headings": [
            normalize_text(node.get_text(" ", strip=True))
            for node in soup.select("h1,h2,h3")
            if normalize_text(node.get_text(" ", strip=True))
        ],
        "anchors": anchors,
        "anchor_count": len(anchors),
        "listing_candidates": listing_candidates,
        "external_ats_links": external_ats_links,
        "ats_embeds": ats_embeds,
        "job_posting_schema_count": job_posting_schema_count(soup),
        "pagination_controls": pagination_controls,
        "whatsapp_ctas": whatsapp_ctas,
        "hiring_contacts": hiring_contacts,
        "visible_text_chars": len(page_text),
        "normalized_text_chars": len(normalized),
        "normalized_text_sha256": sha256(normalized.encode()).hexdigest(),
    }


def _evidence(parsed: dict) -> dict:
    return {
        "title": parsed["title"],
        "h1": parsed["h1"],
        "headings": parsed["headings"],
        "listing_candidate_urls": [item["url"] for item in parsed["listing_candidates"]],
        "external_ats_urls": [item["url"] for item in parsed["external_ats_links"]],
        "ats_embed_urls": [item["url"] for item in parsed["ats_embeds"]],
        "job_posting_schema_count": parsed["job_posting_schema_count"],
        "pagination_controls": parsed["pagination_controls"],
        "whatsapp_cta_urls": [item["url"] for item in parsed["whatsapp_ctas"]],
        "normalized_text_sha256": parsed["normalized_text_sha256"],
    }


def has_listing_signal(parsed: dict) -> bool:
    return bool(
        parsed["listing_candidates"]
        or parsed["external_ats_links"]
        or parsed["ats_embeds"]
        or parsed["job_posting_schema_count"]
    )


class Gupshup(Adapter):
    """Read only the official Gupshup careers page and record its listing state."""

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "expected_careers_url": OFFICIAL_CAREERS,
            "listing_authority": "none: the official careers page publishes no job listing",
            "detail_method": "none: no role or detail route is published",
            "request_scope": "Official Gupshup careers page only; no guessed ATS slug, aggregator or parent board is queried.",
        })

        try:
            self.page_guard()
            initial_response = await self.http.get(OFFICIAL_ENTRY)
            initial_parsed = parse_careers_page(initial_response.text, str(initial_response.url))
        except Exception as exc:
            self.result.errors.append(f"gupshup_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["gupshup_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        initial_entry = {
            "requested_url": OFFICIAL_ENTRY,
            "final_url": str(initial_response.url),
            "status_code": initial_response.status_code,
            "html_bytes": len(initial_response.content),
            "html_sha256": sha256(initial_response.content).hexdigest(),
            "redirects": [
                {"status": item.status_code, "url": str(item.url), "location": item.headers.get("location")}
                for item in initial_response.history
            ],
            "fetched_at": now(),
        }
        self.result.metadata["initial_entry"] = initial_entry
        self.result.metadata["observed_journey"] = {
            "page_title": initial_parsed["title"],
            "h1": initial_parsed["h1"],
            "headings": initial_parsed["headings"],
            "anchor_count": initial_parsed["anchor_count"],
            "job_listing_candidates": initial_parsed["listing_candidates"],
            "external_ats_links": initial_parsed["external_ats_links"],
            "ats_embeds": initial_parsed["ats_embeds"],
            "job_posting_schema_count": initial_parsed["job_posting_schema_count"],
            "pagination_controls": initial_parsed["pagination_controls"],
            "whatsapp_ctas": initial_parsed["whatsapp_ctas"],
            "hiring_contacts": initial_parsed["hiring_contacts"],
            "visible_text_chars": initial_parsed["visible_text_chars"],
            "normalized_text_chars": initial_parsed["normalized_text_chars"],
            "normalized_text_sha256": initial_parsed["normalized_text_sha256"],
        }

        if has_listing_signal(initial_parsed):
            self.result.errors.append("unreviewed_careers_listing_detected")
            self.result.listing_complete = False
            self.result.metadata["detected_listing"] = {
                "job_listing_candidates": initial_parsed["listing_candidates"],
                "external_ats_links": initial_parsed["external_ats_links"],
                "ats_embeds": initial_parsed["ats_embeds"],
                "job_posting_schema_count": initial_parsed["job_posting_schema_count"],
            }
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["gupshup_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        final_started = time.perf_counter()
        final_parsed = None
        try:
            self.page_guard()
            final_response = await self.http.get(OFFICIAL_ENTRY)
            final_parsed = parse_careers_page(final_response.text, str(final_response.url))
            self.result.metadata["final_recheck"] = {
                "requested_url": OFFICIAL_ENTRY,
                "final_url": str(final_response.url),
                "status_code": final_response.status_code,
                "html_bytes": len(final_response.content),
                "html_sha256": sha256(final_response.content).hexdigest(),
                "redirects": [
                    {"status": item.status_code, "url": str(item.url), "location": item.headers.get("location")}
                    for item in final_response.history
                ],
                "fetched_at": now(),
            }
        except Exception as exc:
            self.result.errors.append(f"gupshup_final_recheck_failed: {type(exc).__name__}: {exc}")
        finally:
            self.result.timings["gupshup_final_recheck_seconds"] = max(0.0, time.perf_counter() - final_started)

        matches = False
        if final_parsed is not None:
            if has_listing_signal(final_parsed):
                self.result.errors.append("unreviewed_careers_listing_detected")
            matches = _evidence(initial_parsed) == _evidence(final_parsed)
            if not matches:
                self.result.errors.append("gupshup_careers_page_changed_between_requests")

        if final_parsed is not None and matches and not has_listing_signal(final_parsed):
            self.result.errors.append("no_public_job_listing")

        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata["observed_journey"]["conclusion"] = (
            "The official careers page publishes no public job listing: no job cards, job links, "
            "listing request, filters, pagination, JSON-LD JobPosting or ATS embed. Its only hiring "
            "CTAs open a WhatsApp chat. The empty result is an explicit no-listing outcome, not a "
            "successful zero-job listing."
        )
        self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.timings["gupshup_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
