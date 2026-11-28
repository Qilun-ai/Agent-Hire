"""Steradian Semiconductors Pvt Ltd — inspected official careers journey (2026-11-08).

The ledger entry ``https://www.steradiansemi.com/`` is the company's historical
domain.  It **no longer serves a Steradian site**: the homepage is a three-hop
redirect to an unrelated online-casino affiliate site::

    https://www.steradiansemi.com/  -> 308 -> https://steradiansemi.com/
    https://steradiansemi.com/      -> 301 -> https://www.npspindia.org/
    https://www.npspindia.org/      -> 301 -> https://www.baro-india.com/
    https://www.baro-india.com/     -> 200   "Best Online Casinos in India 2026"

Every path under the domain answers that unrelated site's 404 page (including
``/careers/``, ``/careers``, ``/jobs`` and ``/join-us``), and ``/about`` is the
casino site's own About page.  RDAP records a registration change on
2026-07-28 and Cloudflare nameservers; Wayback shows the genuine company site
was last served in March 2023 and the domain redirected from late 2023 onward.

The last genuine official careers page (archived 2023-03-22) published seven
stale 2022-era WordPress "WP Job Openings" cards; the site has been unreachable
since then, so those records are historical evidence only and are not current
membership.  The last company-looking home copy (2026-06-14) had a Careers link
that decoded to ``mailto:contact@steradiansemi.com`` with no listing.  No ATS,
listing API, pagination, filters, job IDs or current descriptions are published
anywhere on the live journey.

This adapter therefore fails closed: it records the exact redirect/404 evidence
and an unknown membership instead of a successful zero-job listing.  It never
parses the casino page for jobs, never queries a parent board (Renesas acquired
Steradian in 2022) or an aggregator, and never guesses an ATS tenant.  If the
domain starts serving a Steradian page again, or the careers route starts
returning company content, it reports ``unreviewed_careers_journey_detected``
so the new journey is inspected before any collection.
"""
from __future__ import annotations

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


OFFICIAL_ENTRY = "https://www.steradiansemi.com/"
APEX_ENTRY = "https://steradiansemi.com/"
LAST_KNOWN_CAREERS_ROUTE = "https://steradiansemi.com/careers/"
COMPANY_HOSTS = frozenset({"steradiansemi.com", "www.steradiansemi.com"})
REVIEWED_FINAL_HOST = "www.baro-india.com"
REVIEWED_CASINO_404_TITLE = "Page not found - Online Casinos"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

CASINO_PATTERN = re.compile(r"(?i)\b(?:online\s+casinos?|casino|casinos|betting|"
                            r"sportsbook|slot\s+games?|teen\s+patti)\b")
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
# Aggregator hostnames are only a listing signal for their job-search paths; a
# LinkedIn company profile link is identity, not a job board.
AGGREGATOR_HOST = re.compile(
    r"(?i)(?:^|\.)(?:linkedin\.com|indeed\.com|glassdoor\.com|instahyre\.com|"
    r"wellfound\.com|cutshort\.io|naukri\.com)$"
)
AGGREGATOR_JOB_PATH = re.compile(
    r"(?i)^/(?:jobs?|careers?|vacanc|job-search|company/[^/]+/jobs)")
# Career/job *paths* on the company's own host.  Segment-anchored.
CAREER_PATH = re.compile(
    r"(?i)^/(?:careers?|jobs?|roles?|open-roles|open-positions|join-us|"
    r"we-are-hiring|hiring|positions?|openings?)(?:/|$)"
)
JOB_CARD_CLASS = re.compile(r"(?i)job-(?:card|listing|item|post)\b")


def _is_job_card(tag) -> bool:
    return bool(JOB_CARD_CLASS.search(" ".join(tag.get("class") or [])))


def normalize_text(value: str | None) -> str:
    value = (value or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", value).strip()


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines()
                     if line.strip())


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _absolute(response_url: str, href: str) -> str:
    return urljoin(response_url, (href or "").strip())


def has_company_identity(html: str) -> bool:
    return "steradian" in normalize_text(visible_text(html)).lower()


