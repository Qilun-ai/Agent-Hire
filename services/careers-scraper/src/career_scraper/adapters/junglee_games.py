"""Junglee Games' official careers journey (reviewed 2026-11-07).

The ledger entry ``https://www.jungleegames.com/careers`` is reachable, but the
company now serves one static corporate shutdown notice for it: the page says
"All our games have been discontinued."  It publishes no job cards, no job
links, no listing request, no filters, no pagination, no JSON-LD JobPosting and
no ATS iframe/script/link.  The same 10,689-byte page is also returned for
``/``, ``/jobs``, ``/robots.txt`` and ``/sitemap.xml``.

This adapter therefore records the absence explicitly instead of returning a
successful zero-job listing, and it never falls back to a guessed ATS slug, a
parent-company board or an aggregator.  If the official page later publishes a
listing, the adapter fails with ``unreviewed_careers_listing_detected`` so the
new journey is inspected before membership is collected.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now


OFFICIAL_ENTRY = "https://www.jungleegames.com/careers"
# The origin intermittently resets non-browser connections.  These are ordinary
# navigation headers, not a challenge bypass; the adapter still stops on any
# WAF/CAPTCHA/403/429 response and never solves a challenge.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "close",
}
EXPECTED_TITLE = "Junglee Games"
EXPECTED_META_DESCRIPTION = (
    "Junglee Games, a part of Flutter Entertainment, is a technology-led "
    "entertainment powerhouse. All our games have been discontinued."
)
SHUTDOWN_PHRASE = "All our games have been discontinued."
EXPECTED_HEADINGS = ("Who we are", "CSR Commitment")
EXPECTED_CIN = "U72200HR2011PTC114723"

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
    r"phenompeople\.com|eightfold\.ai|kula\.ai|hrone\.cloud"
    r")$"
)
JOB_PATH = re.compile(
    r"(?i)(?:^|/)(?:job|jobs|job-|position|positions|opening|openings|"
    r"vacanc|vacancy|apply)(?:/|$|[?#])"
)
CAREER_ROLE_PATH = re.compile(r"^/careers/[a-z0-9][a-z0-9-]{2,}/?$", re.I)
CAREERS_PATH = re.compile(r"^/careers/?$", re.I)
PAGINATION_TEXT = re.compile(r"(?i)\b(?:next|previous|prev|load more|show more|page \d+)\b")


def normalize_text(value: str | None) -> str:
    value = (value or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", value).strip()


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


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


def parse_careers_page(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Parse the official careers page and describe every listing/ATS signal.

    Raises ``ValueError`` when the response is not on the inspected Junglee
    Games careers route, so a redirect or domain change is reported instead of
    treated as an empty listing.  Identity checks are returned as flags so a
    redesigned page with a new listing is detected as a listing signal first.
    """
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or _host(response_url) not in {"www.jungleegames.com", "jungleegames.com"}
            or parts.path.rstrip("/") != "/careers"):
        raise ValueError(f"Junglee Games careers entry redirected to an unexpected URL: {response_url}")

    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    meta_node = soup.select_one('meta[name="description"]')
    meta_description = normalize_text(meta_node.get("content")) if meta_node else ""
    page_text = visible_text(html)
    normalized = normalize_text(page_text)
    headings = [
        normalize_text(node.get_text(" ", strip=True))
        for node in soup.select("h1,h2,h3")
        if normalize_text(node.get_text(" ", strip=True))
    ]

    anchors, listing_candidates, external_ats_links, mailto_links = [], [], [], []
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
            if not CAREERS_PATH.match(path):
                listing_candidates.append(item)

    ats_embeds = []
    for tag in soup.select("iframe[src], script[src], form[action], link[href], object[data], embed[src]"):
        attribute = next((name for name in ("src", "action", "href", "data") if tag.get(name)), None)
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

    job_card_candidates = soup.select('[class*="job"], [id*="job"], [data-job], [data-job-id], [data-jobid]')
    forms = [
        {"action": form.get("action"), "method": form.get("method")}
        for form in soup.select("form")
    ]
    iframes = [
        {"src": iframe.get("src"), "title": iframe.get("title")}
        for iframe in soup.select("iframe")
    ]
    scripts = [script.get("src") for script in soup.select("script[src]") if script.get("src")]

    identity = {
        "title_matches": title == EXPECTED_TITLE,
        "meta_description_matches": meta_description == EXPECTED_META_DESCRIPTION,
        "shutdown_phrase_present": SHUTDOWN_PHRASE in normalized,
        "headings_present": all(heading in headings for heading in EXPECTED_HEADINGS),
        "registered_address_present": "Registered Address:" in normalized,
        "cin_present": EXPECTED_CIN in normalized,
    }

    return {
        "response_url": response_url,
        "title": title,
        "meta_description": meta_description,
        "headings": headings,
        "anchors": anchors,
        "anchor_count": len(anchors),
        "listing_candidates": listing_candidates,
        "external_ats_links": external_ats_links,
        "ats_embeds": ats_embeds,
        "job_posting_schema_count": job_posting_schema_count(soup),
        "pagination_controls": pagination_controls,
        "job_card_candidates": len(job_card_candidates),
        "forms": forms,
        "iframes": iframes,
        "scripts": scripts,
        "mailto_links": mailto_links,
        "visible_text_chars": len(page_text),
        "normalized_text_chars": len(normalized),
        "normalized_text_sha256": sha256(normalized.encode()).hexdigest(),
        "normalized_text_fnv1a64": fnv1a64(normalized),
        "shutdown_phrase_present": SHUTDOWN_PHRASE in normalized,
        "identity": identity,
    }


