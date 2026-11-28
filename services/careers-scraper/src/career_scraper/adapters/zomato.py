"""Zomato's current official careers journey: a referral-only Eternal page.

Reviewed 2026-11-08.  The ledger entry ``https://www.zomato.com/careers`` is
live and current for Zomato, but it 301-redirects to
``https://www.eternal.com/careers`` and then 302s to
``https://www.eternal.com/careers/``.  The final page is Eternal Ltd.'s careers
page (Zomato is listed under "Our businesses").  It publishes **no job listing
and no public application route**: the client-rendered careers copy says
applications are accepted only through employee referrals, and the only
application-adjacent contact is ``hr@eternal.com`` (for reporting fraudulent
offers).

The page exposes no job cards, job/position links, listing request, filters,
pagination, JSON-LD ``JobPosting`` or ATS embed.  Its linked careers component
contains no ATS host, job API or ``JobPosting`` marker.  This adapter records
that absence explicitly instead of returning a successful zero-job result, and
it never falls back to a guessed ATS slug, a Workable tenant that returns zero
jobs, a parent/group board or an aggregator.

If the official journey later publishes a real listing (job cards, job links,
ATS embed, listing API, filters or pagination), the adapter fails with
``unreviewed_careers_listing_detected`` so the new UI and request shapes are
inspected before membership is collected.

The shared HTTP client's default scraper User-Agent was served a ReadTimeout by
Zomato's edge in the first 2026-11-08 attempt.  The adapter therefore sends a
desktop Chrome User-Agent (the same public-page practice used by other adapters
here).  It does not solve challenges or bypass bot protection; a 403/429 or
challenge response still stops with an explicit error.
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


OFFICIAL_ENTRY = "https://www.zomato.com/careers"
FINAL_ENTRY = "https://www.eternal.com/careers/"
EXPECTED_TITLE = "Careers - Hiring at Eternal"
EXPECTED_META_DESCRIPTION = "A sneak peek into the hiring philosophy of Eternal"
REFERRAL_POLICY_FRAGMENTS = (
    "we only accept applications through",
    "employee referrals",
)
CONTACT_EMAIL = "hr@eternal.com"
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
BUSINESS_LINKS = {
    "/our-businesses/zomato": "Zomato",
    "/our-businesses/blinkit": "Blinkit",
    "/our-businesses/district": "District",
    "/our-businesses/hyperpure": "Hyperpure",
}

# Hosts that would indicate an external job board/ATS embed. None appears on the
# reviewed page or in its careers component; the list exists so a future change
# is detected and reviewed instead of silently scraped.
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
    r"phenompeople\.com|eightfold\.ai|kula\.ai|hrone\.cloud|skima\.ai|"
    r"teamtailor\.com|breezy\.hr|rippling\.com|paylocity\.com|gusto\.com|"
    r"jobscore\.com|recruiterbox\.com"
    r")$"
)
JOB_PATH = re.compile(
    r"(?i)(?:^|/)(?:jobs?|job-[a-z0-9-]+|positions?|openings?|"
    r"vacanc(?:y|ies)|requisitions?|apply)(?:/|$|[?#])"
)
PAGINATION_TEXT = re.compile(
    r"(?i)\b(?:next|previous|prev|load more|show more|page \d+)\b"
)
JOB_FILTER_TEXT = re.compile(
    r"(?i)\b(?:job|jobs|position|opening|vacanc(?:y|ies)|career|"
    r"department|location|keyword|search)\b"
)
JOB_FILTER_FIELD = re.compile(
    r"(?i)(?:job|position|opening|vacanc|department|location|keyword|search)"
)
JOB_API_MARKERS = re.compile(
    r"(?i)(?:ats_job_posts|job_posts|jobposting|"
    r"/api/[^\"'\s]*(?:job|career|position)|"
    r"greenhouse|lever\.co|myworkdayjobs|ashbyhq|smartrecruiters|"
    r"kula\.ai|keka\.com|workable\.com|turbohire|darwinbox|zohorecruit|"
    r"icims|taleo|successfactors|phenompeople|eightfold|teamtailor|breezy\.hr)"
)


def normalize_text(value: str | None) -> str:
    value = (value or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", value).strip()


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _absolute(response_url: str, href: str) -> str:
    return urljoin(response_url, (href or "").strip())


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


def _job_cards(soup: BeautifulSoup) -> list[dict]:
    selectors = (
        '[class*="job-card"]', '[class*="job-list"]', '[class*="job-item"]',
        '[class*="job-offer"]', '[class*="position-card"]', '[class*="vacancy"]',
        '[class*="opening"]', '[id*="job-list"]', '[id*="job-card"]',
    )
    cards, seen = [], set()
    for selector in selectors:
        for node in soup.select(selector):
            text = normalize_text(node.get_text(" ", strip=True))
            classes = " ".join(node.get("class", []))
            key = (node.name, classes, text[:120])
            if key in seen:
                continue
            seen.add(key)
            cards.append({
                "tag": node.name,
                "class": classes,
                "text": text[:300],
            })
    return cards


def _job_links(soup: BeautifulSoup, response_url: str) -> list[dict]:
    links = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = _absolute(response_url, href)
        host = _host(absolute)
        path = urlsplit(absolute).path
        reason = None
        if ATS_HOST.search(host):
            reason = "ats_host"
        elif JOB_PATH.search(path):
            reason = "job_path"
        if reason is None:
            continue
        links.append({
            "text": normalize_text(anchor.get_text(" ", strip=True)),
            "href": href,
            "url": absolute,
            "reason": reason,
        })
    return links


def _ats_embeds(soup: BeautifulSoup, response_url: str) -> list[dict]:
    embeds = []
    for tag in soup.select("iframe[src], script[src], form[action], link[href]"):
        attribute = next(
            (name for name in ("src", "action", "href") if tag.get(name)), None
        )
        if attribute is None:
            continue
        absolute = _absolute(response_url, tag.get(attribute))
        if ATS_HOST.search(_host(absolute)):
            embeds.append({
                "tag": tag.name,
                "attribute": attribute,
                "url": absolute,
            })
    return embeds


def _job_filters(soup: BeautifulSoup) -> list[dict]:
    filters = []
    for form in soup.select("form"):
        action = (form.get("action") or "").strip()
        text = normalize_text(form.get_text(" ", strip=True))
        fields = [
            field.get("name", "")
            for field in form.select("input[name], select[name], textarea[name]")
        ]
        if not fields:
            continue
        action_is_job = bool(JOB_FILTER_TEXT.search(action))
        text_is_job = bool(JOB_FILTER_TEXT.search(text))
        fields_are_job = any(JOB_FILTER_FIELD.search(name) for name in fields)
        if (action_is_job or text_is_job) and fields_are_job:
            filters.append({
                "action": action,
                "text": text[:200],
                "fields": fields,
            })
    return filters


def _pagination_controls(soup: BeautifulSoup) -> list[str]:
    controls = []
    for element in soup.select("a,button"):
        text = normalize_text(element.get_text(" ", strip=True))
        if text and PAGINATION_TEXT.search(text):
            controls.append(text)
    return controls


def parse_entry_page(html: str, response_url: str = FINAL_ENTRY) -> dict:
    """Parse the final Eternal careers page reached from Zomato's official entry.

    Raises ``ValueError`` when the response is not the inspected page, so a
    redirect/redesign is reported instead of being treated as an empty listing.
    """
    parts = urlsplit(response_url)
    if (
        parts.scheme != "https"
        or _host(response_url) not in {"www.eternal.com", "eternal.com"}
        or parts.path.rstrip("/") != "/careers"
    ):
        raise ValueError(
            f"Zomato official careers entry redirected to an unexpected URL: {response_url}"
        )

    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != EXPECTED_TITLE:
        raise ValueError(f"Zomato/Eternal careers page title changed: {title!r}")

    description = soup.select_one('meta[name="description"]')
    meta_description = normalize_text(description.get("content", "")) if description else ""
    if meta_description != EXPECTED_META_DESCRIPTION:
        raise ValueError(
            f"Zomato/Eternal careers meta description changed: {meta_description!r}"
        )

    business_links = []
    for anchor in soup.select('a[href*="/our-businesses/"]'):
        absolute = _absolute(response_url, anchor.get("href", ""))
        path = urlsplit(absolute).path.rstrip("/")
        if path not in BUSINESS_LINKS:
            continue
        business_links.append({
            "name": BUSINESS_LINKS[path],
            "text": normalize_text(anchor.get_text(" ", strip=True)),
            "href": anchor.get("href"),
            "url": absolute,
        })
    seen_business = {item["name"] for item in business_links}
    missing_business = sorted(set(BUSINESS_LINKS.values()) - seen_business)
    if missing_business:
        raise ValueError(
            f"Zomato/Eternal business directory changed; missing {missing_business}"
        )

    component_nodes = [
        node for node in soup.select("astro-island[component-url]")
        if "careers" in (node.get("component-url") or "")
    ]
    if not component_nodes:
        raise ValueError("Zomato/Eternal careers component script is missing")
    component_url = _absolute(response_url, component_nodes[0].get("component-url", ""))

    page_text = visible_text(html)
    body_text = normalize_text(page_text)
    cards = _job_cards(soup)
    job_links = _job_links(soup, response_url)
    ats_embeds = _ats_embeds(soup, response_url)
    schema_count = job_posting_schema_count(soup)
    filters = _job_filters(soup)
    pagination = _pagination_controls(soup)

    listing_signals = {
        "job_cards": cards,
        "job_links": job_links,
        "ats_embeds": ats_embeds,
        "job_posting_schema_count": schema_count,
        "job_filters": filters,
    }
    has_listing_signal = bool(
        cards or job_links or ats_embeds or schema_count or filters
    )

    return {
        "response_url": response_url,
        "title": title,
        "meta_description": meta_description,
        "headings": [
            normalize_text(node.get_text(" ", strip=True))
            for node in soup.select("h1,h2,h3")
            if normalize_text(node.get_text(" ", strip=True))
        ],
        "business_links": business_links,
        "business_names": sorted(seen_business),
        "careers_component_url": component_url,
        "careers_component_count": len(component_nodes),
        "anchors_count": len(soup.select("a[href]")),
        "mailto_links": sorted({
            anchor.get("href")
            for anchor in soup.select('a[href^="mailto:"]')
            if anchor.get("href")
        }),
        "iframes": len(soup.select("iframe")),
        "forms": len(soup.select("form")),
        "inputs": len(soup.select("input")),
        "job_cards": cards,
        "job_links": job_links,
        "ats_embeds": ats_embeds,
        "job_posting_schema_count": schema_count,
        "job_filters": filters,
        "pagination_controls": pagination,
        "listing_signals": listing_signals,
        "has_listing_signal": has_listing_signal,
        "visible_text_chars": len(page_text),
        "normalized_text_chars": len(body_text),
        "normalized_text_sha256": sha256(body_text.encode()).hexdigest(),
        "visible_text_excerpt": page_text[:1000],
    }


def parse_careers_component(js: str, component_url: str = "") -> dict:
    """Verify the page-linked careers component is the referral-only copy.

    Raises ``ValueError`` if the component is no longer the inspected careers
    component or if it now contains a job-listing/ATS marker.
    """
    parts = urlsplit(component_url)
    if (
        parts.scheme != "https"
        or _host(component_url) not in {"www.eternal.com", "eternal.com"}
        or not parts.path.startswith("/_astro/careers.")
        or not parts.path.endswith(".js")
    ):
        raise ValueError(f"Unexpected Zomato/Eternal careers component URL: {component_url}")

    body = js or ""
    missing = [fragment for fragment in REFERRAL_POLICY_FRAGMENTS if fragment not in body]
    if missing:
        raise ValueError(
            "Zomato/Eternal careers component no longer contains the referral-only "
            f"policy (missing {missing})"
        )
    if CONTACT_EMAIL not in body:
        raise ValueError(
            f"Zomato/Eternal careers component no longer contains {CONTACT_EMAIL}"
        )

    ats_hosts = sorted({
        host for host in (
            _host(url) for url in re.findall(r"https?://[^\"'\s)]+", body)
        )
        if host and ATS_HOST.search(host)
    })
    api_markers = sorted({match.group(0).lower() for match in JOB_API_MARKERS.finditer(body)})
    has_listing_signal = bool(ats_hosts or api_markers or "JobPosting" in body)
    return {
        "url": component_url,
        "bytes": len(body.encode()),
        "sha256": sha256(body.encode()).hexdigest(),
        "referral_policy_fragments": list(REFERRAL_POLICY_FRAGMENTS),
        "referral_policy_found": True,
        "contact_email": CONTACT_EMAIL,
        "contact_email_found": True,
        "ats_hosts": ats_hosts,
        "job_api_markers": api_markers,
        "job_posting_marker_found": "JobPosting" in body,
        "has_listing_signal": has_listing_signal,
    }


def _entry_record(response) -> dict:
    return {
        "requested_url": OFFICIAL_ENTRY,
        "final_url": str(response.url),
        "status_code": response.status_code,
        "html_bytes": len(response.content),
        "html_sha256": sha256(response.content).hexdigest(),
        "redirects": [
            {
                "status": item.status_code,
                "url": str(item.url),
                "location": item.headers.get("location"),
            }
            for item in response.history
        ],
        "fetched_at": now(),
    }


def _evidence(parsed: dict) -> dict:
    return {
        "title": parsed["title"],
        "meta_description": parsed["meta_description"],
        "business_links": sorted(item["url"] for item in parsed["business_links"]),
        "careers_component_url": parsed["careers_component_url"],
        "job_cards": parsed["job_cards"],
        "job_links": parsed["job_links"],
        "ats_embeds": parsed["ats_embeds"],
        "job_posting_schema_count": parsed["job_posting_schema_count"],
        "job_filters": parsed["job_filters"],
        "pagination_controls": parsed["pagination_controls"],
        "normalized_text_sha256": parsed["normalized_text_sha256"],
    }


class Zomato(Adapter):
    """Read only Zomato's published careers journey and record its listing state."""

    VERIFICATION_REVISION = "zomato.eternal-referral-only-no-listing.1"

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_final_url": FINAL_ENTRY,
            "listing_authority": (
                "none: the official Zomato entry redirects to Eternal Ltd.'s "
                "referral-only careers page, which publishes no job listing"
            ),
            "detail_method": "none: no Zomato job or detail route is published",
            "request_scope": (
                "Official Zomato entry and the final Eternal careers page it "
                "redirects to, plus that page's own careers component script. "
                "No guessed ATS slug, Workable tenant, parent/group board or "
                "aggregator is queried."
            ),
        })

        try:
            self.page_guard()
            initial_response = await self.http.get(
                OFFICIAL_ENTRY, headers=BROWSER_HEADERS
            )
        except Exception as exc:
            self.result.errors.append(
                f"zomato_official_entry_unreachable: {type(exc).__name__}: {exc}"
            )
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.metadata["entry_fetch_failed"] = True
            self.result.metadata["waf_or_challenge_stop"] = any(
                token in str(exc).lower()
                for token in ("403", "429", "challenge", "captcha")
            )
            self.result.timings["zomato_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            self.result.timings["zomato_detail_phase_seconds"] = 0.0
            return self.result

        self.result.metadata["initial_entry"] = _entry_record(initial_response)
        try:
            initial_parsed = parse_entry_page(
                initial_response.text, str(initial_response.url)
            )
        except Exception as exc:
            self.result.errors.append(
                f"zomato_official_page_changed: {type(exc).__name__}: {exc}"
            )
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.timings["zomato_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            self.result.timings["zomato_detail_phase_seconds"] = 0.0
            return self.result

        chain = self.result.metadata["initial_entry"]["redirects"]
        chain_ok = bool(
            chain
            and chain[0]["status"] == 301
            and chain[0]["url"] == OFFICIAL_ENTRY
            and chain[0]["location"] == "https://www.eternal.com/careers"
        )
        self.result.metadata["official_entry_redirect_chain_ok"] = chain_ok
        if not chain_ok:
            self.result.warnings.append("official_entry_redirect_chain_changed")

        component_url = initial_parsed["careers_component_url"]
        try:
            self.page_guard()
            component_response = await self.http.get(
                component_url, headers=BROWSER_HEADERS
            )
        except Exception as exc:
            self.result.errors.append(
                f"zomato_careers_component_unreachable: {type(exc).__name__}: {exc}"
            )
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.timings["zomato_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            self.result.timings["zomato_detail_phase_seconds"] = 0.0
            return self.result

        try:
            component = parse_careers_component(
                component_response.text, str(component_response.url)
            )
        except Exception as exc:
            self.result.errors.append(
                f"zomato_referral_policy_not_verified: {type(exc).__name__}: {exc}"
            )
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.timings["zomato_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            self.result.timings["zomato_detail_phase_seconds"] = 0.0
            return self.result

        self.result.metadata["careers_component"] = {
            **component,
            "status_code": component_response.status_code,
            "final_url": str(component_response.url),
        }
        self.result.metadata["observed_journey"] = {
            "page_title": initial_parsed["title"],
            "meta_description": initial_parsed["meta_description"],
            "headings": initial_parsed["headings"],
            "business_links": initial_parsed["business_links"],
            "business_names": initial_parsed["business_names"],
            "careers_component_url": component_url,
            "careers_component": component,
            "anchors_count": initial_parsed["anchors_count"],
            "mailto_links": initial_parsed["mailto_links"],
            "iframes": initial_parsed["iframes"],
            "forms": initial_parsed["forms"],
            "inputs": initial_parsed["inputs"],
            "job_cards": initial_parsed["job_cards"],
            "job_links": initial_parsed["job_links"],
            "ats_embeds": initial_parsed["ats_embeds"],
            "job_posting_schema_count": initial_parsed["job_posting_schema_count"],
            "job_filters": initial_parsed["job_filters"],
            "pagination_controls": initial_parsed["pagination_controls"],
            "has_listing_signal": initial_parsed["has_listing_signal"],
            "visible_text_chars_http": initial_parsed["visible_text_chars"],
            "normalized_text_chars_http": initial_parsed["normalized_text_chars"],
            "normalized_text_sha256_http": initial_parsed["normalized_text_sha256"],
        }

        detected = {}
        if initial_parsed["has_listing_signal"]:
            detected["entry_page"] = initial_parsed["listing_signals"]
        if component["has_listing_signal"]:
            detected["careers_component"] = {
                "ats_hosts": component["ats_hosts"],
                "job_api_markers": component["job_api_markers"],
                "job_posting_marker_found": component["job_posting_marker_found"],
            }
        if detected:
            self.result.errors.append("unreviewed_careers_listing_detected")
            self.result.metadata["detected_listing"] = detected
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.timings["zomato_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            self.result.timings["zomato_detail_phase_seconds"] = 0.0
            return self.result

        final_started = time.perf_counter()
        final_parsed = None
        try:
            self.page_guard()
            final_response = await self.http.get(
                OFFICIAL_ENTRY, headers=BROWSER_HEADERS
            )
            final_parsed = parse_entry_page(
                final_response.text, str(final_response.url)
            )
            self.result.metadata["final_recheck"] = _entry_record(final_response)
        except Exception as exc:
            self.result.errors.append(
                f"zomato_final_recheck_failed: {type(exc).__name__}: {exc}"
            )
        finally:
            self.result.timings["zomato_final_recheck_seconds"] = max(
                0.0, time.perf_counter() - final_started
            )

        matches = False
        if final_parsed is not None:
            if final_parsed["has_listing_signal"]:
                self.result.errors.append("unreviewed_careers_listing_detected")
            matches = _evidence(initial_parsed) == _evidence(final_parsed)
            if not matches:
                self.result.errors.append("zomato_careers_page_changed_between_requests")
            self.result.metadata["membership_reconciliation"] = {
                "initial_title": initial_parsed["title"],
                "final_title": final_parsed["title"],
                "initial_page_text_sha256": initial_parsed["normalized_text_sha256"],
                "final_page_text_sha256": final_parsed["normalized_text_sha256"],
                "initial_careers_component_url": initial_parsed["careers_component_url"],
                "final_careers_component_url": final_parsed["careers_component_url"],
                "listing_signals_absent_both": (
                    not initial_parsed["has_listing_signal"]
                    and not final_parsed["has_listing_signal"]
                ),
                "complete": matches,
            }

        if final_parsed is not None and matches and not final_parsed["has_listing_signal"]:
            self.result.errors.append("no_public_job_listing_referral_only")

        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata["scope_decision"] = {
            "scope": "company",
            "requested_company": "Zomato",
            "included": (
                "Zomato's official careers URL and the final Eternal Ltd. careers "
                "page it redirects to (including that page's own careers component)"
            ),
            "excluded": [item["url"] for item in initial_parsed["business_links"]],
            "exclusion_reason": (
                "The final page lists Zomato, Blinkit, District and Hyperpure as "
                "Eternal businesses. Those links are corporate navigation, not a "
                "Zomato-scoped job listing; no other business's careers page and "
                "no guessed/unlinked ATS tenant was enumerated."
            ),
            "referral_only": True,
            "application_contact": CONTACT_EMAIL,
        }
        self.result.metadata["observed_journey"]["conclusion"] = (
            "Zomato's official careers URL 301s to Eternal Ltd.'s careers page, "
            "which is reachable but explicitly referral-only and publishes no "
            "job listing: no job cards/links, listing request, filters, "
            "pagination, JSON-LD JobPosting or ATS embed. The empty result is an "
            "explicit no-public-listing outcome, not a successful zero-job "
            "listing and not an inaccessible listing."
        )
        self.result.metadata["advertised_jobs"] = None
        self.result.metadata["expected_total_source"] = (
            "None: the final page publishes no job count and exposes no listing."
        )
        self.result.timings["zomato_source_collection_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        self.result.timings["zomato_detail_phase_seconds"] = 0.0
        return self.result
