"""Udaan's current official careers journey: a dead redirect and a login-walled ATS.

The ledger entry ``https://udaan.com/careers`` still answers HTTP 301 with
``Location: https://careers.udaan.com``.  That host resolves to
``k8s0.xp.udaan.io`` / ``103.156.106.7`` but its TCP ports 80 and 443 are
unreachable from this environment; certificate transparency has no certificate
issued after 2025-04-04 (expired 2025-07-03).  The company's own archived
careers page is a branding page (no job rows) whose "Join Us" links point three
times at ``https://udaan.darwinbox.in/ms/candidate/careers``.

The live Darwinbox URL returns an Angular shell.  Anonymous browser visits
redirect to ``/ms/candidatev2/main/auth/login``; the public tenant configuration
reports ``recruitment_enabled: false``.  There is therefore no reachable
company-scoped public listing, pagination, job ID or detail route to collect.

This adapter rechecks exactly that journey and records the failure.  It never
substitutes a zero-job success, an aggregator, a guessed ATS slug, or a
parent-company board for the missing company listing.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..http import FetchError
from .base import Adapter


OFFICIAL_ENTRY = "https://udaan.com/careers"
OFFICIAL_HOME = "https://udaan.com/"
OFFICIAL_ABOUT = "https://udaan.com/about-us"
CAREERS_TARGET = "https://careers.udaan.com/"
DARWINBOX_CAREERS = "https://udaan.darwinbox.in/ms/candidate/careers"
DARWINBOX_LOGIN = "https://udaan.darwinbox.in/ms/candidatev2/main/auth/login"
OFFICIAL_CAREERS_HOSTS = {"careers.udaan.com", "careers.dev.udaan.com"}
CAREER_WORD = re.compile(r"\b(?:careers?|jobs?|hiring|openings?|join us)\b", re.I)
JOB_LINK = re.compile(
    r"(?:/jobs?/|/careers?/|/positions?/|gh_jid=|job_id=|jobId=|/job/)", re.I
)
LOGIN_MARKERS = ("sign in", "password", "forgot password")
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def _parts(url: str):
    return urlsplit(url)


def _is_official_careers_host(url: str) -> bool:
    parts = _parts(url)
    return parts.scheme == "https" and parts.netloc.lower() in OFFICIAL_CAREERS_HOSTS


def _is_official_site(url: str) -> bool:
    parts = _parts(url)
    return parts.scheme == "https" and parts.netloc.lower() in {"udaan.com", "www.udaan.com"}


def parse_entry_redirect(status: int, location: str | None, *, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Validate the observed ``udaan.com/careers -> careers.udaan.com`` redirect."""
    parts = _parts(response_url)
    if not _is_official_site(response_url) or parts.path.rstrip("/") != "/careers":
        raise ValueError("Udaan official entry is not the inspected udaan.com/careers route")
    if status != 301:
        raise ValueError(f"Udaan official entry changed: expected HTTP 301, observed HTTP {status}")
    target = normalized(location)
    target_parts = _parts(target)
    if (not target or target_parts.scheme != "https"
            or target_parts.netloc.lower() != "careers.udaan.com"
            or target_parts.path not in ("", "/")):
        raise ValueError(f"Udaan official entry redirect target changed: {target!r}")
    return {
        "request_url": response_url,
        "status": status,
        "location": target,
        "target_url": CAREERS_TARGET,
    }


