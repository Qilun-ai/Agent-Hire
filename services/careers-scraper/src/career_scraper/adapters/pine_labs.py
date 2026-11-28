"""Pine Labs' official careers journey and its company-scoped TurboHire gateway.

Reviewed 2026-11-08.  The ledger URL ``https://www.pinelabs.com/careers`` links
only to ``/careers/open-jobs``, a Next.js client component.  That component's
only membership request is the exact POST below to Pine Labs' own gateway.  On
review date the gateway answered ``Total=0`` with empty filter lists and the
hydrated page showed its "No jobs found" state, so zero postings are recorded
as an **empty/unverified feed**, never as a successful zero-vacancy listing.

No TurboHire tenant, guessed ATS slug, parent board or aggregator is queried.
The adapter still implements the observed request and row parser: if the
company repairs the feed, it collects the rows it can see, marks them
unverified (the apply/detail route has not been reviewed), and stops rather
than inventing descriptions or pagination.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now, text_html


OFFICIAL_ENTRY = "https://www.pinelabs.com/careers"
LISTING_URL = "https://www.pinelabs.com/careers/open-jobs"
GATEWAY_URL = "https://www.pinelabs.com/api/gateway/turbo-hire"
LISTING_TITLE = "Pine Labs Careers | Explore Current Job Openings & Vacancies"
ENTRY_TITLE = "Careers at Pine Labs – Join Our Fintech Innovation Team"
PAGE_CHUNK_PATH = "/_next/static/chunks/app/(global)/careers/open-jobs/page-"
LISTING_BODY = {
    "Skip": 0,
    "Top": 50,
    "SortBy": "None",
    "Filters": {"Departments": [], "Locations": [], "JobTypes": [], "SearchString": ""},
    "GetFiltersData": True,
}
FILTER_KEYS = ("DepartmentData", "LocationData", "JobTypeData")
JOB_ROW_FIELDS = ("JobId", "JobTitle", "Department", "Location", "JobType", "ApplyUrl")
MAX_OBSERVED_PAGE_SIZE = LISTING_BODY["Top"]
# The observed listing exposes no detail route, so even a future non-empty feed
# cannot be certified until its ApplyUrl/detail route is inspected by a human.
DETAIL_ROUTE_REVIEWED = False


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _abs(response_url: str, href: str) -> str:
    return urljoin(response_url, (href or "").strip())


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Verify the ledger URL is still Pine Labs' own careers entry and links to its listing."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or (parts.hostname or "").lower() != "www.pinelabs.com"
            or parts.path.rstrip("/") != "/careers"):
        raise ValueError(f"Pine Labs official entry redirected to an unexpected URL: {response_url}")

    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != ENTRY_TITLE:
        raise ValueError(f"Pine Labs careers entry title changed: {title!r}")

    listing_links: list[str] = []
    for anchor in soup.select("a[href]"):
        absolute = _abs(response_url, anchor.get("href", ""))
        if absolute == LISTING_URL and absolute not in listing_links:
            listing_links.append(absolute)
    if not listing_links:
        raise ValueError("Pine Labs careers entry no longer links to /careers/open-jobs")

    return {
        "url": response_url,
        "title": title,
        "listing_links": listing_links,
        "listing_url": listing_links[0],
        "anchor_count": len(soup.select("a[href]")),
        "issues": [] if listing_links == [LISTING_URL] else ["multiple_or_unexpected_listing_links"],
    }


def parse_listing_page(html: str, response_url: str = LISTING_URL) -> dict:
    """Verify the exact Next.js listing page and record its listing machinery."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or (parts.hostname or "").lower() != "www.pinelabs.com"
            or parts.path.rstrip("/") != "/careers/open-jobs"):
        raise ValueError(f"Pine Labs listing redirected to an unexpected URL: {response_url}")

    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != LISTING_TITLE:
        raise ValueError(f"Pine Labs listing title changed: {title!r}")

    chunks = []
    for script in soup.select("script[src]"):
        src = _abs(response_url, script.get("src", ""))
        path = unquote(urlsplit(src).path)
        if path.startswith(PAGE_CHUNK_PATH):
            chunks.append(src)
    if not chunks:
        raise ValueError("Pine Labs listing no longer loads the observed open-jobs page chunk")
    if not any(normalized_title(h.get_text(" ", strip=True)).lower() == "open roles" for h in soup.select("h2")):
        raise ValueError("Pine Labs listing no longer renders its 'Open roles' heading")

    job_postings = 0
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text() or "{}")
        except (TypeError, ValueError):
            continue
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if item.get("@type") == "JobPosting":
                    job_postings += 1
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)

    return {
        "url": response_url,
        "title": title,
        "page_chunks": chunks,
        "open_roles_heading": True,
        "cms_slug_open_jobs": bool(re.search(r'\\?"slug\\?":\\?"open-jobs\\?"', html)),
        "job_posting_schema_count": job_postings,
        "html_bytes": len(html.encode()),
        "html_sha256": sha256(html.encode()).hexdigest(),
        "issues": [] if re.search(r'\\?"slug\\?":\\?"open-jobs\\?"', html) else ["open_jobs_cms_record_not_found"],
    }


