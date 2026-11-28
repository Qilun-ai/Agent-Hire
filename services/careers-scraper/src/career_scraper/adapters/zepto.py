"""Zepto's own /careers journey, its exact Darwinbox board, and that board's live zero state.

Observed 2026-11-08: ``https://www.zepto.com/careers`` returns HTTP 202 with
``x-amzn-waf-action: challenge`` to ordinary HTTP.  A normal headed Chromium
completed the AWS WAF challenge and observed a 308 redirect to
``https://zepto.darwinbox.in/ms/candidatev2/main/careers/home``.  The board's
``Open Jobs`` link is the company-scoped all-jobs page
``/ms/candidatev2/main/careers/allJobs``; its own unfiltered request
``POST /ms/candidateapi/job/alljobs?companyId=main`` returns
``{"status":"success","data":[],"job_counts":0}``.  The company config reports
``allJobsCount: 0`` and the rendered page says ``0 Open jobs available`` /
``Sorry! No results found``.

This adapter only requests Zepto's exact Darwinbox tenant and candidate route.
It never guesses another ATS, never queries a parent/group board, and stops
without emitting unverified rows if the board becomes non-empty (there is no
observed Zepto detail schema in this review).
"""
from __future__ import annotations

from hashlib import sha256
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError


OFFICIAL_ENTRY = "https://www.zepto.com/careers"
OFFICIAL_ALIASES = frozenset({
    "https://www.zepto.com/careers",
    "https://www.zeptonow.com/careers",
})
LISTING_ORIGIN = "https://zepto.darwinbox.in"
LISTING_HOME = LISTING_ORIGIN + "/ms/candidatev2/main/careers/home"
LISTING_URL = LISTING_ORIGIN + "/ms/candidatev2/main/careers/allJobs"
COMPANY_INFO_API = LISTING_ORIGIN + "/ms/candidateapi/companyinfo"
COMPANY_CONFIG_API = (
    LISTING_ORIGIN + "/ms/candidateapi/getCompanyConfig?is_preview=false&companyId=main"
)
GROUP_SETTING_API = (
    LISTING_ORIGIN + "/ms/candidateapi/groupcompanycareersetting?companyId=main"
)
ALLJOBS_API = LISTING_ORIGIN + "/ms/candidateapi/job/alljobs?companyId=main"
JOB_PAGE_API = LISTING_ORIGIN + "/ms/candidateapi/job?page=1"
NEWFILTERS_API = LISTING_ORIGIN + "/ms/candidateapi/job/newfilters?companyId=main"
COMPANY_ID = "main"
EXPECTED_COMPANY_NAME = "Zepto"
EXPECTED_TENANT_ID = "210"
EXPECTED_LOGO_MARKER = "INSTANCE5_a62f543fae7ad3_210"
ALLJOBS_PAYLOAD = {"companyId": COMPANY_ID, "page": 1, "sort_option": "new", "limit": 10}
NEWFILTERS_PAYLOAD = {"companyId": COMPANY_ID, "id": []}
WAF_MARKERS = (
    "awsWafCookieDomainList",
    "AwsWafIntegration",
    "challenge.js",
    "/challenge-container",
    "awswaf.com",
)
CAREER_FILTER_LABELS = (
    "Sub Department",
    "Location",
    "Employee Type",
    "Job Tag",
    "Country",
    "Experience",
)


def normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _check_page_url(response_url: str, paths: set[str], label: str) -> str:
    parts = urlsplit(response_url)
    _require(parts.scheme == "https", f"{label} must use HTTPS: {response_url}")
    _require(parts.netloc == "zepto.darwinbox.in", f"{label} left Zepto's Darwinbox tenant: {response_url}")
    _require(parts.path in paths, f"{label} path changed from the observed candidate board: {response_url}")
    _require(not parts.query and not parts.fragment, f"{label} must not carry query/fragment: {response_url}")
    return parts.path


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    _require(parts.scheme == "https", "Zepto official entry must use HTTPS")
    _require(parts.netloc in {"www.zepto.com", "zepto.com", "www.zeptonow.com"},
             f"Zepto official entry left its company HTTPS host: {response_url}")
    _require(parts.path.rstrip("/") == "/careers",
             f"Zepto official entry path changed from /careers: {response_url}")
    text = html or ""
    soup = BeautifulSoup(text, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    lowered = text.lower()
    markers = [marker for marker in WAF_MARKERS if marker.lower() in lowered]
    hrefs = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if href:
            hrefs.append(href)
    return {
        "url": response_url,
        "title": title,
        "body_chars": len(text),
        "waf_challenge": bool(markers),
        "waf_markers": markers,
        "hrefs": hrefs,
    }


def parse_listing_shell(html: str, response_url: str = LISTING_URL) -> dict:
    _check_page_url(response_url, {"/ms/candidatev2/main/careers/home",
                                   "/ms/candidatev2/main/careers/allJobs"},
                    "Zepto Darwinbox listing shell")
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    base = soup.base.get("href") if soup.base else None
    scripts = [node.get("src") or "" for node in soup.select("script[src]")]
    bundle = next((src for src in scripts if re.fullmatch(r"main\.[a-f0-9]+\.js", src)), None)
    og_title = (soup.select_one('meta[property="og:title"]') or {}).get("content")
    og_image = (soup.select_one('meta[property="og:image"]') or {}).get("content") or ""
    body_text = normalized_text(soup.body.get_text(" ", strip=True) if soup.body else "")
    if base == "/ms/candidatev2/" and bundle is not None:
        kind = "app_shell"
    elif title == EXPECTED_COMPANY_NAME and EXPECTED_LOGO_MARKER in og_image and body_text.startswith("Zepto"):
        # Darwinbox serves this crawler/OG shell to non-browser clients; it
        # still proves the exact tenant route and branding but not the app DOM.
        kind = "og_shell"
    else:
        raise ValueError(f"Zepto Darwinbox shell changed from its observed app/OG shell: title={title!r}")
    return {
        "url": response_url,
        "kind": kind,
        "title": title,
        "og_title": og_title,
        "og_image": og_image or None,
        "base_href": base,
        "main_bundle": bundle,
        "body_text": body_text,
    }


def parse_company_info(value: dict) -> dict:
    _require(isinstance(value, dict) and value.get("status") == "success",
             "Zepto companyinfo response changed from status=success")
    company = (value.get("message") or {}).get("company")
    _require(isinstance(company, dict), "Zepto companyinfo is missing message.company")
    _require(company.get("company_name") == EXPECTED_COMPANY_NAME,
             f"Zepto Darwinbox company name changed: {company.get('company_name')!r}")
    _require(str(company.get("tenant_id")) == EXPECTED_TENANT_ID,
             f"Zepto Darwinbox tenant changed: {company.get('tenant_id')!r}")
    _require(company.get("recruitment_enabled") is True,
             "Zepto Darwinbox recruitment_enabled is not true")
    logo = company.get("logo") or ""
    _require(isinstance(logo, str) and EXPECTED_LOGO_MARKER in logo,
             "Zepto Darwinbox logo no longer identifies the observed tenant")
    return {
        "company_name": company.get("company_name"),
        "tenant_id": str(company.get("tenant_id")),
        "logo": logo,
        "recruitment_enabled": company.get("recruitment_enabled"),
        "font_color_career_page": company.get("font_color_career_page"),
    }


def parse_company_config(value: dict) -> dict:
    _require(isinstance(value, dict) and value.get("status") == "success",
             "Zepto company config response changed from status=success")
    company = (value.get("message") or {}).get("company")
    _require(isinstance(company, dict), "Zepto company config is missing message.company")
    _require(company.get("company_name") == EXPECTED_COMPANY_NAME,
             f"Zepto config company name changed: {company.get('company_name')!r}")
    _require(company.get("subdomain") == "zepto", "Zepto Darwinbox subdomain changed")
    _require(str(company.get("tenant_id")) == EXPECTED_TENANT_ID, "Zepto config tenant changed")
    _require(company.get("new_careers") is True, "Zepto Darwinbox new_careers flag changed")
    count = company.get("allJobsCount")
    _require(isinstance(count, int) and count >= 0,
             f"Zepto allJobsCount is not a non-negative integer: {count!r}")
    filters = company.get("careers_filters")
    _require(isinstance(filters, dict), "Zepto careers_filters is missing")
    _require((company.get("group_company_site") or "") == "",
             "Zepto careers config unexpectedly exposes a group company site")
    return {
        "company_name": company.get("company_name"),
        "company_id": company.get("id"),
        "subdomain": company.get("subdomain"),
        "tenant_id": str(company.get("tenant_id")),
        "new_careers": company.get("new_careers"),
        "all_jobs_count": count,
        "banner_text": company.get("banner_text"),
        "all_jobs_banner_text": company.get("all_jobs_banner_text"),
        "group_company_site": company.get("group_company_site") or "",
        "careers_filters": filters,
        "job_tile_permissions": company.get("job_tile_permissions"),
    }


def parse_group_setting(value: dict) -> dict:
    _require(isinstance(value, dict) and value.get("status") == "success",
             "Zepto group company careers setting changed from status=success")
    message = value.get("message")
    _require(isinstance(message, dict) and message.get("code") == 0,
             "Zepto group company careers setting no longer returns code 0")
    return {"code": message.get("code")}


def parse_alljobs_payload(value: dict, requested_page: int = 1) -> dict:
    _require(isinstance(value, dict) and value.get("status") == "success",
             "Zepto alljobs response changed from status=success")
    data = value.get("data")
    count = value.get("job_counts")
    _require(isinstance(data, list), "Zepto alljobs response is missing data array")
    _require(isinstance(count, int) and count >= 0,
             f"Zepto alljobs job_counts is not a non-negative integer: {count!r}")
    _require(len(data) <= count, "Zepto alljobs page contains more rows than job_counts")
    issues = []
    seen = set()
    for row in data:
        if not isinstance(row, dict) or not row.get("id"):
            issues.append("zepto_alljobs_row_missing_exact_id")
            continue
        identity = str(row["id"])
        if identity in seen:
            issues.append(f"zepto_alljobs_duplicate_id:{identity}")
        seen.add(identity)
    if count == 0 and data:
        issues.append("zepto_alljobs_zero_count_with_rows")
    return {
        "page": requested_page,
        "count": count,
        "rows": data,
        "job_ids": [str(row["id"]) for row in data if isinstance(row, dict) and row.get("id")],
        "issues": issues,
        "non_empty": bool(data),
    }


def parse_job_page(value: dict) -> dict:
    _require(isinstance(value, dict) and value.get("status") == "success",
             "Zepto home job page response changed from status=success")
    message = value.get("message")
    _require(isinstance(message, dict), "Zepto home job page is missing message")
    count = message.get("jobscount")
    jobs = message.get("jobs")
    _require(isinstance(count, int) and count >= 0, "Zepto home jobscount is not a non-negative integer")
    _require(isinstance(jobs, list), "Zepto home job page is missing jobs array")
    _require(len(jobs) <= count, "Zepto home job page contains more rows than jobscount")
    return {"count": count, "rows": jobs, "non_empty": bool(jobs)}


def parse_newfilters(value: dict) -> dict:
    _require(isinstance(value, dict) and value.get("status") == "success",
             "Zepto job filters response changed from status=success")
    message = value.get("message")
    _require(isinstance(message, dict), "Zepto job filters is missing message")
    keys = ("employee_type", "functional_area", "location", "department",
            "job_tags", "country", "capability")
    for key in keys:
        _require(isinstance(message.get(key), list), f"Zepto job filter {key} is not a list")
    locations = [str(item.get("name")) for item in message["location"]
                 if isinstance(item, dict) and item.get("name")]
    return {
        "employment_types": [str(item.get("name") or item) for item in message["employee_type"]],
        "functional_areas": [str(item.get("name") or item) for item in message["functional_area"]],
        "locations": locations,
        "departments": [str(item.get("name") or item) for item in message["department"]],
        "job_tags": [str(item.get("name") or item) for item in message["job_tags"]],
        "countries": [str(item.get("name") or item) for item in message["country"]],
        "capabilities": [str(item.get("name") or item) for item in message["capability"]],
        "raw": message,
    }


def parse_alljobs_rendered(text: str) -> dict:
    """Read the visible allJobs page text captured by the browser reference."""
    value = text or ""
    match = re.search(r"(\d+)\s+Open jobs available", value, re.I)
    count = int(match.group(1)) if match else None
    filters = [label for label in CAREER_FILTER_LABELS if label.lower() in value.lower()]
    return {
        "job_count": count,
        "no_results_notice": bool(re.search(r"Sorry!\s*No results found", value, re.I)),
        "filters_visible": filters,
        "text_chars": len(normalized_text(value)),
        "text_sha256": sha256(normalized_text(value).encode()).hexdigest(),
        "text_fnv1a64": fnv1a64(normalized_text(value)),
    }


def validate_redirect_reference(value: dict) -> dict:
    _require(isinstance(value, dict), "Zepto official redirect reference is missing")
    entry = value.get("entry") or {}
    home = value.get("home") or {}
    alljobs = value.get("alljobs") or {}
    _require(value.get("official_entry_url") == OFFICIAL_ENTRY,
             "Zepto redirect reference does not start at the commissioned official entry")
    _require(entry.get("initial_status") == 202,
             "Zepto official entry reference is not the observed HTTP 202 challenge")
    _require(entry.get("redirect_status") == 308,
             "Zepto official entry reference is not the observed HTTP 308 redirect")
    _require(entry.get("redirect_location") == LISTING_HOME,
             "Zepto official entry redirect target changed from the observed Darwinbox home")
    _require(entry.get("final_url") == LISTING_HOME,
             "Zepto official entry reference final URL changed")
    _require(home.get("url") == LISTING_HOME and home.get("status") == 200,
             "Zepto Darwinbox home reference is not the observed 200 page")
    _require(home.get("open_jobs_href") == LISTING_URL,
             "Zepto Darwinbox home no longer links Open Jobs to the observed allJobs page")
    _require(alljobs.get("url") == LISTING_URL and alljobs.get("status") == 200,
             "Zepto allJobs reference is not the observed 200 listing page")
    rendered = parse_alljobs_rendered(alljobs.get("visible_text") or "")
    _require(rendered["job_count"] is not None,
             "Zepto allJobs reference is missing its visible job count")
    if rendered["job_count"] == 0:
        _require(rendered["no_results_notice"],
                 "Zepto allJobs reference no longer shows the zero-state no-results notice")
    _require(bool(alljobs.get("visible_text_sha256")),
             "Zepto allJobs reference is missing the rendered text hash")
    return {
        "official_entry_url": OFFICIAL_ENTRY,
        "redirect_status": 308,
        "redirect_location": LISTING_HOME,
        "home_url": LISTING_HOME,
        "open_jobs_href": LISTING_URL,
        "rendered": rendered,
        "reference": value,
    }


def _response_summary(response, *, html: bool = False, json: bool = False,
                      seconds: float | None = None) -> dict:
    body = response.content
    summary = {
        "url": str(response.request.url),
        "final_url": str(response.url),
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "sha256": sha256(body).hexdigest(),
        "bytes": len(body),
    }
    if html:
        summary["html"] = response.text
    else:
        summary["json"] = response.json()
    if seconds is not None:
        summary["seconds"] = round(max(0.0, seconds), 6)
    return summary


class Zepto(Adapter):
    """Follow only Zepto's published Darwinbox candidate board."""

    VERIFICATION_REVISION = "zepto.official-careers-darwinbox-zero-board.1"

    def __init__(self, http, company, source, options, *, initial_capture=None,
                 final_capture=None, redirect_reference=None):
        super().__init__(http, company, source, options)
        self.initial_capture = initial_capture
        self.final_capture = final_capture
        self.redirect_reference = redirect_reference

    @staticmethod
    def _check_entry(company_url: str, source_url: str) -> None:
        _require(company_url in OFFICIAL_ALIASES,
                 f"Zepto dedicated flow must start from its official /careers entry: {company_url}")
        _require(source_url == LISTING_URL,
                 f"Zepto dedicated flow source must be its observed allJobs board: {source_url}")

    async def _official_entry(self) -> dict:
        started = time.perf_counter()
        try:
            response = await self.http.get(OFFICIAL_ENTRY)
        except FetchError as exc:
            return {
                "url": OFFICIAL_ENTRY,
                "fetch_failed": True,
                "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(max(0.0, time.perf_counter() - started), 6),
            }
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update({
            "status": response.status_code,
            "waf_action": response.headers.get("x-amzn-waf-action"),
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "seconds": round(seconds, 6),
        })
        return parsed

    async def _capture_snapshot(self, label: str) -> dict:
        started = time.perf_counter()
        timings = {}

        async def timed(name, awaitable):
            phase_started = time.perf_counter()
            response = await awaitable
            timings[f"{name}_seconds"] = round(max(0.0, time.perf_counter() - phase_started), 6)
            return response

        home = await timed("home_shell", self.http.get(LISTING_HOME))
        listing = await timed("listing_shell", self.http.get(LISTING_URL))
        company_info = await timed("company_info", self.http.get(COMPANY_INFO_API))
        company_config = await timed("company_config", self.http.get(COMPANY_CONFIG_API))
        group = await timed("group_setting", self.http.get(GROUP_SETTING_API))
        alljobs = await timed("alljobs", self.http.request("POST", ALLJOBS_API, json=ALLJOBS_PAYLOAD))
        job_page = await timed("job_page", self.http.get(JOB_PAGE_API))
        newfilters = await timed("newfilters", self.http.request("POST", NEWFILTERS_API, json=NEWFILTERS_PAYLOAD))
        return {
            "label": label,
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
            "timings": timings,
            "home_shell": _response_summary(home, html=True, seconds=timings["home_shell_seconds"]),
            "listing_shell": _response_summary(listing, html=True, seconds=timings["listing_shell_seconds"]),
            "company_info": _response_summary(company_info, json=True, seconds=timings["company_info_seconds"]),
            "company_config": _response_summary(company_config, json=True, seconds=timings["company_config_seconds"]),
            "group_setting": _response_summary(group, json=True, seconds=timings["group_setting_seconds"]),
            "alljobs": {**_response_summary(alljobs, json=True, seconds=timings["alljobs_seconds"]),
                        "request_body": ALLJOBS_PAYLOAD},
            "job_page": _response_summary(job_page, json=True, seconds=timings["job_page_seconds"]),
            "newfilters": {**_response_summary(newfilters, json=True, seconds=timings["newfilters_seconds"]),
                           "request_body": NEWFILTERS_PAYLOAD},
        }

    @staticmethod
    def _parse_snapshot(snapshot: dict) -> dict:
        home = parse_listing_shell(snapshot["home_shell"]["html"], snapshot["home_shell"]["final_url"])
        listing = parse_listing_shell(snapshot["listing_shell"]["html"], snapshot["listing_shell"]["final_url"])
        info = parse_company_info(snapshot["company_info"]["json"])
        config = parse_company_config(snapshot["company_config"]["json"])
        group = parse_group_setting(snapshot["group_setting"]["json"])
        alljobs = parse_alljobs_payload(snapshot["alljobs"]["json"], ALLJOBS_PAYLOAD["page"])
        job_page = parse_job_page(snapshot["job_page"]["json"])
        filters = parse_newfilters(snapshot["newfilters"]["json"])
        return {
            "home_shell": home,
            "listing_shell": listing,
            "company_info": info,
            "company_config": config,
            "group_setting": group,
            "alljobs": alljobs,
            "job_page": job_page,
            "filters": filters,
        }

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_home_url": LISTING_HOME,
            "listing_api_url": ALLJOBS_API,
            "membership_authority": (
                "Zepto's official https://www.zepto.com/careers -> browser-observed HTTP 308 -> "
                "https://zepto.darwinbox.in/ms/candidatev2/main/careers/home -> its Open Jobs link "
                "-> https://zepto.darwinbox.in/ms/candidatev2/main/careers/allJobs; only that exact "
                "Zepto tenant and candidate route are requested."
            ),
            "detail_method": (
                "No detail object is requested or emitted in the current zero-vacancy state. "
                "A non-empty board would require a fresh detail-schema review."
            ),
            "request_scope": (
                "Only zepto.darwinbox.in, candidate route main, and the exact alljobs/newfilters calls "
                "the company board uses; no other Darwinbox tenant, group company, or aggregator."
            ),
        })
        try:
            self._check_entry(self.company.careers_url, self.source.url)
        except ValueError as exc:
            self.result.errors.append(str(exc))
            self.result.listing_complete = False
            return self.result
        if not self.page_guard():
            return self.result

        official = None
        if self.initial_capture is not None and self.initial_capture.get("official_entry"):
            official = dict(self.initial_capture["official_entry"])
        else:
            official = await self._official_entry()
        self.result.metadata["official_entry"] = official
        self.result.timings["official_entry_phase_seconds"] = official.get("seconds", 0.0)
        if official.get("fetch_failed"):
            self.result.errors.append(
                f"Zepto official entry unreachable without challenge solving: {official.get('error')}"
            )
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.timings["official_entry_phase_seconds"] = official.get("seconds", 0.0)
            self.result.timings["source_collection_wall_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6)
            return self.result

        http_followed = urlsplit(official.get("final_url", "")).hostname == "zepto.darwinbox.in"
        if not http_followed:
            if official.get("status") != 202 or not official.get("waf_challenge"):
                self.result.errors.append(
                    "Zepto official entry no longer exposes the observed AWS WAF challenge "
                    f"(status={official.get('status')}, markers={official.get('waf_markers')}); "
                    "the published route must be re-inspected before collecting."
                )
                self.result.expected_total = None
                self.result.listing_complete = False
                self.result.timings["source_collection_wall_seconds"] = round(
                    max(0.0, time.perf_counter() - source_started), 6)
                return self.result

        try:
            redirect = validate_redirect_reference(self.redirect_reference or {})
        except ValueError as exc:
            self.result.errors.append(
                f"Zepto browser-observed official redirect reference is invalid: {exc}"
            )
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6)
            return self.result
        self.result.metadata["official_redirect_reference"] = redirect

        capture_started = time.perf_counter()
        snapshots = []
        try:
            if self.initial_capture is not None:
                initial_raw = self.initial_capture
                final_raw = self.final_capture or self.initial_capture
                self.result.metadata["reused_capture"] = True
            else:
                initial_raw = await self._capture_snapshot("initial")
                final_raw = await self._capture_snapshot("final")
                self.result.metadata["reused_capture"] = False
            capture_seconds = max(0.0, time.perf_counter() - capture_started)
            parse_started = time.perf_counter()
            initial = self._parse_snapshot(initial_raw)
            final = self._parse_snapshot(final_raw)
            snapshots = [initial, final]
        except Exception as exc:
            self.result.errors.append(f"Zepto company board capture failed: {type(exc).__name__}: {exc}")
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.timings["capture_seconds"] = round(
                max(0.0, time.perf_counter() - capture_started), 6)
            self.result.timings["source_collection_wall_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6)
            return self.result

        initial, final = snapshots
        parse_seconds = max(0.0, time.perf_counter() - parse_started)
        initial_ids = initial["alljobs"]["job_ids"]
        final_ids = final["alljobs"]["job_ids"]
        counts = {
            "config_all_jobs_count": initial["company_config"]["all_jobs_count"],
            "initial_alljobs_count": initial["alljobs"]["count"],
            "final_alljobs_count": final["alljobs"]["count"],
            "home_job_page_count": initial["job_page"]["count"],
            "rendered_job_count": redirect["rendered"]["job_count"],
        }
        mismatches = [name for name, value in counts.items() if value != 0]
        membership_matches = initial_ids == final_ids
        if not membership_matches:
            self.result.errors.append(
                "zepto_initial_final_listing_membership_changed_between_snapshots")
        if initial["alljobs"]["issues"]:
            self.result.errors.extend(initial["alljobs"]["issues"])
        if final["alljobs"]["issues"]:
            self.result.errors.extend(final["alljobs"]["issues"])
        if mismatches:
            self.result.errors.append(
                "zepto_board_is_not_the_observed_zero_vacancy_state:"
                + ",".join(f"{name}={counts[name]}" for name in mismatches)
            )
        if initial["alljobs"]["non_empty"] or final["alljobs"]["non_empty"]:
            self.result.errors.append(
                "zepto_non_empty_darwinbox_board_requires_fresh_detail_schema_review")
        if not redirect["rendered"]["no_results_notice"]:
            self.result.errors.append("zepto_rendered_alljobs_no_results_notice_missing")
        filter_options_present = any(initial["filters"][key] for key in (
            "employment_types", "functional_areas", "departments", "job_tags",
            "countries", "capabilities",
        ))
        if filter_options_present or any(name != "Remote" for name in initial["filters"]["locations"]):
            self.result.errors.append("zepto_zero_board_has_unexpected_filter_options")

        zero_vacancy = (
            not self.result.errors
            and not initial_ids
            and not final_ids
            and all(value == 0 for value in counts.values())
            and initial["alljobs"]["count"] == 0
            and final["alljobs"]["count"] == 0
        )
        if zero_vacancy:
            self.result.expected_total = 0
            self.result.listing_complete = True
            self.result.warnings.append("official_board_currently_lists_zero_open_jobs")
        else:
            # A non-empty or inconsistent board is not converted into rows here:
            # no live Zepto detail object has been observed in this review.
            self.result.expected_total = None
            self.result.listing_complete = False

        initial_meta = {
            "count": initial["alljobs"]["count"],
            "job_ids": initial_ids,
            "page": initial["alljobs"]["page"],
            "issues": initial["alljobs"]["issues"],
        }
        final_meta = {
            "count": final["alljobs"]["count"],
            "job_ids": final_ids,
            "page": final["alljobs"]["page"],
            "issues": final["alljobs"]["issues"],
        }
        self.result.metadata.update({
            "listing_shell": {
                "home": initial["home_shell"],
                "alljobs": initial["listing_shell"],
            },
            "company_info": initial["company_info"],
            "company_config": {
                key: value for key, value in initial["company_config"].items()
                if key not in {"careers_filters"}
            },
            "careers_filters": initial["company_config"]["careers_filters"],
            "filters": {
                key: value for key, value in initial["filters"].items() if key != "raw"
            },
            "initial_listing": initial_meta,
            "final_listing": final_meta,
            "membership_reconciliation": {
                "initial_ids": initial_ids,
                "final_ids": final_ids,
                "initial_count": initial["alljobs"]["count"],
                "final_count": final["alljobs"]["count"],
                "initial_final_id_order_matches": membership_matches,
                "matches": zero_vacancy,
                "zero_vacancy": zero_vacancy,
            },
            "zero_vacancy": {
                "confirmed": zero_vacancy,
                "listing_api_returned_empty_data": (
                    initial["alljobs"]["count"] == 0
                    and final["alljobs"]["count"] == 0
                    and not initial["alljobs"]["rows"]
                    and not final["alljobs"]["rows"]
                ),
                "listing_calls": 2,
                "home_job_page_count": initial["job_page"]["count"],
                "company_config_all_jobs_count": initial["company_config"]["all_jobs_count"],
                "rendered_job_count": redirect["rendered"]["job_count"],
                "observed_rendered_notice": (
                    "0 Open jobs available; Sorry! No results found"
                    if redirect["rendered"]["job_count"] == 0
                    else None
                ),
                "note": (
                    "The empty data array and job_counts 0 come from two fresh live requests to "
                    "Zepto's exact Darwinbox board, corroborated by company config allJobsCount 0, "
                    "the home job page count 0, and an independent rendered browser reference. "
                    "This is an authoritative live zero state, not an inaccessible or error response."
                ),
            },
            "source_collection_wall_seconds": round(
                max(0.0, time.perf_counter() - source_started), 6),
            "snapshot_timings": {
                "initial": initial_raw.get("timings", {}),
                "final": final_raw.get("timings", {}),
            },
        })
        initial_timings = initial_raw.get("timings", {}) or {}
        final_timings = final_raw.get("timings", {}) or {}
        self.result.timings["listing_shell_phase_seconds"] = round(
            sum((initial_timings.get(key, 0.0) or 0.0) + (final_timings.get(key, 0.0) or 0.0)
                for key in ("home_shell_seconds", "listing_shell_seconds")), 6)
        self.result.timings["listing_api_phase_seconds"] = round(
            sum((initial_timings.get(key, 0.0) or 0.0) + (final_timings.get(key, 0.0) or 0.0)
                for key in ("company_info_seconds", "company_config_seconds", "group_setting_seconds",
                            "alljobs_seconds", "job_page_seconds", "newfilters_seconds")), 6)
        self.result.timings["capture_seconds"] = round(capture_seconds, 6)
        self.result.timings["parse_seconds"] = round(parse_seconds, 6)
        self.result.timings["detail_phase_seconds"] = 0.0
        self.result.timings["source_collection_wall_seconds"] = round(
            max(0.0, time.perf_counter() - source_started), 6)
        self.result.pages = 1
        return self.result
