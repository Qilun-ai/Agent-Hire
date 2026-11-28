"""Clumio's currently broken official careers entry.

The registry entry ``https://clumio.com/careers/`` returns the company's
branded 404 page (``Page not found | Clumio.com``). The official homepage,
the robots-published sitemap, and the sitemap locations themselves expose no
careers/jobs/hiring route. This adapter records that inspected journey only;
it never guesses a listing, substitutes the parent company's careers board,
or synthesizes job IDs or a poster count. Membership therefore stays unknown.
"""

import gzip
from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from ..http import FetchError
from .base import Adapter


ENTRY = "https://clumio.com/careers/"
HOMEPAGE = "https://www.clumio.com/"
ROBOTS = "https://www.clumio.com/robots.txt"
SITEMAP = "https://www.clumio.com/wp-sitemap.xml"
OFFICIAL_HOSTS = {"clumio.com", "www.clumio.com"}
HIRING_HINT = re.compile(
    r"\b(careers?|jobs?|vacancies|employment|hiring|recruit(?:ing|ment)?|"
    r"open positions|join (?:our|us)|work with us)\b",
    re.I,
)
BLOCK_STATUSES = {401, 403, 429}
RETIRED_CAREERS_PATHS = ("/careers/", "/careers/job-openings")


def _text(value):
    return " ".join((value or "").split())


def _official_url(value):
    parsed = urlsplit(value)
    return parsed.scheme == "https" and parsed.hostname in OFFICIAL_HOSTS


def _hiring_links(soup, page_url):
    links = []
    for anchor in soup.select("a[href]"):
        label = " ".join(anchor.stripped_strings)
        href = anchor.get("href", "").strip()
        if not HIRING_HINT.search(f"{label} {href}"):
            continue
        links.append({"label": label, "href": href, "url": urljoin(page_url, href)})
    return links


def _looks_like_block_page(text):
    value = (text or "").lower()
    return any(marker in value for marker in (
        "cf-chl-", "just a moment...", "checking your browser before accessing",
        "verify you are human", "security challenge",
    ))


def _is_branded_careers_404(soup):
    title = _text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    heading = any("lost in the cloud" in _text(node.get_text(" ", strip=True)).lower()
                  for node in soup.select("h1"))
    return title == "Page not found | Clumio.com" and heading


