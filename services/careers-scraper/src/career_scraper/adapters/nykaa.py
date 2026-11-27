"""Nykaa's official careers journey (reviewed 2026-11-07).

The ledger entry ``https://www.nykaa.com/careers`` 301-redirects to the
corporate page ``https://www.nykaa.com/who_are_we#wwrcopen``.  Its Careers block
is email-only (``careers@nykaa.com``) and publishes no job cards, role links,
listing request, filters, pagination, JSON-LD JobPosting or ATS embed.

Nykaa also operates ``https://careers.nykaa.com/`` (DNS CNAME
``nykaa.skima.ai``), a Nykaa-branded Skima careers tenant.  On 2026-11-07 its
server-rendered page said "Currently there are no job postings available." with
zero cards, zero job links, zero JobPosting JSON-LD, no pagination and an empty
sitemap.

This adapter records that absence explicitly instead of returning a successful
zero-job listing.  If either official surface later publishes a listing, the
adapter fails with ``unreviewed_corporate_listing_detected`` or
``unreviewed_skima_listing_detected`` so the new UI and request shapes are
inspected before membership is collected.  It never falls back to a guessed ATS
slug, a parent-company board or an aggregator.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now


OFFICIAL_ENTRY = "https://www.nykaa.com/careers"
OFFICIAL_FINAL_PATH = "/who_are_we"
SKIMA_LISTING = "https://careers.nykaa.com/"
SKIMA_SITEMAP = "https://careers.nykaa.com/sitemap.xml"
EXPECTED_CORPORATE_TITLE = "Who are We | Nykaa"
EXPECTED_SKIMA_TITLE = "Careers | Nykaa"
CAREERS_EMAIL = "careers@nykaa.com"
NO_POSTINGS_TEXT = "Currently there are no job postings available."
EMAIL_ONLY_TEXT = "To Apply, mail us your resume at"

# Hosts that would indicate a real external job board/ATS embed.  None of these
# appears on the reviewed official pages; the list exists so a future change is
# detected and reviewed rather than silently scraped.  careers.nykaa.com is the
# company-owned Skima tenant and is checked separately.
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
    r"phenompeople\.com|eightfold\.ai|skima\.ai"
    r")$"
)
ROLE_PATH = re.compile(r"(?i)^/(?:careers|jobs?|positions?|openings?|vacanc(?:y|ies))/([a-z0-9][a-z0-9._-]{2,})/?$")
NON_ROLE_CAREERS_PATHS = {"/careers", "/careers/lp"}
PAGINATION_TEXT = re.compile(r"(?i)\b(?:next|previous|prev|load more|show more|page \d+)\b")
UUID_PATH = re.compile(
    r"(?i)^/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/?$"
)
SKIMA_JOB_PATH = re.compile(r"(?i)^/jobs?/[^/]+/?$")
JOB_QUERY = re.compile(r"(?i)(?:^|[?&])(?:job|job_id|job_uuid|jobposting|job_posting)=")


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


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _absolute(response_url: str, href: str) -> str:
    return urljoin(response_url, (href or "").strip())


def _careers_block(soup: BeautifulSoup):
    node = soup.select_one(".idforcurrentopenings")
    if node is not None:
        return node
    for heading in soup.select("h1,h2,h3"):
        if normalize_text(heading.get_text(" ", strip=True)).lower() == "careers":
            parent = heading.find_parent("div")
            if parent is not None:
                return parent
    return None


def _job_anchors(soup: BeautifulSoup, response_url: str) -> list[dict]:
    candidates = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = _absolute(response_url, href)
        path = urlsplit(absolute).path
        query = urlsplit(absolute).query
        if path.rstrip("/") in NON_ROLE_CAREERS_PATHS:
            continue
        if ROLE_PATH.match(path) or JOB_QUERY.search(query):
            candidates.append({
                "text": normalize_text(anchor.get_text(" ", strip=True)),
                "href": href,
                "url": absolute,
            })
    return candidates


def _ats_links_and_embeds(soup: BeautifulSoup, response_url: str):
    links, embeds = [], []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = _absolute(response_url, href)
        host = _host(absolute)
        if host and host != "careers.nykaa.com" and ATS_HOST.search(host):
            links.append({"text": normalize_text(anchor.get_text(" ", strip=True)), "url": absolute})
    for tag in soup.select("iframe[src], script[src], form[action], link[href]"):
        attribute = next((name for name in ("src", "action", "href") if tag.get(name)), None)
        if attribute is None:
            continue
        absolute = _absolute(response_url, tag.get(attribute))
        host = _host(absolute)
        if host and host != "careers.nykaa.com" and ATS_HOST.search(host):
            embeds.append({"tag": tag.name, "attribute": attribute, "url": absolute})
    return links, embeds


def _pagination_controls(soup: BeautifulSoup) -> list[str]:
    controls = []
    for element in soup.select("a,button"):
        text = normalize_text(element.get_text(" ", strip=True))
        if text and PAGINATION_TEXT.search(text):
            controls.append(text)
    return list(dict.fromkeys(controls))


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Parse Nykaa's corporate Careers page and describe every listing signal.

    Raises ``ValueError`` when the response is no longer the inspected Nykaa
    corporate page or its Careers block, so a redirect/redesign is reported
    instead of treated as an empty listing.
    """
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or _host(response_url) not in {"www.nykaa.com", "nykaa.com"}
            or parts.path.rstrip("/") != OFFICIAL_FINAL_PATH):
        raise ValueError(f"Nykaa careers entry redirected to an unexpected URL: {response_url}")

    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != EXPECTED_CORPORATE_TITLE:
        raise ValueError(f"Nykaa corporate careers page title changed: {title!r}")

    careers = _careers_block(soup)
    if careers is None:
        raise ValueError("Nykaa corporate Careers block (.idforcurrentopenings) is missing")
    careers_text = normalize_text(careers.get_text(" ", strip=True))
    if EMAIL_ONLY_TEXT not in careers_text:
        raise ValueError("Nykaa corporate Careers block no longer contains its email-application text")
    if "mailto:" not in str(careers).lower():
        raise ValueError("Nykaa corporate Careers block no longer publishes its careers mailto link")

    page_text = visible_text(html)
    body_text = normalize_text(page_text)
    mailto_links = list(dict.fromkeys(
        _absolute(response_url, anchor.get("href") or "")
        for anchor in soup.select('a[href^="mailto:"]')
    ))
    if f"mailto:{CAREERS_EMAIL}" not in mailto_links:
        raise ValueError(f"Nykaa corporate Careers block no longer links mailto:{CAREERS_EMAIL}")

    listing_candidates = _job_anchors(soup, response_url)
    ats_links, ats_embeds = _ats_links_and_embeds(soup, response_url)
    schema_count = job_posting_schema_count(soup)
    # Only the Careers block is relevant here; the corporate page has unrelated
    # carousels elsewhere whose controls must not look like job pagination.
    pagination = _pagination_controls(careers)
    has_listing = bool(listing_candidates or ats_links or ats_embeds or schema_count or pagination)

    return {
        "response_url": response_url,
        "title": title,
        "careers_block_text": careers_text,
        "careers_block_sha256": sha256(careers_text.encode()).hexdigest(),
        "mailto_links": mailto_links,
        "email_only": (
            CAREERS_EMAIL in careers_text
            and EMAIL_ONLY_TEXT in careers_text
            and not has_listing
        ),
        "listing_candidates": listing_candidates,
        "external_ats_links": ats_links,
        "ats_embeds": ats_embeds,
        "job_posting_schema_count": schema_count,
        "pagination_controls": pagination,
        "has_listing_signal": has_listing,
        "visible_text_chars": len(page_text),
        "normalized_text_chars": len(body_text),
        "normalized_text_sha256": sha256(body_text.encode()).hexdigest(),
    }


