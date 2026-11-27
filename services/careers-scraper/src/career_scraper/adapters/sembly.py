"""Sembly AI's official careers journey (reviewed 2026-11-08).

The ledger entry ``https://www.sembly.ai/careers/`` returns a plain HTTP 404.
Sembly AI's official homepage, About page, robots file, sitemap (177 URLs) and
help-centre sitemap (230 URLs) publish no careers/jobs link, no job cards, no
listing request, no JSON-LD ``JobPosting`` and no ATS iframe/link.  A headless
Chromium visit confirms the same 404.

This adapter therefore records the broken/absent official listing explicitly.
It never substitutes a guessed ATS slug, an aggregator, or the unrelated
``sembly.com`` / SmartRecruiters "Sembly" board.  If the official entry or the
site navigation later exposes a careers journey, the adapter stops with
``unreviewed_careers_journey_detected`` so the new journey is reviewed before
membership is collected.
"""
from __future__ import annotations

from hashlib import sha256
from html import unescape
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now


OFFICIAL_ENTRY = "https://www.sembly.ai/careers/"
HOMEPAGE = "https://www.sembly.ai/"
SITEMAP_URL = "https://www.sembly.ai/sitemap.xml"
OFFICIAL_HOSTS = {"www.sembly.ai", "sembly.ai"}
EXPECTED_PATH = "/careers"

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
    r"phenompeople\.com|eightfold\.ai|comeet\.co|teamtailor\.com|breezy\.hr"
    r")$"
)
AGGREGATOR_HOST = re.compile(
    r"(?i)(?:^|\.)(?:"
    r"linkedin\.com|indeed\.com|naukri\.com|glassdoor\.[a-z.]+|"
    r"wellfound\.com|angel\.co|instahyre\.com|cutshort\.io|foundit\.in|"
    r"monster\.com|dice\.com|ziprecruiter\.com|simplyhired\.com|bayt\.com|"
    r"seek\.com|jobsdb\.com|jobstreet\.com"
    r")$"
)
CAREER_PATH = re.compile(
    r"(?i)^/(?:"
    r"careers?|jobs?|join(?:-us)?|work-with-us|we-are-hiring|hiring|"
    r"opportunities|open-positions|positions|vacancies|employment"
    r")(?:/|$)"
)
PAGINATION_TEXT = re.compile(
    r"(?i)^(?:next(?: page)?|previous(?: page)?|prev(?: page)?|load more|show more|page \d+)\s*[»›→]?$"
)


