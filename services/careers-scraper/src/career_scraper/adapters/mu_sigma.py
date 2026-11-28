"""Mu Sigma's current official careers journey (reviewed 2026-11-07).

The ledger URL ``https://www.mu-sigma.com/careers/`` 301-redirects to the
current official page ``https://www.mu-sigma.com/career/``.  That page is a
marketing/FAQ page: it publishes no job cards, no job links, no listing API, no
filters, no pagination, no JSON-LD ``JobPosting`` and no ATS iframe/script/link.
Its FAQ tells candidates to email ``DSRecruitment@mu-sigma.com`` and says
openings also appear on LinkedIn.

The company's own WordPress ``jobs`` post type is public through
``/wp-json/wp/v2/jobs`` and currently returns zero published entries
(``X-WP-Total: 0``).  The page sitemap contains one unlinked legacy role page
(``/apprentice-leader/``, published 2024-01-11, last modified 2024-01-15); it was
linked from the 2024 careers page, has been absent from the listing since at
least 2025-11, and is recorded as excluded evidence, never as a current job.

This adapter records the absence explicitly instead of returning a successful
zero-job listing, and it never falls back to a guessed ATS slug, the LinkedIn
aggregator, the legacy page or a parent-company board.  If the official page
later publishes a listing, or the company's ``jobs`` collection becomes
non-empty, the adapter fails closed with
``unreviewed_listing_signal_detected`` / ``unreviewed_jobs_api_entries`` so the
new journey is inspected by a human before membership is collected.
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


OFFICIAL_ENTRY = "https://www.mu-sigma.com/careers/"
OFFICIAL_CAREERS = "https://www.mu-sigma.com/career/"
WP_JOBS_API = "https://www.mu-sigma.com/wp-json/wp/v2/jobs"
WP_PAGES_API = "https://www.mu-sigma.com/wp-json/wp/v2/pages"
PAGE_SITEMAP = "https://www.mu-sigma.com/page-sitemap.xml"
LEGACY_ROLE_URL = "https://www.mu-sigma.com/apprentice-leader/"

EXPECTED_TITLE = "Explore Exciting Career Opportunities | Join Mu Sigma"
EXPECTED_H1 = "Help Build Decisions that Shape Markets Around the World"
EXPECTED_IDENTITY_PHRASE = "Help Build Decisions that Shape Markets Around the World"
EXPECTED_LINKEDIN = "https://www.linkedin.com/company/mu-sigma/?viewAsMember=true"
EXPECTED_LEGACY_TITLE = "Apprentice Leader - mu-sigma"
LEGACY_PAGE_ID = 3180
LEGACY_PAGE_DATE = "2024-01-11T06:17:00"
LEGACY_PAGE_MODIFIED = "2024-01-15T05:00:43"
LEGACY_PAGE_SLUG = "apprentice-leader"

WP_JOBS_QUERY = {"per_page": "100", "orderby": "date", "order": "desc"}
WP_PAGES_QUERY = {"slug": LEGACY_PAGE_SLUG, "_fields": "id,date,modified,slug,link,title,status"}
PAGINATION_TEXT = re.compile(r"(?i)\b(?:next|previous|prev|load more|show more|page \d+)\b")
JOB_POSITIONS_HEADING = re.compile(r"(?i)^(?:job positions|latest jobs|open positions|current openings|open roles)$")
JOB_PATH = re.compile(r"(?i)(?:^|/)(?:jobs?|job[-_]openings?|positions?|openings?|vacanc(?:y|ies)|apply)(?:/|$|[?#])")
CAREER_ROLE_PATH = re.compile(r"^/(?:careers|career)/(?!$)[a-z0-9][a-z0-9-]{2,}/?$", re.I)
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
    r"phenompeople\.com|eightfold\.ai|kula\.ai"
    r")$"
)
LINKEDIN_JOB_PATH = re.compile(r"(?i)(?:^|/)(?:jobs|job)(?:/|$)")
ROLE_TOKENS = frozenset({
    "apprentice", "leader", "engineer", "analyst", "scientist", "manager",
    "consultant", "developer", "designer", "director", "intern", "architect",
    "specialist", "associate", "executive", "officer", "recruiter", "job",
    "jobs", "opening", "openings", "position", "positions", "vacancy",
    "vacancies", "apply",
})
COOKIE_UI_SELECTORS = (
    "#cookie-law-info-bar", ".cky-consent-container", ".cky-overlay", ".cky-modal",
    ".cky-consent-bar", ".cky-btn-revisit-wrapper", "[data-cky-tag]",
    ".cli-bar-container",
)
JOB_CARD_SELECTORS = (
    ".job-box", ".job-card", ".job-search", ".career-row .job-box",
    "article.job", "li.job", "[class*='job-card']", "[class*='job-box']",
)


def normalize_text(value: str | None) -> str:
    value = (value or "").replace("\u2019", "'").replace("\u2018", "'")
    value = value.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _strip_cookie_ui(soup: BeautifulSoup) -> None:
    for node in soup.select(", ".join(COOKIE_UI_SELECTORS)):
        if node.parent is not None:
            node.decompose()


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    _strip_cookie_ui(soup)
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


def _unique_nodes(nodes) -> list:
    seen, result = set(), []
    for node in nodes:
        marker = id(node)
        if marker not in seen:
            seen.add(marker)
            result.append(node)
    return result


def parse_careers_page(html: str, response_url: str = OFFICIAL_CAREERS) -> dict:
    """Parse the current Mu Sigma careers page and describe listing signals.

    Raises ``ValueError`` when the response is no longer the inspected Mu Sigma
    page, so a redirect/redesign is reported instead of treated as an empty
    listing.
    """
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or _host(response_url) not in {"www.mu-sigma.com", "mu-sigma.com"}
            or parts.path.rstrip("/") != "/career"):
        raise ValueError(f"Mu Sigma careers entry redirected to an unexpected URL: {response_url}")

    soup = BeautifulSoup(html, "lxml")
    _strip_cookie_ui(soup)
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != EXPECTED_TITLE:
        raise ValueError(f"Mu Sigma careers page title changed: {title!r}")

    page_text = visible_text(html)
    body_text = normalize_text(page_text)
    h1_nodes = [normalize_text(node.get_text(" ", strip=True)) for node in soup.select("h1")]
    if h1_nodes != [EXPECTED_H1]:
        raise ValueError(f"Mu Sigma careers page h1 changed: {h1_nodes!r}")
    if EXPECTED_IDENTITY_PHRASE not in body_text:
        raise ValueError("Mu Sigma careers page no longer contains its current hero identity phrase")

    anchors, listing_candidates, external_ats_links, ats_embeds = [], [], [], []
    linkedin_profile_links, linkedin_job_links, mailto_links = [], [], []
    aggregator_links = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("mailto:"):
            mailto_links.append(_absolute(response_url, href))
            continue
        if href.startswith(("#", "javascript:", "tel:")):
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
        elif "linkedin.com" in host:
            aggregator_links.append(item)
            if LINKEDIN_JOB_PATH.search(path):
                linkedin_job_links.append(item)
            else:
                linkedin_profile_links.append(item)
        elif (JOB_PATH.search(path) or CAREER_ROLE_PATH.match(path)) and path.rstrip("/") != "/career":
            listing_candidates.append(item)

    for tag in soup.select("iframe[src], script[src], form[action], link[href]"):
        attribute = next((name for name in ("src", "action", "href") if tag.get(name)), None)
        if attribute is None:
            continue
        absolute = _absolute(response_url, tag.get(attribute))
        if ATS_HOST.search(_host(absolute)):
            ats_embeds.append({"tag": tag.name, "attribute": attribute, "url": absolute})

    job_cards = _unique_nodes(
        node for selector in JOB_CARD_SELECTORS for node in soup.select(selector)
    )
    job_positions_headings = [
        normalize_text(node.get_text(" ", strip=True))
        for node in soup.select("h1,h2,h3,h4")
        if JOB_POSITIONS_HEADING.match(normalize_text(node.get_text(" ", strip=True)))
    ]
    pagination_controls = []
    for element in soup.select("a,button"):
        text = normalize_text(element.get_text(" ", strip=True))
        if text and PAGINATION_TEXT.search(text):
            pagination_controls.append(text)

    hiring_contacts = [
        url for url in mailto_links
        if re.search(r"(?i)(career|job|hiring|recruit)", url)
    ]
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
        "job_cards": [
            {"text": normalize_text(node.get_text(" ", strip=True))[:400],
             "classes": node.get("class") or []}
            for node in job_cards
        ],
        "job_positions_headings": job_positions_headings,
        "job_posting_schema_count": job_posting_schema_count(soup),
        "pagination_controls": pagination_controls,
        "linkedin_profile_links": linkedin_profile_links,
        "linkedin_job_links": linkedin_job_links,
        "aggregator_links": aggregator_links,
        "hiring_contacts": hiring_contacts,
        "has_expected_linkedin_profile": any(
            item["url"] == EXPECTED_LINKEDIN for item in linkedin_profile_links
        ),
        "has_linkedin_openings_faq": (
            "Openings also appear on our" in body_text and "LinkedIn page" in body_text
        ),
        "visible_text_chars": len(page_text),
        "normalized_text_chars": len(body_text),
        "normalized_text_sha256": sha256(body_text.encode()).hexdigest(),
        "visible_text": page_text,
    }


def listing_signals(parsed: dict) -> dict:
    """First-party signals that would mean an actual scrapeable listing exists."""
    return {
        "job_cards": len(parsed["job_cards"]),
        "job_links": [item["url"] for item in parsed["listing_candidates"]],
        "external_ats_links": [item["url"] for item in parsed["external_ats_links"]],
        "ats_embeds": [item["url"] for item in parsed["ats_embeds"]],
        "job_posting_schema_count": parsed["job_posting_schema_count"],
        "pagination_controls": parsed["pagination_controls"],
        "job_positions_headings": parsed["job_positions_headings"],
    }


def has_listing_signal(parsed: dict) -> bool:
    signals = listing_signals(parsed)
    return bool(
        signals["job_cards"]
        or signals["job_links"]
        or signals["external_ats_links"]
        or signals["ats_embeds"]
        or signals["job_posting_schema_count"]
        or signals["pagination_controls"]
        or signals["job_positions_headings"]
    )


def parse_wp_jobs(value) -> dict:
    """Validate the company's public WordPress ``jobs`` collection response."""
    if not isinstance(value, list):
        raise ValueError("Mu Sigma wp/v2/jobs response is not a JSON array")
    rows = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Mu Sigma wp/v2/jobs response contains a non-object entry")
        title = item.get("title")
        if isinstance(title, dict):
            title = title.get("rendered")
        rows.append({
            "id": item.get("id"),
            "date": item.get("date"),
            "modified": item.get("modified"),
            "slug": item.get("slug"),
            "link": item.get("link"),
            "status": item.get("status"),
            "title": title,
        })
    return {"entries": rows, "count": len(rows)}