def _is_skima_job_url(url: str) -> bool:
    parts = urlsplit(url)
    return bool(
        SKIMA_JOB_PATH.match(parts.path)
        or UUID_PATH.match(parts.path)
        or JOB_QUERY.search(parts.query)
    )


def parse_skima_listing(html: str, response_url: str = SKIMA_LISTING) -> dict:
    """Parse Nykaa's company-owned Skima careers listing.

    The reviewed state is the explicit empty-state page.  Any job card/link,
    JobPosting JSON-LD or pagination control is surfaced as a listing signal so
    a new UI cannot be scraped without a fresh review.
    """
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or _host(response_url) != "careers.nykaa.com"
            or parts.path not in {"", "/"}):
        raise ValueError(f"Nykaa Skima listing redirected to an unexpected URL: {response_url}")

    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != EXPECTED_SKIMA_TITLE:
        raise ValueError(f"Nykaa Skima careers page title changed: {title!r}")

    page_text = visible_text(html)
    body_text = normalize_text(page_text)
    no_postings = NO_POSTINGS_TEXT in body_text

    job_links = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = _absolute(response_url, href)
        if _host(absolute) == "careers.nykaa.com" and _is_skima_job_url(absolute):
            job_links.append({
                "text": normalize_text(anchor.get_text(" ", strip=True)),
                "href": href,
                "url": absolute,
            })

    card_nodes = soup.select(
        '[data-skima-job], [data-job-id], [data-job-uuid], '
        '[class*="job-card"], [class*="jobCard"], [class*="JobCard"]'
    )
    cards = []
    for node in card_nodes:
        cards.append({
            "tag": node.name,
            "text": normalize_text(node.get_text(" ", strip=True))[:300],
            "attrs": sorted(
                (name, value) for name, value in node.attrs.items()
                if name.startswith("data-") or name in {"id", "class", "href"}
            ),
        })
    schema_count = job_posting_schema_count(soup)
    pagination = _pagination_controls(soup)
    ats_links, ats_embeds = _ats_links_and_embeds(soup, response_url)
    has_listing = bool(job_links or cards or schema_count or pagination or ats_links or ats_embeds)
    if has_listing:
        state = "listing"
    elif no_postings:
        state = "empty"
    else:
        state = "unrecognized"

    return {
        "response_url": response_url,
        "title": title,
        "state": state,
        "no_postings_message": no_postings,
        "job_links": job_links,
        "job_cards": cards,
        "job_posting_schema_count": schema_count,
        "pagination_controls": pagination,
        "external_ats_links": ats_links,
        "ats_embeds": ats_embeds,
        "search_input_count": len(soup.select("#skima-search-jobs-input")),
        "has_listing_signal": has_listing,
        "visible_text_chars": len(page_text),
        "normalized_text_chars": len(body_text),
        "normalized_text_sha256": sha256(body_text.encode()).hexdigest(),
        "visible_text_excerpt": body_text[:1000],
    }


