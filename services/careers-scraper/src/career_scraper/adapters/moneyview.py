"""Moneyview's own ``/careers`` page and the exact Darwinbox tenant it publishes.

The official page ``https://moneyview.in/careers`` links four ``View Openings``
controls to ``https://moneyview.darwinbox.in/ms/candidate/careers``.  In a
rendered browser that published link lands on the Moneyview Darwinbox tenant at
``/ms/candidatev2/main/careers/home`` and its ``Open Jobs`` route
``/ms/candidatev2/main/careers/allJobs``.  The tenant config identifies
``company_name=Moneyview``, ``subdomain=moneyview`` and ``tenant_id=135``.

The listing is served by the tenant's own
``POST /ms/candidateapi/job/alljobs?companyId=main`` request with
``{companyId, page, sort_option, limit}``.  Every row already carries the full
employer-authored ``jd`` HTML; the native detail route
``/ms/candidateapi/job/{id}?companyId=main`` is also fetched for each exact
listed job and checked against the listing row.

The Darwinbox host returns Cloudflare HTTP 403 to non-browser User-Agents.  The
standard desktop Chrome User-Agent receives the normal public responses.  This
is User-Agent filtering, not a CAPTCHA; this adapter never solves a challenge
and stops on 403/429.
"""
from __future__ import annotations

from datetime import datetime
from html import unescape
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://moneyview.in/careers"
OFFICIAL_HOSTS = {"moneyview.in", "www.moneyview.in"}
DARWINBOX_HOST = "moneyview.darwinbox.in"
COMPANY_LINK = "https://moneyview.darwinbox.in/ms/candidate/careers"
HOME_URL = "https://moneyview.darwinbox.in/ms/candidatev2/main/careers/home"
LISTING_URL = "https://moneyview.darwinbox.in/ms/candidatev2/main/careers/allJobs"
ALLJOBS_API = "https://moneyview.darwinbox.in/ms/candidateapi/job/alljobs?companyId=main"
COMPANY_CONFIG_API = (
    "https://moneyview.darwinbox.in/ms/candidateapi/getCompanyConfig"
    "?is_preview=false&companyId=main"
)
DETAIL_API_TEMPLATE = (
    "https://moneyview.darwinbox.in/ms/candidateapi/job/{job_id}?companyId=main"
)
DETAIL_PAGE_TEMPLATE = (
    "https://moneyview.darwinbox.in/ms/candidatev2/main/careers/jobDetails/{job_id}"
)
LOGIN_URL = "https://moneyview.darwinbox.in/ms/candidatev2/main/auth/login"
EMPLOYER_NAME = "Moneyview"
COMPANY_SUBDOMAIN = "moneyview"
TENANT_ID = "135"
SOURCE_TIMEZONE = ZoneInfo("Asia/Kolkata")
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PAGE_LIMIT = 10
MAX_LISTING_PAGES = 100
VISIBLE_FILTERS = ("Sub Department", "Employee Type", "Job Tag", "Country", "Experience")
def request_headers(referer: str | None = None, *, json_request: bool = False) -> dict[str, str]:
    """The normal public request shape used for the Moneyview tenant."""
    headers = {
        "User-Agent": DESKTOP_USER_AGENT,
        "Accept": "application/json, text/plain, */*" if json_request
                  else "text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if json_request:
        headers["Content-Type"] = "application/json"
        headers["Origin"] = "https://moneyview.darwinbox.in"
    if referer:
        headers["Referer"] = referer
    return headers


def normalized_text(value: str | None) -> str:
    """Normalize rendered/stored text for exact independent comparison."""
    value = unicodedata.normalize("NFC", value or "").replace("\xa0", " ")
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def decode_jd_html(value: str | None) -> str:
    """Decode the JSON-escaped employer HTML exactly once."""
    return unescape(value or "")


def source_date(epoch_seconds) -> str | None:
    """Darwinbox epoch seconds -> the source tenant's calendar date."""
    if epoch_seconds in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(epoch_seconds), tz=SOURCE_TIMEZONE).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Moneyview official careers entry left its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Moneyview Careers" not in title:
        raise ValueError("Moneyview official careers page title changed")
    links = [
        urljoin(response_url, anchor.get("href", ""))
        for anchor in soup.select("a[href]")
        if normalized_text(anchor.get_text(" ", strip=True)) == "View Openings"
    ]
    if not links:
        raise ValueError("Moneyview official careers page no longer publishes View Openings")
    unexpected = sorted({link for link in links if link != COMPANY_LINK})
    if unexpected:
        raise ValueError(f"Moneyview View Openings link changed: {unexpected}")
    description = soup.select_one('meta[name="description"]')
    return {
        "url": response_url,
        "title": title,
        "company_link": COMPANY_LINK,
        "view_openings_link_count": len(links),
        "view_openings_links": [COMPANY_LINK],
        "hiring_contact": sorted({
            anchor.get("href") for anchor in soup.select('a[href^="mailto:"]')
            if "care" in (anchor.get("href") or "").lower()
        }),
        "meta_description": description.get("content") if description else None,
    }