def parse_wp_headers(headers) -> dict:
    def integer(name):
        raw = headers.get(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return {
        "x_wp_total": integer("X-WP-Total"),
        "x_wp_totalpages": integer("X-WP-TotalPages"),
    }


def parse_page_sitemap(xml: str) -> dict:
    """Read the current page sitemap and identify role-like page slugs."""
    soup = BeautifulSoup(xml, "xml")
    rows = []
    for node in soup.select("url"):
        loc = node.find("loc")
        lastmod = node.find("lastmod")
        url = normalize_text(loc.get_text(" ", strip=True)) if loc else ""
        if not url:
            continue
        path = urlsplit(url).path.rstrip("/")
        slug = path.rsplit("/", 1)[-1]
        rows.append({
            "url": url,
            "slug": slug,
            "lastmod": normalize_text(lastmod.get_text(" ", strip=True)) if lastmod else None,
        })
    role_pages = [
        row for row in rows
        if row["slug"] and row["slug"].lower() != "career"
        and any(token in ROLE_TOKENS for token in row["slug"].lower().split("-"))
    ]
    return {"urls": rows, "count": len(rows), "role_pages": role_pages}


def parse_legacy_page_record(value) -> dict:
    """Read the one public WordPress page object for the unlinked legacy role."""
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ValueError("Mu Sigma legacy page lookup did not return exactly one page object")
    item = value[0]
    title = item.get("title")
    if isinstance(title, dict):
        title = title.get("rendered")
    slug = item.get("slug")
    if slug != LEGACY_PAGE_SLUG:
        raise ValueError(f"Mu Sigma legacy page slug changed: {slug!r}")
    return {
        "id": item.get("id"),
        "date": item.get("date"),
        "modified": item.get("modified"),
        "slug": slug,
        "link": item.get("link"),
        "status": item.get("status"),
        "title": title,
    }


def _next_line(lines: list[str], label: str) -> str | None:
    for index, line in enumerate(lines):
        if line == label:
            return lines[index + 1] if index + 1 < len(lines) else None
        if line.startswith(label + " "):
            return normalize_text(line[len(label):])
    return None


def parse_legacy_role_page(html: str, response_url: str = LEGACY_ROLE_URL) -> dict:
    """Parse the 2024 unlinked role page as excluded evidence, never as a job."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or _host(response_url) not in {"www.mu-sigma.com", "mu-sigma.com"}
            or parts.path.rstrip("/") != "/apprentice-leader"):
        raise ValueError(f"Mu Sigma legacy role page URL changed: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != EXPECTED_LEGACY_TITLE:
        raise ValueError(f"Mu Sigma legacy role page title changed: {title!r}")
    text = visible_text(html)
    normalized = normalize_text(text)
    marker = "Mu Sigma Business Solutions, LLC (Northbrook, IL) needs Apprentice Leader:"
    if marker not in normalized:
        raise ValueError("Mu Sigma legacy role page no longer contains its inspected job advertisement")
    ad_start = normalized.index(marker)
    ad_end = normalized.find("Salary Offered:", ad_start)
    if ad_end < 0:
        raise ValueError("Mu Sigma legacy role page salary section is missing")
    ad = normalized[ad_start:ad_end + 80]
    lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
    salary_match = re.search(r"Salary Offered:\s*(\$[\d,]+\.\d{2}\s*/\s*year)", normalized)
    email_match = re.search(r"[\w.+-]+@[\w.-]+", ad)
    return {
        "url": response_url,
        "title": title,
        "has_h1": bool(soup.select("h1")),
        "employer": "Mu Sigma Business Solutions, LLC",
        "location": "Northbrook, IL",
        "role_title": "Apprentice Leader",
        "salary": salary_match.group(1) if salary_match else None,
        "education": _next_line(lines, "Education:"),
        "experience": _next_line(lines, "Experience:"),
        "special_certificates": _next_line(lines, "Special Certificates:"),
        "special_requirements": _next_line(lines, "Special Requirements:"),
        "hiring_email": email_match.group(0) if email_match else None,
        "advertisement_text": ad,
        "advertisement_normalized_chars": len(ad),
        "advertisement_normalized_sha256": sha256(ad.encode()).hexdigest(),
        "job_posting_schema_count": job_posting_schema_count(soup),
        "has_form": bool(soup.select("form")),
        "form_actions": sorted({_absolute(response_url, form.get("action")) for form in soup.select("form[action]")}),
        "included_in_membership": False,
        "exclusion_reason": (
            "Unlinked 2024 static page: published 2024-01-11, last modified 2024-01-15, "
            "absent from the current careers page and from every inbound link on the 44 "
            "current pages; the current careers journey publishes no listing."
        ),
    }


def _response_meta(response, seconds: float) -> dict:
    return {
        "requested_url": str(response.request.url),
        "final_url": str(response.url),
        "status_code": response.status_code,
        "redirects": [
            {"status": item.status_code, "url": str(item.url), "location": item.headers.get("location")}
            for item in response.history
        ],
        "headers": {
            key: value for key, value in response.headers.items()
            if key.lower() in ("date", "last-modified", "content-type", "server",
                               "x-wp-total", "x-wp-totalpages", "cache-control", "via")
        },
        "bytes": len(response.content),
        "sha256": sha256(response.content).hexdigest(),
        "fetched_at": now(),
        "request_seconds": round(max(0.0, seconds), 6),
    }


def _journey_evidence(parsed: dict, wp_jobs: dict, sitemap: dict) -> dict:
    return {
        "title": parsed["title"],
        "h1": parsed["h1"],
        "listing_signals": listing_signals(parsed),
        "normalized_text_sha256": parsed["normalized_text_sha256"],
        "wp_jobs_count": wp_jobs["count"],
        "role_pages": [
            {"url": row["url"], "lastmod": row["lastmod"]}
            for row in sitemap["role_pages"]
        ],
    }


class MuSigma(Adapter):
    """Read only Mu Sigma's own careers page, jobs collection and sitemap."""

    VERIFICATION_REVISION = "mu_sigma.no-listing.2026-11-07.1"

    async def _careers(self):
        started = time.perf_counter()
        self.page_guard()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != OFFICIAL_CAREERS:
            raise ValueError(f"Mu Sigma careers entry ended at unexpected URL: {response.url}")
        parsed = parse_careers_page(response.text, str(response.url))
        return {"parsed": parsed, "meta": _response_meta(response, seconds)}

    async def _wp_jobs(self):
        started = time.perf_counter()
        self.page_guard()
        response = await self.http.request("GET", WP_JOBS_API, params=WP_JOBS_QUERY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed_url = urlsplit(str(response.url))
        if (parsed_url.scheme != "https" or parsed_url.netloc != "www.mu-sigma.com"
                or parsed_url.path != "/wp-json/wp/v2/jobs"):
            raise ValueError(f"Mu Sigma jobs collection redirected outside its official route: {response.url}")
        parsed = parse_wp_jobs(response.json())
        parsed["headers"] = parse_wp_headers(response.headers)
        return {"parsed": parsed, "meta": _response_meta(response, seconds)}

    async def _sitemap(self):
        started = time.perf_counter()
        self.page_guard()
        response = await self.http.get(PAGE_SITEMAP)
        seconds = max(0.0, time.perf_counter() - started)
        parsed_url = urlsplit(str(response.url))
        if (parsed_url.scheme != "https" or parsed_url.netloc != "www.mu-sigma.com"
                or parsed_url.path != "/page-sitemap.xml"):
            raise ValueError(f"Mu Sigma page sitemap redirected outside its official route: {response.url}")
        parsed = parse_page_sitemap(response.text)
        return {"parsed": parsed, "meta": _response_meta(response, seconds)}

    async def _legacy_probe(self):
        page_record_started = time.perf_counter()
        self.page_guard()
        record_response = await self.http.request("GET", WP_PAGES_API, params=WP_PAGES_QUERY)
        record_seconds = max(0.0, time.perf_counter() - page_record_started)
        record_url = urlsplit(str(record_response.url))
        if (record_url.scheme != "https" or record_url.netloc != "www.mu-sigma.com"
                or record_url.path != "/wp-json/wp/v2/pages"):
            raise ValueError(f"Mu Sigma legacy page lookup redirected outside its official route: {record_response.url}")
        parsed_record = parse_legacy_page_record(record_response.json())
        record_meta = _response_meta(record_response, record_seconds)

        page_started = time.perf_counter()
        self.page_guard()
        page_response = await self.http.get(LEGACY_ROLE_URL)
        page_seconds = max(0.0, time.perf_counter() - page_started)
        if str(page_response.url) != LEGACY_ROLE_URL:
            raise ValueError(f"Mu Sigma legacy role page redirected away: {page_response.url}")
        parsed_page = parse_legacy_role_page(page_response.text, str(page_response.url))
        page_meta = _response_meta(page_response, page_seconds)
        return {"page_record": parsed_record, "page_record_meta": record_meta,
                "page": parsed_page, "page_meta": page_meta,
                "probe_seconds": record_seconds + page_seconds}

    def _legacy_is_known(self, probe: dict) -> bool:
        record = probe["page_record"]
        return (
            record["id"] == LEGACY_PAGE_ID
            and record["date"] == LEGACY_PAGE_DATE
            and record["modified"] == LEGACY_PAGE_MODIFIED
            and record["status"] == "publish"
            and record["link"] == LEGACY_ROLE_URL
        )

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_CAREERS,
            "listing_authority": "none: the official careers page publishes no job listing",
            "detail_method": "none: no role or detail route is part of the current journey",
            "request_scope": (
                "Only official mu-sigma.com routes: the careers page, its public "
                "wp/v2/jobs collection, the page sitemap, and the unlinked legacy role "
                "page as excluded evidence. No guessed ATS slug, LinkedIn/aggregator "
                "board, or parent-company board is queried."
            ),
        })

        try:
            initial_careers = await self._careers()
        except Exception as exc:
            self.result.errors.append(f"mu_sigma_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["mu_sigma_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        parsed = initial_careers["parsed"]
        signals = listing_signals(parsed)
        self.result.metadata["initial_careers"] = initial_careers["meta"]
        self.result.timings["mu_sigma_careers_request_seconds"] = initial_careers["meta"]["request_seconds"]
        self.result.metadata["observed_journey"] = {
            "title": parsed["title"],
            "h1": parsed["h1"],
            "headings": parsed["headings"],
            "anchor_count": parsed["anchor_count"],
            "listing_signals": signals,
            "job_cards": parsed["job_cards"],
            "linkedin_profile_links": parsed["linkedin_profile_links"],
            "linkedin_job_links": parsed["linkedin_job_links"],
            "hiring_contacts": parsed["hiring_contacts"],
            "has_expected_linkedin_profile": parsed["has_expected_linkedin_profile"],
            "has_linkedin_openings_faq": parsed["has_linkedin_openings_faq"],
            "visible_text_chars": parsed["visible_text_chars"],
            "normalized_text_chars": parsed["normalized_text_chars"],
            "normalized_text_sha256": parsed["normalized_text_sha256"],
        }

        wp_jobs = sitemap = None
        wp_jobs_error = sitemap_error = None
        try:
            wp_jobs = await self._wp_jobs()
        except Exception as exc:
            wp_jobs_error = f"mu_sigma_jobs_api_failed: {type(exc).__name__}: {exc}"
            self.result.errors.append(wp_jobs_error)
        try:
            sitemap = await self._sitemap()
        except Exception as exc:
            sitemap_error = f"mu_sigma_page_sitemap_failed: {type(exc).__name__}: {exc}"
            self.result.errors.append(sitemap_error)

        if wp_jobs is not None:
            self.result.timings["mu_sigma_jobs_api_request_seconds"] = wp_jobs["meta"]["request_seconds"]
            self.result.metadata["wp_jobs_collection"] = {
                **wp_jobs["meta"],
                "headers_parsed": wp_jobs["parsed"]["headers"],
                "count": wp_jobs["parsed"]["count"],
                "entries": wp_jobs["parsed"]["entries"],
            }
        if sitemap is not None:
            self.result.timings["mu_sigma_sitemap_request_seconds"] = sitemap["meta"]["request_seconds"]
            self.result.metadata["page_sitemap"] = {
                **sitemap["meta"],
                "count": sitemap["parsed"]["count"],
                "role_pages": sitemap["parsed"]["role_pages"],
            }

        legacy_present = bool(sitemap and any(
            row["url"] == LEGACY_ROLE_URL for row in sitemap["parsed"]["role_pages"]
        ))
        unreviewed_role_pages = []
        if sitemap is not None:
            unreviewed_role_pages = [
                row for row in sitemap["parsed"]["role_pages"]
                if row["url"] != LEGACY_ROLE_URL
            ]
        if unreviewed_role_pages:
            self.result.errors.append("unreviewed_role_page_in_sitemap")
            self.result.metadata["unreviewed_role_pages"] = unreviewed_role_pages

        if legacy_present:
            try:
                legacy = await self._legacy_probe()
                known = self._legacy_is_known(legacy)
                legacy["known_since_review"] = known
                legacy["sitemap_lastmod"] = next(
                    (row["lastmod"] for row in sitemap["parsed"]["role_pages"]
                     if row["url"] == LEGACY_ROLE_URL), None
                )
                self.result.metadata["legacy_role_page"] = legacy
                self.result.timings["mu_sigma_legacy_probe_seconds"] = legacy.get("probe_seconds", 0.0)
                if not known:
                    self.result.warnings.append("legacy_role_page_changed_since_review")
                if legacy["sitemap_lastmod"] and legacy["page_record"]["modified"] and (
                    not legacy["sitemap_lastmod"].startswith(legacy["page_record"]["modified"])
                ):
                    self.result.warnings.append("legacy_role_page_sitemap_lastmod_mismatch")
                if legacy["page"]["job_posting_schema_count"]:
                    self.result.warnings.append("legacy_role_page_now_has_jobposting_schema")
            except Exception as exc:
                self.result.warnings.append("legacy_role_page_probe_failed")
                self.result.metadata["legacy_role_page"] = {
                    "present": True,
                    "included_in_membership": False,
                    "probe_error": f"{type(exc).__name__}: {exc}",
                }
        else:
            self.result.metadata["legacy_role_page"] = {
                "present": False,
                "included_in_membership": False,
                "note": "The known 2024 unlinked /apprentice-leader/ page is no longer in the page sitemap.",
            }

        if has_listing_signal(parsed):
            self.result.errors.append("unreviewed_listing_signal_detected")
            self.result.metadata["detected_listing"] = signals
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["mu_sigma_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        if wp_jobs is not None:
            count = wp_jobs["parsed"]["count"]
            headers = wp_jobs["parsed"]["headers"]
            if headers["x_wp_total"] is not None and headers["x_wp_total"] != count:
                self.result.errors.append("mu_sigma_jobs_api_total_header_mismatch")
            if count:
                self.result.errors.append("unreviewed_jobs_api_entries")
                self.result.expected_total = count
                self.result.listing_complete = False
                self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
                self.result.timings["mu_sigma_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
                return self.result

        final_started = time.perf_counter()
        final_careers = final_jobs = final_sitemap = None
        try:
            final_careers = await self._careers()
            final_jobs = await self._wp_jobs()
            final_sitemap = await self._sitemap()
            self.result.metadata["final_recheck"] = {
                "careers": final_careers["meta"],
                "wp_jobs": {**final_jobs["meta"], "count": final_jobs["parsed"]["count"]},
                "page_sitemap": {**final_sitemap["meta"], "count": final_sitemap["parsed"]["count"],
                                 "role_pages": final_sitemap["parsed"]["role_pages"]},
            }
        except Exception as exc:
            self.result.errors.append(f"mu_sigma_final_recheck_failed: {type(exc).__name__}: {exc}")
        finally:
            self.result.timings["mu_sigma_final_recheck_seconds"] = max(0.0, time.perf_counter() - final_started)

        matches = False
        if (final_careers is not None and final_jobs is not None and final_sitemap is not None
                and wp_jobs is not None and sitemap is not None):
            final_parsed = final_careers["parsed"]
            final_signals = listing_signals(final_parsed)
            if has_listing_signal(final_parsed):
                self.result.errors.append("unreviewed_listing_signal_detected")
            if final_jobs["parsed"]["count"]:
                self.result.errors.append("unreviewed_jobs_api_entries")
            final_unreviewed = [
                row for row in final_sitemap["parsed"]["role_pages"]
                if row["url"] != LEGACY_ROLE_URL
            ]
            if final_unreviewed:
                self.result.errors.append("unreviewed_role_page_in_sitemap")
            matches = (
                _journey_evidence(parsed, wp_jobs["parsed"], sitemap["parsed"])
                == _journey_evidence(final_parsed, final_jobs["parsed"], final_sitemap["parsed"])
                and final_signals == signals
            )
            if not matches:
                self.result.errors.append("mu_sigma_careers_page_changed_between_requests")

        no_listing = (
            wp_jobs is not None
            and sitemap is not None
            and not has_listing_signal(parsed)
            and not (wp_jobs and wp_jobs["parsed"]["count"])
            and not unreviewed_role_pages
            and matches
        )
        if no_listing:
            self.result.errors.append("no_public_job_listing")

        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.jobs = []
        self.result.metadata["job_ids"] = []
        self.result.metadata["membership"] = {
            "advertised_jobs": None,
            "listing_rows": 0,
            "wp_jobs_collection_count": wp_jobs["parsed"]["count"] if wp_jobs else None,
            "unlinked_legacy_role_pages": [LEGACY_ROLE_URL] if legacy_present else [],
            "included_jobs": 0,
            "reconciled": no_listing,
            "note": (
                "Membership is empty by absence, not because a published listing was "
                "enumerated and found empty."
            ),
        }
        self.result.metadata["observed_journey"]["conclusion"] = (
            "The official careers page publishes no public job listing: no job cards, job "
            "links, listing request, filters, pagination, JSON-LD JobPosting, ATS embed or "
            "ATS link. The company's own wp/v2/jobs collection has zero published entries. "
            "The only role-like page is the 2024 unlinked /apprentice-leader/ page, recorded "
            "as excluded evidence. LinkedIn is an out-of-scope aggregator. The empty result "
            "is an explicit no-listing outcome."
        )
        self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.timings["mu_sigma_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