def page_signals(html: str, response_url: str) -> dict:
    """Describe every job-listing/identity signal on one fetched page."""
    soup = BeautifulSoup(html, "lxml")
    anchors, listing_links, ats_links, ats_embeds = [], [], [], []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = _absolute(response_url, href)
        item = {
            "text": normalize_text(anchor.get_text(" ", strip=True)),
            "href": href,
            "url": absolute,
        }
        anchors.append(item)
        host, path = _host(absolute), urlsplit(absolute).path
        if ATS_HOST.search(host) or (AGGREGATOR_HOST.search(host)
                                     and AGGREGATOR_JOB_PATH.match(path)):
            ats_links.append(item)
        elif host in COMPANY_HOSTS and CAREER_PATH.match(path):
            listing_links.append(item)
    for tag in soup.select("iframe[src], script[src], form[action], link[href]"):
        attribute = next((name for name in ("src", "action", "href")
                          if tag.get(name)), None)
        if attribute is None:
            continue
        absolute = _absolute(response_url, tag.get(attribute))
        if ATS_HOST.search(_host(absolute)):
            ats_embeds.append({
                "tag": tag.name,
                "attribute": attribute,
                "url": absolute,
            })
    text = visible_text(html)
    normalized = normalize_text(text)
    canonical_tag = soup.select_one('link[rel="canonical"]')
    cards = [tag for tag in soup.find_all(True) if _is_job_card(tag)]
    top_level_cards = [
        tag for tag in cards
        if not any(_is_job_card(parent) for parent in tag.parents)
    ]
    job_posting_count = 0
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text() or ""
        if re.search(r'"@type"\s*:\s*"JobPosting"', raw):
            job_posting_count += 1
    return {
        "response_url": response_url,
        "title": normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "h1": [normalize_text(node.get_text(" ", strip=True)) for node in soup.select("h1")],
        "canonical": canonical_tag.get("href") if canonical_tag else None,
        "anchors": anchors,
        "anchor_count": len(anchors),
        "listing_links": listing_links,
        "external_ats_links": ats_links,
        "ats_embeds": ats_embeds,
        "job_posting_schema_count": job_posting_count,
        "job_card_like_count": len(top_level_cards),
        "company_identity_present": "steradian" in normalized.lower(),
        "casino_or_unrelated_marker": bool(CASINO_PATTERN.search(text)),
        "visible_text_chars": len(text),
        "normalized_text_chars": len(normalized),
        "normalized_text_sha256": sha256(normalized.encode()).hexdigest(),
    }


def has_listing_signal(parsed: dict) -> bool:
    return bool(
        parsed["listing_links"]
        or parsed["external_ats_links"]
        or parsed["ats_embeds"]
        or parsed["job_posting_schema_count"]
        or parsed["job_card_like_count"]
    )


def parse_live_entry(*, status_code: int, final_url: str, html: str,
                     history: list[dict] | None = None) -> dict:
    """Validate the ledger-URL journey and classify what it now serves.

    ``history`` items are ``{"status", "url", "location"}`` hops observed before
    ``final_url``.  A reviewed journey starts on a Steradian host; an empty
    history is accepted (a direct response means the journey changed and the
    adapter will fail closed).  Anything that ends on a non-Steradian host is
    classified as an unrelated-host redirect, never as a listing.
    """
    hops = list(history or [])
    for hop in hops:
        status = hop.get("status")
        if status is not None and status not in REDIRECT_STATUSES:
            raise ValueError(f"unexpected redirect status {status} in {hop.get('url')}")
    if hops:
        first_host = _host(hops[0].get("url") or "")
        if first_host not in COMPANY_HOSTS:
            raise ValueError(
                f"ledger careers URL journey no longer starts on steradiansemi.com: {hops[0]}")
    if status_code in REDIRECT_STATUSES:
        raise ValueError(f"official entry ended on redirect status {status_code}")
    if status_code >= 400:
        raise ValueError(f"official entry returned HTTP {status_code}")

    parsed = page_signals(html, final_url)
    final_host = _host(final_url)
    if final_host not in COMPANY_HOSTS:
        kind = "unrelated_host"
    elif parsed["company_identity_present"]:
        kind = "company_site"
    elif parsed["casino_or_unrelated_marker"]:
        kind = "unrelated_content_on_company_host"
    else:
        kind = "unknown_company_host_page"
    parsed.update({
        "requested_url": hops[0]["url"] if hops else final_url,
        "final_url": final_url,
        "final_host": final_host,
        "status_code": status_code,
        "redirects": hops,
        "unrelated_host": final_host not in COMPANY_HOSTS,
        "kind": kind,
        "html_text_sha256": sha256(html.encode("utf-8")).hexdigest(),
    })
    return parsed