def is_expected_shutdown_page(parsed: dict) -> bool:
    return all(parsed["identity"].values())


def has_listing_signal(parsed: dict) -> bool:
    return bool(
        parsed["listing_candidates"]
        or parsed["external_ats_links"]
        or parsed["ats_embeds"]
        or parsed["job_posting_schema_count"]
        or parsed["job_card_candidates"]
    )


def _evidence(parsed: dict) -> dict:
    return {
        "title": parsed["title"],
        "meta_description": parsed["meta_description"],
        "headings": parsed["headings"],
        "anchor_urls": [item["url"] for item in parsed["anchors"]],
        "listing_candidate_urls": [item["url"] for item in parsed["listing_candidates"]],
        "external_ats_urls": [item["url"] for item in parsed["external_ats_links"]],
        "ats_embed_urls": [item["url"] for item in parsed["ats_embeds"]],
        "job_posting_schema_count": parsed["job_posting_schema_count"],
        "pagination_controls": parsed["pagination_controls"],
        "job_card_candidates": parsed["job_card_candidates"],
        "forms": parsed["forms"],
        "iframes": parsed["iframes"],
        "scripts": parsed["scripts"],
        "normalized_text_sha256": parsed["normalized_text_sha256"],
        "identity": parsed["identity"],
    }


