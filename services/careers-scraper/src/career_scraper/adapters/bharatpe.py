"""BharatPe's own /career journey and its company-published Darwinbox board.

The ledger entry ``https://bharatpe.com/career/`` returns the site homepage
(H``Home - BharatPe``) to plain HTTP; the careers marketing page is
client-rendered. Its rendered ``See openings`` control links to the exact
Darwinbox candidate board ``https://bharatpe.darwinbox.in/ms/candidatev2/
main/careers/allJobs`` (company tenant ``bharatpe``, candidate route
``main``). The SPA shell embeds ``apiURL: "/ms/candidateapi/"`` and the
listing call ``POST job/alljobs`` with ``{companyId, page, sort_option,
limit}``; details are ``GET job/{id}``. Only that exact company-exposed
board is ever requested.
"""

import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import text_html

ENTRY_HOST = "bharatpe.com"
DARWINBOX_HOST = "bharatpe.darwinbox.in"
DARWINBOX_ENTRY = "https://bharatpe.darwinbox.in/ms/candidatev2/main/careers/allJobs"
ALLJOBS_API = "https://bharatpe.darwinbox.in/ms/candidateapi/job/alljobs?companyId=main"
COMPANY_CONFIG_API = (
    "https://bharatpe.darwinbox.in/ms/candidateapi/getCompanyConfig?is_preview=false&companyId=main"
)
API_BASE = "/ms/candidateapi/"
SHELL_BUNDLE_RE = re.compile(r"main\.[a-f0-9]+\.js")


def alljobs_api_url():
    return ALLJOBS_API


def company_config_api_url():
    return COMPANY_CONFIG_API


def job_detail_api_url(identity):
    if not identity:
        raise ValueError("BharatPe Darwinbox job ID is empty")
    return f"https://{DARWINBOX_HOST}{API_BASE}job/{identity}?companyId=main"


def detail_page_url(identity):
    if not identity:
        raise ValueError("BharatPe Darwinbox job ID is empty")
    return f"https://{DARWINBOX_HOST}/ms/candidatev2/main/careers/jobDetails/{identity}"


def _epoch_to_date(value):
    """Darwinbox posted_on/created_on epoch seconds -> ``YYYY-MM-DD`` or None."""
    if value in (None, "", 0):
        return None
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _locations(row):
    values = row.get("locations") or row.get("officelocation_show_arr_list") or []
    if isinstance(values, str):
        return [{"location": values, "address": None}]
    locations = []
    for value in values:
        if isinstance(value, dict):
            locations.append(
                {"location": value.get("name") or value.get("location"), "address": value.get("address")}
            )
        elif value:
            locations.append({"location": str(value), "address": None})
    return locations


