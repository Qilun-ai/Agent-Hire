"""BrowserStack's explicitly linked public Workday External careers board."""
from __future__ import annotations

import re
import time
from copy import deepcopy
from urllib.parse import urlsplit

from .workday import Workday


CAREERS_URL = "https://www.browserstack.com/careers"
PUBLISHED_BOARD_URL = "https://browserstack.wd3.myworkdayjobs.com/External"
WORKDAY_LISTING_URL = "https://browserstack.wd3.myworkdayjobs.com/wday/cxs/browserstack/External/jobs"
WORKDAY_CLIENT_URL = "https://wd3.myworkdaycdn.com/wday/asset/candidate-experience-jobs/cx-jobs.min.js"
WORKDAY_CLIENT_SHA256 = "271acbf33d016832ca865e4487f7c13e756dfe54cc6a342e1e3e36b0b9ba1dc8"
WORKDAY_APPLY_ENTRY_FORMULA = "window.location.origin + window.location.pathname + '/apply' + window.location.search"
RENDERED_ROUTE_PREFIX = "/en-US/External"
RENDERED_ROUTE_REFERENCE = "https://browserstack.wd3.myworkdayjobs.com/en-US/External/job/India-Remote/Business-Development-Representative_JR103750"
MANUAL_APPLY_REFERENCE = "https://browserstack.wd3.myworkdayjobs.com/en-US/External/job/India-Remote/Business-Development-Representative_JR103750/apply/applyManually"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def browserstack_apply_routes(external_path: str, search: str = "") -> tuple[str, str, str]:
    """Derive the Workday application entry and manual route from the exact CXS path."""
    if not isinstance(external_path, str) or not external_path.startswith("/job/"):
        raise ValueError("BrowserStack apply routes require an exact listed /job/ externalPath")
    if search and not search.startswith("?"):
        raise ValueError("Workday search suffix must preserve the literal leading '?'")
    origin = urlsplit(PUBLISHED_BOARD_URL)
    origin_text = f"{origin.scheme}://{origin.netloc}"
    rendered_job_url = f"{origin_text}{RENDERED_ROUTE_PREFIX}{external_path}{search}"
    apply_url = f"{origin_text}{RENDERED_ROUTE_PREFIX}{external_path}/apply{search}"
    manual_apply_url = f"{origin_text}{RENDERED_ROUTE_PREFIX}{external_path}/apply/applyManually{search}"
    return rendered_job_url, apply_url, manual_apply_url