def parse_skima_sitemap(xml: str, response_url: str = SKIMA_SITEMAP) -> dict:
    """Parse the Skima sitemap and count every published URL."""
    if _host(response_url) != "careers.nykaa.com" or urlsplit(response_url).path != "/sitemap.xml":
        raise ValueError(f"Nykaa Skima sitemap URL is unexpected: {response_url}")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Nykaa Skima sitemap is not valid XML: {exc}") from exc
    root_name = root.tag.rsplit("}", 1)[-1]
    if root_name != "urlset":
        raise ValueError(f"Nykaa Skima sitemap root changed: {root_name!r}")
    urls = [
        normalize_text(node.text)
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] == "loc" and normalize_text(node.text)
    ]
    return {
        "response_url": response_url,
        "root": root_name,
        "url_count": len(urls),
        "urls": urls,
        "job_like_urls": [url for url in urls if _is_skima_job_url(url)],
    }


def _entry_evidence(parsed: dict) -> dict:
    return {
        "title": parsed["title"],
        "careers_block_sha256": parsed["careers_block_sha256"],
        "mailto_links": parsed["mailto_links"],
        "email_only": parsed["email_only"],
        "listing_candidate_urls": [item["url"] for item in parsed["listing_candidates"]],
        "external_ats_urls": [item["url"] for item in parsed["external_ats_links"]],
        "ats_embed_urls": [item["url"] for item in parsed["ats_embeds"]],
        "job_posting_schema_count": parsed["job_posting_schema_count"],
        "pagination_controls": parsed["pagination_controls"],
        "has_listing_signal": parsed["has_listing_signal"],
    }


def _skima_evidence(parsed: dict) -> dict:
    return {
        "title": parsed["title"],
        "state": parsed["state"],
        "no_postings_message": parsed["no_postings_message"],
        "normalized_text_sha256": parsed["normalized_text_sha256"],
        "job_link_urls": [item["url"] for item in parsed["job_links"]],
        "job_card_count": len(parsed["job_cards"]),
        "job_posting_schema_count": parsed["job_posting_schema_count"],
        "pagination_controls": parsed["pagination_controls"],
        "external_ats_urls": [item["url"] for item in parsed["external_ats_links"]],
        "ats_embed_urls": [item["url"] for item in parsed["ats_embeds"]],
        "has_listing_signal": parsed["has_listing_signal"],
    }


