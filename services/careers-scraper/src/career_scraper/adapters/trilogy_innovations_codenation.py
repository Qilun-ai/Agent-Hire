"""Trilogy Innovations (ledger label "Trilogy Innovations (Codenation)") — official
hiring journey, reviewed 2026-11-08.

Scope correction (this module supersedes the 2026-11-08 review of the ledger URL):
the ledger's ``https://www.trilogyinnovations.com/careers`` belongs to *Trilogy
Leasing Co., LLC*, a Seminole FL federal IT reseller that never mentions
CodeNation.  The requested entity is Trilogy Innovations (formerly CodeNation
Innovation Labs, Bengaluru).  Its official site is
``https://innovations.trilogy.com/`` — the legacy ``codenation.co.in`` domain
301-redirects there — and its published hiring entry is
``https://innovations.trilogy.com/join-trilogy-innovations/`` ("Join TI:
Hiring Methodology, Hiring Events, FAQs").

What that journey publishes (inspected 2026-11-08):

* the Join TI landing page is a stub: ``div.page-content`` is empty, the page is
  global navigation (mega-menu) plus footer;
* the three hiring sub-pages describe a hiring *process*, not vacancies;
* the only hiring action anywhere on the site is a stale "CodeAgon 2021"
  InterviewBit contest link on the Hiring Events page;
* the WordPress sitemap (``wp-sitemap.xml``, the only sitemap in ``robots.txt``)
  lists 18 URLs (12 pages, 2 posts, 2 categories, 2 authors); every one was
  scanned: 0 job cards, 0 job/ATS links, 0 JSON-LD ``JobPosting``, 0 pagination;
* WordPress registers no job post type (``/wp-json/wp/v2/types``) and
  ``/wp-json/wp/v2/jobs`` / ``job_listing`` are 404; ``/careers/``, ``/jobs/``,
  ``/openings/`` and five other listing routes are 404;
* the browser issues no listing XHR (only invisible reCAPTCHA on the contact
  form; no challenge was presented, nothing was solved).

So the official journey publishes **no current open-roles listing**.  This
adapter records that state explicitly, keeps ``expected_total`` null, and never
substitutes a guessed ATS slug, the parent-wide ``trilogy.com`` →
``crossover.com`` board, ``codenation.com`` (Cloudflare 403, not bypassed) or an
aggregator.  If the company later publishes a listing, the adapter fails with
``unreviewed_listing_detected`` so the new journey is inspected first.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import now


COMPANY_LABEL = "Trilogy Innovations (Codenation)"
OFFICIAL_SITE = "https://innovations.trilogy.com"
OFFICIAL_ENTRY = f"{OFFICIAL_SITE}/join-trilogy-innovations/"
HIRING_SUBPAGES = {
    "hiring-methodology": f"{OFFICIAL_ENTRY}hiring-methodology/",
    "hiring-events": f"{OFFICIAL_ENTRY}hiring-events/",
    "faqs": f"{OFFICIAL_ENTRY}faqs/",
}
LEGACY_ENTRY = "https://codenation.co.in/"
SITEMAP_INDEX = f"{OFFICIAL_SITE}/wp-sitemap.xml"
SITEMAP_CHILDREN = {
    "post": f"{OFFICIAL_SITE}/wp-sitemap-posts-post-1.xml",
    "page": f"{OFFICIAL_SITE}/wp-sitemap-posts-page-1.xml",
    "category": f"{OFFICIAL_SITE}/wp-sitemap-taxonomies-category-1.xml",
    "user": f"{OFFICIAL_SITE}/wp-sitemap-users-1.xml",
}
SITEMAP_EXPECTED_TOTAL = 18
MAX_SCAN_PAGES = 30

WP_TYPES = f"{OFFICIAL_SITE}/wp-json/wp/v2/types"
WP_JOBS = f"{OFFICIAL_SITE}/wp-json/wp/v2/jobs"
WP_JOB_LISTING = f"{OFFICIAL_SITE}/wp-json/wp/v2/job_listing"
WP_SEARCH_JOB = f"{OFFICIAL_SITE}/wp-json/wp/v2/search?search=job"
LISTING_ROUTE_PROBES = [
    f"{OFFICIAL_SITE}/careers/",
    f"{OFFICIAL_SITE}/jobs/",
    f"{OFFICIAL_SITE}/job-openings/",
    f"{OFFICIAL_SITE}/openings/",
    f"{OFFICIAL_SITE}/join-us/",
    f"{OFFICIAL_SITE}/work-with-us/",
    f"{OFFICIAL_SITE}/apply/",
]

OFFICIAL_HOSTS = {"innovations.trilogy.com", "www.innovations.trilogy.com"}
ENTRY_PATH = "/join-trilogy-innovations/"
EXPECTED_TITLE = "Join Trilogy Innovations \u2013 Trilogy Innovations"
HIRING_CONTACT = "startgreat@trilogy.com"
CONTACT_PHONE = "+91 80 4216 0164"
OFFICE_CITY = "Bengaluru"
IDENTITY_PHRASE = "Trilogy Innovations"
# The only hiring call-to-action on the site, on the Hiring Events page.
STALE_CONTEST_LABEL = "CodeAgon 2021"
STALE_CONTEST_HOST = "www.interviewbit.com"
REVIEW_DATE = "2026-11-08"

# Hosts that would indicate a real external job board/ATS embed.  None appears on
# the site; the list exists so a future change is detected and reviewed instead
# of being silently scraped.  InterviewBit/HackerEarth/Unstop are contest
# platforms, not job boards, and are classified as hiring-event links.
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
    r"phenompeople\.com|eightfold\.ai|crossover\.com"
    r")$"
)
EVENT_HOST = re.compile(r"(?i)(?:^|\.)(?:interviewbit\.com|hackerearth\.com|unstop\.com|"
                        r"codingninjas\.com|codechef\.com|hackerrank\.com)$")
JOB_PATH = re.compile(
    r"(?i)(?:^|/)(?:job|jobs|job-|position|positions|opening|openings|"
    r"vacanc|vacancy|apply)(?:/|$|[?#])"
)
ROLE_PATH = re.compile(r"^/(?:job|jobs|position|positions|opening|openings|careers?)/"
                       r"[a-z0-9][a-z0-9_-]{2,}/?$", re.I)
PAGINATION_TEXT = re.compile(r"(?i)^(?:next|previous|prev|load more|show more|page \d+)$")
SITEMAP_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I)
JOB_POST_TYPE = re.compile(r"(?i)(?:job|jobs|position|vacanc|career|opening)")


def normalize_text(value: str | None) -> str:
    value = (value or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", value).strip()


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def page_content_text(html: str) -> str:
    """Text of the WordPress page body (``div.page-content``), excluding chrome."""
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("div.page-content")
    if node is None:
        return ""
    for child in node(["script", "style", "noscript"]):
        child.decompose()
    return normalize_text(node.get_text(" ", strip=True))


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


def sitemap_locs(xml: str) -> list[str]:
    return [normalize_text(item) for item in SITEMAP_LOC.findall(xml or "") if normalize_text(item)]


def _absolute(response_url: str, href: str) -> str:
    absolute = urljoin(response_url, (href or "").strip())
    return re.sub(r"^mailto:\s+", "mailto:", absolute)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _path(url: str) -> str:
    return urlsplit(url).path or "/"


def _year_in(*values: str) -> int | None:
    for value in values:
        match = re.search(r"(?<!\d)(20\d{2})(?!\d)", value or "")
        if match:
            return int(match.group(1))
    return None


def classify_link(anchor, response_url: str) -> dict:
    """Classify one anchor: ats / event / jobish / plain, with its absolute URL."""
    href = (anchor.get("href") or "").strip()
    absolute = _absolute(response_url, href)
    return {
        "text": normalize_text(anchor.get_text(" ", strip=True)),
        "href": href,
        "url": absolute,
        "host": _host(absolute),
        "path": _path(absolute),
    }


def parse_hiring_page(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Parse the "Join TI" entry and describe every listing/hiring signal.

    Raises ``ValueError`` when the response is no longer the inspected Join TI
    page, so a redirect or redesign is reported rather than treated as an empty
    listing.
    """
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or _host(response_url) not in OFFICIAL_HOSTS
            or _path(response_url).rstrip("/") != ENTRY_PATH.rstrip("/")):
        raise ValueError(f"Trilogy Innovations hiring entry redirected to an unexpected URL: {response_url}")

    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != EXPECTED_TITLE:
        raise ValueError(f"Trilogy Innovations hiring page title changed: {title!r}")

    page_text = visible_text(html)
    body_text = normalize_text(page_text)
    for phrase, label in ((IDENTITY_PHRASE, "company identity"),
                          (OFFICE_CITY, "office city"),
                          (CONTACT_PHONE, "contact phone"),
                          (HIRING_CONTACT, "hiring contact")):
        if phrase not in body_text:
            raise ValueError(f"Trilogy Innovations hiring page no longer contains its {label} phrase")

    headings = [normalize_text(node.get_text(" ", strip=True))
                for node in soup.select("h1,h2,h3")
                if normalize_text(node.get_text(" ", strip=True))]
    anchors, ats_links, jobish_links, event_links, mailto_links = [], [], [], [], []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if href.startswith("mailto:"):
            mailto_links.append(_absolute(response_url, href))
            continue
        if not href or href.startswith(("#", "javascript:", "tel:")):
            continue
        item = classify_link(anchor, response_url)
        anchors.append(item)
        if ATS_HOST.search(item["host"]):
            ats_links.append(item)
        elif EVENT_HOST.search(item["host"]):
            event_links.append({**item, "year": _year_in(item["text"], item["url"]),
                                "stale": (_year_in(item["text"], item["url"]) or 9999) < int(REVIEW_DATE[:4])})
        elif JOB_PATH.search(item["path"]) or ROLE_PATH.match(item["path"]):
            jobish_links.append(item)

    ats_embeds = []
    for tag in soup.select("iframe[src], script[src], form[action], link[href]"):
        attribute = next((name for name in ("src", "action", "href") if tag.get(name)), None)
        if attribute is None:
            continue
        absolute = _absolute(response_url, tag.get(attribute))
        if ATS_HOST.search(_host(absolute)):
            ats_embeds.append({"tag": tag.name, "attribute": attribute, "url": absolute})

    pagination_controls = [text for text in
                           (normalize_text(element.get_text(" ", strip=True))
                            for element in soup.select("a,button"))
                           if text and PAGINATION_TEXT.match(text)]
    content_text = page_content_text(html)
    hiring_contacts = [url for url in mailto_links
                       if re.search(r"(?i)(career|job|hiring|recruit|startgreat|hr@)", url)]

    return {
        "response_url": response_url,
        "title": title,
        "headings": headings,
        "anchor_count": len(anchors),
        "anchors": anchors,
        "jobish_links": jobish_links,
        "external_ats_links": ats_links,
        "ats_embeds": ats_embeds,
        "hiring_event_links": event_links,
        "job_posting_schema_count": job_posting_schema_count(soup),
        "pagination_controls": pagination_controls,
        "mailto_links": mailto_links,
        "hiring_contacts": hiring_contacts,
        "page_content_chars": len(content_text),
        "page_content_text": content_text,
        "landing_stub": not content_text,
        "visible_text_chars": len(page_text),
        "normalized_text_chars": len(body_text),
        "normalized_text_sha256": sha256(body_text.encode()).hexdigest(),
        "office_city_present": OFFICE_CITY in body_text,
        "hiring_contact_present": HIRING_CONTACT in body_text,
    }


