"""Audify Tech's official-site journey, with an explicit unknown-membership gate.

The current official site publishes no careers/jobs link or listing. This flow
follows only the homepage's linked language alternate and the sitemap advertised
by the site's robots.txt; it never guesses a /careers route or an ATS tenant.
"""

import re
import time
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from .. import access
from ..http import FetchError
from .base import Adapter


ENTRY = "https://audify.tech/"
ROBOTS = "https://audify.tech/robots.txt"
HIRING_HINT = re.compile(
    r"\b(careers?|jobs?|vacancies|employment|hiring|recruit(?:ing|ment)?|"
    r"open positions|join (?:our|us)|work with us)\b",
    re.I,
)
BLOCK_STATUSES = {401, 403, 429}


def _links(soup, page_url):
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


class AudifyTech(Adapter):
    """Inspect only Audify-published routes; unknown membership is not zero jobs."""

    VERIFICATION_REVISION = "audify-tech.official-site-no-published-job-journey.1"
    OFFICIAL_HOST = "audify.tech"

    def _last_request(self, url):
        output = getattr(self.http, "output", None)
        if not output:
            return None
        path = output / "requests.jsonl"
        if not path.is_file():
            return None
        import json

        for line in reversed(path.read_text().splitlines()):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (item.get("url") == url or item.get("final_url") == url
                    or url in item.get("redirects", [])):
                return item
        return None

    async def _fetch(self, url, purpose):
        if not self.page_guard():
            return None
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            evidence = self._last_request(url) or {}
            status = evidence.get("status")
            observation = {
                "purpose": purpose,
                "url": url,
                "status": status,
                "final_url": evidence.get("final_url"),
                "sha256": evidence.get("sha256"),
                "error": f"{type(exc).__name__}: {exc}",
                "outcome": "blocked" if status in BLOCK_STATUSES else "request_failed",
            }
            self.result.metadata["journey"].append(observation)
            if status in BLOCK_STATUSES:
                self.result.errors.append(f"audify_official_journey_blocked_http_{status}: {url}")
                response = await self._escalate(url, purpose, observation)
                if response is None:
                    return None
            else:
                self.result.errors.append(f"audify_official_journey_request_failed: {observation['error']}")
                return None

        final_url = str(response.url)
        request_evidence = self._last_request(url) or {}
        observation = {
            "purpose": purpose,
            "url": url,
            "status": response.status_code,
            "final_url": final_url,
            "redirects": [{"url": str(item.url), "status": item.status_code}
                          for item in response.history],
            "sha256": request_evidence.get("sha256"),
            "outcome": "received",
        }
        self.result.metadata["journey"].append(observation)
        parsed = urlsplit(final_url)
        if parsed.scheme != "https" or parsed.hostname != self.OFFICIAL_HOST:
            self.result.errors.append(f"audify_official_journey_left_company_host: {url} -> {final_url}")
            observation["outcome"] = "offsite_redirect"
            return None
        if _looks_like_block_page(response.text):
            self.result.errors.append(f"audify_official_journey_waf_challenge: {url} -> {final_url}")
            observation["outcome"] = "waf_challenge"
            escalated = await self._escalate(url, purpose, observation)
            if escalated is None:
                return None
            response = escalated
            final_url = str(response.url)
        return response

    async def _escalate(self, url, purpose, observation):
        """Retry a challenged official route through the stealth browser."""
        page = await access.fetch_with_stealth(url)
        observation["access"] = access.attempt_metadata(page, url=url)
        if page is None or not page.ok:
            self.result.metadata.setdefault("access_attempts", []).append(observation["access"])
            return None
        self.result.metadata.setdefault("access_attempts", []).append(observation["access"])
        restored = access.as_response(page)
        observation["outcome"] = "received_after_stealth_escalation"
        observation["status"] = restored.status_code
        observation["final_url"] = str(restored.url)
        return restored

    @staticmethod
    def _sitemap_urls(text):
        root = ElementTree.fromstring(text)
        return [node.text.strip() for node in root.iter()
                if node.tag.rsplit("}", 1)[-1] == "loc" and node.text and node.text.strip()]

    def _changed(self, message="audify_official_hiring_journey_changed_requires_individual_review"):
        self.result.errors.append(message)
        return self.result

    async def run(self):
        started = time.perf_counter()
        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry": self.company.careers_url,
            "membership_authority": "Audify Tech's own company-published website journey only",
            "company_membership_verified": False,
            "membership_state": "unknown",
            "exact_job_ids_exposed": [],
            "listing_pages": [],
            "listing_pages_reconciled": False,
            "ats_board_requests": 0,
            "detail_requests": 0,
            "journey": [],
            "page_links": {},
            "sitemap_locations": [],
            "hiring_links": [],
            "review_evidence": "companies/audify-tech/manual-reference.json",
            "zero_openings_claimed": False,
        })

        entry = await self._fetch(self.company.careers_url, "registry_official_entry")
        if entry is None:
            self._record_source_timing(started)
            return self.result
        if str(entry.url) != ENTRY:
            self._record_source_timing(started)
            return self._changed("audify_registry_entry_changed_requires_individual_review")

        entry_soup = BeautifulSoup(entry.text, "lxml")
        entry_links = _links(entry_soup, str(entry.url))
        self.result.metadata["page_links"][str(entry.url)] = entry_links
        self.result.metadata["hiring_links"].extend(entry_links)
        if entry_links:
            self._record_source_timing(started)
            return self._changed()

        # Follow the exact French alternate link published by the homepage; do
        # not infer a localized careers route.
        language_links = [
            urljoin(str(entry.url), anchor.get("href", ""))
            for anchor in entry_soup.select("a[href]")
            if " ".join(anchor.stripped_strings).strip().casefold() == "fr"
        ]
        if len(set(language_links)) != 1:
            self._record_source_timing(started)
            return self._changed("audify_official_language_link_changed_requires_individual_review")
        language_url = language_links[0]
        self.result.metadata["french_alternate_url"] = language_url
        language = await self._fetch(language_url, "homepage_published_french_alternate")
        if language is None:
            self._record_source_timing(started)
            return self.result
        language_links_found = _links(BeautifulSoup(language.text, "lxml"), str(language.url))
        self.result.metadata["page_links"][str(language.url)] = language_links_found
        self.result.metadata["hiring_links"].extend(language_links_found)
        if language_links_found:
            self._record_source_timing(started)
            return self._changed()

        robots = await self._fetch(ROBOTS, "official_site_robots_discovery")
        if robots is None:
            self._record_source_timing(started)
            return self.result
        sitemap_urls = re.findall(r"(?im)^\s*Sitemap:\s*(\S+)\s*$", robots.text)
        if len(sitemap_urls) != 1:
            self._record_source_timing(started)
            return self._changed("audify_official_sitemap_declaration_changed_requires_individual_review")
        sitemap_url = sitemap_urls[0]
        sitemap_parts = urlsplit(sitemap_url)
        if sitemap_parts.scheme != "https" or sitemap_parts.hostname != self.OFFICIAL_HOST:
            self.result.errors.append(f"audify_official_sitemap_left_company_host: {sitemap_url}")
            self._record_source_timing(started)
            return self.result
        self.result.metadata["sitemap_url"] = sitemap_url

        sitemap = await self._fetch(sitemap_url, "robots_published_sitemap")
        if sitemap is None:
            self._record_source_timing(started)
            return self.result
        try:
            sitemap_locations = self._sitemap_urls(sitemap.text)
        except ElementTree.ParseError as exc:
            self.result.errors.append(f"audify_official_sitemap_invalid_xml: {exc}")
            self._record_source_timing(started)
            return self.result
        self.result.metadata["sitemap_locations"] = sitemap_locations
        sitemap_hiring_locations = [url for url in sitemap_locations if HIRING_HINT.search(url)]
        self.result.metadata["sitemap_hiring_locations"] = sitemap_hiring_locations
        if sitemap_hiring_locations:
            self.result.errors.append("audify_official_sitemap_exposes_unreviewed_hiring_route")
            self._record_source_timing(started)
            return self.result

        self.result.errors.append(
            "audify_official_site_has_no_published_careers_or_job_listing_journey_membership_unknown"
        )
        self._record_source_timing(started)
        return self.result

    def _record_source_timing(self, started):
        elapsed = max(0.0, time.perf_counter() - started)
        self.result.timings["source_journey_inspection_seconds"] = round(elapsed, 6)
        self.result.timings["detail_phase_seconds"] = 0.0
