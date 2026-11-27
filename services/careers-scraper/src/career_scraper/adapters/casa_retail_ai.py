"""CASA Retail AI's currently broken official careers entry.

The official careers URL currently returns the company's branded 404 page. Its
published homepage recovery link resolves to a marketing homepage with no
career/job route. This adapter records that journey and leaves membership
unknown; it does not substitute marketing copy or enumerate a guessed ATS.
"""

import gzip
from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..http import FetchError
from ..models import now
from .base import Adapter


ENTRY = "https://www.casaretail.ai/careers"
OFFICIAL_HOSTS = {"casaretail.ai", "www.casaretail.ai"}
JOB_LINK = re.compile(r"\b(careers?|jobs?|open positions|openings?|hiring|join (?:us|our team|the team))\b", re.I)


def _text(value):
    return " ".join((value or "").split())


def _official_url(value):
    parsed = urlsplit(value)
    return parsed.scheme == "https" and parsed.hostname in OFFICIAL_HOSTS


class CasaRetailAI(Adapter):
    VERIFICATION_REVISION = "casa-retail-ai.official-careers-404-membership-unknown.1"

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

    async def _fetch(self, url, *, purpose):
        if not self.page_guard():
            record = {"url": url, "purpose": purpose, "outcome": "page_budget_exhausted"}
            self.result.metadata["journey"].append(record)
            return None, record
        started = time.perf_counter()
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            capture = self._last_capture(url)
            record = {
                "url": url, "purpose": purpose, "outcome": "request_failed",
                "error": str(exc), "final_url": capture.get("final_url"),
                "status": capture.get("status"), "sha256": capture.get("sha256"),
                "redirects": capture.get("redirects", []),
                "duration_seconds": max(0.0, time.perf_counter() - started),
            }
            self.result.metadata["journey"].append(record)
            return None, record
        record = {
            "url": url, "purpose": purpose, "outcome": "received",
            "final_url": str(response.url), "status": response.status_code,
            "sha256": sha256(response.content).hexdigest(),
            "redirects": [str(item.url) for item in response.history],
            "duration_seconds": max(0.0, time.perf_counter() - started),
        }
        self.result.metadata["journey"].append(record)
        return response, record

    @staticmethod
    def _is_branded_careers_404(soup):
        title = _text(soup.title.get_text(" ", strip=True)) if soup.title else ""
        text = _text(soup.get_text(" ", strip=True))
        heading = any(_text(node.get_text(" ", strip=True)) == "Page Not Found"
                      for node in soup.select("h1"))
        return title == "Page Not Found | CASA Retail AI" and heading and "404" in text

    def _recovery_link(self, soup, final_url):
        for anchor in soup.select("a[href]"):
            if _text(anchor.get_text(" ", strip=True)) != "Go to Homepage":
                continue
            target = urljoin(final_url, anchor["href"])
            parsed = urlsplit(target)
            if _official_url(target) and parsed.path == "/":
                return target
        return None

    @staticmethod
    def _job_link_candidates(soup, final_url):
        candidates = []
        for anchor in soup.select("a[href]"):
            label = _text(anchor.get_text(" ", strip=True))
            target = urljoin(final_url, anchor["href"])
            if JOB_LINK.search(label) or JOB_LINK.search(urlsplit(target).path):
                candidates.append({"label": label, "url": target})
        return candidates

    async def run(self):
        started = time.perf_counter()
        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=ENTRY,
            review_evidence="companies/casa-retail-ai/manual-reference.json",
            rendered_reference="companies/casa-retail-ai/browser-reference-parent.json",
            company_membership_verified=False,
            expected_company_posting_count=None,
            zero_openings_inferred=False,
            exact_company_job_ids=[],
            ats_board_requests=0,
            journey=[],
            homepage_job_link_candidates=[],
        )

        entry_started = time.perf_counter()
        entry, entry_record = await self._fetch(self.company.careers_url, purpose="official_careers_entry")
        self.result.timings["official_entry_seconds"] = max(0.0, time.perf_counter() - entry_started)
        entry_soup = None
        if entry is not None:
            if not _official_url(str(entry.url)) or urlsplit(str(entry.url)).path.rstrip("/") != "/careers":
                self.result.errors.append("casa_retail_ai_careers_entry_destination_changed_membership_unknown")
                return self._finish(started)
            entry_soup = BeautifulSoup(entry.text, "lxml")
            if entry.status_code != 404 and not self._is_branded_careers_404(entry_soup):
                self.result.errors.append("casa_retail_ai_careers_page_changed_requires_listing_review")
                return self._finish(started)
        else:
            raw = self._captured_body(entry_record)
            if raw is None:
                self.result.errors.append("casa_retail_ai_careers_entry_unavailable_without_body_membership_unknown")
                return self._finish(started)
            entry_soup = BeautifulSoup(raw, "lxml")
            if entry_record.get("status") != 404:
                self.result.errors.append("casa_retail_ai_careers_entry_request_failed_membership_unknown")
                return self._finish(started)
        if not self._is_branded_careers_404(entry_soup):
            self.result.errors.append("casa_retail_ai_unexpected_careers_404_content_membership_unknown")
            return self._finish(started)

        recovery = self._recovery_link(entry_soup, entry_record.get("final_url") or ENTRY)
        if not recovery:
            self.result.errors.append("casa_retail_ai_careers_404_has_no_verified_official_recovery_link")
            return self._finish(started)
        self.result.metadata["official_careers_entry_outcome"] = "branded_404"
        self.result.metadata["official_recovery_link"] = recovery
        self.result.metadata["entry_response_sha256"] = entry_record.get("sha256")

        home_started = time.perf_counter()
        home, home_record = await self._fetch(recovery, purpose="official_404_recovery_homepage")
        self.result.timings["homepage_recovery_seconds"] = max(0.0, time.perf_counter() - home_started)
        if home is None:
            self.result.errors.append("casa_retail_ai_official_recovery_homepage_unavailable_membership_unknown")
            return self._finish(started)
        final = urlsplit(str(home.url))
        if not _official_url(str(home.url)) or final.path != "/":
            self.result.errors.append("casa_retail_ai_recovery_link_left_official_homepage_membership_unknown")
            return self._finish(started)

        home_soup = BeautifulSoup(home.text, "lxml")
        title = _text(home_soup.title.get_text(" ", strip=True)) if home_soup.title else ""
        if "CASA Retail AI" not in title:
            self.result.errors.append("casa_retail_ai_recovery_destination_identity_changed_membership_unknown")
            return self._finish(started)
        candidates = self._job_link_candidates(home_soup, str(home.url))
        self.result.metadata.update(
            recovery_homepage_final_url=str(home.url),
            recovery_homepage_status=home.status_code,
            recovery_homepage_response_sha256=home_record.get("sha256"),
            recovery_homepage_title=title,
            homepage_job_link_candidates=candidates,
        )
        if candidates:
            self.result.errors.append("casa_retail_ai_homepage_now_exposes_unreviewed_career_routes")
        else:
            self.result.errors.append(
                "casa_retail_ai_official_careers_entry_404_and_homepage_has_no_job_route_membership_unknown"
            )
        self.result.metadata["collection_outcome"] = "failed_waitlist_membership_unknown"
        return self._finish(started)

    def _finish(self, started):
        elapsed = max(0.0, time.perf_counter() - started)
        self.result.timings["company_collection_seconds"] = elapsed
        self.result.timings["source_collection_seconds"] = elapsed
        self.result.duration_seconds = elapsed
        self.result.finished_at = now()
        return self.result
