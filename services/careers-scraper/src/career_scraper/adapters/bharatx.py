"""BharatX official homepage -> /careers/ -> 'Check open roles' Workable journey.

Registry URL is the homepage (https://bharatx.tech/). Published journey:
homepage ``Careers`` link -> https://bharatx.tech/careers/ -> the page's
``Check open roles`` CTA -> https://apply.workable.com/bharatx/?lng=en.

Round-1 established the CTA board HTML is a Workable SPA shell with
Cloudflare challenge markers (``window.__CF$cv$params``,
``/cdn-cgi/challenge-platform/``, Turnstile site key) and zero
server-rendered anchors. Round-2 (2026-11-08) inspected the rendered UI with
headless Playwright Chromium: the exact CTA renders ``Careers at BharatX /
Job Openings / Sorry, no job openings at the moment.`` with zero job cards,
and the page's own XHR calls succeed (HTTP 200, no challenge, no solver).

Membership authority (company-scoped, same ``bharatx`` slug as the CTA URL,
observed via the board page's network — no guessed slug, no login):

- ``GET /api/v1/accounts/bharatx/jobs/count`` -> ``{"incountry":0,"total":0}``
- ``GET /api/v1/accounts/bharatx?full=true&lng=en`` -> identity
  (``subdomain=="bharatx"``, ``name=="BharatX"``, ``url=="https://bharatx.tech/"``)
- ``POST /api/v3/accounts/bharatx/jobs`` body
  ``{"query":"","department":[],"location":[],"workplace":[],"worktype":[]}``
  -> ``{"results":[],"total":0}``
- ``GET /api/v3/accounts/bharatx/jobs/filters`` ->
  ``{"departments":[],"locations":[],"remotes":[],"workplaces":[],"worktypes":[]}``

All endpoints agree at total 0 and the rendered UI shows zero job cards,
so membership is a verified zero vacancy (``expected_total==0``,
``listing_complete==True``). A non-zero listing is never collected here:
no live detail body has been inspected, so a non-zero response stops as a
fresh-inspection requirement (failed, unknown detail state) rather than
emitting summaries as verified postings. Any API failure/429/WAF response
stops as failed with unknown membership (``expected_total is None``).
"""
import hashlib
import json
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .. import access
from ..http import FetchError
from .base import Adapter


HOMEPAGE_URL = "https://bharatx.tech/"
CAREERS_URL = "https://bharatx.tech/careers/"
BOARD_URL = "https://apply.workable.com/bharatx/?lng=en"
CTA_TEXT = "Check open roles"
CAREERS_HOSTS = {"bharatx.tech", "www.bharatx.tech"}
BOARD_HOST = "apply.workable.com"

# Company-scoped board data endpoints: the CTA page's own XHR calls, observed
# in headless Chromium (round-2). Slug ``bharatx`` is the CTA URL's own slug.
COUNT_URL = "https://apply.workable.com/api/v1/accounts/bharatx/jobs/count"
ACCOUNT_URL = "https://apply.workable.com/api/v1/accounts/bharatx?full=true&lng=en"
JOBS_URL = "https://apply.workable.com/api/v3/accounts/bharatx/jobs"
FILTERS_URL = "https://apply.workable.com/api/v3/accounts/bharatx/jobs/filters"
JOBS_POST_BODY = {"query": "", "department": [], "location": [], "workplace": [], "worktype": []}

# WAF / challenge indicators reviewed on the company-published board HTML shell.
CHALLENGE_MARKERS = (
    "window.__CF$cv$params",
    "/cdn-cgi/challenge-platform/",
    "challenge-platform",
    "turnstileWidgetSiteKey",
    "Just a moment",
    "cf-challenge",
)


def challenge_indicators(body_text):
    """Return the challenge/WAF markers present in a board response body."""
    return [marker for marker in CHALLENGE_MARKERS if marker in (body_text or "")]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _response_summary(response):
    content = response.content or b""
    return {
        "url": str(response.url),
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "bytes": len(content),
        "sha256": _sha256(content),
    }


