"""Etsy Clinch careers journey — observed listing, pagination and detail bodies.

Official journey (re-inspected live 2026-11-08, AWS WAF cleared from this
egress; earlier 2026-11-07 attempts were HTTP 202 WAF-challenged):

- Ledger ``careers_url`` ``https://careers.etsy.com/jobs`` returns the Clinch
  404 not-found page (``<title>Etsy | Page not found</title>``). Its site
  navigation and the homepage both publish the global job search at
  ``https://careers.etsy.com/jobs/search``.
- ``GET /jobs/search`` returns 200 HTML (``<title>Search Page</title>``) with
  30 job cards per page. Each card is
  ``article.job-search-results-card-col`` containing an
  ``h3.card-title > a[href="/jobs/<slug>"]`` title link, component spans for
  workplace type / location / department / employment type
  (``.job-component-workplace-type span`` etc.), a truncated
  ``p.job-search-results-summary`` (never used as a description) and a
  "Read more" link to the same detail URL. Pagination is
  ``a.page-link[href="/jobs/search?page=N"]``; observed pages 1-3
  (30 + 30 + 9 = 69 cards). Filters in the search form (workplace type,
  department, employment type, country, city, keyword ``query``) are left
  unselected so the unfiltered global membership is collected.
- Detail ``GET /jobs/<slug>`` returns 200 HTML whose full body is the
  ``div.job-description`` element (all sections: Company Description,
  What's the Role?, team, day-to-day, qualifications, Compensation &
  Benefits incl. Salary Range, Location, What's Next, Our Promise). The
  same page carries a ``script[type="application/ld+json"]`` JobPosting
  block with ``title``, ``datePosted``, ``validThrough``,
  ``employmentType``, ``hiringOrganization.name`` ("Etsy"),
  ``identifier.value`` (the Clinch job uid, also in ``js_vars.job.uid``
  and the ``job-component-details-<uid>`` class) and ``jobLocation``
  address parts. The apply call-to-action is a JavaScript-loaded
  Turbo-Frame on the detail page itself
  (``.block-call-to-action[data-call-to-action--form-kind-value="apply_url"]``
  with ``turbo-frame[src="/pages/<page_id>/blocks/<block_id>?job_uid=<uid>"]``);
  static HTML exposes no separate apply URL, so the detail URL is the
  apply entry point.
- Static ``/sitemap.xml`` returned 200 with the same 69 ``/jobs/<slug>``
  URLs as the listing on 2026-11-08. The sitemap is still never used as
  membership authority: the paginated listing is. Sitemap agreement is
  recorded as a reconciliation check only; sitemap ``<lastmod>`` values
  are not posting dates.
- Plain HTTP is tried first. When a listing or detail request returns the
  HTTP 202 AWS WAF challenge (``awsWafCookieDomainList`` /
  ``token.awswaf.com`` / ``challenge-container``), the adapter falls back
  to a WAF-warmed headless Playwright Chromium session (one browser, one
  page, as in the Confluent adapter precedent): the browser fetches the
  exact same company-published URLs and every browser fetch is archived
  with ``fetcher="playwright-chromium"``. No manual CAPTCHA solving, no
  token replay, no ATS enumeration. If the browser also cannot obtain
  real content, collection stops for that scope and records the evidence.
"""
import asyncio
import gzip
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now, text_html

ENTRY_JOBS = "https://careers.etsy.com/jobs"
LISTING_URL = "https://careers.etsy.com/jobs/search"
SITEMAP_URL = "https://careers.etsy.com/sitemap.xml"
ROBOTS_URL = "https://careers.etsy.com/robots.txt"
HOST = "careers.etsy.com"

WAF_MARKERS = (
    "awsWafCookieDomainList",
    "gokuProps",
    "token.awswaf.com",
    "challenge-container",
    "AwsWafIntegration",
)

JOB_DETAIL_PREFIX = "https://careers.etsy.com/jobs/"
DETAIL_PATH = re.compile(r"^/jobs/(?!search(?:/|$))[^/]+$")
JOB_UID_RE = re.compile(r"js_vars\.job=\{\"uid\":\"([0-9a-f]{32})\"\}")
COMPONENT_UID_RE = re.compile(r"job-component-details-([0-9a-f]{32})")
PAGE_LINK_RE = re.compile(r"^/jobs/search\?page=(\d+)$")
SALARY_RE = re.compile(
    r"(Salary Range:\s*(?:•\s*Geo-\d:[^\n•]+)+)", re.I
)

EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": "Full-time",
    "PART_TIME": "Part-time",
    "CONTRACT": "Contract",
    "TEMPORARY": "Temporary",
    "INTERN": "Internship",
}


def is_waf_challenge(status, text):
    """True only for the observed AWS WAF JavaScript challenge page."""
    if int(status or 0) != 202:
        return False
    body = text or ""
    return (
        "awsWafCookieDomainList" in body
        and "token.awswaf.com" in body
        and "challenge-container" in body
    )


def waf_challenge_evidence(status, text, headers=None):
    """Return the exact markers found, for audit metadata."""
    headers = headers or {}
    body = text or ""
    return {
        "status": status,
        "markers": [marker for marker in WAF_MARKERS if marker in body],
        "waf_action": headers.get("x-amzn-waf-action") or headers.get("X-Amzn-Waf-Action"),
        "content_type": headers.get("content-type") or headers.get("Content-Type"),
    }


def parse_sitemap(xml_text):
    """Parse sitemap ``<loc>/<lastmod>`` pairs without trusting membership."""
    pairs = re.findall(
        r"<loc>\s*([^<]+?)\s*</loc>\s*<lastmod>\s*([^<]+?)\s*</lastmod>",
        xml_text or "",
        flags=re.S,
    )
    locs = [loc.strip() for loc, _ in pairs]
    if not locs:
        locs = [loc.strip() for loc in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml_text or "", flags=re.S)]
    jobs = [url for url in locs if url.startswith(JOB_DETAIL_PREFIX) and DETAIL_PATH.match(urlsplit(url).path)]
    non_jobs = [url for url in locs if url not in jobs]
    return {
        "locs": locs,
        "job_urls": jobs,
        "non_job_urls": non_jobs,
        "lastmod": {loc.strip(): date.strip() for loc, date in pairs},
    }


def _component_text(card, kind):
    node = card.select_one(f".job-component-{kind} span")
    return node.get_text(" ", strip=True) if node else None


def parse_listing_page(html, page_url=LISTING_URL):
    """Parse one observed Clinch listing page (cards + pagination).

    Cards are ``article.job-search-results-card-col``; the title link is
    ``h3.card-title > a`` (the first link to the detail URL). The card
    summary paragraph is a truncated preview and is returned only as
    ``summary_preview`` — never as a description.
    """
    soup = BeautifulSoup(html or "", "lxml")
    cards = []
    for article in soup.select("article.job-search-results-card-col"):
        title_link = article.select_one("h3.card-title a[href]")
        if title_link is None:
            continue
        href = urljoin(page_url, title_link.get("href", ""))
        if not DETAIL_PATH.match(urlsplit(href).path):
            continue
        summary = article.select_one("p.job-search-results-summary")
        uid_match = COMPONENT_UID_RE.search(str(article))
        cards.append({
            "title": title_link.get_text(" ", strip=True),
            "url": href,
            "job_id": uid_match.group(1) if uid_match else None,
            "workplace_type": _component_text(article, "workplace-type"),
            "location": _component_text(article, "location"),
            "department": _component_text(article, "department"),
            "employment_type": _component_text(article, "employment-type"),
            "summary_preview": summary.get_text(" ", strip=True) if summary else None,
        })
    page_numbers = []
    next_url = None
    for link in soup.select("a.page-link[href]"):
        href = link.get("href", "")
        match = PAGE_LINK_RE.match(href)
        if match:
            page_numbers.append(int(match.group(1)))
        if link.get_text(" ", strip=True) == "›" and href:
            next_url = urljoin(page_url, href)
    return {
        "cards": cards,
        "page_numbers": sorted(set(page_numbers)),
        "next_url": next_url,
    }


def _jsonld_jobposting(soup):
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.get_text() or "")
        except ValueError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _location_string(address):
    if not isinstance(address, dict):
        return None
    parts = [
        address.get("addressLocality"),
        address.get("addressRegion"),
        {"US": "United States", "GB": "United Kingdom", "IE": "Ireland", "MX": "Mexico"}.get(address.get("addressCountry"), address.get("addressCountry")),
    ]
    return ", ".join(part for part in parts if part) or None