def parse_careers_probe(*, status_code: int, final_url: str, html: str) -> dict:
    """Classify the domain's last known careers route (live response)."""
    parsed = page_signals(html, final_url)
    if status_code == 404:
        kind = "http_404"
    elif status_code == 200 and parsed["company_identity_present"]:
        kind = "company_careers_page"
    elif status_code == 200 and parsed["casino_or_unrelated_marker"]:
        kind = "unrelated_page"
    elif status_code == 200:
        kind = "unknown_page"
    else:
        kind = "unexpected_status"
    parsed.update({
        "status_code": status_code,
        "final_url": final_url,
        "final_host": _host(final_url),
        "kind": kind,
        "title_matches_reviewed_casino_404": (
            normalize_text(parsed["title"]) == REVIEWED_CASINO_404_TITLE),
        "html_text_sha256": sha256(html.encode("utf-8")).hexdigest(),
    })
    return parsed


def _redirect_evidence(response) -> list[dict]:
    return [
        {
            "status": item.status_code,
            "url": str(item.url),
            "location": item.headers.get("location"),
        }
        for item in response.history
    ]


def _journey_signature(parsed: dict) -> tuple:
    return (
        parsed["final_url"],
        parsed["status_code"],
        tuple((hop.get("status"), hop.get("url"), hop.get("location"))
              for hop in parsed["redirects"]),
    )