def parse_gateway_payload(value, *, requested: dict | None = None) -> dict:
    """Validate the exact gateway JSON and return its counts, filters and rows."""
    if not isinstance(value, dict):
        raise ValueError("Pine Labs gateway response is not a JSON object")
    total = value.get("Total")
    filtered = value.get("FilteredCount")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError("Pine Labs gateway response is missing a valid Total")
    if not isinstance(filtered, int) or isinstance(filtered, bool) or filtered < 0:
        raise ValueError("Pine Labs gateway response is missing a valid FilteredCount")

    filters = value.get("FiltersData")
    if filters is not None:
        if not isinstance(filters, dict) or set(filters) != set(FILTER_KEYS):
            raise ValueError("Pine Labs gateway FiltersData schema changed")
        for key in FILTER_KEYS:
            rows = filters[key]
            if not isinstance(rows, list):
                raise ValueError(f"Pine Labs gateway FiltersData.{key} is not a list")
            if any(not isinstance(row, dict) or not isinstance(row.get("Name"), str) for row in rows):
                raise ValueError(f"Pine Labs gateway FiltersData.{key} contains an unidentified option")

    jobs = value.get("Jobs")
    if jobs is None:
        jobs = []
    if not isinstance(jobs, list):
        raise ValueError("Pine Labs gateway Jobs field is not a list")
    if any(not isinstance(row, dict) for row in jobs):
        raise ValueError("Pine Labs gateway Jobs contains a non-object row")
    if total == 0 and jobs:
        raise ValueError("Pine Labs gateway reports Total=0 but returns job rows")
    if total > 0 and not jobs:
        raise ValueError("Pine Labs gateway reports jobs but returns no rows")
    if len(jobs) > max(total, filtered):
        raise ValueError("Pine Labs gateway returns more rows than its reported counts")
    if len(jobs) > MAX_OBSERVED_PAGE_SIZE:
        raise ValueError("Pine Labs gateway returned more rows than the observed page size")

    body = requested or LISTING_BODY
    filters_in_request = body.get("Filters") or {}
    unfiltered = not any(filters_in_request.get(key) for key in ("Departments", "Locations", "JobTypes")) \
        and not (filters_in_request.get("SearchString") or "").strip()
    if unfiltered and filtered != total:
        raise ValueError("Pine Labs gateway filtered/total counts disagree for an unfiltered request")

    issues = []
    if total > len(jobs):
        issues.append("pinelabs_membership_exceeds_observed_page_size")
    if total == 0 and filters is not None and any(filters[key] for key in FILTER_KEYS):
        issues.append("pinelabs_zero_jobs_but_filters_present")
    job_ids = [str(row.get("JobId")) for row in jobs if isinstance(row, dict)]
    return {
        "total": total,
        "filtered_count": filtered,
        "job_ids": job_ids,
        "jobs": jobs,
        "filters": filters,
        "unfiltered_request": unfiltered,
        "issues": issues,
    }