def scan_page(html: str, response_url: str) -> dict:
    """Describe every listing signal on one site page (used by the site-wide audit)."""
    soup = BeautifulSoup(html, "lxml")
    text = visible_text(html)
    normalized = normalize_text(text)
    jobish, ats_links, event_links = [], [], []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        item = classify_link(anchor, response_url)
        if ATS_HOST.search(item["host"]):
            ats_links.append({"text": item["text"], "url": item["url"]})
        elif EVENT_HOST.search(item["host"]):
            event_links.append({"text": item["text"], "url": item["url"],
                                "year": _year_in(item["text"], item["url"])})
        elif JOB_PATH.search(item["path"]) or ROLE_PATH.match(item["path"]):
            jobish.append({"text": item["text"], "url": item["url"]})
    ats_embeds = []
    for tag in soup.select("iframe[src], script[src], form[action], link[href]"):
        attribute = next((name for name in ("src", "action", "href") if tag.get(name)), None)
        if attribute is None:
            continue
        absolute = _absolute(response_url, tag.get(attribute))
        if ATS_HOST.search(_host(absolute)):
            ats_embeds.append({"tag": tag.name, "attribute": attribute, "url": absolute})
    content_text = page_content_text(html)
    return {
        "url": response_url,
        "title": normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "jobish_links": [item for item in jobish
                         if _path(item["url"]).rstrip("/") != ENTRY_PATH.rstrip("/")],
        "ats_links": ats_links,
        "ats_embeds": ats_embeds,
        "hiring_event_links": event_links,
        "job_posting_schema_count": job_posting_schema_count(soup),
        "page_content_chars": len(content_text),
        "form_count": len(soup.select("form")),
        "visible_text_chars": len(text),
        "normalized_text_sha256": sha256(normalized.encode()).hexdigest(),
    }