class JungleeGames(Adapter):
    """Read only the official Junglee Games careers page and record its state."""

    def __init__(self, http, company, source, options, *, fetch_attempts: int = 12,
                 fetch_retry_seconds: float = 2.0):
        super().__init__(http, company, source, options)
        self.fetch_attempts = max(1, fetch_attempts)
        self.fetch_retry_seconds = max(0.0, fetch_retry_seconds)

    async def _fetch_official(self):
        attempts = []
        last_error = None
        for attempt in range(1, self.fetch_attempts + 1):
            started = time.monotonic()
            try:
                response = await self.http.get(OFFICIAL_ENTRY, headers=REQUEST_HEADERS)
                attempts.append({
                    "attempt": attempt,
                    "status": response.status_code,
                    "final_url": str(response.url),
                    "bytes": len(response.content),
                    "seconds": round(max(0.0, time.monotonic() - started), 6),
                    "error": None,
                })
                return response, attempts
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                attempts.append({
                    "attempt": attempt,
                    "status": None,
                    "final_url": None,
                    "bytes": 0,
                    "seconds": round(max(0.0, time.monotonic() - started), 6),
                    "error": last_error,
                })
                if attempt < self.fetch_attempts:
                    await asyncio.sleep(self.fetch_retry_seconds)
        raise RuntimeError(
            f"official careers page fetch failed after {self.fetch_attempts} attempts: {last_error}"
        )

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "expected_title": EXPECTED_TITLE,
            "expected_shutdown_phrase": SHUTDOWN_PHRASE,
            "listing_authority": "none: the official careers page publishes no job listing",
            "detail_method": "none: no role or detail route is published",
            "request_scope": (
                "Official Junglee Games careers page only; no guessed ATS slug, "
                "parent-company board or aggregator is queried."
            ),
        })

        try:
            self.page_guard()
            initial_response, initial_fetch_attempts = await self._fetch_official()
            initial_parsed = parse_careers_page(initial_response.text, str(initial_response.url))
        except Exception as exc:
            self.result.errors.append(f"junglee_games_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["junglee_games_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
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
            "fetch_attempts": initial_fetch_attempts,
            "fetched_at": now(),
        }
        self.result.metadata["initial_entry"] = initial_entry
        self.result.metadata["observed_journey"] = {
            "page_title": initial_parsed["title"],
            "meta_description": initial_parsed["meta_description"],
            "headings": initial_parsed["headings"],
            "anchor_count": initial_parsed["anchor_count"],
            "anchors": initial_parsed["anchors"],
            "job_listing_candidates": initial_parsed["listing_candidates"],
            "external_ats_links": initial_parsed["external_ats_links"],
            "ats_embeds": initial_parsed["ats_embeds"],
            "job_posting_schema_count": initial_parsed["job_posting_schema_count"],
            "pagination_controls": initial_parsed["pagination_controls"],
            "job_card_candidates": initial_parsed["job_card_candidates"],
            "forms": initial_parsed["forms"],
            "iframes": initial_parsed["iframes"],
            "scripts": initial_parsed["scripts"],
            "visible_text_chars": initial_parsed["visible_text_chars"],
            "normalized_text_chars": initial_parsed["normalized_text_chars"],
            "normalized_text_sha256": initial_parsed["normalized_text_sha256"],
            "normalized_text_fnv1a64": initial_parsed["normalized_text_fnv1a64"],
            "shutdown_phrase_present": initial_parsed["shutdown_phrase_present"],
            "identity": initial_parsed["identity"],
        }

        if has_listing_signal(initial_parsed):
            self.result.errors.append("unreviewed_careers_listing_detected")
            self.result.listing_complete = False
            self.result.metadata["detected_listing"] = {
                "job_listing_candidates": initial_parsed["listing_candidates"],
                "external_ats_links": initial_parsed["external_ats_links"],
                "ats_embeds": initial_parsed["ats_embeds"],
                "job_posting_schema_count": initial_parsed["job_posting_schema_count"],
                "job_card_candidates": initial_parsed["job_card_candidates"],
            }
            self.result.timings["junglee_games_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        if not is_expected_shutdown_page(initial_parsed):
            self.result.errors.append("junglee_games_careers_page_changed")
            self.result.listing_complete = False
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The official careers route no longer matches the reviewed shutdown page and publishes "
                "no listing signal. The changed page must be reviewed before any membership is inferred."
            )
            self.result.timings["junglee_games_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        final_started = time.perf_counter()
        final_parsed = None
        try:
            self.page_guard()
            final_response, final_fetch_attempts = await self._fetch_official()
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
                "fetch_attempts": final_fetch_attempts,
                "fetched_at": now(),
            }
        except Exception as exc:
            self.result.errors.append(f"junglee_games_final_recheck_failed: {type(exc).__name__}: {exc}")
        finally:
            self.result.timings["junglee_games_final_recheck_seconds"] = max(
                0.0, time.perf_counter() - final_started
            )

        matches = False
        if final_parsed is not None:
            if has_listing_signal(final_parsed):
                self.result.errors.append("unreviewed_careers_listing_detected")
            matches = _evidence(initial_parsed) == _evidence(final_parsed)
            if not matches:
                self.result.errors.append("junglee_games_careers_page_changed_between_requests")

        if final_parsed is not None and matches and not has_listing_signal(final_parsed):
            if is_expected_shutdown_page(final_parsed):
                self.result.errors.append("no_public_job_listing")
                self.result.metadata["observed_journey"]["conclusion"] = (
                    "The official careers page is a reachable corporate shutdown notice that says all "
                    "games have been discontinued. It publishes no job cards, job links, listing request, "
                    "filters, pagination, JSON-LD JobPosting or ATS embed. The empty result is an explicit "
                    "no-listing outcome, not a successful zero-job listing."
                )
            else:
                self.result.errors.append("junglee_games_careers_page_changed")

        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata["job_ids"] = []
        self.result.metadata["listing_state"] = (
            "no_public_job_listing"
            if "no_public_job_listing" in self.result.errors
            else "unreviewed_or_changed"
        )
        self.result.timings["junglee_games_source_collection_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        self.result.metadata["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result