def parse_location(value) -> tuple[list[str], str | None]:
    """Return source address strings and the card's displayed location, if known."""
    addresses: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return [], None
        try:
            value = json.loads(text)
        except ValueError:
            return [text], text
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                addresses.append(item.strip())
            elif isinstance(item, dict) and isinstance(item.get("Address"), str) and item["Address"].strip():
                addresses.append(item["Address"].strip())
    if not addresses:
        return [], None
    if len(addresses) == 1:
        return addresses, addresses[0]
    # The bundle's F() joins the address parts as "city, state, ... , country";
    # keep the raw addresses as the source-of-truth list and expose the simple join.
    return addresses, ", ".join(addresses)


def parse_job_row(row: dict, *, card_url: str | None = None) -> dict:
    """Validate one Jobs[] row using only fields the official page exposes."""
    if not isinstance(row, dict):
        raise ValueError("Pine Labs job row is not an object")
    identity = row.get("JobId")
    if isinstance(identity, bool) or identity is None or not re.fullmatch(r"\d+", str(identity)):
        raise ValueError("Pine Labs job row is missing a numeric JobId")
    title = normalized_title(row.get("JobTitle"))
    if not title:
        raise ValueError(f"Pine Labs job row {identity} is missing JobTitle")
    apply_url = row.get("ApplyUrl")
    if not isinstance(apply_url, str) or not apply_url.startswith(("https://", "http://")):
        raise ValueError(f"Pine Labs job row {identity} is missing a valid ApplyUrl")
    locations, location_text = parse_location(row.get("Location"))
    department = row.get("Department")
    if department is not None and not isinstance(department, str):
        raise ValueError(f"Pine Labs job row {identity} has a non-string Department")
    job_type = row.get("JobType")
    if job_type is not None and not isinstance(job_type, str):
        raise ValueError(f"Pine Labs job row {identity} has a non-string JobType")
    return {
        "id": str(identity),
        "title": title,
        "display_title": re.sub(r"\([^)]*\)", "", title).strip(),
        "url": card_url or apply_url,
        "apply_url": apply_url,
        "department": department,
        "locations": locations,
        "location_display": location_text,
        "employment_type": job_type,
        "raw": dict(row),
    }