class BrowserStack(Workday):
    """Scrape only the Workday board linked by BrowserStack's careers page.

    The official page publishes three ``View open positions`` CTAs, all to the
    same ``/External`` board. Its rendered board shows 29 unfiltered jobs over
    two pages (20 + 9); Workday's public CXS endpoints provide the full detail
    bodies for those exact listing paths. This class intentionally rejects any
    other board, tenant, or company to avoid turning a shared ATS into scope.
    """

    VERIFICATION_REVISION = "2026-11-07.2"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._current_listing_rows: dict[str, dict] = {}
        self._archive_cache_invalidations: dict[str, list[str]] = {}

    def add(self, job):
        """Save current listing rows and invalidate stale same-job cached fields."""
        row = deepcopy(job.raw)
        path = row.get("externalPath")
        if isinstance(path, str):
            self._current_listing_rows[path] = row
        cached = self.options.cached_jobs.get(job.url)
        if cached:
            old = cached.raw
            old_info = (old.get("detail") or {}).get("jobPostingInfo") or {}
            stale_fields = []
            for field in ("externalPath", "title", "postedOn", "locationsText", "bulletFields"):
                if old.get(field) != row.get(field):
                    stale_fields.append(field)
            if old_info.get("postedOn") != row.get("postedOn"):
                stale_fields.append("detail.postedOn")
            if stale_fields:
                req_id = next((value for value in row.get("bulletFields", [])
                              if isinstance(value, str) and re.fullmatch(r"[A-Za-z]*\d[\w-]*", value)), path)
                self._archive_cache_invalidations[str(req_id)] = sorted(set(stale_fields))
                self.options.cached_jobs.pop(job.url, None)
        super().add(job)

    async def run(self):
        if self.company.slug != "browserstack":
            raise ValueError("BrowserStack adapter requires the BrowserStack company")
        if self.source.url != PUBLISHED_BOARD_URL:
            raise ValueError(f"BrowserStack careers only publishes {PUBLISHED_BOARD_URL}")
        if self.source.evidence_url != CAREERS_URL:
            raise ValueError(f"BrowserStack board evidence must point to {CAREERS_URL}")

        started = time.perf_counter()
        result = await super().run()
        initial_paths = [job.raw.get("externalPath") for job in result.jobs]
        initial_paths = [path for path in initial_paths if isinstance(path, str) and path.startswith("/job/")]
        final_recheck = {"status": "skipped", "reason": "initial_listing_incomplete", "pages": []}
        if result.listing_complete and not result.errors:
            final_pages = []
            final_rows = []
            try:
                for offset in (0, 20):
                    page = await self.http.json(
                        WORKDAY_LISTING_URL,
                        method="POST",
                        json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
                    )
                    rows = page.get("jobPostings", [])
                    final_rows.extend(rows)
                    final_pages.append({
                        "offset": offset,
                        "response_total": page.get("total"),
                        "returned_rows": len(rows),
                        "external_paths": [row.get("externalPath") for row in rows],
                        "requisitions": [
                            next((field for field in row.get("bulletFields", [])
                                  if isinstance(field, str) and re.fullmatch(r"[A-Za-z]*\d[\w-]*", field)), None)
                            for row in rows
                        ],
                    })
                final_paths = [row.get("externalPath") for row in final_rows]
                same_paths = len(final_paths) == len(set(final_paths)) and set(final_paths) == set(initial_paths)
                final_by_path = {row.get("externalPath"): row for row in final_rows}
                listing_fields = ("title", "postedOn", "locationsText", "bulletFields")
                same_listing_fields = all(
                    all(self._current_listing_rows.get(path, {}).get(field) == final_by_path.get(path, {}).get(field)
                        for field in listing_fields)
                    for path in set(initial_paths) & set(final_paths)
                )
                expected_total = result.expected_total
                page_shape_ok = (
                    len(final_pages) == 2
                    and len(final_pages[0]["external_paths"]) == min(20, int(expected_total or 0))
                    and len(final_pages[1]["external_paths"]) == max(0, int(expected_total or 0) - 20)
                    and final_pages[0]["response_total"] == expected_total
                    and final_pages[1]["response_total"] in (0, expected_total)
                )
                final_recheck = {
                    "status": "complete" if same_paths and page_shape_ok else "mismatch",
                    "pages": final_pages,
                    "initial_external_paths": initial_paths,
                    "final_external_paths": final_paths,
                    "same_exact_paths": same_paths,
                    "same_listing_metadata": same_listing_fields,
                    "page_order_changed": [row.get("externalPath") for row in final_rows] != initial_paths,
                    "page_shape_reconciles": page_shape_ok,
                }
                if not same_paths or not same_listing_fields or not page_shape_ok:
                    result.listing_complete = False
                    result.errors.append("browserstack_final_membership_recheck_mismatch")
            except Exception as exc:
                final_recheck = {"status": "failed", "pages": final_pages,
                                 "error": f"{type(exc).__name__}: {exc}"}
                result.listing_complete = False
                result.errors.append("browserstack_final_membership_recheck_failed")
        result.metadata["final_membership_recheck"] = final_recheck
        result.metadata["archive_cache_invalidations"] = self._archive_cache_invalidations
        total = max(0.0, time.perf_counter() - started)
        detail_seconds = result.timings.get("detail_phase_seconds", 0.0)
        result.timings["browserstack_listing_seconds"] = round(max(0.0, total - detail_seconds), 6)
        result.timings["browserstack_adapter_seconds"] = round(total, 6)
        result.metadata["published_journey"] = {
            "official_entry_url": CAREERS_URL,
            "published_board_url": PUBLISHED_BOARD_URL,
            "listing_request_url": WORKDAY_LISTING_URL,
            "listing_method": "POST",
            "default_listing_payload": {
                "appliedFacets": {}, "limit": 20, "offset": "0, then +20", "searchText": ""
            },
            "rendered_unfiltered_total": 29,
            "rendered_pages": [20, 9],
            "filter_state": "No location, time type, job category, or other filters applied",
            "request_scope": "Only exact paths returned by the published BrowserStack /External listing are hydrated",
            "membership_rechecked_after_detail_hydration": final_recheck["status"] == "complete",
        }

        apply_routes = 0
        for job in result.jobs:
            path = job.raw.get("externalPath")
            listing = self._current_listing_rows.get(path)
            if listing and listing != {key: value for key, value in job.raw.items() if key != "detail"}:
                old_raw = job.raw
                job.raw = {
                    **listing,
                    "detail": old_raw.get("detail"),
                    "archive_detail_reuse": old_raw.get("archive_detail_reuse"),
                }
                job.title = listing.get("title") or job.title
            info = (job.raw.get("detail") or {}).get("jobPostingInfo") or {}
            # Workday publishes the true ISO posting start date in startDate and
            # the UI's relative age in postedOn. Retain both; never turn the
            # relative label or request time into a fabricated date.
            start_date = info.get("startDate")
            job.date_posted = start_date if isinstance(start_date, str) and _DATE.fullmatch(start_date) else None
            job.date_posted_raw = job.raw.get("postedOn") or info.get("postedOn")
            if info.get("postedOn") and job.raw.get("postedOn") and info.get("postedOn") != job.raw.get("postedOn"):
                job.date_posted = None
                job.warnings.append("detail_and_listing_posted_on_conflict")

            # BrowserStack's own published CXS client constructs the Apply
            # entry route from the rendered detail pathname, then opens a
            # "Start Your Application" modal. The manual option adds
            # /applyManually. Use the exact listed externalPath and the locale
            # route observed in the independently rendered JR103750 page.
            job.url = info.get("externalUrl") or job.url
            source_query = urlsplit(job.url).query
            search = f"?{source_query}" if source_query else ""
            external_path = job.raw.get("externalPath")
            rendered_job_url, job.apply_url, manual_apply_url = browserstack_apply_routes(external_path, search)
            apply_routes += 1
            job.valid_through = (
                info.get("endDate")
                if isinstance(info.get("endDate"), str) and _DATE.fullmatch(info["endDate"])
                else None
            )
            # Keep all exact publisher fields, including the complete rich HTML
            # description, dates, location, type, requisition and API URLs, in
            # raw.detail even where the public schema has no separate column.
            job.raw["published_detail_fields"] = sorted(info)
            job.raw["browserstack_apply_semantics"] = {
                "client_url": WORKDAY_CLIENT_URL,
                "client_sha256": WORKDAY_CLIENT_SHA256,
                "entry_formula": WORKDAY_APPLY_ENTRY_FORMULA,
                "entry_url": job.apply_url,
                "rendered_job_url": rendered_job_url,
                "manual_apply_formula": "window.location.origin + window.location.pathname + '/apply/applyManually' + window.location.search",
                "manual_apply_url": manual_apply_url,
                "button_behavior": "Apply opens the Start Your Application modal; it does not immediately navigate. Apply Manually reaches the manual application route.",
                "route_prefix_reference": RENDERED_ROUTE_REFERENCE,
                "manual_route_cua_reference": MANUAL_APPLY_REFERENCE,
                "search_suffix_preserved_from_published_external_url": search,
            }
        result.metadata["apply_url_review"] = {
            "client_formula_verified_from_browserstack_asset": True,
            "client_url": WORKDAY_CLIENT_URL,
            "client_sha256": WORKDAY_CLIENT_SHA256,
            "button_behavior": "Apply opens the Start Your Application modal",
            "manual_apply_cua_reference_job_id": "JR103750",
            "apply_entry_routes_derived": apply_routes,
            "manual_routes_derived": apply_routes,
        }
        return result