def parse_detail_page(html, detail_url):
    """Parse one observed Clinch detail page.

    The full description is the ``div.job-description`` element; the
    JSON-LD JobPosting block is cross-checked against it (title, uid,
    dates, organisation, locations). Raises ValueError when neither a
    job-description body nor a JobPosting block is present, so callers
    never certify a stub as a full description.
    """
    soup = BeautifulSoup(html or "", "lxml")
    jsonld = _jsonld_jobposting(soup)
    body_node = soup.select_one(".job-description")
    title_node = soup.select_one(".job-title")
    title = title_node.get_text(" ", strip=True) if title_node else None
    if jsonld and jsonld.get("title"):
        title = title or jsonld["title"]
    if not title:
        raise ValueError("Etsy detail page has no job title")

    description_html = ""
    if body_node is not None:
        description_html = body_node.decode_contents().strip()
    elif jsonld and jsonld.get("description"):
        description_html = str(jsonld["description"])
    description_text = text_html(description_html)
    if len(description_text) < 100:
        raise ValueError("Etsy detail page has no full job-description body")
    jsonld_description_text = text_html(str(jsonld.get("description", ""))) if jsonld else ""
    description_matches_jsonld = bool(
        jsonld_description_text and description_text == jsonld_description_text
    )

    uid = None
    if jsonld and isinstance(jsonld.get("identifier"), dict):
        uid = jsonld["identifier"].get("value") or None
    if not uid:
        match = JOB_UID_RE.search(html or "") or COMPONENT_UID_RE.search(html or "")
        uid = match.group(1) if match else None

    job_locations = []
    if jsonld:
        raw_locations = jsonld.get("jobLocation") or []
        if isinstance(raw_locations, dict):
            raw_locations = [raw_locations]
        for place in raw_locations:
            if isinstance(place, dict):
                address = place.get("address") or {}
                label = _location_string(address)
                if label:
                    job_locations.append(label)
    detail_location = _component_text(soup, "location")
    workplace_type = _component_text(soup, "workplace-type")
    employment_type = _component_text(soup, "employment-type")
    if jsonld and jsonld.get("employmentType") and not employment_type:
        employment_type = EMPLOYMENT_TYPE_MAP.get(str(jsonld["employmentType"]).upper(), str(jsonld["employmentType"]))

    salary = None
    salary_match = SALARY_RE.search(description_text)
    if salary_match:
        salary = re.sub(r"\s+", " ", salary_match.group(1)).strip()

    employer = None
    if jsonld and isinstance(jsonld.get("hiringOrganization"), dict):
        employer = jsonld["hiringOrganization"].get("name") or None

    apply_frame = soup.select_one(
        '.block-call-to-action[data-call-to-action--form-kind-value="apply_url"] turbo-frame[src]'
    )
    apply_frame_src = urljoin(detail_url, apply_frame.get("src")) if apply_frame is not None else None

    return {
        "title": title,
        "job_id": str(uid) if uid else None,
        "description_html": description_html,
        "description_text": description_text,
        "description_matches_jsonld": description_matches_jsonld,
        "date_posted": jsonld.get("datePosted") if jsonld else None,
        "valid_through": jsonld.get("validThrough") if jsonld else None,
        "employment_type": employment_type,
        "workplace_type": workplace_type,
        "detail_location": detail_location,
        "job_locations": job_locations,
        "employer": employer,
        "salary": salary,
        "apply_frame_src": apply_frame_src,
        "jsonld": jsonld,
    }


def extract_preview_slugs(homepage_html):
    """Homepage teaser slugs (audit only; never membership)."""
    soup = BeautifulSoup(homepage_html or "", "lxml")
    cards = soup.select(".jobs-list .job a[href]")
    return sorted({a["href"] for a in cards if a.get("href", "").startswith(JOB_DETAIL_PREFIX)})