class SteradianSemiconductorsPvtLtd(Adapter):
    """Read the live ledger journey and record that it is no longer the company site."""

    VERIFICATION_REVISION = "steradian-semiconductors-pvt-ltd.hijacked-domain.1"

    # ------------------------------------------------------------- fetch evidence
    def _request_log_lines(self) -> int:
        if not self.http.output:
            return 0
        path = self.http.output / "requests.jsonl"
        if not path.exists():
            return 0
        return len(path.read_text(encoding="utf-8").splitlines())

    def _last_capture(self, url: str, since_line: int) -> dict:
        if not self.http.output:
            return {}
        path = self.http.output / "requests.jsonl"
        if not path.exists():
            return {}
        lines = path.read_text(encoding="utf-8").splitlines()
        for raw in reversed(lines[since_line:]):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if (record.get("url") == url or record.get("final_url") == url
                    or url in record.get("redirects", [])):
                return record
        return {}

    def _captured_body(self, record: dict) -> bytes | None:
        digest = record.get("sha256")
        if not digest or not self.http.output:
            return None
        path = self.http.output / "responses" / f"{digest}.gz"
        if not path.exists():
            return None
        return gzip.decompress(path.read_bytes())

    async def _fetch(self, url: str, purpose: str):
        """GET through the shared client; on 4xx read the archived body back."""
        if not self.page_guard():
            return None, {"url": url, "purpose": purpose,
                          "outcome": "page_budget_exhausted"}
        since = self._request_log_lines()
        started = time.perf_counter()
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            capture = self._last_capture(url, since)
            raw = self._captured_body(capture)
            record = {
                "url": url,
                "purpose": purpose,
                "outcome": "request_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "final_url": capture.get("final_url"),
                "status_code": capture.get("status"),
                "html_bytes": len(raw) if raw is not None else None,
                "html_sha256": capture.get("sha256"),
                "redirects": [
                    {"url": item, "status": None, "location": None}
                    for item in capture.get("redirects", [])
                ],
                "redirect_hop_statuses_known": False,
                "body": raw,
                "duration_seconds": max(0.0, time.perf_counter() - started),
            }
            return None, record
        raw = response.content
        return response, {
            "url": url,
            "purpose": purpose,
            "outcome": "received",
            "final_url": str(response.url),
            "status_code": response.status_code,
            "html_bytes": len(raw),
            "html_sha256": sha256(raw).hexdigest(),
            "redirects": _redirect_evidence(response),
            "redirect_hop_statuses_known": True,
            "body": raw,
            "duration_seconds": max(0.0, time.perf_counter() - started),
        }

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _public_record(record: dict) -> dict:
        return {key: value for key, value in record.items() if key != "body"}

    @staticmethod
    def _evidence_from_record(record: dict) -> dict:
        return {
            "requested_url": record.get("url"),
            "final_url": record.get("final_url"),
            "status_code": record.get("status_code"),
            "html_bytes": record.get("html_bytes"),
            "html_sha256": record.get("html_sha256"),
            "redirects": record.get("redirects"),
            "redirect_hop_statuses_known": record.get("redirect_hop_statuses_known"),
            "error": record.get("error"),
            "fetched_at": now(),
        }

    @staticmethod
    def _entry_evidence(parsed: dict) -> dict:
        return {
            "kind": parsed["kind"],
            "requested_url": parsed["requested_url"],
            "final_url": parsed["final_url"],
            "final_host": parsed["final_host"],
            "status_code": parsed["status_code"],
            "canonical": parsed["canonical"],
            "title": parsed["title"],
            "h1": parsed["h1"],
            "company_identity_present": parsed["company_identity_present"],
            "casino_or_unrelated_marker": parsed["casino_or_unrelated_marker"],
            "visible_text_chars": parsed["visible_text_chars"],
            "normalized_text_sha256": parsed["normalized_text_sha256"],
            "html_text_sha256": parsed["html_text_sha256"],
            "redirects": parsed["redirects"],
            "redirect_hop_statuses_known": all(
                hop.get("status") is not None for hop in parsed["redirects"]),
        }

    # ---------------------------------------------------------------------- run
    async def run(self):
        source_started = time.perf_counter()

        def finish(seconds_key: str):
            elapsed = max(0.0, time.perf_counter() - source_started)
            self.result.metadata["source_collection_wall_seconds"] = elapsed
            self.result.timings[seconds_key] = elapsed
            self.result.expected_total = None
            self.result.listing_complete = False
            if "unreviewed_careers_journey_detected" in self.result.errors:
                outcome = "unreviewed_careers_journey_detected"
            elif self.result.errors and (
                    self.result.errors[0].startswith("steradian_official_entry_failed")
                    or self.result.errors[0].startswith("steradian_final_recheck_failed")):
                outcome = "failed_waitlist_journey_unavailable"
            else:
                outcome = "failed_waitlist_official_domain_hijacked"
            self.result.metadata["collection_outcome"] = outcome
            return self.result

        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "apex_entry_url": APEX_ENTRY,
            "last_known_careers_route": LAST_KNOWN_CAREERS_ROUTE,
            "reviewed_final_host": REVIEWED_FINAL_HOST,
            "listing_authority": (
                "none: the official domain no longer serves the company site; the ledger URL "
                "redirects to an unrelated online-casino host and the domain's careers paths "
                "are that host's 404 page"
            ),
            "detail_method": "none: no current role, requisition, detail route or apply route is published",
            "request_scope": (
                "Only the ledger URL, the domain's last known /careers/ route and one exact "
                "ledger recheck. No casino-site enumeration, no parent company board, no "
                "aggregator, no guessed ATS tenant."
            ),
            "verification_revision": self.VERIFICATION_REVISION,
            "historical_reference": {
                "note": (
                    "The genuine official site was last served in March 2023. Its last archived "
                    "careers page had 7 stale 2022-era posts; the site is unreachable today, so "
                    "those records are evidence only and are not exported as current vacancies."
                ),
                "evidence": "companies/steradian-semiconductors-pvt-ltd/journey-research.md",
                "archived_listing": (
                    "companies/steradian-semiconductors-pvt-ltd/source/"
                    "archived-official-careers-20230322.html"
                ),
            },
        })

        # 1. The ledger URL: the company's own domain, if it still served it.
        _, entry_record = await self._fetch(OFFICIAL_ENTRY, "official_careers_entry")
        body = entry_record.get("body")
        if body is None:
            self.result.metadata["initial_entry"] = self._evidence_from_record(entry_record)
            self.result.errors.append(
                f"steradian_official_entry_failed: "
                f"{entry_record.get('error') or 'response body unavailable'}")
            self.result.metadata["observed_journey"] = {
                "conclusion": (
                    "The ledger careers URL could not be fetched; current company membership "
                    "is unknown and no listing was substituted."
                ),
            }
            return finish("steradian_source_collection_seconds")
        try:
            entry = parse_live_entry(
                status_code=entry_record["status_code"],
                final_url=entry_record.get("final_url") or "",
                html=body.decode("utf-8", errors="replace"),
                history=entry_record.get("redirects") or [],
            )
        except Exception as exc:
            self.result.metadata["initial_entry"] = self._evidence_from_record(entry_record)
            self.result.errors.append(
                f"steradian_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.metadata["observed_journey"] = {
                "conclusion": (
                    "The ledger careers URL no longer matches the reviewed company journey; "
                    "the new response must be inspected before any membership is collected."
                ),
            }
            return finish("steradian_source_collection_seconds")

        self.result.metadata["initial_entry"] = {
            **self._evidence_from_record(entry_record),
            **self._entry_evidence(entry),
        }
        self.result.metadata["observed_journey"] = {
            "ledger_redirect_chain": entry["redirects"],
            "final_url": entry["final_url"],
            "final_host": entry["final_host"],
            "status_code": entry["status_code"],
            "kind": entry["kind"],
            "canonical": entry["canonical"],
            "title": entry["title"],
            "h1": entry["h1"],
            "anchor_count": entry["anchor_count"],
            "anchors": entry["anchors"],
            "listing_links": entry["listing_links"],
            "external_ats_links": entry["external_ats_links"],
            "ats_embeds": entry["ats_embeds"],
            "job_posting_schema_count": entry["job_posting_schema_count"],
            "job_card_like_count": entry["job_card_like_count"],
            "company_identity_present": entry["company_identity_present"],
            "casino_or_unrelated_marker": entry["casino_or_unrelated_marker"],
            "visible_text_chars": entry["visible_text_chars"],
            "normalized_text_sha256": entry["normalized_text_sha256"],
        }

        if entry["kind"] == "unrelated_host":
            self.result.errors.append("official_domain_redirects_to_unrelated_host")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The ledger URL leaves the company domain through 308/301/301 redirects and "
                f"lands on the unrelated host {entry['final_host']!r} "
                f"({entry['title']!r}), with no Steradian identity and no job listing. This is "
                "a hijacked/redirected domain, not an empty company listing."
            )
        elif entry["kind"] == "unrelated_content_on_company_host":
            self.result.errors.append("official_domain_serves_unrelated_content")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The ledger URL still lands on steradiansemi.com, but the page is unrelated "
                "casino content with no Steradian identity or listing. This is not the company site."
            )
        else:
            self.result.errors.append("unreviewed_careers_journey_detected")
            self.result.metadata["observed_journey"]["conclusion"] = (
                "The ledger URL now serves a Steradian-branded/unknown page instead of the "
                "reviewed redirect; the new journey must be inspected before any membership "
                "is collected."
            )
            return finish("steradian_source_collection_seconds")

        # 2. The domain's last known careers route: now the unrelated site's 404.
        _, careers_record = await self._fetch(
            LAST_KNOWN_CAREERS_ROUTE, "last_known_careers_route")
        careers_body = careers_record.get("body")
        careers = None
        if careers_body is None:
            self.result.metadata["careers_route"] = {
                **self._evidence_from_record(careers_record),
                "body_unavailable": True,
            }
            self.result.warnings.append("last_known_careers_route_body_unavailable")
        else:
            try:
                careers = parse_careers_probe(
                    status_code=careers_record["status_code"],
                    final_url=careers_record.get("final_url") or "",
                    html=careers_body.decode("utf-8", errors="replace"),
                )
            except Exception as exc:
                self.result.metadata["careers_route"] = {
                    **self._evidence_from_record(careers_record),
                    "parse_error": f"{type(exc).__name__}: {exc}",
                }
                self.result.warnings.append("last_known_careers_route_parse_failed")
            else:
                self.result.metadata["careers_route"] = {
                    **self._evidence_from_record(careers_record),
                    "kind": careers["kind"],
                    "title": careers["title"],
                    "canonical": careers["canonical"],
                    "company_identity_present": careers["company_identity_present"],
                    "casino_or_unrelated_marker": careers["casino_or_unrelated_marker"],
                    "title_matches_reviewed_casino_404": careers[
                        "title_matches_reviewed_casino_404"],
                    "job_card_like_count": careers["job_card_like_count"],
                    "listing_links": careers["listing_links"],
                    "external_ats_links": careers["external_ats_links"],
                    "job_posting_schema_count": careers["job_posting_schema_count"],
                    "visible_text_chars": careers["visible_text_chars"],
                    "normalized_text_sha256": careers["normalized_text_sha256"],
                }
                if careers["status_code"] == 200 and careers["company_identity_present"]:
                    self.result.errors.append("unreviewed_careers_journey_detected")
                    self.result.metadata["careers_route"]["conclusion"] = (
                        "The domain's careers route now serves Steradian content; the new "
                        "journey must be inspected before any collection."
                    )
                    return finish("steradian_source_collection_seconds")
                if careers["status_code"] == 404:
                    self.result.metadata["careers_route"]["conclusion"] = (
                        "The last known careers path is the unrelated site's 404 page; no "
                        "listing, job card or apply route exists there."
                    )

        # 3. Exact final recheck of the ledger URL.
        recheck_started = time.perf_counter()
        recheck = None
        try:
            _, recheck_record = await self._fetch(
                OFFICIAL_ENTRY, "official_entry_final_recheck")
            recheck_body = recheck_record.get("body")
            if recheck_body is None:
                raise ValueError("final recheck response body is unavailable")
            recheck = parse_live_entry(
                status_code=recheck_record["status_code"],
                final_url=recheck_record.get("final_url") or "",
                html=recheck_body.decode("utf-8", errors="replace"),
                history=recheck_record.get("redirects") or [],
            )
            self.result.metadata["final_recheck"] = {
                **self._evidence_from_record(recheck_record),
                **self._entry_evidence(recheck),
            }
        except Exception as exc:
            self.result.errors.append(
                f"steradian_final_recheck_failed: {type(exc).__name__}: {exc}")
        finally:
            self.result.timings["steradian_final_recheck_seconds"] = max(
                0.0, time.perf_counter() - recheck_started)

        if recheck is not None:
            same_journey = _journey_signature(entry) == _journey_signature(recheck)
            self.result.metadata["final_recheck_matches_initial"] = same_journey
            self.result.metadata["final_recheck_body_changed"] = (
                entry["html_text_sha256"] != recheck["html_text_sha256"])
            if not same_journey:
                self.result.errors.append(
                    "steradian_ledger_entry_changed_between_requests")
            if recheck["kind"] not in (
                    "unrelated_host", "unrelated_content_on_company_host"):
                self.result.errors.append("unreviewed_careers_journey_detected")
                self.result.metadata["observed_journey"]["conclusion"] = (
                    "The final ledger recheck serves a different kind of page; the new "
                    "journey must be inspected before any collection."
                )

        if not self.result.errors:
            self.result.errors.append("no_current_company_listing")

        self.result.metadata["membership"] = {
            "advertised_jobs": None,
            "listing_rows": 0,
            "careers_page_published": 0,
            "company_site_reachable": False,
            "unrelated_final_url": (
                entry["final_url"] if entry["kind"] in (
                    "unrelated_host", "unrelated_content_on_company_host") else None),
            "careers_route_http_status": (
                careers["status_code"] if careers is not None else None),
            "careers_route_title": (
                careers["title"] if careers is not None else None),
            "careers_route_job_cards": (
                careers["job_card_like_count"] if careers is not None else None),
            "job_listing_candidates": entry["listing_links"],
            "external_ats_links": entry["external_ats_links"],
            "ats_embeds": entry["ats_embeds"],
            "job_posting_schema_count": entry["job_posting_schema_count"],
            "reconciled": True,
            "note": (
                "Membership is unknown because the ledger domain no longer serves the company "
                "site (it redirects to an unrelated online-casino host) and every careers path "
                "is that host's 404. This is not a successful zero-job listing and not a "
                "WAF/403/429-blocked listing."
            ),
        }
        self.result.metadata["observed_journey"]["conclusion"] = (
            "The official domain is hijacked/redirected: the ledger URL reaches "
            f"{entry['final_host']!r} through {len(entry['redirects'])} redirects "
            f"({entry['title']!r}), and the domain's last known /careers/ route is the "
            "unrelated site's 404. The last genuine official site (archived 2023-03-22) had "
            "7 stale 2022-era posts and is unreachable today, so no current membership, "
            "description or ID can be verified. The empty result is an explicit hijacked-"
            "domain failure, not zero vacancies."
        )
        return finish("steradian_source_collection_seconds")