def has_listing_signal(parsed: dict) -> bool:
    return bool(
        parsed["jobish_links"]
        or parsed["external_ats_links"]
        or parsed["ats_embeds"]
        or parsed["job_posting_schema_count"]
    )


def scan_has_listing_signal(page: dict) -> bool:
    return bool(page.get("jobish_links") or page.get("ats_links")
                or page.get("ats_embeds") or page.get("job_posting_schema_count"))


def _evidence(parsed: dict) -> dict:
    return {
        "title": parsed["title"],
        "headings": parsed["headings"],
        "jobish_urls": [item["url"] for item in parsed["jobish_links"]],
        "ats_urls": [item["url"] for item in parsed["external_ats_links"]],
        "ats_embed_urls": [item["url"] for item in parsed["ats_embeds"]],
        "event_urls": [item["url"] for item in parsed["hiring_event_links"]],
        "job_posting_schema_count": parsed["job_posting_schema_count"],
        "pagination_controls": parsed["pagination_controls"],
        "page_content_chars": parsed["page_content_chars"],
        "normalized_text_sha256": parsed["normalized_text_sha256"],
    }


def _response_summary(response, requested_url: str, *, started: float) -> dict:
    return {
        "requested_url": requested_url,
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
        "duration_seconds": round(max(0.0, time.perf_counter() - started), 6),
    }


