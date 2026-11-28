"""Swiggy's own careers SPA feed -> the exact SWIGGY SmartRecruiters postings.

The official entry redirected from ``careers.swiggy.com`` to
``careers.swiggy.in``, whose **Explore Open Roles** page reads one company
feed, ``/api/jobs.json``. That feed publishes the complete company-scoped
listing (190 roles at review time), 50 rows per client-side page. Every row
links to one exact ``jobs.smartrecruiters.com/SWIGGY/<id>`` posting; the wider
SmartRecruiters board is never enumerated.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html import escape
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://careers.swiggy.com/"
SITE_ORIGIN = "https://careers.swiggy.in"
OFFICIAL_FINAL = SITE_ORIGIN + "/"
LISTING_URL = SITE_ORIGIN + "/explore-jobs"
FEED_URL = SITE_ORIGIN + "/api/jobs.json"
BOARD = "SWIGGY"
POSTING_API = "https://api.smartrecruiters.com/v1/companies/SWIGGY/postings/{posting_id}"
POSTING_PAGE = "https://jobs.smartrecruiters.com/SWIGGY/"
DESCRIPTION_METHOD = "swiggy.company-feed.linked-smartrecruiters.sections"
SECTION_ORDER = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
# Observed client-side page size; the adapter fetches the one feed response, not UI pages.
UI_PAGE_SIZE = 50
JOB_PATH = re.compile(r"^/([A-Za-z0-9]+)/(\d+)(?:-[^/]*)?$")
BUNDLE_MARKERS = (
    "/api/jobs.json",
    "Explore Open Roles",
    "ej-table",
    "Filter by location",
    "Filter by business unit",
    "All locations",
    "All teams",
    "Previous",
    "Next",
    "Search by title, e.g. Engineer",
    "No roles match your search.",
    "We couldn't load our open roles just now.",
)


def parse_iso_date(value):
    """Normalise a source ISO timestamp; never substitute a request time."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    stamp = stamp.astimezone(timezone.utc)
    timespec = "seconds" if not stamp.microsecond else (
        "milliseconds" if stamp.microsecond % 1000 == 0 else "microseconds")
    return stamp.isoformat(timespec=timespec).replace("+00:00", "Z")


def parse_epoch_ms(value):
    """Normalise the feed's epoch-millisecond release date as the same instant."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    try:
        stamp = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    timespec = "seconds" if not stamp.microsecond else (
        "milliseconds" if stamp.microsecond % 1000 == 0 else "microseconds")
    return stamp.isoformat(timespec=timespec).replace("+00:00", "Z")


def parse_posting_url(url: str) -> tuple[str, str]:
    """Return ``(board, numeric posting id)`` from one exact company feed link."""
    parts = urlsplit(url or "")
    if parts.scheme != "https" or parts.hostname != "jobs.smartrecruiters.com":
        raise ValueError(f"Swiggy feed no longer links to jobs.smartrecruiters.com: {url!r}")
    match = JOB_PATH.match(parts.path)
    if match is None:
        raise ValueError(f"Swiggy feed URL is not a SmartRecruiters posting: {url!r}")
    board, posting_id = match.groups()
    if board != BOARD:
        raise ValueError(f"Swiggy feed URL references unexpected board {board!r}")
    return board, posting_id


def parse_official_entry(html: str, *, requested_url: str, response_url: str,
                         redirects: list | None = None) -> dict:
    """Validate the reviewed ``careers.swiggy.com`` -> ``careers.swiggy.in`` entry."""
    requested = urlsplit(requested_url)
    final = urlsplit(response_url)
    if requested.scheme != "https" or requested.hostname != "careers.swiggy.com":
        raise ValueError("Swiggy official entry is no longer the reviewed ledger URL")
    if (final.scheme != "https" or final.hostname != "careers.swiggy.in"
            or final.path not in ("", "/")):
        raise ValueError("Swiggy official entry redirected away from careers.swiggy.in")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Swiggy Careers" not in title:
        raise ValueError("Swiggy official entry no longer has the reviewed careers title")
    root = soup.select_one("#root")
    script = soup.select_one('script[type="module"][src^="/assets/index-"]')
    if root is None or script is None:
        raise ValueError("Swiggy official entry is no longer the reviewed SPA shell")
    chain = [{"status": row.get("status"), "url": row.get("url")} for row in (redirects or [])]
    return {
        "requested_url": requested_url,
        "final_url": response_url,
        "title": title,
        "redirect_chain": chain,
        "root_element": "root",
        "bundle_src": urljoin(response_url, script.get("src", "")),
    }


def parse_listing_shell(html: str, response_url: str) -> dict:
    """Validate the company-owned Explore Open Roles route and its one bundle."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname != "careers.swiggy.in"
            or parts.path != "/explore-jobs"):
        raise ValueError("Swiggy Explore Open Roles URL changed or redirected away")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    root = soup.select_one("#root")
    script = soup.select_one('script[type="module"][src^="/assets/index-"]')
    if "Swiggy Careers" not in title or root is None or script is None:
        raise ValueError("Swiggy Explore Open Roles shell changed")
    return {
        "url": response_url,
        "title": title,
        "root_element": "root",
        "bundle_url": urljoin(response_url, script.get("src", "")),
    }