def parse_darwinbox_shell(html: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != DARWINBOX_HOST:
        raise ValueError("Moneyview Darwinbox shell is not on the inspected company tenant")
    soup = BeautifulSoup(html, "lxml")
    base = soup.base.get("href") if soup.base else None
    path = parsed.path.rstrip("/")
    if path == "/ms/candidate/careers":
        expected_base = "/ms/candidate/"
        kind = "published_entry_shell"
    elif path in {
        "/ms/candidatev2/main/careers/home",
        "/ms/candidatev2/main/careers/allJobs",
    }:
        expected_base = "/ms/candidatev2/"
        kind = "v2_app_shell"
    else:
        raise ValueError(f"Moneyview Darwinbox shell path changed: {parsed.path}")
    if base != expected_base:
        raise ValueError(f"Moneyview Darwinbox {kind} base href changed: {base!r}")
    if soup.select_one("app-root") is None:
        raise ValueError(f"Moneyview Darwinbox {kind} no longer contains app-root")
    scripts = [node.get("src") or "" for node in soup.select("script[src]")]
    return {
        "url": response_url,
        "kind": kind,
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "base_href": base,
        "script_count": len(scripts),
        "has_turnstile_script": any("challenges.cloudflare.com/turnstile" in src for src in scripts),
    }


def parse_company_config(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Moneyview company config is not an object")
    if payload.get("status") != "success":
        raise ValueError("Moneyview company config did not return success")
    message = payload.get("message")
    company = message.get("company") if isinstance(message, dict) else None
    if not isinstance(company, dict):
        raise ValueError("Moneyview company config has no company object")
    if company.get("company_name") != EMPLOYER_NAME:
        raise ValueError(f"Moneyview company_name changed: {company.get('company_name')!r}")
    if company.get("subdomain") != COMPANY_SUBDOMAIN:
        raise ValueError(f"Moneyview subdomain changed: {company.get('subdomain')!r}")
    if str(company.get("tenant_id")) != TENANT_ID:
        raise ValueError(f"Moneyview tenant_id changed: {company.get('tenant_id')!r}")
    if company.get("recruitment_enabled") is not True:
        raise ValueError("Moneyview recruitment is no longer enabled on its tenant")
    all_jobs_count = company.get("allJobsCount")
    if not isinstance(all_jobs_count, int) or all_jobs_count < 0:
        raise ValueError(f"Moneyview allJobsCount is not a non-negative integer: {all_jobs_count!r}")
    date_time_format = company.get("date_time_format") or {}
    return {
        "company_name": company.get("company_name"),
        "subdomain": company.get("subdomain"),
        "tenant_id": str(company.get("tenant_id")),
        "recruitment_enabled": company.get("recruitment_enabled"),
        "new_careers": company.get("new_careers"),
        "all_jobs_count": all_jobs_count,
        "timezone": date_time_format.get("timezone"),
        "date_format": date_time_format.get("dateFormat"),
        "job_tile_permissions": company.get("job_tile_permissions") or {},
        "job_detail_permissions": company.get("job_detail_permissions") or {},
        "careers_filters": company.get("careers_filters") or {},
        "aliases": company.get("aliases") or {},
        "group_company_site": company.get("group_company_site") or "",
    }


def parse_alljobs_payload(payload: dict, snapshot_name: str) -> tuple[list[dict], int]:
    if not isinstance(payload, dict):
        raise ValueError(f"Moneyview {snapshot_name} listing response is not an object")
    if payload.get("status") != "success":
        raise ValueError(f"Moneyview {snapshot_name} listing response did not return success")
    rows = payload.get("data")
    counts = payload.get("job_counts")
    if not isinstance(rows, list):
        raise ValueError(f"Moneyview {snapshot_name} listing response has no data array")
    if not isinstance(counts, int) or counts < 0:
        raise ValueError(f"Moneyview {snapshot_name} job_counts is not a non-negative integer")
    if counts < len(rows):
        raise ValueError(f"Moneyview {snapshot_name} job_counts is smaller than its page")
    ids = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or not row.get("title"):
            raise ValueError(f"Moneyview {snapshot_name} listing contains a row without id/title")
        ids.append(str(row["id"]))
    if len(ids) != len(set(ids)):
        raise ValueError(f"Moneyview {snapshot_name} listing repeats an exact job ID")
    return rows, counts


def parse_job_detail(payload: dict, expected_id: str) -> dict:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError(f"Moneyview detail response for {expected_id} did not return success")
    message = payload.get("message")
    jobs = message.get("job") if isinstance(message, dict) else None
    if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
        raise ValueError(f"Moneyview detail response for {expected_id} has no single job object")
    job = jobs[0]
    if str(job.get("id")) != str(expected_id):
        raise ValueError(f"Moneyview detail response ID changed: {job.get('id')!r} != {expected_id!r}")
    if not isinstance(job.get("jd"), str):
        raise ValueError(f"Moneyview detail response for {expected_id} has no jd string")
    return job


def location_values(row: dict) -> list[str]:
    for key in ("tool_tip_locations", "officelocation_show_arr_list", "officelocations_area"):
        values = row.get(key)
        if isinstance(values, list) and values:
            cleaned = [normalized_text(value) for value in values if value]
            if cleaned:
                return cleaned
    raw = row.get("locations")
    if isinstance(raw, str) and raw.strip() and raw.strip() != "Multiple Locations":
        return [normalized_text(raw)]
    return []


def salary_value(row: dict):
    if row.get("salary_range"):
        return row["salary_range"]
    if row.get("salary_min") or row.get("salary_max"):
        return {
            "min": row.get("salary_min") or None,
            "max": row.get("salary_max") or None,
            "currency": row.get("salary_currency") or None,
            "timeframe": row.get("salary_timeframe") or None,
        }
    return None


def parse_rendered_detail(html: str) -> dict:
    """Read the independently rendered detail DOM for the reference audit."""
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    body = normalized_text(soup.get_text(" ", strip=True))
    descriptions = soup.select(".jd-container .jd")
    description = normalized_text(descriptions[0].get_text(" ", strip=True)) if descriptions else ""
    snapshot = {}
    for item in soup.select(".section .grid-item"):
        label = item.select_one(".label")
        value = item.select_one(".value")
        if label and value:
            snapshot[normalized_text(label.get_text(" ", strip=True))] = normalized_text(
                value.get_text(" ", strip=True)
            )
    anchors = [
        (normalized_text(a.get_text(" ", strip=True)), a.get("href"))
        for a in soup.select("a[href]")
    ]
    return {
        "body_text": body,
        "description_text": description,
        "job_snapshot": snapshot,
        "anchors": anchors,
    }


class Moneyview(Adapter):
    """Follow Moneyview's own careers page to its exact Darwinbox tenant."""

    VERIFICATION_REVISION = "moneyview.official-careers-darwinbox-tenant.1"

    async def _get_shell(self, url: str) -> tuple[str, dict]:
        response = await self.http.request(
            "GET", url, headers=request_headers(referer=COMPANY_LINK)
        )
        if str(response.url) != url:
            raise ValueError(f"Moneyview shell request redirected away from {url}: {response.url}")
        return response.text, parse_darwinbox_shell(response.text, str(response.url))

    async def _get_config(self) -> dict:
        response = await self.http.request(
            "GET", COMPANY_CONFIG_API,
            headers=request_headers(referer=HOME_URL, json_request=True),
        )
        if str(response.url) != COMPANY_CONFIG_API:
            raise ValueError("Moneyview company config request redirected")
        return parse_company_config(response.json())

    async def _listing_page(self, page: int, snapshot_name: str) -> tuple[list[dict], int]:
        payload = {
            "companyId": "main",
            "page": page,
            "sort_option": "new",
            "limit": PAGE_LIMIT,
        }
        response = await self.http.request(
            "POST", ALLJOBS_API, json=payload,
            headers=request_headers(referer=LISTING_URL, json_request=True),
        )
        if str(response.url) != ALLJOBS_API:
            raise ValueError(f"Moneyview {snapshot_name} listing request redirected")
        return parse_alljobs_payload(
            response.json(), f"{snapshot_name} page {page}"
        )

    async def _listing_snapshot(self, snapshot_name: str) -> dict:
        started = time.perf_counter()
        rows, seen, counts, pages = [], set(), None, 0
        page = 1
        while page <= MAX_LISTING_PAGES:
            page_rows, page_counts = await self._listing_page(page, snapshot_name)
            pages += 1
            if counts is None:
                counts = page_counts
            elif page_counts != counts:
                raise ValueError(
                    f"Moneyview {snapshot_name} job_counts changed across pages: "
                    f"{counts} -> {page_counts}"
                )
            if not page_rows and len(rows) < counts:
                raise ValueError(
                    f"Moneyview {snapshot_name} listing ended before job_counts {counts}"
                )
            new_rows = [row for row in page_rows if str(row["id"]) not in seen]
            if page > 1 and not new_rows:
                raise ValueError(
                    f"Moneyview {snapshot_name} listing repeated IDs before job_counts {counts}"
                )
            for row in new_rows:
                seen.add(str(row["id"]))
            rows.extend(new_rows)
            if len(rows) >= counts:
                break
            page += 1
        if counts is None:
            raise ValueError(f"Moneyview {snapshot_name} listing returned no page")
        if len(rows) != counts:
            raise ValueError(
                f"Moneyview {snapshot_name} listing collected {len(rows)} rows, "
                f"job_counts says {counts}"
            )
        return {
            "rows": rows,
            "ids": [str(row["id"]) for row in rows],
            "counts": counts,
            "pages": pages,
            "seconds": time.perf_counter() - started,
        }

    def _make_job(self, row: dict, detail_row: dict | None, config: dict, listing_api_url: str):
        identity = str(row["id"])
        source_row = detail_row or row
        listing_jd = row.get("jd") or ""
        detail_jd = (detail_row or {}).get("jd")
        jd_matches = detail_row is None or detail_jd == listing_jd
        if detail_row is not None and detail_jd != listing_jd:
            self.result.warnings.append(f"moneyview_detail_jd_differs_from_listing:{identity}")
        if detail_row is not None and detail_jd:
            jd_raw = detail_jd
        else:
            jd_raw = listing_jd
        description_html = decode_jd_html(jd_raw)
        description_text = text_html(description_html) if description_html else ""

        description_method = None
        description_url = None
        if description_html and detail_row is not None and detail_jd:
            description_method = "moneyview.official_careers.darwinbox_job.jd"
            description_url = DETAIL_API_TEMPLATE.format(job_id=identity)
        elif description_html:
            description_method = "moneyview.official_careers.darwinbox_listing.jd"
            description_url = listing_api_url

        posted_on = source_row.get("posted_on") or row.get("posted_on")
        created_on = source_row.get("created_on") or row.get("created_on")
        listing_posting = row.get("job_posting") or {}
        detail_posting = source_row.get("job_posting") or {}
        careers_window = {}
        if isinstance(listing_posting, dict):
            careers_window.update(listing_posting.get("careers") or {})
        if isinstance(detail_posting, dict):
            careers_window.update(detail_posting.get("careers") or {})
        date_updated = source_date(posted_on or source_row.get("job_posting_on"))
        valid_through = source_date(careers_window.get("to"))
        apply_flow = {
            "button": "Apply Now",
            "observed_target": LOGIN_URL,
            "requires_account": True,
            "job_specific_apply_url_published": False,
        }
        raw = {
            "listing_api_url": listing_api_url,
            "listing_row": row,
            "detail_api_url": DETAIL_API_TEMPLATE.format(job_id=identity),
            "detail_page_url": DETAIL_PAGE_TEMPLATE.format(job_id=identity),
            "detail_row": detail_row,
            "detail_jd_matches_listing": jd_matches,
            "dates": {
                "posted_on_epoch": posted_on,
                "created_on": created_on,
                "job_posting": listing_posting,
                "detail_job_posting": detail_posting,
                "careers_window": careers_window,
                "visible_label": "Updated Date",
                "source_timezone": "Asia/Kolkata",
                "date_format": config.get("date_format"),
                "posted_date_is_not_published": True,
            },
            "apply_flow": apply_flow,
            "employer": config.get("company_name"),
            "tenant_id": config.get("tenant_id"),
            "subdomain": config.get("subdomain"),
            "legal_entity_ids": row.get("legal_entity_ids") or [],
            "source_ui": {
                "listing_url": LISTING_URL,
                "detail_route": DETAIL_PAGE_TEMPLATE.format(job_id=identity),
                "department_alias": (config.get("aliases") or {}).get("department"),
                "employee_type_alias": (config.get("aliases") or {}).get("emp_type"),
            },
        }
        title = (
            source_row.get("title")
            or source_row.get("designation_name")
            or row.get("title")
            or row.get("designation_name")
            or ""
        )
        job = self.job(
            raw,
            title=title,
            url=DETAIL_PAGE_TEMPLATE.format(job_id=identity),
            job_id=identity,
            requisition_id=str(source_row.get("internal_job_code") or row.get("internal_job_code") or "") or None,
            apply_url=DETAIL_PAGE_TEMPLATE.format(job_id=identity),
            description=description_html,
            method=description_method,
            description_url=description_url,
            locations=location_values(source_row) or location_values(row),
            department=source_row.get("department_name") or row.get("department_name"),
            team=source_row.get("functional_area_name") or row.get("functional_area_name") or None,
            employment_type=source_row.get("emp_type_name") or row.get("emp_type_name") or None,
            workplace_type="Remote" if (source_row.get("is_remote", row.get("is_remote")) or 0) else None,
            experience=source_row.get("experience") or row.get("experience") or None,
            salary=salary_value(source_row) or salary_value(row),
            date_posted=None,
            date_posted_raw=None,
            date_updated=date_updated,
            valid_through=valid_through,
            employer=config.get("company_name"),
            description_unavailable=not bool(description_html),
        )
        job.description_text = description_text
        job.record_identity = f"moneyview:{identity}"
        if not description_html:
            job.warnings.append("publisher_empty_jd")
            self.result.errors.append(f"{identity}: Moneyview detail jd is empty")
        elif len(normalized_text(description_text)) < 100:
            job.warnings.append("publisher_placeholder_description")
            self.result.warnings.append(f"moneyview_publisher_placeholder_description:{identity}")
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (entry_parts.scheme != "https" or entry_parts.hostname not in OFFICIAL_HOSTS
                or entry_parts.path.rstrip("/") != "/careers"):
            raise ValueError("Moneyview dedicated flow must start from its official HTTPS /careers page")

        entry_started = time.perf_counter()
        try:
            entry = await self.http.request(
                "GET", entry_url, headers=request_headers()
            )
            if str(entry.url) != entry_url:
                raise ValueError(f"Moneyview official entry redirected away: {entry.url}")
            entry_observation = parse_official_entry(entry.text, str(entry.url))
        except (FetchError, ValueError) as exc:
            self.result.errors.append(f"Moneyview official entry failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": entry_url,
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
            })
            self.result.timings["official_entry_phase_seconds"] = round(
                time.perf_counter() - entry_started, 6
            )
            return self.result
        entry_seconds = time.perf_counter() - entry_started
        self.result.metadata["official_entry_observation"] = entry_observation

        company_link_started = time.perf_counter()
        try:
            _, company_link_observation = await self._get_shell(COMPANY_LINK)
        except (FetchError, ValueError) as exc:
            self.result.errors.append(
                f"Moneyview published Darwinbox link failed: {type(exc).__name__}: {exc}"
            )
            self.result.listing_complete = False
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": entry_url,
                "company_link": COMPANY_LINK,
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
            })
            self.result.timings["official_entry_phase_seconds"] = round(entry_seconds, 6)
            self.result.timings["company_link_phase_seconds"] = round(
                time.perf_counter() - company_link_started, 6
            )
            return self.result
        company_link_seconds = time.perf_counter() - company_link_started
        self.result.metadata["company_link_observation"] = company_link_observation

        home_started = time.perf_counter()
        try:
            _, home_observation = await self._get_shell(HOME_URL)
            self.result.metadata["darwinbox_home_observation"] = home_observation
        except (FetchError, ValueError) as exc:
            self.result.warnings.append(f"moneyview_darwinbox_home_shell_unavailable:{type(exc).__name__}")
            self.result.metadata["darwinbox_home_observation"] = {
                "url": HOME_URL, "error": f"{type(exc).__name__}: {exc}"
            }
        home_seconds = time.perf_counter() - home_started

        config_started = time.perf_counter()
        try:
            config = await self._get_config()
        except (FetchError, ValueError) as exc:
            self.result.errors.append(f"Moneyview company config failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": entry_url,
                "company_link": COMPANY_LINK,
                "config_error": f"{type(exc).__name__}: {exc}",
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
            })
            self.result.timings["official_entry_phase_seconds"] = round(entry_seconds, 6)
            self.result.timings["company_link_phase_seconds"] = round(company_link_seconds, 6)
            self.result.timings["company_config_phase_seconds"] = round(
                time.perf_counter() - config_started, 6
            )
            return self.result
        config_seconds = time.perf_counter() - config_started
        self.result.metadata["company_config"] = config

        initial_started = time.perf_counter()
        try:
            initial = await self._listing_snapshot("initial")
        except (FetchError, ValueError) as exc:
            self.result.errors.append(
                f"Moneyview company-scoped listing failed: {type(exc).__name__}: {exc}"
            )
            self.result.expected_total = config.get("all_jobs_count")
            self.result.listing_complete = False
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": entry_url,
                "company_link": COMPANY_LINK,
                "listing_url": LISTING_URL,
                "listing_api_url": ALLJOBS_API,
                "listing_error": f"{type(exc).__name__}: {exc}",
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
            })
            self.result.timings["official_entry_phase_seconds"] = round(entry_seconds, 6)
            self.result.timings["company_link_phase_seconds"] = round(company_link_seconds, 6)
            self.result.timings["company_config_phase_seconds"] = round(config_seconds, 6)
            self.result.timings["listing_phase_seconds"] = round(
                time.perf_counter() - initial_started, 6
            )
            return self.result
        initial_seconds = time.perf_counter() - initial_started
        initial_rows, counts = initial["rows"], initial["counts"]
        if config["all_jobs_count"] != counts:
            self.result.errors.append(
                "moneyview_company_config_allJobsCount_differs_from_listing_job_counts:"
                f"{config['all_jobs_count']}!={counts}"
            )

        detail_started = time.perf_counter()
        jobs = []
        detail_failures = []
        for row in initial_rows:
            identity = str(row["id"])
            detail_row = None
            try:
                response = await self.http.request(
                    "GET", DETAIL_API_TEMPLATE.format(job_id=identity),
                    headers=request_headers(
                        referer=DETAIL_PAGE_TEMPLATE.format(job_id=identity),
                        json_request=True,
                    ),
                )
                if str(response.url) != DETAIL_API_TEMPLATE.format(job_id=identity):
                    raise ValueError("Moneyview detail request redirected")
                detail_row = parse_job_detail(response.json(), identity)
            except (FetchError, ValueError) as exc:
                detail_failures.append(identity)
                self.result.errors.append(
                    f"Moneyview exact detail failed for {identity}: {type(exc).__name__}: {exc}"
                )
                # A challenge/rate-limit stops further detail requests.
                if isinstance(exc, FetchError) and re.search(r"HTTP (401|403|429|500|502|503|504)\b", str(exc)):
                    self.result.warnings.append("moneyview_detail_fetch_stopped_on_block_status")
                    break
            jobs.append(self._make_job(row, detail_row, config, ALLJOBS_API))
        detail_seconds = time.perf_counter() - detail_started

        final_started = time.perf_counter()
        try:
            final = await self._listing_snapshot("final")
        except (FetchError, ValueError) as exc:
            final = None
            self.result.errors.append(
                f"Moneyview final listing recheck failed: {type(exc).__name__}: {exc}"
            )
        final_seconds = time.perf_counter() - final_started

        if final is not None:
            membership_equal = initial["ids"] == final["ids"]
            if not membership_equal:
                self.result.errors.append(
                    "moneyview_listing_membership_changed_between_initial_and_final_snapshots"
                )
            if initial["counts"] != final["counts"]:
                self.result.errors.append(
                    f"moneyview_listing_count_changed:{initial['counts']}!={final['counts']}"
                )
            expected_ids = final["ids"]
            by_id = {str(row["id"]): row for row in initial_rows}
            for identity in expected_ids:
                if identity not in by_id:
                    self.result.errors.append(f"moneyview_final_listing_added_uncollected_id:{identity}")
            rows_for_jobs = [by_id[identity] for identity in expected_ids if identity in by_id]
            # Preserve the exact final membership order if it is unchanged.
            if membership_equal and rows_for_jobs:
                jobs_by_id = {str(job.job_id): job for job in jobs}
                jobs = [jobs_by_id[identity] for identity in expected_ids if identity in jobs_by_id]
        else:
            expected_ids = initial["ids"]
            membership_equal = False

        for job in jobs:
            self.add(job)

        self.result.expected_total = counts
        self.result.pages = initial["pages"] + (final["pages"] if final else 0)
        verified = sum(1 for job in jobs if job.description_verified)
        self.result.listing_complete = (
            not self.result.errors
            and len(jobs) == counts
            and membership_equal
        )
        if counts == 0 and not self.result.errors:
            self.result.warnings.append("official_board_currently_lists_zero_open_jobs")
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": entry_url,
            "official_entry_observation": entry_observation,
            "company_link": COMPANY_LINK,
            "company_link_observation": company_link_observation,
            "darwinbox_home_observation": self.result.metadata.get("darwinbox_home_observation"),
            "listing_url": LISTING_URL,
            "listing_api_url": ALLJOBS_API,
            "detail_page_template": DETAIL_PAGE_TEMPLATE,
            "detail_api_template": DETAIL_API_TEMPLATE,
            "membership_authority": (
                "Moneyview's own https://moneyview.in/careers page publishes four "
                "View Openings controls to https://moneyview.darwinbox.in/ms/candidate/careers; "
                "the rendered journey lands on the Moneyview Darwinbox tenant and its "
                "/ms/candidatev2/main/careers/allJobs route. Membership is the tenant's own "
                "POST /ms/candidateapi/job/alljobs?companyId=main response."
            ),
            "company_identity": {
                "company_name": config["company_name"],
                "subdomain": config["subdomain"],
                "tenant_id": config["tenant_id"],
                "all_jobs_count": config["all_jobs_count"],
            },
            "listing_request": {
                "method": "POST",
                "url": ALLJOBS_API,
                "payload": {
                    "companyId": "main",
                    "page": "1-based page from the UI",
                    "sort_option": "new",
                    "limit": PAGE_LIMIT,
                },
                "filters_applied": False,
                "scope": "Moneyview Darwinbox tenant, companyId=main only",
            },
            "listing_filters": {
                "visible_labels": list(VISIBLE_FILTERS),
                "behavior": (
                    "Department/Sub Department, Employee Type, Job Tag, Country and Experience "
                    "filters are client-side over the same unfiltered alljobs response."
                ),
            },
            "pagination": {
                "api_page_parameter": "page (1-based)",
                "api_limit": PAGE_LIMIT,
                "job_counts": counts,
                "initial_pages": initial["pages"],
                "final_pages": final["pages"] if final else None,
                "ui_pagination_controls": [],
                "note": (
                    "The current rendered UI shows all 6 rows with no next/previous or load-more "
                    "control; job_counts=6 and the first page limit=10 covers the membership."
                ),
            },
            "listing_snapshots": {
                "initial_ids": initial["ids"],
                "final_ids": final["ids"] if final else None,
                "initial_counts": initial["counts"],
                "final_counts": final["counts"] if final else None,
                "same_ids": membership_equal,
                "initial_seconds": round(initial_seconds, 6),
                "final_seconds": round(final_seconds, 6),
            },
            "detail_fetch": {
                "mode": "sequential exact detail API per listed ID",
                "requested": len(initial_rows),
                "failed_ids": detail_failures,
                "method": "GET " + DETAIL_API_TEMPLATE,
            },
            "description_source": (
                "Each exact listed row's employer-authored jd HTML, checked against the native "
                "GET /ms/candidateapi/job/{id}?companyId=main detail object."
            ),
            "date_semantics": {
                "date_posted": None,
                "date_posted_reason": (
                    "Darwinbox publishes posted_on/job_posting_on but the detail UI labels it "
                    "'Updated Date'; it is retained as date_updated and the raw epoch, not "
                    "misreported as a posted date."
                ),
                "date_updated_source": "posted_on/job_posting_on epoch rendered in Asia/Kolkata",
                "valid_through_source": "job_posting.careers.to epoch rendered in Asia/Kolkata",
                "created_on_source": "retained raw only; never used as a posting date",
            },
            "apply_flow": {
                "detail_button": "Apply Now",
                "observed_target": LOGIN_URL,
                "requires_account": True,
                "job_specific_apply_url_published": False,
            },
            "publisher_placeholder_jobs": [
                job.job_id for job in jobs
                if "publisher_placeholder_description" in job.warnings
            ],
            "request_user_agent_note": (
                "The Darwinbox host returns Cloudflare 403 to non-browser User-Agents; "
                "the standard desktop Chrome User-Agent receives the normal public responses. "
                "No CAPTCHA is solved and a 403/429 stops the run."
            ),
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings.update({
            "official_entry_phase_seconds": round(entry_seconds, 6),
            "company_link_phase_seconds": round(company_link_seconds, 6),
            "darwinbox_home_phase_seconds": round(home_seconds, 6),
            "company_config_phase_seconds": round(config_seconds, 6),
            "listing_phase_seconds": round(initial_seconds, 6),
            "detail_phase_seconds": round(detail_seconds, 6),
            "final_listing_phase_seconds": round(final_seconds, 6),
        })
        if verified < len(jobs):
            self.result.warnings.append(
                f"moneyview_unverified_publisher_placeholder_descriptions:{len(jobs) - verified}"
            )
        return self.result