class Etsy(Adapter):
    """Follow Etsy's observed Clinch journey; HTTP first, WAF-warmed browser fallback."""

    VERIFICATION_REVISION = "etsy.clinch-listing-details-observed.4"

    def __init__(self, http, company, source, options, browser_fetcher=None):
        super().__init__(http, company, source, options)
        # An async callable (url) -> (html_text, final_url, http_status).
        # Defaults to a WAF-warmed Playwright session; tests inject fixtures.
        self.browser_fetcher = browser_fetcher
        self._browser_lock = asyncio.Lock()
        self._browser = None
        self._browser_pages_fetched = 0
        self._use_browser = False

    # -- archival (browser fetches share the HTTP archive schema) ---------
    def _archive_browser(self, *, url, final_url, status, content: bytes):
        output = getattr(self.http, "output", None)
        if not output:
            return None
        output = Path(output)
        (output / "responses").mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        target = output / "responses" / f"{digest}.gz"
        if not target.exists():
            target.write_bytes(gzip.compress(content))
        record = {
            "url": url, "final_url": final_url, "method": "GET",
            "request_body": None, "status": status, "sha256": digest,
            "content_type": "text/html", "redirects": [], "fetched_at": now(),
            "fetcher": "playwright-chromium",
        }
        with (output / "requests.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")
        return record

    # -- browser fetching ---------------------------------------------------
    async def _browser_fetch(self, url):
        """Fetch one company-published URL in the WAF-warmed browser session."""
        if self.browser_fetcher is not None:
            html, final_url, status = await self.browser_fetcher(url)
            self._browser_pages_fetched += 1
            self._archive_browser(url=url, final_url=final_url, status=status, content=html.encode())
            return html, final_url, status
        async with self._browser_lock:
            return await self._browser_fetch_locked(url)

    async def _browser_fetch_locked(self, url):
        from playwright.async_api import async_playwright

        if self._browser is None:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            )
            page = await ctx.new_page()
            self._browser = (pw, browser, page)
        pw, browser, page = self._browser
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        status = response.status if response is not None else None
        await page.wait_for_timeout(3000)
        try:
            await page.wait_for_selector(
                "article.job-search-results-card-col, .job-description", timeout=30000
            )
        except Exception:  # noqa: BLE001 - content check below decides
            pass
        html = await page.content()
        final_url = page.url
        self._browser_pages_fetched += 1
        self._archive_browser(url=url, final_url=final_url, status=status, content=html.encode())
        return html, final_url, status

    async def _close_browser(self):
        if self._browser is not None:
            pw, browser, page = self._browser
            self._browser = None
            try:
                await browser.close()
            finally:
                await pw.stop()

    async def _detail(self, job):
        if self._use_browser or getattr(self, "_detail_waf_failed", False):
            self._use_browser = True
            html, final_url, status = await self._browser_fetch(job.url)
            if is_waf_challenge(status, html) or "job-description" not in html:
                raise RuntimeError(
                    f"Etsy detail {job.url} browser fetch returned no job-description body "
                    f"(HTTP {status}); collection stopped for this job"
                )
            parsed = parse_detail_page(html, job.url)
        else:
            response = await self.http.get(job.url)
            if is_waf_challenge(response.status_code, response.text):
                self._detail_waf_failed = True
                self._use_browser = True
                self.result.warnings.append(
                    f"Etsy detail {job.url} HTTP returned AWS WAF challenge; "
                    "falling back to WAF-warmed browser for details"
                )
                html, final_url, status = await self._browser_fetch(job.url)
                if is_waf_challenge(status, html) or "job-description" not in html:
                    raise RuntimeError(
                        f"Etsy detail {job.url} returned HTTP 202 AWS WAF challenge and "
                        "browser fallback returned no job-description body; no challenge solving attempted"
                    )
                parsed = parse_detail_page(html, job.url)
            elif response.status_code != 200:
                raise RuntimeError(f"Etsy detail {job.url} returned HTTP {response.status_code}")
            else:
                parsed = parse_detail_page(response.text, job.url)
        raw = dict(job.raw or {})
        raw.update({
            "listing_location": (raw.get("listing") or {}).get("location"),
            "listing_department": (raw.get("listing") or {}).get("department"),
            "listing_workplace_type": (raw.get("listing") or {}).get("workplace_type"),
            "listing_employment_type": (raw.get("listing") or {}).get("employment_type"),
            "listing_summary_preview": (raw.get("listing") or {}).get("summary_preview"),
            "detail_location": parsed["detail_location"],
            "jsonld_job_locations": parsed["job_locations"],
            "jsonld_employer": parsed["employer"],
            "jsonld_valid_through": parsed["valid_through"],
            "description_matches_jsonld": parsed["description_matches_jsonld"],
            "apply_frame_src": parsed["apply_frame_src"],
            "apply_note": (
                "Apply call-to-action is a JavaScript-loaded Turbo-Frame on the "
                "detail page itself; static HTML exposes no separate apply URL, "
                "so the detail URL is the apply entry point."
            ),
        })
        locations = list(parsed["job_locations"])
        listing_location = (raw.get("listing") or {}).get("location")
        if listing_location and listing_location not in locations:
            locations.insert(0, listing_location)
        if parsed["detail_location"] and parsed["detail_location"] not in locations and not locations:
            locations.append(parsed["detail_location"])
        return self.job(
            raw,
            title=parsed["title"],
            url=job.url,
            job_id=parsed["job_id"],
            requisition_id=None,
            apply_url=job.url,
            description=parsed["description_html"],
            method="detail_html_.job-description",
            description_url=job.url,
            locations=locations,
            department=(raw.get("listing") or {}).get("department"),
            employment_type=parsed["employment_type"] or (raw.get("listing") or {}).get("employment_type"),
            workplace_type=parsed["workplace_type"] or (raw.get("listing") or {}).get("workplace_type"),
            date_posted=parsed["date_posted"],
            date_posted_raw=parsed["date_posted"],
            valid_through=parsed["valid_through"],
            salary=parsed["salary"],
            employer=parsed["employer"] or "Etsy",
            fetched_at=job.fetched_at,
        )

    async def run(self):
        started = self.result._started_clock
        if self.company.careers_url != ENTRY_JOBS:
            raise ValueError(
                "Etsy dedicated flow must start from its ledger entry "
                f"{ENTRY_JOBS} (observed Clinch 404 listing nav); got {self.company.careers_url!r}"
            )
        if not self.page_guard():
            return self.result

        entry_status = None
        entry_final_url = None
        entry_is_waf = False
        entry_404_not_found = False
        entry_nav_listing_urls = []

        # 1. Exact ledger entry. A Clinch 404 raises FetchError in the shared
        # HTTP client; record it and continue to the observed listing URL
        # published by that page's navigation.
        try:
            entry = await self.http.get(ENTRY_JOBS)
            entry_status = entry.status_code
            entry_final_url = str(entry.url)
            entry_is_waf = is_waf_challenge(entry.status_code, entry.text)
            if entry_is_waf:
                self.result.warnings.append(
                    f"Etsy entry {ENTRY_JOBS} returned HTTP 202 AWS WAF challenge "
                    "(x-amzn-waf-action: challenge); no challenge solving attempted; "
                    "listing is the membership authority"
                )
            elif entry.status_code != 200:
                self.result.warnings.append(
                    f"Etsy entry {ENTRY_JOBS} returned HTTP {entry.status_code}"
                )
            else:
                soup = BeautifulSoup(entry.text, "lxml")
                title = soup.title.get_text(strip=True) if soup.title else ""
                if title == "Etsy | Page not found":
                    entry_404_not_found = True
                    self.result.warnings.append(
                        f"Etsy entry {ENTRY_JOBS} is the Clinch 404 not-found page; "
                        f"official listing is {LISTING_URL} per that page's nav"
                    )
                hrefs = sorted({a["href"] for a in soup.select("a[href]") if a.get("href")})
                entry_nav_listing_urls = [
                    h for h in hrefs if h in {"/jobs/search", "/jobs/search/engineering"}
                ]
        except Exception as exc:  # noqa: BLE001 - FetchError on 404 is expected evidence
            message = f"{type(exc).__name__}: {exc}"
            entry_status = 404 if "404" in message else None
            if "404" in message:
                entry_404_not_found = True
            self.result.warnings.append(
                f"Etsy entry {ENTRY_JOBS} fetch failed; site nav publishes "
                f"listing {LISTING_URL}: {message}"
            )

        # 2. The observed global listing, following its own pagination.
        listing_entries = []
        seen_urls = set()
        listing_pages = []
        waf_stop = None
        listing_complete = False
        next_url = LISTING_URL
        visited = set()
        while next_url and next_url not in visited:
            visited.add(next_url)
            listing_status = None
            listing_text = ""
            listing_sha = None
            via_browser = False
            try:
                if self._use_browser:
                    listing_text, _final, listing_status = await self._browser_fetch(next_url)
                    via_browser = True
                    listing_sha = hashlib.sha256(listing_text.encode()).hexdigest()
                else:
                    listing = await self.http.get(next_url)
                    listing_status = listing.status_code
                    listing_text = listing.text
                    listing_sha = hashlib.sha256(listing.content).hexdigest()
                    if is_waf_challenge(listing_status, listing_text):
                        self.result.warnings.append(
                            f"Etsy listing {next_url} HTTP returned AWS WAF challenge; "
                            "falling back to WAF-warmed browser for the listing"
                        )
                        self._use_browser = True
                        listing_text, _final, listing_status = await self._browser_fetch(next_url)
                        via_browser = True
                        listing_sha = hashlib.sha256(listing_text.encode()).hexdigest()
            except Exception as exc:  # noqa: BLE001
                waf_stop = f"Etsy listing {next_url} fetch failed: {type(exc).__name__}: {exc}"
                self.result.errors.append(waf_stop)
                break
            if is_waf_challenge(listing_status, listing_text):
                waf_stop = (
                    f"Etsy listing {next_url} returned HTTP 202 AWS WAF challenge "
                    "(awsWafCookieDomainList/token.awswaf.com/challenge-container); "
                    "membership, pagination and filters unknown; collection stopped"
                )
                self.result.errors.append(waf_stop)
                break
            if listing_status not in (200, 202):
                waf_stop = (
                    f"Etsy listing {next_url} returned HTTP {listing_status}; "
                    "membership unknown; collection stopped"
                )
                self.result.errors.append(waf_stop)
                break
            parsed = parse_listing_page(listing_text, next_url)
            if via_browser and not parsed["cards"]:
                waf_stop = (
                    f"Etsy listing {next_url} browser fetch (HTTP {listing_status}) returned "
                    "no job cards; membership unknown; collection stopped"
                )
                self.result.errors.append(waf_stop)
                break
            listing_pages.append({
                "url": next_url,
                "status": listing_status,
                "sha256": listing_sha,
                "cards": len(parsed["cards"]),
                "page_numbers": parsed["page_numbers"],
                "via_browser": via_browser,
            })
            if not parsed["cards"]:
                waf_stop = f"Etsy listing {next_url} returned 200 with no job cards; pagination structure changed, collection stopped"
                self.result.errors.append(waf_stop)
                break
            for card in parsed["cards"]:
                if card["url"] in seen_urls:
                    self.result.warnings.append(f"duplicate_listing_card:{card['url']}")
                    continue
                seen_urls.add(card["url"])
                listing_entries.append(card)
                if self.options.max_jobs and len(listing_entries) >= self.options.max_jobs:
                    self.result.warnings.append("sample_limit")
                    break
            next_url = parsed["next_url"]
            if self.options.max_jobs and len(listing_entries) >= self.options.max_jobs:
                break
        else:
            listing_complete = True
        if not listing_entries and not waf_stop:
            listing_complete = False

        # 3. Static sitemap for reconciliation only — never membership.
        sitemap_status = None
        sitemap_sha = None
        sitemap_parsed = {"locs": [], "job_urls": [], "non_job_urls": [], "lastmod": {}}
        try:
            sitemap = await self.http.get(SITEMAP_URL)
            sitemap_status = sitemap.status_code
            sitemap_sha = hashlib.sha256(sitemap.content).hexdigest()
            if sitemap.status_code != 200 or is_waf_challenge(sitemap.status_code, sitemap.text):
                self.result.warnings.append(
                    f"Etsy sitemap {SITEMAP_URL} returned HTTP {sitemap.status_code}; "
                    "reconciliation unavailable (listing remains membership authority)"
                )
            else:
                sitemap_parsed = parse_sitemap(sitemap.text)
                if not sitemap_parsed["locs"]:
                    self.result.warnings.append(
                        f"Etsy sitemap {SITEMAP_URL} contained no <loc> entries"
                    )
        except Exception as exc:  # noqa: BLE001
            self.result.warnings.append(
                f"Etsy sitemap {SITEMAP_URL} fetch failed: {type(exc).__name__}: {exc}"
            )
        listing_urls = sorted(seen_urls)
        sitemap_job_urls = sorted(sitemap_parsed["job_urls"])
        sitemap_delta = {
            "listing_only": sorted(set(listing_urls) - set(sitemap_job_urls)),
            "sitemap_only": sorted(set(sitemap_job_urls) - set(listing_urls)),
        }

        # 4. Build listing jobs and hydrate every detail body.
        jobs = []
        for card in listing_entries:
            jobs.append(self.job(
                {"listing": card},
                title=card["title"],
                url=card["url"],
                job_id=card.get("job_id"),
                locations=[card["location"]] if card["location"] else [],
                department=card["department"],
                employment_type=card["employment_type"],
                workplace_type=card["workplace_type"],
            ))
        if listing_entries:
            await self.hydrate(jobs, self._detail)
            for job in jobs:
                self.add(job)

        self.result.pages = len(listing_pages) or 1
        self.result.expected_total = len(listing_entries) if listing_complete else None
        self.result.listing_complete = listing_complete
        if listing_complete and sitemap_delta["listing_only"] or listing_complete and sitemap_delta["sitemap_only"]:
            self.result.warnings.append(
                "sitemap_delta_vs_listing: listing remains membership authority; "
                f"listing_only={len(sitemap_delta['listing_only'])} sitemap_only={len(sitemap_delta['sitemap_only'])}"
            )
        self.result.metadata.update(
            {
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": ENTRY_JOBS,
                "observed_listing_url": LISTING_URL,
                "membership_authority": (
                    "Paginated Clinch listing /jobs/search (article.job-search-results-card-col "
                    "cards, a.page-link pagination). Listing reconciled against sitemap candidates; "
                    "sitemap is not membership authority."
                ),
                "entry_status": entry_status,
                "entry_final_url": entry_final_url,
                "entry_is_waf_challenge": entry_is_waf,
                "entry_404_not_found": entry_404_not_found,
                "entry_nav_listing_urls": entry_nav_listing_urls,
                "listing_pages": listing_pages,
                "listing_status": listing_pages[0]["status"] if listing_pages else None,
                "listing_is_waf_challenge": bool(waf_stop and "WAF challenge" in waf_stop),
                "waf_stop": waf_stop,
                "listing_selector": "article.job-search-results-card-col h3.card-title a[href]",
                "pagination_selector": "a.page-link[href^='/jobs/search?page=']",
                "detail_selector": ".job-description",
                "detail_jsonld_selector": "script[type='application/ld+json'] JobPosting",
                "sitemap_url": SITEMAP_URL,
                "sitemap_status": sitemap_status,
                "sitemap_sha256": sitemap_sha,
                "sitemap_total_locs": len(sitemap_parsed["locs"]),
                "sitemap_job_candidates": len(sitemap_parsed["job_urls"]),
                "sitemap_reconciliation": sitemap_delta,
                "sitemap_lastmod_is_not_posting_date": True,
                "listing_html_inspected": bool(listing_pages),
                "detail_html_inspected": bool(jobs),
                "live_detail_bodies_inspected": len(jobs),
                "pagination_inspected": bool(listing_pages),
                "filters_inspected": "search form filters observed (workplace type, department, employment type, country, city, query); none applied",
                "homepage_preview_collected_as_jobs": False,
                "ats_enumeration_used": False,
                "browser_fetcher_used": self._browser_pages_fetched > 0,
                "browser_pages_fetched": self._browser_pages_fetched,
                "detail_requests_made": (self.result.metadata.get("details") or {}).get("fetched_jobs", len(jobs)),
                "independent_browser_comparison": (
                    "See companies/etsy/browser-reference-worker.json (headless "
                    "Playwright listing + first/last detail comparison)."
                ),
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
            }
        )
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata[
            "collection_wall_seconds"
        ]
        await self._close_browser()
        return self.result
