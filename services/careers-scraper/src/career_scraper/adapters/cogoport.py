"""Cogoport's observed official-entry outcome, without ATS-board enumeration.

The requested entry ``https://www.cogoport.com/en-IN/careers`` returns the
Next.js 404 page. The canonical official careers page
``https://www.cogoport.com/en-IN/company/careers`` returns 200, but it
publishes no job listing at all: its only hiring call-to-action is
``mailto:careers@cogoport.com`` (two anchors). It exposes no job IDs, no
listing requests, no pagination and no detail routes. This adapter therefore
archives the two official responses, audits the canonical page for any
job-like membership, and records an explicit scope failure. It never requests
the Freshteam board: that host is not linked, embedded, requested or
redirected to by the official journey, so enumerating it would violate the
company-first contract. Unknown membership is never reported as zero.

Retry when the official journey publishes a Cogoport-specific listing, exact
vacancy links or a complete company-scope membership mapping; then reinspect
that journey before implementing any detail handler.
"""

import re
import time

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError

REQUESTED_ENTRY = "https://www.cogoport.com/en-IN/careers"
CANONICAL_ENTRY = "https://www.cogoport.com/en-IN/company/careers"

JOB_LIKE_HREF = re.compile(r"freshteam|/jobs?/|/openings?|/positions?|/open-roles?", re.I)


class Cogoport(Adapter):
    """Fail-closed gate for Cogoport's mailto-only official careers journey."""

    VERIFICATION_REVISION = "cogoport.official-careers-mailto-only-no-membership.1"

    def _changed(self):
        self.result.errors.append(
            "cogoport_official_journey_changed_requires_individual_membership_review"
        )
        return self.result

    async def run(self):
        started = time.perf_counter()
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=self.company.careers_url,
            requested_entry_url=REQUESTED_ENTRY,
            canonical_official_page=CANONICAL_ENTRY,
            company_membership_verified=False,
            ats_board_requests=0,
            detail_requests=0,
            review_evidence="companies/cogoport/manual-reference.json",
        )
        # Unknown membership never represents an authoritative zero-job listing.
        self.result.expected_total = None
        self.result.listing_complete = False
        try:
            if self.company.careers_url != REQUESTED_ENTRY:
                return self._changed()
            if not self.page_guard():
                return self.result

            # 1) Requested entry: observed to be the Next.js 404 page.
            requested_status = None
            requested_error = None
            try:
                requested = await self.http.get(REQUESTED_ENTRY)
                self.result.metadata.update(
                    requested_entry_final_url=str(requested.url),
                    requested_entry_status=requested.status_code,
                )
                # A 200 here means the published journey has changed.
                return self._changed()
            except FetchError as exc:
                requested_error = f"{type(exc).__name__}: {exc}"
                match = re.search(r"HTTP\s+(\d{3})\b", requested_error)
                requested_status = int(match.group(1)) if match else None
                self.result.metadata.update(
                    requested_entry_status=requested_status,
                    requested_entry_error=requested_error,
                    requested_entry_observed_404=(requested_status == 404),
                )
                if requested_status != 404:
                    # Stop on 401/403/429/WAF and similar with exact evidence.
                    self.result.errors.append(
                        "cogoport_requested_careers_route_unreachable_requires_individual_membership_review"
                    )
                    return self.result
            if not self.page_guard():
                return self.result

            # 2) Canonical official careers page: mailto-only, no membership.
            canonical = await self.http.get(CANONICAL_ENTRY)
            if str(canonical.url) != CANONICAL_ENTRY:
                self.result.metadata.update(
                    canonical_final_url=str(canonical.url),
                    canonical_status=canonical.status_code,
                )
                return self._changed()
            soup = BeautifulSoup(canonical.text, "lxml")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            anchors = list(soup.select("a[href]"))
            hrefs = [(anchor, str(anchor.get("href", ""))) for anchor in anchors]
            mailto_careers = [
                href for anchor, href in hrefs
                if href.strip().lower().startswith("mailto:")
                and "careers@cogoport.com" in href.lower()
            ]
            job_like = []
            for anchor, href in hrefs:
                candidate = href.strip()
                if not candidate:
                    continue
                # Self-references to the canonical page are navigation, not membership.
                if candidate.rstrip("/") in {CANONICAL_ENTRY.rstrip("/"), "/en-IN/company/careers"}:
                    continue
                if candidate.lower().startswith("mailto:"):
                    continue
                if JOB_LIKE_HREF.search(candidate) or "freshteam" in candidate.lower():
                    job_like.append(candidate)
            freshteam_mentions = canonical.text.lower().count("freshteam")
            self.result.metadata.update(
                canonical_final_url=str(canonical.url),
                canonical_status=canonical.status_code,
                canonical_title=title,
                canonical_anchor_count=len(anchors),
                mailto_careers_links=len(mailto_careers),
                job_like_link_count=len(job_like),
                job_like_links=job_like[:20],
                freshteam_mentions_in_html=freshteam_mentions,
                next_data_present=soup.find("script", id="__NEXT_DATA__") is not None,
                listing_pagination="None; no job cards, listing request or pagination published on the canonical page",
                listing_filters="None; no filters published on the canonical page",
            )
            if job_like or freshteam_mentions > 0:
                return self._changed()
            if len(mailto_careers) == 0 or "Join Cogoport" not in title:
                return self._changed()
            self.result.errors.append(
                "cogoport_official_careers_publishes_no_job_listing_membership"
            )
            if self.result.metadata.get("requested_entry_observed_404"):
                self.result.errors.append(
                    "cogoport_requested_careers_route_returns_404"
                )
            return self.result
        finally:
            self.result.timings["official_scope_probe_seconds"] = round(
                time.perf_counter() - started, 6
            )