class Clumio(Adapter):
    """Inspect only Clumio-published routes; unknown membership is not zero jobs."""

    VERIFICATION_REVISION = "clumio.official-careers-404-membership-unknown.1"

    def _last_capture(self, url):
        if not self.http.output:
            return {}
        path = self.http.output / "requests.jsonl"
        if not path.exists():
            return {}
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (record.get("url") == url or record.get("final_url") == url
                    or url in record.get("redirects", [])):
                return record
        return {}

    def _captured_body(self, record):
        digest = record.get("sha256")
        if not digest or not self.http.output:
            return None
        path = self.http.output / "responses" / f"{digest}.gz"
        if not path.exists():
            return None
        return gzip.decompress(path.read_bytes())

    async def _fetch(self, url, purpose):
        if not self.page_guard():
            return None
        started = time.perf_counter()
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            capture = self._last_capture(url)
            status = capture.get("status")
            observation = {
                "purpose": purpose,
                "url": url,
                "status": status,
                "final_url": capture.get("final_url"),
                "sha256": capture.get("sha256"),
                "redirects": [{"url": item, "status": capture.get("status")}
                               for item in capture.get("redirects", [])],
                "error": f"{type(exc).__name__}: {exc}",
                "outcome": "blocked" if status in BLOCK_STATUSES else "request_failed",
                "duration_seconds": max(0.0, time.perf_counter() - started),
            }
            self.result.metadata["journey"].append(observation)
            if status in BLOCK_STATUSES:
                self.result.errors.append(f"clumio_official_journey_blocked_http_{status}: {url}")
            return None
        record = {
            "purpose": purpose,
            "url": url,
            "status": response.status_code,
            "final_url": str(response.url),
            "redirects": [{"url": str(item.url), "status": item.status_code}
                          for item in response.history],
            "sha256": sha256(response.content).hexdigest(),
            "outcome": "received",
            "duration_seconds": max(0.0, time.perf_counter() - started),
        }
        self.result.metadata["journey"].append(record)
        if _looks_like_block_page(response.text):
            self.result.errors.append(f"clumio_official_journey_waf_challenge: {url} -> {response.url}")
            record["outcome"] = "waf_challenge"
            return None
        return response

    @staticmethod
    def _sitemap_urls(text):
        root = ElementTree.fromstring(text)
        return [node.text.strip() for node in root.iter()
                if node.tag.rsplit("}", 1)[-1] == "loc" and node.text and node.text.strip()]

    def _changed(self, message):
        self.result.errors.append(message)
        return self._finish()

    def _finish(self):
        self.result.timings["company_collection_seconds"] = max(
            0.0, time.perf_counter() - self.result._started_clock)
        self.result.timings["source_collection_seconds"] = self.result.timings["company_collection_seconds"]
        self.result.timings["detail_phase_seconds"] = 0.0
        self.result.duration_seconds = self.result.timings["source_collection_seconds"]
        return self.result

    async def run(self):
        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry": ENTRY,
            "review_evidence": "companies/clumio/manual-reference.json",
            "company_membership_verified": False,
            "membership_state": "unknown",
            "expected_company_posting_count": None,
            "zero_openings_inferred": False,
            "exact_job_ids_exposed": [],
            "listing_pages": [],
            "listing_pages_reconciled": False,
            "ats_board_requests": 0,
            "detail_requests": 0,
            "journey": [],
            "page_links": {},
            "sitemap_locations": [],
            "hiring_links": [],
            "collection_outcome": "failed_waitlist_membership_unknown",
        })

        entry_started = time.perf_counter()
        entry = await self._fetch(self.company.careers_url, "official_careers_entry")
        self.result.timings["official_entry_seconds"] = max(0.0, time.perf_counter() - entry_started)
        entry_record = self.result.metadata["journey"][-1] if self.result.metadata["journey"] else {}
        if entry_record.get("outcome") in {"blocked", "waf_challenge"}:
            # The exact terminal failure is already recorded; stop without
            # inventing a listing or retrying.
            return self._finish()
        if entry is not None:
            # The entry now serves content instead of the reviewed branded 404.
            return self._changed("clumio_official_careers_entry_changed_requires_listing_review")
        if entry_record.get("outcome") != "request_failed" or entry_record.get("status") != 404:
            return self._changed("clumio_official_careers_entry_request_failed_membership_unknown")
        raw = self._captured_body(entry_record)
        if raw is None:
            return self._changed("clumio_official_careers_entry_unavailable_without_body_membership_unknown")
        if not _is_branded_careers_404(BeautifulSoup(raw, "lxml")):
            return self._changed("clumio_unexpected_careers_404_content_membership_unknown")
        self.result.metadata.update({
            "official_careers_entry_outcome": "branded_404",
            "entry_response_sha256": entry_record.get("sha256"),
            "entry_final_url": entry_record.get("final_url"),
        })

        home_started = time.perf_counter()
        home = await self._fetch(HOMEPAGE, "official_homepage_check")
        self.result.timings["homepage_check_seconds"] = max(0.0, time.perf_counter() - home_started)
        if home is None:
            if self.result.metadata["journey"][-1].get("outcome") in {"blocked", "waf_challenge"}:
                return self._finish()
            return self._changed("clumio_official_homepage_unavailable_membership_unknown")
        if not _official_url(str(home.url)) or urlsplit(str(home.url)).path.rstrip("/") != "":
            self.result.errors.append("clumio_homepage_destination_left_official_root_membership_unknown")
            return self._finish()
        home_soup = BeautifulSoup(home.text, "lxml")
        title = _text(home_soup.title.get_text(" ", strip=True)) if home_soup.title else ""
        if "Clumio" not in title:
            return self._changed("clumio_homepage_destination_identity_changed_membership_unknown")
        candidates = _hiring_links(home_soup, str(home.url))
        self.result.metadata["page_links"][str(home.url)] = candidates
        self.result.metadata["hiring_links"].extend(candidates)
        self.result.metadata.update({
            "homepage_final_url": str(home.url),
            "homepage_status": home.status_code,
            "homepage_response_sha256": sha256(home.content).hexdigest(),
            "homepage_title": title,
        })
        if candidates:
            return self._changed("clumio_homepage_now_exposes_unreviewed_career_routes")

        robots_started = time.perf_counter()
        robots = await self._fetch(ROBOTS, "official_site_robots_discovery")
        self.result.timings["robots_seconds"] = max(0.0, time.perf_counter() - robots_started)
        if robots is None:
            if self.result.metadata["journey"][-1].get("outcome") in {"blocked", "waf_challenge"}:
                return self._finish()
            return self._changed("clumio_official_robots_unavailable_membership_unknown")
        sitemap_urls = re.findall(r"(?im)^\s*Sitemap:\s*(\S+)\s*$", robots.text)
        if sitemap_urls != [SITEMAP]:
            return self._changed("clumio_official_sitemap_declaration_changed_requires_individual_review")
        self.result.metadata["sitemap_url"] = SITEMAP

        sitemap_started = time.perf_counter()
        sitemap = await self._fetch(SITEMAP, "robots_published_sitemap")
        self.result.timings["sitemap_seconds"] = max(0.0, time.perf_counter() - sitemap_started)
        if sitemap is None:
            if self.result.metadata["journey"][-1].get("outcome") in {"blocked", "waf_challenge"}:
                return self._finish()
            return self._changed("clumio_official_sitemap_unavailable_membership_unknown")
        try:
            locations = self._sitemap_urls(sitemap.text)
        except ElementTree.ParseError as exc:
            self.result.errors.append(f"clumio_official_sitemap_invalid_xml: {exc}")
            return self._finish()
        self.result.metadata["sitemap_locations"] = locations
        hiring_locations = [url for url in locations if HIRING_HINT.search(url)]
        self.result.metadata["sitemap_hiring_locations"] = hiring_locations
        if hiring_locations:
            return self._changed("clumio_official_sitemap_exposes_unreviewed_hiring_route")
        return self._changed(
            "clumio_official_careers_entry_404_and_official_site_has_no_job_route_membership_unknown"
        )