def _cta_anchors(soup):
    """All 'Check open roles' CTA anchors (case-insensitive text match)."""
    return [
        anchor for anchor in soup.find_all("a", href=True)
        if anchor.get_text(" ", strip=True).lower() == CTA_TEXT.lower()
    ]


def _careers_anchors(soup):
    """All anchors linking exactly to the official /careers/ page."""
    return [
        anchor for anchor in soup.find_all("a", href=True)
        if anchor.get("href") == CAREERS_URL
    ]


def parse_count_payload(payload):
    """Validate and normalise the /jobs/count payload."""
    if not isinstance(payload, dict):
        raise ValueError("BharatX jobs/count payload is not an object")
    total = payload.get("total")
    incountry = payload.get("incountry")
    if not isinstance(total, int) or isinstance(total, bool):
        raise ValueError("BharatX jobs/count total is not an integer")
    if not isinstance(incountry, int) or isinstance(incountry, bool):
        raise ValueError("BharatX jobs/count incountry is not an integer")
    return {"total": total, "incountry": incountry}


def parse_account_payload(payload):
    """Validate that the account payload is the CTA board's own tenant."""
    if not isinstance(payload, dict):
        raise ValueError("BharatX account payload is not an object")
    if payload.get("subdomain") != "bharatx":
        raise ValueError(f"BharatX account subdomain changed: {payload.get('subdomain')!r}")
    if payload.get("name") != "BharatX":
        raise ValueError(f"BharatX account name changed: {payload.get('name')!r}")
    if payload.get("url") != HOMEPAGE_URL:
        raise ValueError(f"BharatX account url changed: {payload.get('url')!r}")
    return {
        "id": payload.get("id"),
        "uid": payload.get("uid"),
        "subdomain": payload.get("subdomain"),
        "name": payload.get("name"),
        "url": payload.get("url"),
    }


def parse_jobs_payload(payload):
    """Validate and normalise the v3 jobs payload."""
    if not isinstance(payload, dict):
        raise ValueError("BharatX v3 jobs payload is not an object")
    total = payload.get("total")
    results = payload.get("results")
    if not isinstance(total, int) or isinstance(total, bool):
        raise ValueError("BharatX v3 jobs total is not an integer")
    if not isinstance(results, list):
        raise ValueError("BharatX v3 jobs results is not a list")
    if total != len(results):
        raise ValueError(
            f"BharatX v3 jobs total ({total}) differs from results length ({len(results)})"
        )
    return {"total": total, "results": results}


def parse_filters_payload(payload):
    """Validate and normalise the v3 filters payload."""
    if not isinstance(payload, dict):
        raise ValueError("BharatX v3 filters payload is not an object")
    normalised = {}
    for key in ("departments", "locations", "remotes", "workplaces", "worktypes"):
        value = payload.get(key)
        if not isinstance(value, list):
            raise ValueError(f"BharatX v3 filters {key} is not a list")
        normalised[key] = value
    return normalised


def _json_body(response):
    try:
        return json.loads(response.content.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"BharatX API response is not JSON ({response.url}): {exc}") from exc