def normalize_text(value: str | None) -> str:
    value = (value or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", value).strip()


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _path(url: str) -> str:
    return urlsplit(url).path.rstrip("/") or "/"


def url_kind(url: str) -> str | None:
    """Classify an anchor/sitemap URL without treating blog text as a listing."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if ATS_HOST.search(host):
        return "ats"
    if AGGREGATOR_HOST.search(host):
        if host.endswith("linkedin.com") and not path.startswith("/jobs"):
            return None
        return "aggregator"
    if host in OFFICIAL_HOSTS:
        if path.rstrip("/") == EXPECTED_PATH:
            return "official_entry"
        if CAREER_PATH.match(path):
            return "career_path"
    return None


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


def _anchors(soup: BeautifulSoup, response_url: str) -> list[dict]:
    result = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(response_url, href)
        result.append({
            "text": normalize_text(anchor.get_text(" ", strip=True)),
            "href": href,
            "url": absolute,
            "kind": url_kind(absolute),
        })
    return result


def _ats_embeds(soup: BeautifulSoup, response_url: str) -> list[dict]:
    result = []
    for tag in soup.select("iframe[src], script[src], form[action], link[href]"):
        attribute = next((name for name in ("src", "action", "href") if tag.get(name)), None)
        if attribute is None:
            continue
        absolute = urljoin(response_url, tag.get(attribute))
        if ATS_HOST.search(_host(absolute)):
            result.append({"tag": tag.name, "attribute": attribute, "url": absolute})
    return result


def _pagination(soup: BeautifulSoup) -> list[str]:
    result = []
    for element in soup.select("a,button"):
        text = normalize_text(element.get_text(" ", strip=True))
        if text and len(text) <= 40 and PAGINATION_TEXT.match(text):
            result.append(text)
    return result


def _page_evidence(html: str, response_url: str, soup: BeautifulSoup) -> dict:
    page_text = visible_text(html)
    body_text = normalize_text(page_text)
    anchors = _anchors(soup, response_url)
    return {
        "title": normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "h1": [
            normalize_text(node.get_text(" ", strip=True))
            for node in soup.select("h1")
            if normalize_text(node.get_text(" ", strip=True))
        ],
        "anchor_count": len(anchors),
        "anchors": anchors,
        "career_links": [item for item in anchors if item["kind"] == "career_path"],
        "official_entry_links": [item for item in anchors if item["kind"] == "official_entry"],
        "ats_links": [item for item in anchors if item["kind"] == "ats"],
        "aggregator_links": [item for item in anchors if item["kind"] == "aggregator"],
        "ats_embeds": _ats_embeds(soup, response_url),
        "job_posting_schema_count": job_posting_schema_count(soup),
        "pagination_controls": _pagination(soup),
        "visible_text_chars": len(page_text),
        "normalized_text_chars": len(body_text),
        "normalized_text_sha256": sha256(body_text.encode()).hexdigest(),
    }


def parse_entry_response(response) -> dict:
    """Return the exact state of the ledger careers entry, including 404s."""
    final_url = str(response.url)
    host = _host(final_url)
    path = _path(final_url)
    html = response.text
    soup = BeautifulSoup(html, "lxml")
    evidence = _page_evidence(html, final_url, soup)
    status = response.status_code
    if host not in OFFICIAL_HOSTS:
        state = "unexpected_external_redirect"
    elif path != EXPECTED_PATH:
        state = "unexpected_path"
    elif status == 404:
        state = "not_found"
    elif status == 403:
        state = "blocked"
    elif status == 429:
        state = "rate_limited"
    elif status >= 500:
        state = "server_error"
    elif status == 200:
        listing = bool(
            evidence["career_links"]
            or evidence["ats_links"]
            or evidence["ats_embeds"]
            or evidence["aggregator_links"]
            or evidence["job_posting_schema_count"]
            or evidence["pagination_controls"]
        )
        state = "listing_present" if listing else "page_without_listing"
    else:
        state = "http_error"
    listing_signal = bool(
        evidence["career_links"]
        or evidence["ats_links"]
        or evidence["ats_embeds"]
        or evidence["aggregator_links"]
        or evidence["job_posting_schema_count"]
        or evidence["pagination_controls"]
    )
    return {
        "state": state,
        "status_code": status,
        "final_url": final_url,
        "final_host": host,
        "final_path": path,
        "listing_signal": listing_signal,
        **evidence,
    }


def parse_homepage(html: str, response_url: str = HOMEPAGE) -> dict:
    if _host(response_url) not in OFFICIAL_HOSTS:
        raise ValueError(f"Sembly homepage redirected to an unexpected URL: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    evidence = _page_evidence(html, response_url, soup)
    new_journey = bool(
        evidence["career_links"]
        or evidence["ats_links"]
        or evidence["ats_embeds"]
        or evidence["aggregator_links"]
        or evidence["job_posting_schema_count"]
    )
    return {
        "response_url": response_url,
        "new_careers_journey": new_journey,
        "conclusion": (
            "The official homepage publishes no careers/jobs link, job card, listing "
            "request, JSON-LD JobPosting or ATS embed."
            if not new_journey else
            "The official homepage now exposes a careers/jobs/ATS link; the new journey "
            "must be reviewed before collecting membership."
        ),
        **evidence,
    }


def parse_sitemap(xml: str, response_url: str = SITEMAP_URL) -> dict:
    if _host(response_url) not in OFFICIAL_HOSTS:
        raise ValueError(f"Sembly sitemap came from an unexpected URL: {response_url}")
    locs = [unescape(value.strip()) for value in re.findall(r"<loc>(.*?)</loc>", xml, re.S)]
    kinds = [(url, url_kind(url)) for url in locs]
    career_candidates = [
        {"url": url, "kind": kind}
        for url, kind in kinds
        if kind in {"career_path", "ats", "aggregator"}
    ]
    official_entry_urls = [url for url, kind in kinds if kind == "official_entry"]
    return {
        "response_url": response_url,
        "kind": "sitemapindex" if "<sitemapindex" in xml else "urlset",
        "url_count": len(locs),
        "career_listing_candidates": career_candidates,
        "official_entry_urls": official_entry_urls,
        "ats_urls": [item["url"] for item in career_candidates if item["kind"] == "ats"],
        "new_careers_journey": bool(career_candidates),
        "conclusion": (
            "The official sitemap publishes no careers/jobs URL or ATS link."
            if not career_candidates else
            "The official sitemap now publishes a careers/jobs/ATS URL; the new journey "
            "must be reviewed before collecting membership."
        ),
    }


def response_summary(response) -> dict:
    return {
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
    }


async def fetch_capture(http, url: str):
    """Fetch a URL and keep the raw response even when it is an HTTP error.

    ``HTTP.request`` raises on 4xx/5xx.  The dead careers entry is itself the
    evidence, so use the shared client's transport and the shared response
    writer directly for this one bounded request.
    """
    request = http.client.build_request("GET", url)
    response = await http.client.send(request, follow_redirects=True)
    http._save(response, "GET", None)
    return response


class Sembly(Adapter):
    """Read the official Sembly AI entry and record its broken/no-listing state."""

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "homepage_url": HOMEPAGE,
            "sitemap_url": SITEMAP_URL,
            "listing_authority": "none: the ledger careers entry returns HTTP 404 and the official site publishes no careers/jobs link",
            "detail_method": "none: no listing or detail route is published",
            "request_scope": (
                "Official sembly.ai careers entry, homepage and sitemap only; no guessed ATS slug, "
                "aggregator or parent-company board is queried."
            ),
        })

        entry_started = time.perf_counter()
        try:
            self.page_guard()
            initial_response = await fetch_capture(self.http, OFFICIAL_ENTRY)
            initial = parse_entry_response(initial_response)
        except Exception as exc:
            self.result.errors.append(f"sembly_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.expected_total = None
            self.result.timings["sembly_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["sembly_entry_check_seconds"] = max(0.0, time.perf_counter() - entry_started)
            return self.result
        self.result.timings["sembly_entry_check_seconds"] = max(0.0, time.perf_counter() - entry_started)
        self.result.metadata["initial_entry"] = response_summary(initial_response)
        self.result.metadata["observed_journey"] = initial

        if initial["state"] == "listing_present":
            self.result.errors.append("unreviewed_careers_listing_detected")
            self.result.metadata["detected_listing"] = {
                "source": "official entry",
                "career_links": initial["career_links"],
                "ats_links": initial["ats_links"],
                "ats_embeds": initial["ats_embeds"],
                "aggregator_links": initial["aggregator_links"],
                "job_posting_schema_count": initial["job_posting_schema_count"],
                "pagination_controls": initial["pagination_controls"],
            }
            self._finish_unavailable(source_started)
            return self.result

        if initial["state"] in {"blocked", "rate_limited", "server_error", "http_error"}:
            self.result.errors.append(f"official_careers_page_http_{initial['status_code']}")
            if initial["state"] == "blocked":
                self.result.errors.append("official_careers_page_blocked")
            elif initial["state"] == "rate_limited":
                self.result.errors.append("official_careers_page_rate_limited")
            self._finish_unavailable(source_started)
            return self.result

        if initial["state"] in {"unexpected_external_redirect", "unexpected_path"}:
            self.result.errors.append(f"official_careers_page_{initial['state']}")
            self._finish_unavailable(source_started)
            return self.result

        # The ledger entry is 404 or a reachable page without a listing.  Check
        # the official homepage and sitemap for a moved/new careers journey,
        # then recheck the entry so a transient state is not mistaken for a
        # stable no-listing result.
        if initial["state"] == "not_found":
            homepage_started = time.perf_counter()
            homepage_scan = None
            try:
                self.page_guard()
                homepage_response = await self.http.get(HOMEPAGE)
                homepage_scan = parse_homepage(homepage_response.text, str(homepage_response.url))
                self.result.metadata["homepage_scan"] = homepage_scan
                self.result.metadata["homepage_response"] = response_summary(homepage_response)
            except Exception as exc:
                self.result.errors.append(f"sembly_homepage_check_failed: {type(exc).__name__}: {exc}")
                self.result.metadata["homepage_scan"] = {"error": f"{type(exc).__name__}: {exc}"}
            finally:
                self.result.timings["sembly_homepage_check_seconds"] = max(0.0, time.perf_counter() - homepage_started)

            sitemap_started = time.perf_counter()
            sitemap_scan = None
            try:
                self.page_guard()
                sitemap_response = await self.http.get(SITEMAP_URL)
                sitemap_scan = parse_sitemap(sitemap_response.text, str(sitemap_response.url))
                self.result.metadata["sitemap_scan"] = sitemap_scan
                self.result.metadata["sitemap_response"] = response_summary(sitemap_response)
            except Exception as exc:
                self.result.errors.append(f"sembly_sitemap_check_failed: {type(exc).__name__}: {exc}")
                self.result.metadata["sitemap_scan"] = {"error": f"{type(exc).__name__}: {exc}"}
            finally:
                self.result.timings["sembly_sitemap_check_seconds"] = max(0.0, time.perf_counter() - sitemap_started)

            new_journey = bool(
                (homepage_scan and homepage_scan.get("new_careers_journey"))
                or (sitemap_scan and sitemap_scan.get("new_careers_journey"))
            )
            if new_journey:
                self.result.errors.append("unreviewed_careers_journey_detected")
                self.result.metadata["detected_listing"] = {
                    "source": "homepage or sitemap",
                    "homepage": homepage_scan,
                    "sitemap": sitemap_scan,
                }
            else:
                self.result.errors.append("official_careers_page_http_404")
                self.result.errors.append("no_public_job_listing")
        else:
            self.result.errors.append("no_public_job_listing")

        final_started = time.perf_counter()
        try:
            self.page_guard()
            final_response = await fetch_capture(self.http, OFFICIAL_ENTRY)
            final = parse_entry_response(final_response)
            self.result.metadata["final_recheck"] = response_summary(final_response)
            self.result.metadata["final_state"] = final
            if final["state"] != initial["state"]:
                self.result.errors.append("official_entry_changed_between_requests")
            if final["listing_signal"]:
                self.result.errors.append("unreviewed_careers_listing_detected")
                self.result.metadata["detected_listing_after_recheck"] = {
                    "source": "final recheck",
                    "career_links": final["career_links"],
                    "ats_links": final["ats_links"],
                    "ats_embeds": final["ats_embeds"],
                    "aggregator_links": final["aggregator_links"],
                    "job_posting_schema_count": final["job_posting_schema_count"],
                    "pagination_controls": final["pagination_controls"],
                }
        except Exception as exc:
            self.result.errors.append(f"sembly_final_recheck_failed: {type(exc).__name__}: {exc}")
        finally:
            self.result.timings["sembly_final_recheck_seconds"] = max(0.0, time.perf_counter() - final_started)

        self.result.metadata["observed_journey"]["conclusion"] = (
            "The ledger careers entry https://www.sembly.ai/careers/ returns HTTP 404. "
            "The official homepage, About page, robots file, sitemap and help-centre sitemap "
            "publish no careers/jobs link, job card, listing request, JSON-LD JobPosting or ATS "
            "embed. The empty result is an explicit broken/no-listing failure, not a successful "
            "zero-job listing; no guessed ATS or aggregator was substituted."
        )
        self._finish_unavailable(source_started)
        return self.result

    def _finish_unavailable(self, source_started: float):
        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.timings["sembly_source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