class PineLabs(Adapter):
    """Read only Pine Labs' official careers page and its company-scoped gateway."""

    VERIFICATION_REVISION = "pine-labs.official-nextjs-turbo-hire-gateway.1"

    async def _entry(self) -> dict:
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(
            requested_url=OFFICIAL_ENTRY,
            final_url=str(response.url),
            status_code=response.status_code,
            seconds=seconds,
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
            redirects=[[item.status_code, str(item.url)] for item in response.history],
            fetched_at=now(),
        )
        return parsed

    async def _listing_page(self) -> dict:
        started = time.perf_counter()
        response = await self.http.get(LISTING_URL)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_listing_page(response.text, str(response.url))
        parsed.update(
            requested_url=LISTING_URL,
            final_url=str(response.url),
            status_code=response.status_code,
            seconds=seconds,
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
            redirects=[[item.status_code, str(item.url)] for item in response.history],
            fetched_at=now(),
        )
        return parsed

    async def _gateway(self) -> dict:
        started = time.perf_counter()
        response = await self.http.request(
            "POST", GATEWAY_URL, json=LISTING_BODY,
            headers={"Accept": "application/json", "Origin": "https://www.pinelabs.com", "Referer": LISTING_URL},
        )
        seconds = max(0.0, time.perf_counter() - started)
        parts = urlsplit(str(response.url))
        if ((parts.hostname or "").lower() not in {"www.pinelabs.com", "pinelabs.com"}
                or parts.path != "/api/gateway/turbo-hire"):
            raise ValueError(f"Pine Labs gateway redirected to an unexpected URL: {response.url}")
        parsed = parse_gateway_payload(response.json(), requested=LISTING_BODY)
        parsed.update(
            requested_url=GATEWAY_URL,
            final_url=str(response.url),
            status_code=response.status_code,
            seconds=seconds,
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
            request_body=LISTING_BODY,
            response_json=response.json(),
            fetched_at=now(),
        )
        return parsed

    def _make_job(self, row: dict):
        apply_url = row["apply_url"]
        job = self.job(
            {"listing_row": row["raw"], "apply_url": apply_url, "detail_route_reviewed": False},
            title=row["title"],
            url=row["url"],
            job_id=row["id"],
            requisition_id=None,
            apply_url=apply_url,
            description="",
            method="pine-labs.gateway.jobs-row (no detail route exposed on the official listing)",
            description_url=GATEWAY_URL,
            locations=row["locations"],
            department=row["department"],
            employment_type=row["employment_type"],
            workplace_type=None,
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            valid_through=None,
            salary=None,
            employer="Pine Labs",
            language="en",
        )
        job.description_text = text_html(job.description_html)
        job.warnings.append("detail_route_not_reviewed")
        job.raw["listing"] = {
            "source": "POST /api/gateway/turbo-hire",
            "row": row["raw"],
            "display_title": row["display_title"],
            "location_display": row["location_display"],
            "detail_route_reviewed": False,
        }
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_authority": (
                "The Next.js /careers/open-jobs client component published by Pine Labs; its only "
                "membership request is POST https://www.pinelabs.com/api/gateway/turbo-hire."
            ),
            "listing_membership_request": {
                "method": "POST",
                "url": GATEWAY_URL,
                "body": LISTING_BODY,
                "filters_applied": False,
                "scope": "Pine Labs host only; no ATS tenant, board or aggregator is guessed or queried.",
            },
            "detail_method": (
                "The current listing exposes no detail route or job ID. The bundle documents "
                "ApplyUrl as the card's apply link; its destination has not been reviewed, so no "
                "description is fabricated and no apply URL is fetched."
            ),
            "request_scope": "Official www.pinelabs.com careers entry, listing page and gateway only.",
        })
        try:
            entry = await self._entry()
            listing = await self._listing_page()
            initial = await self._gateway()
        except Exception as exc:
            self.result.errors.append(f"pinelabs_source_request_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["initial_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.timings["source_collection_seconds"] = self.result.timings["initial_collection_seconds"]
            self.result.metadata["source_collection_wall_seconds"] = self.result.timings["source_collection_seconds"]
            return self.result

        initial_collection_seconds = max(0.0, time.perf_counter() - source_started)
        self.result.timings["initial_collection_seconds"] = initial_collection_seconds

        self.result.metadata["initial_entry"] = {k: v for k, v in entry.items() if k != "listing_links"}
        self.result.metadata["initial_entry_listing_links"] = entry["listing_links"]
        self.result.metadata["initial_listing_page"] = listing
        self.result.metadata["initial_gateway"] = initial
        self.result.expected_total = initial["total"]
        self.result.metadata["advertised_jobs"] = initial["total"]
        self.result.metadata["filter_options"] = initial["filters"]
        if entry["issues"]:
            self.result.errors.extend(entry["issues"])
        if listing["issues"]:
            self.result.errors.extend(listing["issues"])
        if entry["listing_url"] != LISTING_URL:
            self.result.errors.append("pinelabs_entry_does_not_link_to_the_observed_listing")

        jobs = []
        if initial["total"] > 0:
            for raw_row in initial["jobs"]:
                try:
                    row = parse_job_row(raw_row)
                except ValueError as exc:
                    self.result.errors.append(f"pinelabs_job_row_invalid: {exc}")
                    continue
                job = self._make_job(row)
                jobs.append(job)
                self.add(job)
            self.result.errors.append("pinelabs_detail_route_not_reviewed_for_listing_rows")
            if len(jobs) != initial["filtered_count"]:
                self.result.errors.append("pinelabs_retained_rows_do_not_match_filtered_count")
        else:
            self.result.errors.append("pinelabs_official_listing_reports_zero_jobs")
            self.result.errors.append("pinelabs_zero_jobs_not_certified_as_zero_vacancies")

        final_started = time.perf_counter()
        try:
            final_entry = await self._entry()
            final_listing = await self._listing_page()
            final = await self._gateway()
        except Exception as exc:
            self.result.errors.append(f"pinelabs_final_recheck_failed: {type(exc).__name__}: {exc}")
            final_entry = final_listing = final = None
        finally:
            self.result.timings["final_recheck_seconds"] = max(0.0, time.perf_counter() - final_started)

        complete = False
        if final is not None:
            self.result.metadata["final_entry"] = {k: v for k, v in final_entry.items() if k != "listing_links"}
            self.result.metadata["final_listing_page"] = final_listing
            self.result.metadata["final_gateway"] = final
            same_entry = (entry["response_sha256"] == final_entry["response_sha256"]
                          and entry["listing_links"] == final_entry["listing_links"])
            same_listing = (listing["response_sha256"] == final_listing["response_sha256"]
                            and listing["page_chunks"] == final_listing["page_chunks"]
                            and listing["title"] == final_listing["title"])
            same_gateway = (
                initial["total"] == final["total"]
                and initial["filtered_count"] == final["filtered_count"]
                and initial["job_ids"] == final["job_ids"]
                and initial["filters"] == final["filters"]
                and initial["response_sha256"] == final["response_sha256"]
            )
            changed_rows = []
            if initial["jobs"] and final["jobs"]:
                initial_by_id = {str(row["JobId"]): row for row in initial["jobs"]}
                final_by_id = {str(row["JobId"]): row for row in final["jobs"]}
                for identity in initial_by_id:
                    if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]:
                        changed_rows.append(identity)
            same_fields = not changed_rows
            complete = bool(
                same_entry and same_listing and same_gateway and same_fields
                and initial["total"] > 0
                and not initial["issues"] and not final["issues"]
                and not listing["issues"] and not entry["issues"]
                and len(jobs) == initial["total"]
                and DETAIL_ROUTE_REVIEWED
            )
            self.result.metadata["membership_reconciliation"] = {
                "initial_total": initial["total"],
                "final_total": final["total"],
                "initial_filtered_count": initial["filtered_count"],
                "final_filtered_count": final["filtered_count"],
                "initial_job_ids": initial["job_ids"],
                "final_job_ids": final["job_ids"],
                "same_ids_and_order": initial["job_ids"] == final["job_ids"],
                "same_filters": initial["filters"] == final["filters"],
                "same_gateway_sha256": initial["response_sha256"] == final["response_sha256"],
                "same_entry_page_sha256": entry["response_sha256"] == final_entry["response_sha256"],
                "same_listing_page_sha256": listing["response_sha256"] == final_listing["response_sha256"],
                "changed_row_fields_by_job_id": changed_rows,
                "complete": complete,
            }
            if not same_entry:
                self.result.errors.append("pinelabs_entry_page_changed_during_collection")
            if not same_listing:
                self.result.errors.append("pinelabs_listing_page_changed_during_collection")
            if not same_gateway:
                self.result.errors.append("pinelabs_gateway_payload_changed_during_collection")
            if not same_fields:
                self.result.errors.append("pinelabs_source_row_fields_changed_during_collection")
            if initial["issues"] or final["issues"]:
                self.result.errors.extend(initial["issues"])
                self.result.errors.extend(final["issues"])
        else:
            complete = False

        if initial["total"] == 0:
            self.result.metadata["source_health"] = {
                "status": "empty_feed_unverified",
                "gateway_total": initial["total"],
                "gateway_filtered_count": initial["filtered_count"],
                "gateway_has_jobs_member": "Jobs" in initial["response_json"],
                "filter_options_empty": all(not (initial["filters"] or {}).get(key) for key in FILTER_KEYS),
                "visible_job_ids": [],
                "detail_routes_exposed": 0,
                "note": (
                    "The official gateway answered Total=0 with empty filter data and the hydrated "
                    "listing showed 'No jobs found matching your criteria.' This is recorded as an "
                    "empty/unverified feed, not as a certified zero-vacancy membership. No ATS "
                    "tenant, guessed slug, parent board or aggregator was substituted."
                ),
            }
        self.result.listing_complete = complete
        self.result.metadata["detail_hydration_mode"] = (
            "No detail fetch: the current listing exposes no job ID or detail URL. "
            "A future non-empty row would remain unverified until its ApplyUrl route is reviewed."
        )
        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.metadata["current_native_membership_reconciled"] = complete
        recheck_seconds = self.result.timings.get("final_recheck_seconds", 0.0)
        self.result.timings["source_collection_seconds"] = initial_collection_seconds + recheck_seconds
        self.result.metadata["source_collection_wall_seconds"] = self.result.timings["source_collection_seconds"]
        return self.result