class BharatX(Adapter):
    """Follow the registry homepage journey, then reconcile board API counts."""

    VERIFICATION_REVISION = "bharatx.workable-zero-vacancy.1"

    @staticmethod
    def _require_official(url, *, hostnames, exact=None):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in hostnames:
            raise ValueError(f"BharatX official page left the reviewed HTTPS host set: {url}")
        if exact is not None and str(url) != exact:
            raise ValueError(f"BharatX official page redirected away from its exact URL: {url}")

    async def _get_official(self, url, *, hostnames, exact):
        response = await self.http.get(url)
        self._require_official(response.url, hostnames=hostnames, exact=exact)
        return response

    async def run(self):
        started = self.result._started_clock
        journey_started = time.perf_counter()
        if not self.page_guard():
            return self.result
        if self.company.careers_url != HOMEPAGE_URL:
            raise ValueError(
                "BharatX dedicated flow must start from the registry homepage "
                f"{HOMEPAGE_URL}, got {self.company.careers_url!r}"
            )

        try:
            homepage = await self._get_official(
                HOMEPAGE_URL, hostnames=CAREERS_HOSTS, exact=HOMEPAGE_URL
            )
        except FetchError as exc:
            self.result.errors.append(f"BharatX official homepage request failed: {exc}")
            return self._finish(started, journey_started, homepage=None, careers=None)

        home_soup = BeautifulSoup(homepage.text, "lxml")
        careers_links = _careers_anchors(home_soup)
        if not careers_links:
            raise ValueError(
                "BharatX homepage no longer links to its official /careers/ page"
            )

        try:
            careers = await self._get_official(
                CAREERS_URL, hostnames=CAREERS_HOSTS, exact=CAREERS_URL
            )
        except FetchError as exc:
            self.result.errors.append(f"BharatX official careers page request failed: {exc}")
            return self._finish(started, journey_started, homepage=homepage, careers=None)

        careers_soup = BeautifulSoup(careers.text, "lxml")
        ctas = _cta_anchors(careers_soup)
        if not ctas:
            raise ValueError(
                "BharatX official careers page no longer exposes a 'Check open roles' CTA"
            )
        drifted = sorted({(anchor.get("href") or "") for anchor in ctas if anchor.get("href") != BOARD_URL})
        if drifted:
            raise ValueError(
                "BharatX 'Check open roles' CTA drifted from its reviewed board URL "
                f"{BOARD_URL}: {drifted}"
            )
        self.result.timings["journey_phase_seconds"] = round(
            time.perf_counter() - journey_started, 6
        )

        # Board HTML shell: provenance only (SPA + challenge markers expected).
        listing_started = time.perf_counter()
        board = {}
        try:
            board_response = await self.http.get(BOARD_URL)
        except FetchError as exc:
            self.result.errors.append(
                f"BharatX company-published board shell request failed: {exc}; "
                "continuing to the board page's own data endpoints for membership"
            )
            board = {"url": BOARD_URL, "error": str(exc), "readable_listing": False}
        else:
            if str(board_response.url) != BOARD_URL:
                raise ValueError(
                    "BharatX board request redirected away from its exact CTA URL"
                )
            board_summary = _response_summary(board_response)
            board_soup = BeautifulSoup(board_response.text, "lxml")
            indicators = challenge_indicators(board_response.text)
            anchors = board_soup.find_all("a", href=True)
            headings = board_soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
            board_self = [
                anchor for anchor in anchors
                if (anchor.get("href") or "").startswith("https://apply.workable.com/bharatx/")
            ]
            board = {
                **board_summary,
                "title": board_soup.title.string.strip()
                if board_soup.title and board_soup.title.string else None,
                "challenge_indicators": indicators,
                "anchor_count": len(anchors),
                "heading_count": len(headings),
                "board_self_anchor_count": len(board_self),
                "readable_listing": False,
                "note": (
                    "HTML shell is a Workable SPA (challenge markers expected); "
                    "membership authority is the board's own JSON endpoints below, "
                    "not this shell."
                ),
            }

        # Company-scoped data endpoints: the CTA page's own XHR calls. Same
        # slug as the CTA URL; no guessing and no login. Plain HTTP is tried
        # first; a challenge is escalated to the stealth browser, which fetches
        # the same official endpoints with a solved, warmed cookie jar.
        api_started = time.perf_counter()
        summaries = {}
        access_attempts = {}
        count = account = jobs_payload = filters = None
        self._access_attempts = access_attempts

        async def _collect_plain():
            nonlocal count, account, jobs_payload, filters
            count_response = await self.http.get(COUNT_URL)
            count = parse_count_payload(_json_body(count_response))
            summaries["count"] = _response_summary(count_response)

            account_response = await self.http.get(ACCOUNT_URL)
            account = parse_account_payload(_json_body(account_response))
            summaries["account"] = _response_summary(account_response)

            jobs_response = await self.http.request("POST", JOBS_URL, json=JOBS_POST_BODY)
            jobs_payload = parse_jobs_payload(_json_body(jobs_response))
            summaries["jobs"] = _response_summary(jobs_response)

            filters_response = await self.http.get(FILTERS_URL)
            filters = parse_filters_payload(_json_body(filters_response))
            summaries["filters"] = _response_summary(filters_response)

        try:
            await _collect_plain()
        except (FetchError, ValueError) as exc:
            self.result.errors.append(
                f"BharatX plain-HTTP board endpoint failed ({type(exc).__name__}: {exc}); "
                "escalating to the stealth browser for the same official endpoints."
            )
            if isinstance(exc, ValueError):
                self.result.errors.append(
                    f"BharatX board data endpoint payload failed scope validation: {exc}; "
                    "current membership is unknown (not zero openings)"
                )
            specs = (
                ("count", COUNT_URL, "GET", None, parse_count_payload),
                ("account", ACCOUNT_URL, "GET", None, parse_account_payload),
                ("jobs", JOBS_URL, "POST", JOBS_POST_BODY, parse_jobs_payload),
                ("filters", FILTERS_URL, "GET", None, parse_filters_payload),
            )
            for key, url, method, body, parser in specs:
                payload, page = await access.fetch_json_via_browser(
                    url, method=method, body=body, warm_url=BOARD_URL
                )
                if page is None:
                    access_attempts[key] = {
                        "outcome": "stealth_disabled",
                        "url": url,
                        "policy_version": access.POLICY_VERSION,
                    }
                    continue
                access_attempts[key] = access.attempt_metadata(page, url=url)
                if payload is None:
                    continue
                try:
                    value = parser(payload)
                except ValueError as parse_exc:
                    self.result.errors.append(
                        f"BharatX browser-fetched {key} payload failed validation: {parse_exc}"
                    )
                    continue
                summaries[f"{key}_browser"] = {
                    "url": page.final_url,
                    "status": page.status,
                    "techniques": page.techniques,
                }
                if key == "count":
                    count = value
                elif key == "account":
                    account = value
                elif key == "jobs":
                    jobs_payload = value
                elif key == "filters":
                    filters = value
            if None in (count, account, jobs_payload, filters):
                self.result.errors.append(
                    "BharatX board is still inaccessible after the stealth-browser "
                    "attempt; current membership is unknown (not zero openings)"
                )
                return self._finish(
                    started, journey_started, homepage=homepage, careers=careers,
                    board=board, cta_count=len(ctas), api_summaries=summaries,
                    listing_seconds=round(time.perf_counter() - listing_started, 6),
                )

        listing_seconds = round(time.perf_counter() - listing_started, 6)
        api_seconds = round(time.perf_counter() - api_started, 6)
        membership = {
            "account": account,
            "count_total": count["total"],
            "count_incountr": count["incountry"],
            "jobs_total": jobs_payload["total"],
            "jobs_count": len(jobs_payload["results"]),
            "filters": filters,
            "api_summaries": summaries,
            "pagination": "Unfiltered POST returns total plus every result row; total equals results length; no page 2.",
            "filters_note": "All filter dimensions are empty lists, consistent with zero live postings; no department/location/workplace/worktype option is hidden.",
            "rendered_notice": "Sorry, no job openings at the moment. (headless Chromium, Careers at BharatX / Job Openings, zero job cards)",
        }

        totals = {count["total"], jobs_payload["total"], len(jobs_payload["results"])}
        if len(totals) != 1:
            self.result.errors.append(
                f"BharatX board counts disagree (count={count['total']}, jobs_total="
                f"{jobs_payload['total']}, results={len(jobs_payload['results'])}); "
                "current membership is unknown (not zero openings)"
            )
            return self._finish(
                started, journey_started, homepage=homepage, careers=careers,
                board=board, cta_count=len(ctas), api_summaries=summaries,
                membership=membership, listing_seconds=listing_seconds, api_seconds=api_seconds,
            )

        total = count["total"]
        if total != 0:
            # No live detail body has been inspected on this board; do not emit
            # summaries as verified postings. Require fresh inspection first.
            self.result.errors.append(
                f"BharatX board is non-zero (total={total}) but no live detail body has "
                "been inspected; summaries were not collected as verified postings. "
                "A fresh listing/detail inspection (at least two full bodies) must "
                "land before collection; current membership is uncollected."
            )
            return self._finish(
                started, journey_started, homepage=homepage, careers=careers,
                board=board, cta_count=len(ctas), api_summaries=summaries,
                membership=membership, listing_seconds=listing_seconds, api_seconds=api_seconds,
            )

        # Verified zero vacancy: all counts agree at 0 with empty results and
        # empty filters, account identity matches the CTA tenant.
        self.result.listing_complete = True
        self.result.expected_total = 0
        self.result.jobs = []
        return self._finish(
            started, journey_started, homepage=homepage, careers=careers,
            board=board, cta_count=len(ctas), api_summaries=summaries,
            membership=membership, listing_seconds=listing_seconds, api_seconds=api_seconds,
            complete=True,
        )

    def _finish(self, started, journey_started, *, homepage, careers, board,
                cta_count=0, api_summaries=None, membership=None,
                listing_seconds=None, api_seconds=None, complete=False):
        if not complete:
            self.result.listing_complete = False
            self.result.expected_total = None  # unknown, never zero
        summary = {
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": HOMEPAGE_URL,
            "registry_careers_url": HOMEPAGE_URL,
            "homepage": _response_summary(homepage) if homepage is not None else None,
            "homepage_careers_anchor_count": (
                len(_careers_anchors(BeautifulSoup(homepage.text, "lxml")))
                if homepage is not None else None
            ),
            "careers_page": _response_summary(careers) if careers is not None else None,
            "careers_page_cta_count": cta_count,
            "careers_page_cta_href": BOARD_URL,
            "membership_authority": (
                "Registry homepage's Careers link is the reviewed path to the official "
                "/careers/ page; the page's exact 'Check open roles' CTA URL is the only "
                "jobs-journey authority. Listing membership is the CTA board page's own "
                "company-scoped JSON endpoints (same bharatx slug as the CTA URL), "
                "observed via headless-Chromium network capture. No guessed slug, "
                "login link, parent board, or staging mirror was used."
            ),
            "board": board,
            "api_summaries": api_summaries or {},
            "membership": membership,
            "zero_vacancy": bool(complete),
            "ats_enumeration_used": False,
            "access_attempts": getattr(self, "_access_attempts", {}),
            "bypass_attempted": any(
                attempt.get("bypass_attempted")
                for attempt in getattr(self, "_access_attempts", {}).values()
                if isinstance(attempt, dict)
            ),
            "retry_policy": (
                "Re-run the same official journey. If plain HTTP is challenged, the "
                "same company-scoped JSON endpoints are retried through the stealth "
                "browser (UA/fingerprint profile, proxy when configured, CAPTCHA "
                "solver when configured) and the attempt is recorded in "
                "access_attempts. If the board becomes non-zero, inspect at least "
                "two full live detail bodies before emitting rows. An empty board "
                "is a verified zero vacancy only when count, jobs, filters, "
                "rendered UI and request evidence all reconcile at 0."
            ),
            "request_urls": [HOMEPAGE_URL, CAREERS_URL, BOARD_URL, COUNT_URL,
                             ACCOUNT_URL, JOBS_URL, FILTERS_URL],
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        }
        self.result.metadata.update(summary)
        if "journey_phase_seconds" not in self.result.timings:
            self.result.timings["journey_phase_seconds"] = round(
                time.perf_counter() - journey_started, 6
            )
        if listing_seconds is not None:
            self.result.timings["listing_phase_seconds"] = listing_seconds
        if api_seconds is not None:
            self.result.timings["listing_api_phase_seconds"] = api_seconds
        if complete:
            self.result.timings["detail_phase_seconds"] = 0.0
        return self.result
