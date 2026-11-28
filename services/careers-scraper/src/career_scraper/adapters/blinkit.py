"""Blinkit's own /careers journey: official-entry inspection with stealth escalation.

Membership authority is Blinkit's own official careers URL only
(https://blinkit.com/careers). This adapter never follows, embeds, or
enumerates a parent (Eternal/Zomato) career page or any ATS board. It
fetches the commissioned entry once per attempt, archives the raw response
through the shared HTTP store, and on 401/403/429/WAF escalates to the
stealth browser (fingerprint profile, proxy/CAPTCHA solving when configured)
before deciding the entry is inaccessible.

Observed 2026-11-07: the entry returns an HTTP 403 Cloudflare challenge
(``cf-mitigated: challenge`` header, ``<title>blinkit | Error Page</title>``,
``challenge-platform`` script) to programmatic HTTP. A 200 response without
a previously verified native listing structure also stops with an explicit
error; a zero-opening claim is never made.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .. import access
from .base import Adapter


def inspect_entry_html(html: str) -> dict:
    """Describe an archived Blinkit entry body without inventing a listing.

    Returns challenge markers, the page title, and anchor hrefs found in
    the document. Job-shaped hrefs are reported as observations only; the
    Blinkit flow has no verified native listing structure, so hrefs are
    never turned into membership.
    """
    text = html or ""
    soup = BeautifulSoup(text, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    lowered = text.lower()
    hrefs = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if href:
            hrefs.append(href)
    return {
        "title": title,
        "has_cloudflare_challenge": (
            "challenge-platform" in lowered
            or "cf-mitigated" in lowered
            or (title == "blinkit | Error Page")
        ),
        "has_error_page_title": title == "blinkit | Error Page",
        "href_count": len(hrefs),
        "hrefs": hrefs,
        "job_like_hrefs": [h for h in hrefs if re.search(
            r"job|position|opening|vacanc|career|apply", h, re.I)],
        "body_chars": len(text),
    }


class Blinkit(Adapter):
    """Fetch only Blinkit's commissioned official entry; escalate on WAF."""

    VERIFICATION_REVISION = "blinkit.official-careers-entry.stealth-escalation.2"
    ENTRY_HOSTS = frozenset({"blinkit.com", "www.blinkit.com"})

    @classmethod
    def _validate_entry_url(cls, entry_url: str) -> None:
        parsed = urlsplit(str(entry_url))
        if parsed.scheme != "https":
            raise ValueError(f"Blinkit dedicated flow requires HTTPS: {entry_url}")
        if parsed.hostname not in cls.ENTRY_HOSTS:
            raise ValueError(f"Blinkit dedicated flow must start from blinkit.com: {entry_url}")
        if parsed.path.rstrip("/") != "/careers":
            raise ValueError(f"Blinkit dedicated flow must start from /careers: {entry_url}")
        if parsed.query or parsed.fragment:
            raise ValueError(f"Blinkit dedicated flow entry must not carry query/fragment: {entry_url}")

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        self._validate_entry_url(entry_url)
        if not self.page_guard():
            return self.result
        self.result.expected_total = None
        self.result.listing_complete = False
        self.result.metadata["membership_authority"] = (
            "Blinkit official https://blinkit.com/careers only; "
            "parent Eternal/Zomato pages and ATS boards are not membership."
        )

        entry_started = time.perf_counter()
        response = None
        entry_error = None
        try:
            response = await self.http.get(entry_url)
        except Exception as exc:  # 403/429/timeout; response already archived.
            entry_error = f"{type(exc).__name__}: {exc}"

        html = response.text if response is not None else ""
        final_url = str(response.url) if response is not None else entry_url
        status = response.status_code if response is not None else None
        inspected = inspect_entry_html(html)
        challenge = access.detect_challenge(
            html, dict(response.headers) if response is not None else None
        )

        # A blocked entry is escalated, not accepted as final. The stealth
        # browser earns the challenge cookie and, when a solver key is
        # configured, solves the widget; the settled body is then inspected.
        access_page = None
        if entry_error is not None or challenge is not None:
            access_page = await access.fetch_with_stealth(entry_url)
            if access_page is not None:
                if access_page.ok:
                    html = access_page.html
                    final_url = access_page.final_url
                    status = access_page.status
                    inspected = inspect_entry_html(html)
                    challenge = None
                else:
                    self.result.errors.append(
                        "Blinkit stealth escalation did not settle the entry: "
                        f"{access_page.challenge or access_page.error or 'unknown challenge'}"
                    )
            else:
                self.result.errors.append(
                    "Blinkit entry is challenged and stealth escalation is disabled "
                    "in this environment (SCRAPER_STEALTH=0)."
                )

        if entry_error is not None and access_page is None:
            self.result.errors.append(
                f"Blinkit official entry {entry_url} unreachable: {entry_error}"
            )

        entry_resolved = inspected["has_cloudflare_challenge"] is False and entry_error is None
        still_blocked = inspected["has_cloudflare_challenge"] or (
            entry_error is not None and not entry_resolved
        )
        final_host = (urlsplit(final_url).hostname or "").lower()
        offsite_redirect = entry_resolved and final_host not in self.ENTRY_HOSTS
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": entry_url,
            "official_entry_final_url": final_url,
            "official_entry_status": status,
            "official_entry_title": inspected["title"],
            "entry_body_chars": inspected["body_chars"],
            "entry_href_count": inspected["href_count"],
            "entry_hrefs": inspected["hrefs"][:50],
            "entry_job_like_hrefs": inspected["job_like_hrefs"][:50],
            "entry_has_cloudflare_challenge": inspected["has_cloudflare_challenge"],
            "entry_has_error_page_title": inspected["has_error_page_title"],
            "entry_fetch_failed": entry_error is not None and access_page is None,
            "entry_resolved": entry_resolved,
            "official_entry_offsite_redirect": final_url if offsite_redirect else None,
            # A 200 is still not membership: no native Blinkit listing parser
            # has been reviewed, so the listing is never marked inspected.
            "listing_inspected": False,
            "waf_or_challenge_stop": still_blocked,
            "access": access.attempt_metadata(
                access_page, url=entry_url,
                fallback_reason="entry not challenged on plain HTTP" if challenge is None else "",
            ),
            "ui_pagination": (
                "Unknown; no verified native listing structure observed."
                if inspected["has_cloudflare_challenge"] else
                "Entry resolved; listing structure must be reviewed before collection."
            ),
            "expected_total_unknown": True,
            "notes": (
                "Plain HTTP met a Cloudflare challenge; the stealth browser was "
                "used on the same commissioned Blinkit entry. No parent/ATS "
                "substitution and no zero-opening claim is made."
                if access_page is not None else
                "Entry inspected over plain HTTP."
            ),
        })

        if still_blocked:
            self.result.errors.append(
                f"Blinkit entry {final_url} (HTTP {status}, title {inspected['title']!r}) "
                "still exposes no verified Blinkit-owned listing/pagination/detail "
                "structure after stealth escalation; refusing to claim zero openings "
                "or substitute a parent/ATS board."
            )
        elif offsite_redirect:
            self.result.errors.append(
                f"Blinkit entry redirected off the commissioned Blinkit host to "
                f"{final_url} (HTTP {status}, title {inspected['title']!r}); the parent "
                "corporate page is not a Blinkit membership listing and is not "
                "substituted. Membership remains uncollected."
            )
        else:
            # The entry resolved, but this flow has no reviewed listing parser
            # yet. Record the observed surface instead of inventing membership.
            self.result.errors.append(
                f"Blinkit entry {final_url} resolved (HTTP {status}, title "
                f"{inspected['title']!r}) but exposes no verified native listing "
                "structure; membership remains uncollected."
            )

        self.result.timings["entry_phase_seconds"] = round(
            time.perf_counter() - entry_started, 6
        )
        self.result.timings["source_snapshot_phase_seconds"] = round(
            time.perf_counter() - started, 6
        )
        return self.result