def parse_bundle(text: str, response_url: str) -> dict:
    """Check that the shipped UI still reads the reviewed feed and renders this table."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.hostname != "careers.swiggy.in" or not parts.path.startswith("/assets/"):
        raise ValueError("Swiggy listing bundle redirected away from its own asset route")
    missing = [marker for marker in BUNDLE_MARKERS if marker not in text]
    if missing:
        raise ValueError(f"Swiggy listing bundle lost reviewed UI markers: {missing}")
    return {
        "url": response_url,
        "bytes": len(text.encode()),
        "markers_present": list(BUNDLE_MARKERS),
        "feed_request_literal": "/api/jobs.json",
        "observed_ui_page_size": UI_PAGE_SIZE,
    }


def parse_feed(data: dict) -> tuple[list[dict], dict]:
    """Return the complete unfiltered company feed membership and diagnostics."""
    if not isinstance(data, dict):
        raise ValueError("Swiggy jobs feed is not an object")
    rows = data.get("jobs")
    total = data.get("total")
    generated = parse_iso_date(data.get("generatedAt"))
    if not isinstance(rows, list) or not isinstance(total, int) or isinstance(total, bool):
        raise ValueError("Swiggy jobs feed lost its jobs array or integer total")
    if total != len(rows):
        raise ValueError(f"Swiggy feed total {total!r} does not match {len(rows)} rows")
    if generated is None:
        raise ValueError("Swiggy feed generatedAt is missing or not an ISO timestamp")

    members, seen = [], set()
    remote = hybrid = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"Swiggy feed row {index} is not an object")
        title = row.get("t")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"Swiggy feed row {index} has no title")
        for key in ("c", "l", "b"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"Swiggy feed row {index} has no {key!r} value")
        if not isinstance(row.get("e"), str):
            raise ValueError(f"Swiggy feed row {index} has no string employment label")
        if not isinstance(row.get("d"), int) or isinstance(row.get("d"), bool) or parse_epoch_ms(row["d"]) is None:
            raise ValueError(f"Swiggy feed row {index} has no valid release timestamp")
        for key in ("r", "h"):
            if row.get(key) not in (0, 1):
                raise ValueError(f"Swiggy feed row {index} has an unexpected {key!r} flag")
        _, posting_id = parse_posting_url(row.get("u"))
        if posting_id in seen:
            raise ValueError(f"Swiggy feed repeats posting ID {posting_id}")
        seen.add(posting_id)
        remote += row["r"]
        hybrid += row["h"]
        members.append(row)

    dates = [row["d"] for row in members]
    diagnostics = {
        "listing_url": LISTING_URL,
        "feed_url": FEED_URL,
        "feed_generated_at": generated,
        "feed_total": total,
        "feed_rows": len(rows),
        "unique_posting_ids": len(seen),
        "first_row": {
            "id": parse_posting_url(members[0]["u"])[1],
            "title": members[0]["t"],
            "date_ms": members[0]["d"],
        } if members else None,
        "last_row": {
            "id": parse_posting_url(members[-1]["u"])[1],
            "title": members[-1]["t"],
            "date_ms": members[-1]["d"],
        } if members else None,
        "remote_rows": remote,
        "hybrid_rows": hybrid,
        "employment_labels": _counts(members, "e"),
        "business_units": _counts(members, "b"),
        "locations": len({row["c"] for row in members}),
        "release_date_min": parse_epoch_ms(min(dates)) if dates else None,
        "release_date_max": parse_epoch_ms(max(dates)) if dates else None,
        "feed_order_is_release_date_desc": all(
            dates[i] >= dates[i + 1] for i in range(len(dates) - 1)),
        "ui_page_size": UI_PAGE_SIZE,
        "ui_pages": (len(members) + UI_PAGE_SIZE - 1) // UI_PAGE_SIZE,
        "filters_applied": False,
    }
    return members, diagnostics


def _counts(rows, key):
    counts = {}
    for row in rows:
        value = row.get(key) or ""
        counts[value] = counts.get(value, 0) + 1
    return counts


def section_records(job_ad) -> list[dict]:
    """Return non-empty employer-authored sections in the published order."""
    sections = (job_ad or {}).get("sections") if isinstance(job_ad, dict) else None
    if not isinstance(sections, dict):
        raise ValueError("SmartRecruiters posting lost jobAd.sections")
    order = list(SECTION_ORDER) + [key for key in sections if key not in SECTION_ORDER]
    records = []
    for key in order:
        section = sections.get(key)
        if not isinstance(section, dict):
            continue
        body = section.get("text") or ""
        if not text_html(body):
            continue
        records.append({
            "key": key,
            "title": str(section.get("title") or key).strip(),
            "html": body,
            "text": text_html(body),
        })
    return records


def sections_html(records: list[dict]) -> str:
    return "\n".join(
        f"<h2>{escape(record['title'])}</h2>{record['html']}" for record in records)


def _custom_field(row, label):
    for field in row.get("customField") or []:
        if isinstance(field, dict) and field.get("fieldLabel") == label:
            return field.get("valueLabel")
    return None


def _custom_fields(row):
    return {
        field.get("fieldLabel"): field.get("valueLabel")
        for field in row.get("customField") or []
        if isinstance(field, dict) and field.get("fieldLabel")
    }


def detail_fields(row: dict, feed_row: dict, *, expected_id: str) -> dict:
    """Map one exact SWIGGY SmartRecruiters posting, including source-empty ads."""
    if not isinstance(row, dict):
        raise ValueError("SmartRecruiters posting response is not an object")
    posting_id = str(row.get("id") or "")
    if not posting_id.isdigit():
        raise ValueError("SmartRecruiters posting has no numeric id")
    if posting_id != str(expected_id):
        raise ValueError(f"SmartRecruiters returned posting {posting_id}, expected {expected_id}")
    company = row.get("company") or {}
    if company.get("identifier") != BOARD:
        raise ValueError(f"SmartRecruiters posting {posting_id} belongs to {company.get('identifier')!r}")
    if row.get("visibility") != "PUBLIC" or row.get("active") is not True:
        raise ValueError(f"SmartRecruiters posting {posting_id} is no longer public and active")
    title = str(row.get("name") or "").strip()
    if not title:
        raise ValueError(f"SmartRecruiters posting {posting_id} has no title")
    rows_title = str(feed_row.get("t") or "").strip()
    seen_date = parse_iso_date(row.get("releasedDate"))
    feed_date = parse_epoch_ms(feed_row.get("d"))
    if seen_date is None:
        raise ValueError(f"SmartRecruiters posting {posting_id} has no valid releasedDate")

    records = section_records(row.get("jobAd"))
    html = sections_html(records)
    description = text_html(html)
    unavailable = not bool(description.strip())
    custom = _custom_fields(row)
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    legal_entity = custom.get("LegalEntity")
    department = custom.get("Department.") or custom.get("Department") or (
        (row.get("function") or {}).get("label"))
    employment = custom.get("Employment Type") or (
        (row.get("typeOfEmployment") or {}).get("label"))
    workplace = "Remote" if location.get("remote") else "Hybrid" if location.get("hybrid") else None
    feed_remote = bool(feed_row.get("r"))
    feed_hybrid = bool(feed_row.get("h"))
    warnings = []
    if unavailable:
        warnings.append("source_description_missing")
    if rows_title and rows_title != title:
        warnings.append("feed_title_differs_from_posting_title")
    if feed_date != seen_date:
        warnings.append("feed_release_date_differs_from_posting_date")
    if feed_remote != bool(location.get("remote")):
        warnings.append("feed_remote_flag_differs_from_posting")
    if feed_hybrid != bool(location.get("hybrid")):
        warnings.append("feed_hybrid_flag_differs_from_posting")
    if not custom.get("BusinessUnit") or custom["BusinessUnit"] != feed_row.get("b"):
        warnings.append("feed_business_unit_differs_from_posting")

    locations = []
    if isinstance(feed_row.get("l"), str) and feed_row["l"].strip():
        locations.append(feed_row["l"].strip())
    full_location = location.get("fullLocation")
    if isinstance(full_location, str) and full_location.strip() and full_location not in locations:
        locations.append(full_location.strip())

    apply_url = row.get("applyUrl") or row.get("postingUrl")
    if apply_url:
        _, apply_id = parse_posting_url(apply_url)
        if apply_id != posting_id:
            raise ValueError(f"SmartRecruiters apply URL for {posting_id} targets {apply_id}")

    return {
        "title": title,
        "job_id": posting_id,
        "requisition_id": row.get("refNumber") or None,
        "apply_url": apply_url,
        "description_html": html,
        "description_text": description,
        "description_method": DESCRIPTION_METHOD,
        "description_source_url": POSTING_API.format(posting_id=posting_id),
        "description_unavailable": unavailable,
        "qualifications": next((record["text"] for record in records
                                if record["key"] == "qualifications"), None),
        "responsibilities": next((record["text"] for record in records
                                  if record["key"] == "jobDescription"), None),
        "locations": locations,
        "department": department,
        "team": custom.get("Team"),
        "employment_type": employment,
        "workplace_type": workplace,
        "employer": legal_entity or company.get("name"),
        "experience": (row.get("experienceLevel") or {}).get("label"),
        "language": (row.get("language") or {}).get("label"),
        "date_posted": seen_date,
        "date_posted_raw": row.get("releasedDate"),
        "date_updated": None,
        "valid_through": None,
        "salary": None,
        "warnings": warnings,
        "sections": records,
        "custom_fields": custom,
        "business_unit": custom.get("BusinessUnit") or feed_row.get("b"),
        "posting_url": row.get("postingUrl"),
        "feed_date_iso": feed_date,
        "feed_date_matches_release_date": feed_date == seen_date,
        "posting_location": location,
    }


class Swiggy(Adapter):
    """Read only Swiggy's own feed and the exact SmartRecruiters IDs it links."""

    VERIFICATION_REVISION = "swiggy.company-feed.exact-smartrecruiters.1"

    def _make_job(self, row: dict, index: int, feed_generated_at: str):
        _, posting_id = parse_posting_url(row["u"])
        job = self.job(
            {"listing": dict(row), "feed_index": index, "feed_generated_at": feed_generated_at},
            title=str(row["t"]).strip(),
            url=row["u"],
            job_id=posting_id,
            apply_url=row["u"],
            locations=[row["l"].strip()],
            department=row.get("b"),
            employment_type=row.get("e") or None,
            workplace_type="Remote" if row.get("r") else "Hybrid" if row.get("h") else None,
            date_posted=parse_epoch_ms(row.get("d")),
            date_posted_raw=row.get("d"),
        )
        job.raw["feed_index"] = index
        job.raw["feed_generated_at"] = feed_generated_at
        return job

    async def _feed_snapshot(self):
        started = time.perf_counter()
        response = await self.http.request("GET", FEED_URL, headers={"Accept": "application/json"})
        seconds = max(0.0, time.perf_counter() - started)
        parts = urlsplit(str(response.url))
        if parts.scheme != "https" or parts.hostname != "careers.swiggy.in" or parts.path != "/api/jobs.json":
            raise ValueError("Swiggy jobs feed redirected away from its reviewed company route")
        members, diagnostics = parse_feed(response.json())
        diagnostics.update({
            "status": response.status_code,
            "response_url": str(response.url),
            "request_seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
        })
        return members, diagnostics

    async def _detail(self, job):
        api = POSTING_API.format(posting_id=job.job_id)
        try:
            response = await self.http.request("GET", api, headers={"Accept": "application/json"})
            parts = urlsplit(str(response.url))
            expected = urlsplit(api)
            if (parts.scheme, parts.hostname, parts.path) != (expected.scheme, expected.hostname, expected.path):
                raise ValueError(f"Swiggy posting {job.job_id} detail redirected away from its exact API route")
            row = response.json()
            fields = detail_fields(row, job.raw["listing"], expected_id=str(job.job_id))
        except Exception as exc:
            job.raw["detail_error"] = f"{type(exc).__name__}: {exc}"
            raise
        job.raw["detail"] = row
        job.raw["detail_source"] = {
            "url": api,
            "posting_api_status": response.status_code,
            "response_bytes": len(response.content),
        }
        job.raw["detail_sections"] = fields.pop("sections")
        job.raw["posting_location"] = fields.pop("posting_location")
        job.raw["custom_fields"] = fields.pop("custom_fields")
        job.raw["business_unit"] = fields.pop("business_unit")
        job.raw["posting_url"] = fields.pop("posting_url")
        job.raw["feed_date_iso"] = fields.pop("feed_date_iso")
        job.raw["feed_date_matches_release_date"] = fields.pop("feed_date_matches_release_date")
        for key, value in fields.items():
            if key == "warnings":
                job.warnings.extend(value)
            else:
                setattr(job, key, value)
        return job

    def _reconcile_rows(self, initial_rows: list[dict], final_rows: list[dict]) -> dict:
        initial_by_id = {parse_posting_url(row["u"])[1]: row for row in initial_rows}
        final_by_id = {parse_posting_url(row["u"])[1]: row for row in final_rows}
        changed = {}
        for posting_id in initial_by_id.keys() & final_by_id.keys():
            if initial_by_id[posting_id] != final_by_id[posting_id]:
                changed[posting_id] = sorted(
                    key for key in set(initial_by_id[posting_id]) | set(final_by_id[posting_id])
                    if initial_by_id[posting_id].get(key) != final_by_id[posting_id].get(key)
                )
        return {
            "initial_ids": list(initial_by_id),
            "final_ids": list(final_by_id),
            "added_ids": [i for i in final_by_id if i not in initial_by_id],
            "removed_ids": [i for i in initial_by_id if i not in final_by_id],
            "same_ids_and_order": list(initial_by_id) == list(final_by_id),
            "changed_fields_by_id": changed,
        }

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_final_url": OFFICIAL_FINAL,
            "listing_url": LISTING_URL,
            "feed_url": FEED_URL,
            "board": BOARD,
            "listing_authority": "Swiggy's own /api/jobs.json feed, the single request used by its Explore Open Roles SPA.",
            "detail_method": "Exact-ID SmartRecruiters posting API for every feed link; the board is not enumerated.",
            "request_scope": "Only the 190 exact SWIGGY posting IDs published by Swiggy's own feed.",
        })
        listing_started = time.perf_counter()
        try:
            entry_response = await self.http.request("GET", OFFICIAL_ENTRY)
            requested_entry = (entry_response.history[0].request.url
                               if entry_response.history else entry_response.request.url)
            entry = parse_official_entry(
                entry_response.text,
                requested_url=str(requested_entry),
                response_url=str(entry_response.url),
                redirects=[{"status": r.status_code, "url": str(r.url)} for r in entry_response.history],
            )
            shell_response = await self.http.request("GET", LISTING_URL)
            shell = parse_listing_shell(shell_response.text, str(shell_response.url))
            bundle_response = await self.http.request("GET", shell["bundle_url"])
            bundle = parse_bundle(bundle_response.text, str(bundle_response.url))
            initial_rows, initial_feed = await self._feed_snapshot()
        except Exception as exc:
            self.result.errors.append(f"Swiggy source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.timings["listing_phase_seconds"] = max(
            0.0, time.perf_counter() - listing_started)
        self.result.metadata["official_entry"] = entry
        self.result.metadata["listing_shell"] = shell
        self.result.metadata["listing_bundle"] = bundle
        self.result.metadata["feed_initial"] = initial_feed
        self.result.expected_total = initial_feed["feed_total"]
        self.result.pages = 1

        feed_generated = initial_feed["feed_generated_at"]
        jobs = [self._make_job(row, index, feed_generated) for index, row in enumerate(initial_rows)]
        for job in jobs:
            self.add(job)
        self.result.jobs = jobs

        self.result.jobs = await self.hydrate(jobs, self._detail)
        reconcile_started = time.perf_counter()
        final_rows, final_feed = None, None
        reconciliation = None
        rounds = 0
        while rounds < 2:
            rounds += 1
            try:
                current_rows, current_feed = await self._feed_snapshot()
            except Exception as exc:
                self.result.errors.append(f"Swiggy final feed recheck failed: {type(exc).__name__}: {exc}")
                final_rows, final_feed = None, None
                reconciliation = None
                break
            if final_rows is None:
                final_rows, final_feed = current_rows, current_feed
            else:
                final_rows, final_feed = current_rows, current_feed
            value = self._reconcile_rows(initial_rows, final_rows)
            reconciliation = value
            if not value["added_ids"] and not value["removed_ids"]:
                break
            # Membership changed while details were collected. Extend/drop and recheck once more.
            current_by_id = {parse_posting_url(row["u"])[1]: row for row in final_rows}
            keep = set(value["final_ids"])
            self.result.jobs = [job for job in self.result.jobs if job.job_id in keep]
            added_rows = [current_by_id[posting_id] for posting_id in value["added_ids"]]
            if added_rows:
                added_jobs = [self._make_job(row, initial_feed["feed_total"] + index, feed_generated)
                              for index, row in enumerate(added_rows)]
                for job in added_jobs:
                    self.add(job)
                await self.hydrate(added_jobs, self._detail)

        self.result.timings["membership_reconciliation_seconds"] = max(
            0.0, time.perf_counter() - reconcile_started)
        self.result.metadata["feed_final"] = final_feed
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.metadata["reconciliation_rounds"] = rounds
        if final_feed is None or reconciliation is None:
            self.result.errors.append("swiggy_membership_not_reconciled")
            self.result.listing_complete = False
        else:
            self.result.expected_total = final_feed["feed_total"]
            self.result.metadata["ui_page_size"] = final_feed.get("ui_page_size")
            self.result.metadata["ui_pages"] = final_feed.get("ui_pages")
            by_id = {job.job_id: job for job in self.result.jobs}
            final_ids = reconciliation["final_ids"]
            self.result.jobs = [by_id[posting_id] for posting_id in final_ids if posting_id in by_id]
            if reconciliation["added_ids"] or reconciliation["removed_ids"]:
                self.result.errors.append("swiggy_membership_changed_during_collection")
            if len(self.result.jobs) != final_feed["feed_total"]:
                self.result.errors.append("swiggy_retained_rows_do_not_match_company_feed")
            self.result.listing_complete = (
                len(self.result.jobs) == final_feed["feed_total"]
                and not any("membership" in error for error in self.result.errors)
            )

        sparse = [job.job_id for job in self.result.jobs
                  if job.description_html and len(job.raw.get("detail_sections") or []) < len(SECTION_ORDER)]
        empty = [
            {"id": job.job_id, "url": job.url, "title": job.title,
             "reason": "The official SWIGGY SmartRecruiters posting publishes empty "
                       "Company Description/Job Description/Qualifications/Additional Information sections."}
            for job in self.result.jobs if job.description_unavailable
        ]
        failed = [
            {"id": job.job_id, "url": job.url, "title": job.title,
             "reason": job.raw.get("detail_error") or "detail request failed"}
            for job in self.result.jobs if "detail_fetch_failed" in job.warnings or job.raw.get("detail_error")
        ]
        if sparse:
            self.result.metadata["sparse_source_descriptions"] = {"count": len(sparse), "job_ids": sparse}
            self.result.warnings.append("source_description_sections_sparse")
        if empty:
            self.result.metadata["empty_source_descriptions"] = {"count": len(empty), "jobs": empty}
            self.result.warnings.append("source_descriptions_empty")
            self.result.errors.append(f"empty_source_descriptions:{len(empty)}")
        if failed:
            self.result.metadata["failed_detail_jobs"] = {"count": len(failed), "jobs": failed}
        self.result.metadata["job_ids"] = [job.job_id for job in self.result.jobs]
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started)
        return self.result