def _snapshot(response, parsed: dict, kind: str) -> dict:
    return {
        "kind": kind,
        "requested_url": str(response.request.url),
        "final_url": str(response.url),
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
        "html_bytes": len(response.content),
        "html_sha256": sha256(response.content).hexdigest(),
        "redirects": [
            {"status": item.status_code, "url": str(item.url), "location": item.headers.get("location")}
            for item in response.history
        ],
        "fetched_at": now(),
        "parsed": parsed,
    }


class Nykaa(Adapter):
    """Read only Nykaa's official careers surfaces and record their listing state."""

    VERIFICATION_REVISION = "nykaa.official-email-and-empty-skima.1"

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_final_path": OFFICIAL_FINAL_PATH,
            "company_listing_url": SKIMA_LISTING,
            "listing_authority": "Nykaa's company-owned careers.nykaa.com Skima tenant; the ledger entry is email-only.",
            "detail_method": "none: neither official surface publishes a job or detail route",
            "request_scope": (
                "Official Nykaa entry and the company-owned careers.nykaa.com listing only; "
                "no guessed ATS slug, parent board or aggregator is queried."
            ),
        })

        try:
            self.page_guard()
            entry_response = await self.http.get(OFFICIAL_ENTRY)
            entry = parse_official_entry(entry_response.text, str(entry_response.url))
            self.page_guard()
            skima_response = await self.http.get(SKIMA_LISTING)
            skima = parse_skima_listing(skima_response.text, str(skima_response.url))
            self.page_guard()
            sitemap_response = await self.http.get(SKIMA_SITEMAP)
            sitemap = parse_skima_sitemap(sitemap_response.text, str(sitemap_response.url))
        except Exception as exc:
            self.result.errors.append(f"nykaa_official_sources_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.expected_total = None
            self.result.timings["nykaa_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["nykaa_detail_phase_seconds"] = 0.0
            return self.result

        self.result.metadata["initial_entry"] = _snapshot(entry_response, entry, "official_entry")
        self.result.metadata["initial_skima_listing"] = _snapshot(skima_response, skima, "company_skima_listing")
        self.result.metadata["initial_sitemap"] = _snapshot(sitemap_response, sitemap, "company_skima_sitemap")
        self.result.metadata["observed_journey"] = {
            "corporate_entry": {
                "title": entry["title"],
                "careers_block_text": entry["careers_block_text"],
                "careers_block_sha256": entry["careers_block_sha256"],
                "mailto_links": entry["mailto_links"],
                "email_only": entry["email_only"],
                "job_listing_candidates": entry["listing_candidates"],
                "external_ats_links": entry["external_ats_links"],
                "ats_embeds": entry["ats_embeds"],
                "job_posting_schema_count": entry["job_posting_schema_count"],
                "pagination_controls": entry["pagination_controls"],
                "visible_text_chars": entry["visible_text_chars"],
                "normalized_text_chars": entry["normalized_text_chars"],
            },
            "company_listing": {
                "title": skima["title"],
                "state": skima["state"],
                "no_postings_message": skima["no_postings_message"],
                "visible_text": skima["visible_text_excerpt"],
                "job_links": skima["job_links"],
                "job_cards": skima["job_cards"],
                "job_posting_schema_count": skima["job_posting_schema_count"],
                "pagination_controls": skima["pagination_controls"],
                "external_ats_links": skima["external_ats_links"],
                "ats_embeds": skima["ats_embeds"],
                "search_input_count": skima["search_input_count"],
                "normalized_text_chars": skima["normalized_text_chars"],
                "normalized_text_sha256": skima["normalized_text_sha256"],
            },
            "company_sitemap": {
                "root": sitemap["root"],
                "url_count": sitemap["url_count"],
                "urls": sitemap["urls"],
                "job_like_urls": sitemap["job_like_urls"],
            },
        }

        detected = {}
        if entry["has_listing_signal"]:
            self.result.errors.append("unreviewed_corporate_listing_detected")
            detected["corporate_entry"] = {
                "job_listing_candidates": entry["listing_candidates"],
                "external_ats_links": entry["external_ats_links"],
                "ats_embeds": entry["ats_embeds"],
                "job_posting_schema_count": entry["job_posting_schema_count"],
                "pagination_controls": entry["pagination_controls"],
            }
        if skima["state"] == "listing":
            self.result.errors.append("unreviewed_skima_listing_detected")
            detected["company_listing"] = _skima_evidence(skima)
        elif skima["state"] == "unrecognized":
            self.result.errors.append("skima_listing_state_unrecognized")
            detected["company_listing"] = _skima_evidence(skima)
        if sitemap["url_count"]:
            self.result.errors.append("unreviewed_skima_sitemap_detected")
            detected["company_sitemap"] = sitemap
        if detected:
            self.result.metadata["detected_listing"] = detected
            self.result.listing_complete = False
            self.result.expected_total = None
            self.result.timings["nykaa_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["nykaa_detail_phase_seconds"] = 0.0
            return self.result

        final_started = time.perf_counter()
        final_entry = final_skima = final_sitemap = None
        try:
            self.page_guard()
            final_entry_response = await self.http.get(OFFICIAL_ENTRY)
            final_entry = parse_official_entry(final_entry_response.text, str(final_entry_response.url))
            self.page_guard()
            final_skima_response = await self.http.get(SKIMA_LISTING)
            final_skima = parse_skima_listing(final_skima_response.text, str(final_skima_response.url))
            self.page_guard()
            final_sitemap_response = await self.http.get(SKIMA_SITEMAP)
            final_sitemap = parse_skima_sitemap(final_sitemap_response.text, str(final_sitemap_response.url))
            self.result.metadata["final_entry"] = _snapshot(final_entry_response, final_entry, "official_entry")
            self.result.metadata["final_skima_listing"] = _snapshot(
                final_skima_response, final_skima, "company_skima_listing"
            )
            self.result.metadata["final_sitemap"] = _snapshot(
                final_sitemap_response, final_sitemap, "company_skima_sitemap"
            )
        except Exception as exc:
            self.result.errors.append(f"nykaa_final_recheck_failed: {type(exc).__name__}: {exc}")
        finally:
            self.result.timings["nykaa_final_recheck_seconds"] = max(0.0, time.perf_counter() - final_started)

        matches = False
        if final_entry is not None and final_skima is not None and final_sitemap is not None:
            entry_same = _entry_evidence(entry) == _entry_evidence(final_entry)
            skima_same = _skima_evidence(skima) == _skima_evidence(final_skima)
            sitemap_same = (
                (sitemap["root"], sitemap["url_count"], sitemap["urls"])
                == (final_sitemap["root"], final_sitemap["url_count"], final_sitemap["urls"])
            )
            matches = entry_same and skima_same and sitemap_same
            self.result.metadata["membership_reconciliation"] = {
                "initial_corporate_title": entry["title"],
                "final_corporate_title": final_entry["title"],
                "initial_careers_block_sha256": entry["careers_block_sha256"],
                "final_careers_block_sha256": final_entry["careers_block_sha256"],
                "initial_skima_state": skima["state"],
                "final_skima_state": final_skima["state"],
                "initial_skima_text_sha256": skima["normalized_text_sha256"],
                "final_skima_text_sha256": final_skima["normalized_text_sha256"],
                "initial_sitemap_urls": sitemap["url_count"],
                "final_sitemap_urls": final_sitemap["url_count"],
                "corporate_entry_unchanged": entry_same,
                "company_listing_unchanged": skima_same,
                "company_sitemap_unchanged": sitemap_same,
                "complete": matches,
            }
            if not matches:
                self.result.errors.append("nykaa_careers_surfaces_changed_between_requests")
            if final_entry["has_listing_signal"]:
                self.result.errors.append("unreviewed_corporate_listing_detected")
            if final_skima["state"] != "empty":
                self.result.errors.append("unreviewed_skima_listing_detected")
            if final_sitemap["url_count"]:
                self.result.errors.append("unreviewed_skima_sitemap_detected")

        if matches and not any(
            error.startswith(("unreviewed_", "skima_listing_state_unrecognized"))
            for error in self.result.errors
        ):
            self.result.errors.append("no_public_job_listing")

        self.result.expected_total = 0
        self.result.listing_complete = False
        self.result.metadata["observed_journey"]["conclusion"] = (
            "Nykaa's official entry is email-only (careers@nykaa.com) and its company-owned "
            "careers.nykaa.com Skima listing renders zero public postings with an empty sitemap. "
            "The zero is an explicit no-public-listing outcome, not a successful zero-job scrape."
        )
        self.result.metadata["expected_total_source"] = (
            "Zero visible public postings on the company-owned Skima listing; the official "
            "corporate entry publishes no job listing."
        )
        self.result.metadata["zero_public_postings_reconciled"] = bool(matches)
        self.result.timings["nykaa_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.timings["nykaa_detail_phase_seconds"] = 0.0
        return self.result