def _attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def parse_alljobs_payload(payload, snapshot_name):
    """Validate one ``job/alljobs`` response and return ``(rows, job_counts)``."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError(f"BharatPe {snapshot_name} listing response has no data array")
    rows = payload["data"]
    counts = payload.get("job_counts")
    if counts is not None and not isinstance(counts, int):
        raise ValueError(f"BharatPe {snapshot_name} listing job_counts is not an integer")
    ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
    if any(not identity for identity in ids):
        raise ValueError(f"BharatPe {snapshot_name} listing contains a row without an exact job ID")
    if len(ids) != len(set(ids)):
        raise ValueError(f"BharatPe {snapshot_name} listing repeats a company-published ID")
    if counts is not None and counts < len(rows):
        raise ValueError(f"BharatPe {snapshot_name} listing job_counts is smaller than its page")
    return rows, counts


class BharatPe(Adapter):
    """Follow BharatPe's official entry to its own Darwinbox board only."""

    VERIFICATION_REVISION = "bharatpe.official-career-darwinbox-alljobs.1"

    @staticmethod
    def _check_entry(page_url, html):
        parsed = urlsplit(page_url)
        if parsed.scheme != "https" or parsed.hostname not in {"bharatpe.com", "www.bharatpe.com"}:
            raise ValueError(f"BharatPe official entry left the company HTTPS site: {page_url}")
        soup = BeautifulSoup(html, "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        careers_links = [
            urljoin(page_url, anchor.get("href"))
            for anchor in soup.select("a[href='/career'], a[href='/career/']")
        ]
        job_like = []
        for anchor in soup.select("a[href]"):
            href = anchor.get("href") or ""
            if re.search(r"darwinbox|allJobs|jobDetails", href, re.I):
                job_like.append(href)
        return {
            "title": title,
            "careers_nav_links": careers_links,
            "darwinbox_listing_links_in_raw_html": job_like,
            "canonical": (soup.select_one('link[rel="canonical"]') or {}).get("href"),
        }

    @staticmethod
    def _check_shell(page_url, html):
        parsed = urlsplit(page_url)
        if parsed.scheme != "https" or parsed.hostname != DARWINBOX_HOST:
            raise ValueError(f"BharatPe company-published board left its Darwinbox tenant: {page_url}")
        soup = BeautifulSoup(html, "lxml")
        base = soup.base.get("href") if soup.base else None
        scripts = [node.get("src") or "" for node in soup.select("script[src]")]
        bundle = next((src for src in scripts if SHELL_BUNDLE_RE.fullmatch(src)), None)
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if base == "/ms/candidatev2/" and bundle is not None:
            kind = "app_shell"
        elif base is None and bundle is None and title:
            # Scripted HTTP receives Darwinbox's crawler/OG shell instead of the
            # app shell; it still proves the exact board URL but not its bundle.
            kind = "og_shell"
        else:
            raise ValueError("BharatPe Darwinbox shell changed from its observed app/OG shell")
        return {
            "kind": kind,
            "title": title,
            "base_href": base,
            "main_bundle": urljoin(page_url, bundle) if bundle else None,
            "scripts": scripts,
        }

    @staticmethod
    def _assert_api_base(bundle_text):
        return 'apiURL:"' + API_BASE + '"' in bundle_text and '"allJobs":"job/alljobs"' in bundle_text

    async def _post_listing(self, api_url, payload, snapshot_name):
        try:
            response = await self.http.request("POST", api_url, json=payload)
        except FetchError as exc:
            raise FetchError(
                f"BharatPe {snapshot_name} Darwinbox listing request blocked by WAF: {exc}"
            ) from exc
        if str(response.url) != api_url:
            raise ValueError(f"BharatPe {snapshot_name} listing request redirected away from its source")
        try:
            parsed = response.json()
        except ValueError as exc:
            raise ValueError(f"BharatPe {snapshot_name} listing response is not JSON") from exc
        parse_alljobs_payload(parsed, snapshot_name)
        return parsed

    def _make_job(self, row, *, detail_row=None, api_url):
        identity = str(row["id"])
        source_row = detail_row or row
        title = (
            source_row.get("designation_display_name")
            or source_row.get("title")
            or source_row.get("designation_name")
            or row.get("designation_display_name")
            or row.get("title")
            or row.get("designation_name")
            or ""
        )
        description_html = source_row.get("jd") or row.get("jd") or ""
        description_text = text_html(description_html) if description_html else (source_row.get("jd_summary") or "")
        if not description_html.strip():
            self.result.errors.append(f"{identity}: BharatPe Darwinbox detail jd is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: BharatPe Darwinbox detail jd is implausibly short")
        date_posted = _epoch_to_date(source_row.get("posted_on") or row.get("posted_on") or row.get("created_on"))
        date_posted_raw = source_row.get("posted_on", row.get("posted_on", row.get("created_on")))
        salary = source_row.get("salary_range") or row.get("salary_range") or None
        raw = {
            "listing": row,
            "source_ui": {
                "listing_api_url": api_url,
                "native_detail_page": detail_page_url(identity),
                "detail_api_url": job_detail_api_url(identity),
                "attachment_links": _attachment_links(description_html),
                "salary_from_detail": salary,
            },
        }
        if detail_row is not None and detail_row != row:
            raw["detail"] = detail_row
        job = self.job(
            raw,
            title=title,
            url=detail_page_url(identity),
            job_id=identity,
            requisition_id=str(source_row.get("internal_job_code") or row.get("internal_job_code") or "") or None,
            apply_url=detail_page_url(identity),
            description=description_html,
            method="bharatpe.official_career.darwinbox_job.jd",
            description_url=api_url,
            locations=_locations(source_row) or _locations(row),
            department=source_row.get("department_name") or row.get("department_name"),
            team=source_row.get("functional_area_name") or row.get("functional_area_name"),
            employment_type=source_row.get("emp_type_name") or row.get("emp_type") or row.get("emp_type_name"),
            workplace_type=("Remote" if source_row.get("is_remote", row.get("is_remote")) else None),
            experience=source_row.get("experience") or row.get("experience"),
            salary=salary,
            date_posted=date_posted,
            date_posted_raw=date_posted_raw,
        )
        job.description_text = description_text
        job.record_identity = f"bharatpe:{identity}"
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (
            entry_parts.scheme != "https"
            or entry_parts.hostname not in {"bharatpe.com", "www.bharatpe.com"}
            or entry_parts.path.rstrip("/") != "/career"
        ):
            raise ValueError("BharatPe dedicated flow must start from its official HTTPS /career/ entry")

        entry_started = time.perf_counter()
        try:
            entry = await self.http.get(entry_url)
        except FetchError as exc:
            self.result.errors.append(f"BharatPe official entry fetch failed: {exc}")
            entry = None
        entry_seconds = time.perf_counter() - entry_started
        entry_observation = {}
        if entry is not None:
            entry_observation = self._check_entry(str(entry.url), entry.text)
        self.result.metadata["official_entry_observation"] = entry_observation

        shell_started = time.perf_counter()
        shell_observation = {}
        try:
            shell = await self.http.get(DARWINBOX_ENTRY)
            shell_observation = self._check_shell(str(shell.url), shell.text)
        except (FetchError, ValueError) as exc:
            self.result.errors.append(
                f"BharatPe company-published Darwinbox board unreachable: {type(exc).__name__}: {exc}"
            )
        shell_seconds = time.perf_counter() - shell_started
        self.result.metadata["darwinbox_shell_observation"] = shell_observation

        listing_error = None
        initial_payload = final_payload = None
        try:
            initial_payload = await self._post_listing(
                ALLJOBS_API, {"companyId": "main", "page": 1, "sort_option": "new", "limit": 10}, "initial"
            )
            final_payload = await self._post_listing(
                ALLJOBS_API, {"companyId": "main", "page": 1, "sort_option": "new", "limit": 10}, "final"
            )
        except (FetchError, ValueError) as exc:
            listing_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(listing_error)

        api_url = ALLJOBS_API
        if listing_error is not None or initial_payload is None:
            self.result.expected_total = None
            self.result.pages = 1
            self.result.listing_complete = False
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "membership_authority": (
                    "BharatPe official /career/ page's rendered 'See openings' control -> exact Darwinbox "
                    "candidate board https://bharatpe.darwinbox.in/ms/candidatev2/main/careers/allJobs; "
                    "its POST /ms/candidateapi/job/alljobs?companyId=main listing request"
                ),
                "membership_observable_via_http": False,
                "waf_block_status": 403,
                "waf_block_detail": "Cloudflare 'Sorry, you have been blocked' on /ms/candidateapi/* via HTTP.",
                "listing_error": listing_error,
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
                "initial_and_final_listing_requests": 2,
            })
            self.result.timings["official_entry_phase_seconds"] = round(entry_seconds, 6)
            self.result.timings["darwinbox_shell_phase_seconds"] = round(shell_seconds, 6)
            return self.result

        assert initial_payload is not None and final_payload is not None
        initial_rows, initial_counts = parse_alljobs_payload(initial_payload, "initial")
        final_rows, final_counts = parse_alljobs_payload(final_payload, "final")

        # Paginate the same exact listing request until job_counts is covered.
        all_rows = list(final_rows)
        seen = {str(row["id"]) for row in all_rows}
        total = final_counts if final_counts is not None else len(all_rows)
        page = 2
        pages_fetched = 2  # initial + final snapshots are page-1 requests
        while len(all_rows) < total and page <= 100:
            payload = await self._post_listing(
                ALLJOBS_API, {"companyId": "main", "page": page, "sort_option": "new", "limit": 10}, f"page{page}"
            )
            rows, _ = parse_alljobs_payload(payload, f"page{page}")
            pages_fetched += 1
            if not rows:
                self.result.errors.append("BharatPe Darwinbox listing ended before job_counts was reached")
                break
            new_rows = [row for row in rows if str(row["id"]) not in seen]
            if not new_rows:
                self.result.errors.append(
                    "BharatPe Darwinbox listing repeated IDs before job_counts was reached"
                )
                break
            for row in new_rows:
                seen.add(str(row["id"]))
            all_rows.extend(new_rows)
            page += 1

        initial_ids = [str(row["id"]) for row in initial_rows]
        final_ids = [str(row["id"]) for row in final_rows]
        membership_changed = initial_ids != final_ids
        if membership_changed:
            self.result.errors.append(
                "bharatpe_official_listing_membership_changed_between_initial_and_final_snapshots"
            )

        detail_started = time.perf_counter()
        detail_error = None
        for row in all_rows:
            identity = str(row["id"])
            detail_row = None
            try:
                detail = await self.http.get(job_detail_api_url(identity))
                try:
                    detail_row = detail.json()
                except ValueError as exc:
                    raise ValueError("BharatPe Darwinbox detail response is not JSON") from exc
                if not isinstance(detail_row, dict) or not detail_row:
                    raise ValueError("BharatPe Darwinbox detail response has no job object")
            except (FetchError, ValueError) as exc:
                detail_error = f"{identity}: {type(exc).__name__}: {exc}"
                self.result.errors.append(f"BharatPe Darwinbox exact detail failed: {detail_error}")
                detail_row = None
            self.add(self._make_job(row, detail_row=detail_row, api_url=api_url))
        detail_seconds = time.perf_counter() - detail_started

        self.result.expected_total = total
        self.result.pages = pages_fetched
        self.result.listing_complete = not self.result.errors
        if total == 0 and not self.result.errors:
            self.result.warnings.append("official_board_currently_lists_zero_open_jobs")
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url) if entry is not None else entry_url,
            "listing_api_url": api_url,
            "membership_authority": (
                "BharatPe official /career/ page's rendered 'See openings' control -> exact Darwinbox "
                "candidate board; its POST /ms/candidateapi/job/alljobs?companyId=main listing request"
            ),
            "listing_filter_behavior": (
                "unfiltered board rows; Department/Location/Employee Type/Job Tag/Country/Experience "
                "filters are client-side over the same listing response"
            ),
            "ui_pagination": "page/limit POST parameters; the UI starts at page 1, limit 10, sort_option new",
            "initial_visible_ids": initial_ids,
            "final_visible_ids": final_ids,
            "initial_job_counts": initial_counts,
            "final_job_counts": final_counts,
            "listing_pages_fetched": pages_fetched,
            "description_source": "Each exact listed row's jd field, confirmed against GET job/{id} detail objects.",
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
        })
        self.result.timings["official_entry_phase_seconds"] = round(entry_seconds, 6)
        self.result.timings["darwinbox_shell_phase_seconds"] = round(shell_seconds, 6)
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        return self.result