def _status_from_error(exc: Exception) -> int | None:
    match = re.search(r"HTTP (\d{3})", str(exc))
    return int(match.group(1)) if match else None


class TrilogyInnovationsCodenation(Adapter):
    """Inspect the official Trilogy Innovations hiring journey and record its listing state."""

    async def audit_site(self) -> dict:
        """Scan every URL the official WordPress sitemap publishes for job/ATS signals."""
        started = time.perf_counter()
        summary = {
            "sitemap_index": SITEMAP_INDEX,
            "sitemap_children": {},
            "pages": [],
            "failed_pages": [],
            "listing_signals": [],
            "hiring_event_links": [],
            "external_hosts": [],
            "pages_checked": 0,
            "urls_available": 0,
            "duration_seconds": 0.0,
        }
        try:
            index_response = await self.http.get(SITEMAP_INDEX)
            children = sitemap_locs(index_response.text)
            summary["sitemap_children"]["index"] = {"url": SITEMAP_INDEX, "loc_count": len(children),
                                                   "locs": children}
        except Exception as exc:  # noqa: BLE001
            summary["failed_pages"].append({"url": SITEMAP_INDEX, "reason": f"{type(exc).__name__}: {exc}"})
            summary["duration_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
            return summary

        urls: list[str] = []
        for name, url in SITEMAP_CHILDREN.items():
            try:
                response = await self.http.get(url)
                locs = sitemap_locs(response.text)
            except Exception as exc:  # noqa: BLE001
                summary["failed_pages"].append({"url": url, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            summary["sitemap_children"][name] = {"url": url, "loc_count": len(locs), "locs": locs}
            urls.extend(locs)

        summary["urls_available"] = len(urls)
        hosts: set[str] = set()
        for url in urls[:MAX_SCAN_PAGES]:
            if not self.page_guard():
                summary["failed_pages"].append({"url": url, "reason": "page_budget_exhausted"})
                break
            try:
                response = await self.http.get(url)
                page = scan_page(response.text, str(response.url))
            except Exception as exc:  # noqa: BLE001
                summary["failed_pages"].append({"url": url, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            summary["pages_checked"] += 1
            summary["pages"].append({
                "url": url,
                "final_url": page["url"],
                "title": page["title"],
                "jobish_links": page["jobish_links"],
                "ats_links": page["ats_links"],
                "ats_embeds": page["ats_embeds"],
                "hiring_event_links": page["hiring_event_links"],
                "job_posting_schema_count": page["job_posting_schema_count"],
                "page_content_chars": page["page_content_chars"],
                "form_count": page["form_count"],
                "normalized_text_sha256": page["normalized_text_sha256"],
            })
            for item in page["jobish_links"] + page["ats_links"]:
                hosts.add(_host(item["url"]))
            summary["hiring_event_links"].extend(
                {**item, "page": url} for item in page["hiring_event_links"])
            if scan_has_listing_signal(page):
                summary["listing_signals"].append({
                    "page": url,
                    "jobish_links": page["jobish_links"],
                    "ats_links": page["ats_links"],
                    "ats_embeds": page["ats_embeds"],
                    "job_posting_schema_count": page["job_posting_schema_count"],
                })

        summary["external_hosts"] = sorted(hosts)
        summary["duration_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        return summary

    async def check_rest_api(self) -> dict:
        """Inspect the WordPress REST API: no job post type, no job collection."""
        report: dict = {"types_url": WP_TYPES, "job_post_types": [], "errors": []}
        if not self.page_guard():
            return report
        try:
            response = await self.http.get(WP_TYPES)
            types = response.json()
            report["post_types"] = sorted(types) if isinstance(types, dict) else []
            report["job_post_types"] = [name for name in report["post_types"]
                                        if name not in ("post", "page", "attachment")
                                        and JOB_POST_TYPE.search(name)]
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"{WP_TYPES}: {type(exc).__name__}: {exc}")
        for url in (WP_JOBS, WP_JOB_LISTING):
            if not self.page_guard():
                break
            try:
                response = await self.http.get(url)
                report[url] = {"status_code": response.status_code}
                if response.status_code == 200:
                    report["errors"].append(f"{url} unexpectedly returned 200")
            except FetchError as exc:
                report[url] = {"status_code": _status_from_error(exc), "error": str(exc)}
        if not self.page_guard():
            return report
        try:
            response = await self.http.get(WP_SEARCH_JOB)
            results = response.json() if isinstance(response.json(), list) else []
            report["search_job"] = [{"title": (item.get("title") or ""),
                                     "url": item.get("url"), "subtype": item.get("subtype")}
                                    for item in results]
            report["search_job_listing_hits"] = [
                item for item in report["search_job"]
                if JOB_PATH.search(_path(str(item.get("url") or "")))]
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"{WP_SEARCH_JOB}: {type(exc).__name__}: {exc}")
        return report

    async def check_legacy_domain(self) -> dict:
        """Verify the legacy codenation.co.in domain still resolves to the official site."""
        started = time.perf_counter()
        report = {"url": LEGACY_ENTRY, "final_url": None, "status_code": None,
                  "redirects": [], "resolves_to_official_site": False}
        if not self.page_guard():
            return report
        try:
            response = await self.http.get(LEGACY_ENTRY)
            report.update({
                "status_code": response.status_code,
                "final_url": str(response.url),
                "redirects": [{"status": item.status_code, "url": str(item.url),
                               "location": item.headers.get("location")}
                              for item in response.history],
                "resolves_to_official_site": _host(str(response.url)) in OFFICIAL_HOSTS,
            })
        except Exception as exc:  # noqa: BLE001
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            report["duration_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        return report

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "official_site": OFFICIAL_SITE,
            "legacy_domain": LEGACY_ENTRY,
            "hiring_subpages": HIRING_SUBPAGES,
            "scope_reason": (
                "identified through the legacy codenation.co.in domain, which 301-redirects to "
                "https://innovations.trilogy.com/, and through the site's own 'Join TI' navigation and "
                "wp-sitemap.xml; the previously reviewed www.trilogyinnovations.com is a different "
                "company (Trilogy Leasing Co., LLC, Seminole FL)"
            ),
            "hiring_contact": HIRING_CONTACT,
            "office_city": OFFICE_CITY,
            "listing_authority": "none: the official Join TI pages publish no open-roles listing",
            "detail_method": "none: no role or detail route is published",
            "request_scope": (
                "Official Trilogy Innovations site only (Join TI entry, its hiring sub-pages, every URL "
                "in wp-sitemap.xml, WordPress REST endpoints and eight listing-route probes); the legacy "
                "codenation.co.in redirect is followed to prove identity. No guessed ATS slug, no "
                "parent-wide trilogy.com/crossover board, no codenation.com and no aggregator is queried."
            ),
        })

        legacy = await self.check_legacy_domain()
        self.result.metadata["legacy_domain_check"] = legacy
        if not legacy.get("resolves_to_official_site"):
            self.result.warnings.append("legacy_domain_not_resolving_to_official_site")

        try:
            self.page_guard()
            entry_started = time.perf_counter()
            entry_response = await self.http.get(OFFICIAL_ENTRY)
            initial_entry = _response_summary(entry_response, OFFICIAL_ENTRY, started=entry_started)
            initial_parsed = parse_hiring_page(entry_response.text, str(entry_response.url))
        except Exception as exc:  # noqa: BLE001
            self.result.errors.append(f"trilogy_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["trilogy_source_collection_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6)
            return self.result

        self.result.metadata["initial_entry"] = initial_entry
        self.result.metadata["observed_journey"] = {
            "page_title": initial_parsed["title"],
            "headings": initial_parsed["headings"],
            "anchor_count": initial_parsed["anchor_count"],
            "job_listing_candidates": initial_parsed["jobish_links"],
            "external_ats_links": initial_parsed["external_ats_links"],
            "ats_embeds": initial_parsed["ats_embeds"],
            "hiring_event_links": initial_parsed["hiring_event_links"],
            "job_posting_schema_count": initial_parsed["job_posting_schema_count"],
            "pagination_controls": initial_parsed["pagination_controls"],
            "hiring_contacts": initial_parsed["hiring_contacts"],
            "mailto_links": initial_parsed["mailto_links"],
            "page_content_chars": initial_parsed["page_content_chars"],
            "landing_stub": initial_parsed["landing_stub"],
            "office_city_present": initial_parsed["office_city_present"],
            "hiring_contact_present": initial_parsed["hiring_contact_present"],
            "visible_text_chars": initial_parsed["visible_text_chars"],
            "normalized_text_chars": initial_parsed["normalized_text_chars"],
            "normalized_text_sha256": initial_parsed["normalized_text_sha256"],
        }

        if has_listing_signal(initial_parsed):
            self.result.errors.append("unreviewed_listing_detected")
            self.result.listing_complete = False
            self.result.metadata["detected_listing"] = {
                "job_listing_candidates": initial_parsed["jobish_links"],
                "external_ats_links": initial_parsed["external_ats_links"],
                "ats_embeds": initial_parsed["ats_embeds"],
                "job_posting_schema_count": initial_parsed["job_posting_schema_count"],
            }
            self.result.timings["trilogy_source_collection_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6)
            return self.result

        site_scan = await self.audit_site()
        self.result.metadata["site_scan"] = site_scan
        self.result.timings["trilogy_site_scan_seconds"] = site_scan["duration_seconds"]
        if site_scan["listing_signals"]:
            self.result.errors.append("unreviewed_site_listing_detected")
        for failed in site_scan["failed_pages"]:
            self.result.errors.append(f"trilogy_site_scan_page_failed: {failed['url']}: {failed['reason']}")
        if site_scan["failed_pages"]:
            self.result.errors.append("trilogy_site_scan_incomplete")
        if site_scan["urls_available"] != SITEMAP_EXPECTED_TOTAL:
            self.result.warnings.append(f"sitemap_url_count_changed:{site_scan['urls_available']}")

        route_started = time.perf_counter()
        route_probes = []
        for url in LISTING_ROUTE_PROBES:
            if not self.page_guard():
                break
            probe_started = time.perf_counter()
            try:
                response = await self.http.get(url)
                page = scan_page(response.text, str(response.url))
                route_probes.append({
                    "url": url, "status_code": response.status_code, "final_url": str(response.url),
                    "jobish_links": page["jobish_links"], "ats_links": page["ats_links"],
                    "job_posting_schema_count": page["job_posting_schema_count"],
                    "seconds": round(max(0.0, time.perf_counter() - probe_started), 6),
                })
                if scan_has_listing_signal(page):
                    self.result.errors.append(f"unreviewed_listing_route_detected: {url}")
            except FetchError as exc:
                route_probes.append({"url": url, "status_code": _status_from_error(exc),
                                     "error": str(exc),
                                     "seconds": round(max(0.0, time.perf_counter() - probe_started), 6)})
        self.result.metadata["listing_route_probes"] = route_probes
        self.result.timings["trilogy_route_probe_seconds"] = round(
            max(0.0, time.perf_counter() - route_started), 6)

        rest = await self.check_rest_api()
        self.result.metadata["wordpress_rest"] = rest
        if rest["job_post_types"]:
            self.result.errors.append(f"unreviewed_job_post_type_detected: {rest['job_post_types']}")
        if rest.get("search_job_listing_hits"):
            self.result.errors.append("unreviewed_search_job_listing_hit")

        final_started = time.perf_counter()
        final_parsed = None
        try:
            self.page_guard()
            final_response = await self.http.get(OFFICIAL_ENTRY)
            self.result.metadata["final_recheck"] = _response_summary(
                final_response, OFFICIAL_ENTRY, started=final_started)
            final_parsed = parse_hiring_page(final_response.text, str(final_response.url))
        except Exception as exc:  # noqa: BLE001
            self.result.errors.append(f"trilogy_final_recheck_failed: {type(exc).__name__}: {exc}")
        finally:
            self.result.timings["trilogy_final_recheck_seconds"] = round(
                max(0.0, time.perf_counter() - final_started), 6)

        matches = False
        if final_parsed is not None:
            if has_listing_signal(final_parsed):
                self.result.errors.append("unreviewed_listing_detected")
            matches = _evidence(initial_parsed) == _evidence(final_parsed)
            if not matches:
                self.result.errors.append("trilogy_hiring_page_changed_between_requests")

        if (final_parsed is not None and matches and not has_listing_signal(final_parsed)
                and not site_scan["listing_signals"] and not site_scan["failed_pages"]):
            self.result.errors.append("no_public_job_listing")

        self.result.expected_total = None
        self.result.listing_complete = False
        event_links = site_scan["hiring_event_links"]
        self.result.metadata["observed_journey"]["conclusion"] = (
            "The official Trilogy Innovations hiring journey publishes no current open-roles listing: "
            "the Join TI landing page body is empty, the hiring sub-pages describe the process only, and "
            "none of the 18 URLs in wp-sitemap.xml carries a job card, job/ATS link, JSON-LD JobPosting "
            "or pagination. The only hiring action is a stale 'CodeAgon 2021' InterviewBit contest link. "
            f"Hiring contact: {HIRING_CONTACT}. The zero-job result is an explicit no-listing outcome, "
            "not a successful zero-job listing."
        )
        self.result.metadata["observed_journey"]["stale_hiring_event_links"] = [
            item for item in event_links
            if (item.get("year") or 9999) < int(REVIEW_DATE[:4])
        ]
        self.result.timings["trilogy_source_collection_seconds"] = round(
            max(0.0, time.perf_counter() - source_started), 6)
        return self.result