def parse_official_careers_page(html: str, response_url: str) -> dict:
    """Validate the archived company careers branding page and its exact ATS links.

    Both the production 2024-05-20 snapshot and the 2025-04-06 dev snapshot are
    byte-identical.  The page has no job rows; it publishes the Darwinbox URL.
    """
    if not _is_official_careers_host(response_url):
        raise ValueError(f"Udaan careers page came from an unexpected host: {response_url}")
    parts = _parts(response_url)
    if parts.path not in ("", "/"):
        raise ValueError(f"Udaan careers page is not the inspected root route: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != "udaan - Home Page":
        raise ValueError(f"Udaan careers page identity changed: title={title!r}")
    text = normalized(soup.get_text(" ", strip=True))
    if "We are udaan" not in text or "Join Us" not in text:
        raise ValueError("Udaan careers page no longer contains its inspected branding text")

    darwinbox_links, job_links = [], []
    for anchor in soup.select("a[href]"):
        href = normalized(anchor.get("href"))
        if href.rstrip("/") == DARWINBOX_CAREERS.rstrip("/"):
            darwinbox_links.append(href)
        elif JOB_LINK.search(href):
            job_links.append({"text": normalized(anchor.get_text(" ", strip=True)), "href": href})
    if not darwinbox_links:
        raise ValueError("Udaan careers page no longer publishes the inspected Darwinbox destination")
    return {
        "request_url": response_url,
        "title": title,
        "darwinbox_links": darwinbox_links,
        "job_links": job_links,
        "job_rows": len(job_links),
        "branding_only": not job_links,
    }


def parse_official_site_page(html: str, response_url: str) -> dict:
    """Read a current udaan.com page and list any careers/jobs navigation links."""
    if not _is_official_site(response_url):
        raise ValueError(f"Udaan page came from an unexpected host: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    careers_links = []
    for anchor in soup.select("a[href]"):
        text = normalized(anchor.get_text(" ", strip=True))
        href = normalized(anchor.get("href"))
        if CAREER_WORD.search(text) or "careers.udaan.com" in href:
            careers_links.append({"text": text, "href": href})
    return {
        "request_url": response_url,
        "title": title,
        "careers_links": careers_links,
        "has_careers_navigation": bool(careers_links),
        "text_chars": len(normalized(soup.get_text(" ", strip=True))),
    }


def parse_darwinbox_shell(html: str, *, response_url: str = DARWINBOX_CAREERS) -> dict:
    """Validate the live Darwinbox candidate-portal shell (no server-rendered jobs)."""
    parts = _parts(response_url)
    if parts.scheme != "https" or parts.netloc.lower() != "udaan.darwinbox.in":
        raise ValueError(f"Udaan ATS response came from an unexpected host: {response_url}")
    if not parts.path.startswith("/ms/candidate"):
        raise ValueError(f"Udaan ATS response is not the inspected candidate route: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    app_root = soup.select_one("app-root")
    if app_root is None:
        raise ValueError("Udaan ATS response is no longer the inspected Angular shell")
    scripts = [normalized(node.get("src")) for node in soup.select("script[src]")]
    job_links = [
        normalized(anchor.get("href"))
        for anchor in soup.select("a[href]")
        if JOB_LINK.search(normalized(anchor.get("href")))
    ]
    return {
        "request_url": response_url,
        "title": normalized(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "angular_app_root": True,
        "requires_javascript": True,
        "turnstile_loaded": any("turnstile" in src for src in scripts),
        "job_links": job_links,
        "job_rows": len(job_links),
        "shell_bytes": len(html),
    }


def parse_darwinbox_config(value: dict) -> dict:
    """Read the public tenant configuration returned by the company ATS."""
    if not isinstance(value, dict):
        raise ValueError("Udaan ATS configuration is not an object")
    company = (value.get("message") or {}).get("company") if isinstance(value.get("message"), dict) else None
    if not isinstance(company, dict):
        raise ValueError("Udaan ATS configuration is missing message.company")
    if (company.get("company_name") != "udaan"
            or company.get("subdomain") != "udaan"
            or str(company.get("tenant_id")) != "154"):
        raise ValueError("Udaan ATS tenant identity changed")
    return {
        "company_name": company.get("company_name"),
        "subdomain": company.get("subdomain"),
        "tenant_id": str(company.get("tenant_id")),
        "recruitment_enabled": bool(company.get("recruitment_enabled")),
        "new_careers": bool(company.get("new_careers")),
        "all_jobs_count": company.get("allJobsCount"),
        "public_recruitment_enabled": bool(company.get("recruitment_enabled")),
        "job_tile_permissions": company.get("job_tile_permissions") or {},
        "job_detail_permissions": company.get("job_detail_permissions") or {},
    }


def parse_login_wall_text(text: str, final_url: str) -> dict:
    """Validate the rendered sign-in wall observed at the end of the ATS link."""
    parts = _parts(final_url)
    if parts.scheme != "https" or parts.netloc.lower() != "udaan.darwinbox.in":
        raise ValueError(f"Udaan ATS login wall came from an unexpected host: {final_url}")
    if not parts.path.startswith("/ms/candidatev2/main/auth/login"):
        raise ValueError(f"Udaan ATS no longer redirects to the inspected login route: {final_url}")
    lowered = normalized(text).lower()
    markers = [marker for marker in LOGIN_MARKERS if marker in lowered]
    if len(markers) < 2:
        raise ValueError("Udaan ATS login wall no longer exposes the inspected sign-in form")
    return {
        "final_url": final_url,
        "markers": markers,
        "login_wall": True,
        "job_rows": 0,
        "text_chars": len(normalized(text)),
    }


def classify_host_failure(error: str) -> dict:
    message = normalized(error)
    lowered = message.lower()
    if "network is unreachable" in lowered or "errno 101" in lowered:
        kind = "route_unreachable"
    elif "connecttimeout" in lowered or "timed out" in lowered or "timeout" in lowered:
        kind = "tcp_connect_timeout"
    elif "ssl" in lowered or "tls" in lowered or "certificate" in lowered:
        kind = "tls_failure"
    elif "name or service not known" in lowered or "nodename" in lowered or "getaddrinfo" in lowered:
        kind = "dns_failure"
    else:
        kind = "transport_failure"
    return {"kind": kind, "message": message}


class Udaan(Adapter):
    VERIFICATION_REVISION = "udaan.unavailable-journey.1"

    def _failed_source(self, url: str, reason: str, **extra):
        row = {"url": url, "reason": reason}
        row.update(extra)
        self.result.metadata.setdefault("failed_sources", []).append(row)

    def _record(self, url: str, *, status, final_url, seconds, error=None, bytes_=None):
        self.result.metadata.setdefault("journey", []).append({
            "url": url,
            "status": status,
            "final_url": final_url,
            "bytes": bytes_,
            "seconds": seconds,
            "error": error,
        })

    async def _fetch(self, url: str, **kwargs):
        if "page_budget_exhausted" in self.result.errors:
            return None
        if not self.page_guard():
            return None
        started = time.perf_counter()
        try:
            response = await self.http.get(url, **kwargs)
            seconds = round(max(0.0, time.perf_counter() - started), 6)
            self._record(url, status=response.status_code, final_url=str(response.url),
                         seconds=seconds, bytes_=len(response.content))
            return response
        except FetchError as exc:
            seconds = round(max(0.0, time.perf_counter() - started), 6)
            self._record(url, status=None, final_url=None, seconds=seconds, error=str(exc))
            return None

    async def _entry(self):
        if not self.page_guard():
            return None
        started = time.perf_counter()
        try:
            response = await self.http.client.get(OFFICIAL_ENTRY, follow_redirects=False)
            seconds = round(max(0.0, time.perf_counter() - started), 6)
            self._record(OFFICIAL_ENTRY, status=response.status_code, final_url=str(response.url),
                         seconds=seconds, bytes_=len(response.content))
            return response
        except Exception as exc:
            seconds = round(max(0.0, time.perf_counter() - started), 6)
            self._record(OFFICIAL_ENTRY, status=None, final_url=None, seconds=seconds,
                         error=f"{type(exc).__name__}: {exc}")
            return None

    async def run(self):
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=OFFICIAL_ENTRY,
            official_home=OFFICIAL_HOME,
            official_about=OFFICIAL_ABOUT,
            careers_target=CAREERS_TARGET,
            published_ats_url=DARWINBOX_CAREERS,
            observed_ats_login_url=DARWINBOX_LOGIN,
            company_membership_verified=False,
            public_listing_available=False,
            expected_total=None,
            ats_reported_all_jobs_count=None,
            listed_job_rows=0,
            exact_job_ids_exposed=[],
            journey=[],
            failed_sources=[],
            review_evidence="companies/udaan/journey-research.md",
            rendered_reference="companies/udaan/browser-reference-worker.json",
            scope_note=("The Darwinbox URL is published by the company's archived careers page; "
                        "the live ATS is login-walled and its public config disables recruitment. "
                        "No aggregator or guessed ATS slug is used."),
        )
        self.result.expected_total = None
        self.result.listing_complete = False

        # 1. The ledger route must still be the inspected 301.
        entry_response = await self._entry()
        if "page_budget_exhausted" in self.result.errors:
            return self.result
        if entry_response is None:
            self._failed_source(OFFICIAL_ENTRY, "official entry request failed before redirect validation")
            self.result.errors.append("udaan_official_entry_unavailable_before_membership_review")
            return self.result
        try:
            entry = parse_entry_redirect(
                entry_response.status_code,
                entry_response.headers.get("location"),
                response_url=str(entry_response.url),
            )
        except ValueError as exc:
            self._failed_source(OFFICIAL_ENTRY, f"official redirect changed: {exc}",
                                http_status=entry_response.status_code)
            self.result.errors.append("udaan_official_journey_changed_requires_individual_review")
            return self.result
        self.result.metadata["official_entry_redirect"] = entry

        # 2. The redirected careers host is currently unreachable.  Record the
        # exact failure; a reachable page is a changed journey, not this flow.
        target_response = await self._fetch(CAREERS_TARGET)
        last = self.result.metadata["journey"][-1]
        if "page_budget_exhausted" in self.result.errors:
            return self.result
        if target_response is not None:
            self._failed_source(
                CAREERS_TARGET,
                f"expected unreachable careers host, observed HTTP {target_response.status_code}",
                http_status=target_response.status_code,
            )
            self.result.errors.append("udaan_official_careers_host_reachable_requires_individual_review")
            return self.result
        failure = classify_host_failure(last.get("error") or "")
        self.result.metadata["careers_host_failure"] = failure
        self._failed_source(
            CAREERS_TARGET,
            f"redirect target unreachable: {last.get('error')}",
            failure_kind=failure["kind"],
            elapsed_seconds=last.get("seconds"),
        )

        # 3. The current official site must not publish a different careers route.
        for url in (OFFICIAL_HOME, OFFICIAL_ABOUT):
            response = await self._fetch(url)
            if "page_budget_exhausted" in self.result.errors:
                return self.result
            if response is None:
                self._failed_source(url, "current official page request failed")
                continue
            try:
                parsed = parse_official_site_page(response.text, str(response.url))
            except ValueError as exc:
                self._failed_source(url, f"official page changed: {exc}")
                continue
            self.result.metadata.setdefault("official_site_pages", {})[url] = parsed
            if parsed["has_careers_navigation"]:
                self._failed_source(url, "a new careers/jobs navigation link appeared")
                self.result.errors.append("udaan_official_site_publishes_new_careers_route_requires_individual_review")
                return self.result

        # 4. The exact ATS destination published by the archived official page.
        ats_response = await self._fetch(DARWINBOX_CAREERS, headers=BROWSER_HEADERS)
        if "page_budget_exhausted" in self.result.errors:
            return self.result
        if ats_response is None:
            last = self.result.metadata["journey"][-1]
            self._failed_source(
                DARWINBOX_CAREERS,
                f"company-published ATS request failed: {last.get('error')}",
                failure_kind=classify_host_failure(last.get("error") or "")["kind"],
            )
        else:
            try:
                shell = parse_darwinbox_shell(ats_response.text, response_url=str(ats_response.url))
            except ValueError as exc:
                self._failed_source(DARWINBOX_CAREERS, f"company-published ATS changed: {exc}")
                self.result.errors.append("udaan_published_ats_changed_requires_individual_review")
                return self.result
            self.result.metadata["published_ats_shell"] = shell
            if shell["job_rows"] or shell["job_links"]:
                self._failed_source(
                    DARWINBOX_CAREERS,
                    "company-published ATS exposes job links but no inspected membership flow",
                    job_rows=shell["job_rows"],
                )
                self.result.errors.append("udaan_published_ats_job_links_require_individual_membership_review")
                return self.result
            self._failed_source(
                DARWINBOX_CAREERS,
                ("HTTP 200 Angular shell with no server-rendered job rows; the independent "
                 "browser observes a redirect to the candidate sign-in route."),
                http_status=ats_response.status_code,
                requires_javascript=shell["requires_javascript"],
            )

        self.result.metadata["company_membership_verified"] = False
        self.result.metadata["public_listing_available"] = False
        self.result.errors.append("udaan_public_listing_unavailable_careers_host_dead_and_ats_login_walled")
        return self.result
