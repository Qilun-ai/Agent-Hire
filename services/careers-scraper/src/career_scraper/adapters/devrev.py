"""DevRev's official careers journey.

Observed journey (see companies/devrev/research/):
- The company career entry point is https://devrev.ai/careers. The native
  company detail routes are https://devrev.ai/job/<greenhouse-job-id>,
  titled "DevRev Careers".
- The company's hosted careers listing
  https://devrev.ai/positions/4301528004 ("Current openings at DevRev")
  links every role to the Greenhouse board
  https://job-boards.greenhouse.io/devrev/jobs/<id> — the same public
  board exposed by boards-api.greenhouse.io. This adapter therefore uses
  the exact board the official journey publishes; it performs no ATS-wide
  enumeration and touches no other board, slug or guessed tenant.
- A request to the native devrev.ai origin from this network is answered
  by a Vercel Security Checkpoint with HTTP 429 and no job data. That
  access denial is recorded as evidence; it is never worked around with
  challenge bypass or alternative hosts.

Membership is the full unfiltered board listing (initial + final
snapshots reconciled). Each stored job retains the full source board row,
the native company job URL, the exact Greenhouse job/apply URL, source
first_published/updated_at dates, offices/departments and the full
description body. Dates are source-only; unreachable or unknown dates stay
null.
"""
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

BOARD_API = "https://boards-api.greenhouse.io/v1/boards/devrev/jobs?content=true"
BOARD_DETAIL_BASE = "https://boards-api.greenhouse.io/v1/boards/devrev/jobs"
JOB_PAGE_BASE = "https://job-boards.greenhouse.io/devrev/jobs"
NATIVE_JOB_BASE = "https://devrev.ai/job"
EXPECTED_BOARD = "devrev"
GREENHOUSE_ID = re.compile(r"^\d{7,13}004$")


def native_job_url(identity):
    """Company's own published detail route for this exact job ID."""
    if not GREENHOUSE_ID.fullmatch(str(identity)):
        raise ValueError(f"DevRev official listing job ID is not a Greenhouse ID: {identity!r}")
    return f"{NATIVE_JOB_BASE}/{identity}"


def job_page_url(identity):
    if not GREENHOUSE_ID.fullmatch(str(identity)):
        raise ValueError(f"DevRev official listing job ID is not a Greenhouse ID: {identity!r}")
    return f"{JOB_PAGE_BASE}/{identity}"


def board_detail_url(identity):
    if not GREENHOUSE_ID.fullmatch(str(identity)):
        raise ValueError(f"DevRev official listing job ID is not a Greenhouse ID: {identity!r}")
    return f"{BOARD_DETAIL_BASE}/{identity}"


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _validate_board_url(url, label):
    parsed = urlsplit(str(url))
    if (parsed.scheme != "https" or parsed.hostname != "boards-api.greenhouse.io"
            or parsed.path != "/v1/boards/devrev/jobs" or parsed.query != "content=true"):
        raise ValueError(f"DevRev {label} source left the inspected company-published board: {url}")


def _validate_row(row, snapshot_name):
    if not isinstance(row, dict):
        raise ValueError(f"DevRev {snapshot_name} listing row is not an object")
    identity = row.get("id")
    if not isinstance(identity, int) or not GREENHOUSE_ID.fullmatch(str(identity)):
        raise ValueError(f"DevRev {snapshot_name} listing contains a missing or invalid exact job ID")
    title = row.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"DevRev {snapshot_name} listing contains an unidentified job (ID {identity})")
    expected = job_page_url(identity)
    if row.get("absolute_url") != expected:
        raise ValueError(f"DevRev {identity} absolute_url differs from the company-published job page: {row.get('absolute_url')}")
    if row.get("company_name") not in ("DevRev", None):
        raise ValueError(f"DevRev {identity} company_name differs from the published employer: {row.get('company_name')}")
    content = row.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"DevRev {identity} published board row has no full description content")
    if len(text_html(content).strip()) < 100:
        raise ValueError(f"DevRev {identity} published description is implausibly short")


class DevRev(Adapter):
    """Follow DevRev's official entry evidence and its published board only."""

    VERIFICATION_REVISION = "devrev.official-entry-and-company-published-greenhouse-board.1"
    REPRESENTATIVE_DETAIL_IDS = ("5578063004", "5722574004")

    async def _entry_probe(self, entry_url):
        """Record the native company entry's reachable state (evidence only)."""
        try:
            response = await self.http.get(entry_url)
            return {
                "attempted": True,
                "status": int(response.status_code),
                "final_url": str(response.url),
                "waf_or_checkpoint_blocked": False,
            }
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            blocked = "429" in message or "security checkpoint" in message.lower() or "vercel" in message.lower()
            return {
                "attempted": True,
                "status": 429 if blocked else None,
                "final_url": entry_url,
                "error": message,
                "waf_or_checkpoint_blocked": bool(blocked),
            }

    async def _board_snapshot(self, url, label):
        response = await self.http.get(url)
        if str(response.url) != url:
            raise ValueError(f"DevRev {label} listing request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"DevRev {label} listing response is not JSON") from exc
        rows = self._rows(payload, label)
        return payload, rows

    @staticmethod
    def _rows(payload, snapshot_name):
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError(f"DevRev {snapshot_name} published listing response has no jobs array")
        rows = payload["jobs"]
        ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError(f"DevRev {snapshot_name} listing repeats a company-published ID")
        for row in rows:
            _validate_row(row, snapshot_name)
        meta = payload.get("meta")
        total = meta.get("total") if isinstance(meta, dict) else None
        if total is not None and total != len(rows):
            raise ValueError(
                f"DevRev {snapshot_name} board meta.total ({total}) differs from row count ({len(rows)})"
            )
        return rows

    @staticmethod
    def _ui_order(rows):
        # The company-published board groups rows by department; within a
        # department the board's own order is preserved (as served).
        return list(rows)

    def _make_job(self, row, *, entry_url, initial_row=None, membership_changed=False):
        identity = str(row["id"])
        description_html = row.get("content") or ""
        description_text = text_html(description_html)
        job_url = native_job_url(identity)
        apply_url = job_page_url(identity)
        offices = row.get("offices") or []
        locations = [{"location": office.get("location"), "name": office.get("name")}
                     for office in offices if isinstance(office, dict)]
        departments = [dept.get("name") for dept in (row.get("departments") or [])
                       if isinstance(dept, dict) and dept.get("name")]
        date_posted = row.get("first_published") if isinstance(row.get("first_published"), str) else None
        date_updated = row.get("updated_at") if isinstance(row.get("updated_at"), str) else None
        deadline = row.get("application_deadline") if isinstance(row.get("application_deadline"), str) else None
        attachment_links = _job_attachment_links(description_html)
        raw = {
            "listing": row,
            "source_ui": {
                "native_company_job_url": job_url,
                "greenhouse_job_url": apply_url,
                "greenhouse_apply_url": apply_url,
                "board_api_url": BOARD_API,
                "board_detail_url": board_detail_url(identity),
                "attachment_links": attachment_links,
            },
        }
        if initial_row is not None and initial_row != row:
            raw["initial_listing"] = initial_row
        job = self.job(
            raw,
            title=row["title"],
            url=job_url,
            job_id=identity,
            requisition_id=row.get("requisition_id"),
            apply_url=apply_url,
            description=description_html,
            method="devrev.official_entry.company_published_board.content",
            description_url=board_detail_url(identity),
            locations=locations,
            department="; ".join(departments) if departments else None,
            employment_type=None,
            workplace_type=None,
            date_posted=date_posted,
            date_posted_raw=row.get("first_published"),
            date_updated=date_updated,
            valid_through=deadline,
            salary=None,
        )
        job.description_text = description_text
        job.record_identity = f"devrev:{identity}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        if attachment_links:
            job.warnings.append("role_attachment_links_present_without_full_attachment_text")
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        parts = urlsplit(entry_url)
        if (parts.scheme != "https" or parts.hostname not in {"devrev.ai", "www.devrev.ai"}
                or parts.path.rstrip("/") != "/careers"):
            raise ValueError("DevRev dedicated flow must start from its official HTTPS /careers entry")
        if not self.page_guard():
            return self.result

        entry_probe = await self._entry_probe(entry_url)
        entry_waf_blocked = entry_probe.get("waf_or_checkpoint_blocked", False)
        if entry_waf_blocked:
            self.result.errors.append(
                f"devrev.ai native entry unavailable on this network (Vercel Security Checkpoint, HTTP 429); "
                f"company-published Greenhouse board collected as the reachable published source: {entry_probe.get('error')}"
            )
            self.result.warnings.append("native_company_entry_waf_blocked")

        _validate_board_url(BOARD_API, "DevRev published board")

        initial_payload, initial_rows = await self._board_snapshot(BOARD_API, "initial")
        initial_by_id = {str(row["id"]): row for row in initial_rows}
        if not initial_rows:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "DevRev company-published board listing returned zero jobs; empty state requires independent review"
            )
            self.result.metadata.update(self._metadata(entry_probe, initial_payload, initial_rows, None, None))
            return self.result

        # Representative exact detail bodies: the board's published per-job
        # endpoints for two exact IDs must carry byte-identical full content
        # for the same job rows (never enumerated; two exact IDs only).
        detail_started = time.perf_counter()
        representative = {}
        detail_errors = []
        for identity in self.REPRESENTATIVE_DETAIL_IDS:
            if identity not in initial_by_id:
                detail_errors.append(f"{identity}: representative ID absent from initial board listing")
                continue
            try:
                url = board_detail_url(identity)
                response = await self.http.get(url)
                if str(response.url) != url:
                    raise ValueError("representative detail request redirected away")
                detail = response.json()
                row = initial_by_id[identity]
                representative[identity] = {
                    "detail_url": url,
                    "title_match": detail.get("title") == row.get("title"),
                    "content_match": detail.get("content") == row.get("content"),
                    "first_published_match": detail.get("first_published") == row.get("first_published"),
                    "updated_at_match": detail.get("updated_at") == row.get("updated_at"),
                }
                if not rep_ok(representative[identity]):
                    detail_errors.append(f"{identity}: board detail endpoint content differs from the listing row")
            except Exception as exc:
                detail_errors.append(f"{identity}: {type(exc).__name__}: {exc}")
        detail_seconds = time.perf_counter() - detail_started
        if detail_errors:
            self.result.errors.extend(f"DevRev representative exact detail {item}" for item in detail_errors)

        final_snapshot_verified = True
        try:
            final_payload, final_rows = await self._board_snapshot(BOARD_API, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_payload = initial_payload
            final_rows = initial_rows
            self.result.errors.append(
                "DevRev final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        final_by_id = {str(row["id"]): row for row in final_rows}
        initial_ids = set(initial_by_id)
        final_ids = set(final_by_id)
        changed_membership = final_snapshot_verified and initial_ids != final_ids
        if changed_membership:
            self.result.errors.append("devrev_company_published_listing_membership_changed_between_initial_and_final_snapshots")

        all_ids = list(dict.fromkeys([str(row["id"]) for row in self._ui_order(final_rows)]
                                     + [str(row["id"]) for row in self._ui_order(initial_rows)]))
        changed_rows = sorted(identity for identity in final_by_id.keys() & initial_by_id.keys()
                              if final_by_id[identity] != initial_by_id[identity])

        mapping_started = time.perf_counter()
        for identity in all_ids:
            final_row = final_by_id.get(identity)
            initial_row = initial_by_id.get(identity)
            row = final_row or initial_row
            assert row is not None
            self.add(self._make_job(
                row, entry_url=entry_url,
                initial_row=initial_row if initial_row != row else None,
                membership_changed=changed_membership and identity not in final_by_id,
            ))
        mapping_seconds = time.perf_counter() - mapping_started

        self.result.expected_total = len(final_rows)
        self.result.pages = 1
        if changed_membership or entry_waf_blocked:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors

        self.result.metadata.update(self._metadata(
            entry_probe, initial_payload, initial_rows, final_payload, final_rows,
            initial_by_id=initial_by_id, final_by_id=final_by_id,
            representative=representative, detail_errors=detail_errors,
            final_snapshot_verified=final_snapshot_verified,
            changed_rows=changed_rows,
        ))
        self.result.timings["detail_phase_seconds"] = round(mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = round(time.perf_counter() - started, 6)
        return self.result

    def _metadata(self, entry_probe, initial_payload, initial_rows, final_payload, final_rows,
                  *, initial_by_id=None, final_by_id=None, representative=None, detail_errors=None,
                  final_snapshot_verified=None, changed_rows=None):
        final_rows = final_rows if final_rows is not None else []
        meta = {
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": self.company.careers_url,
            "native_entry_probe": entry_probe,
            "listing_api_url": BOARD_API,
            "membership_authority": (
                "DevRev's official careers journey publishes its roles on the Greenhouse board "
                "job-boards.greenhouse.io/devrev (same exact IDs as the native devrev.ai/job/<id> detail "
                "routes and the hosted 'Current openings at DevRev' listing at devrev.ai/positions/4301528004); "
                "the boards-api listing for that exact board is the full unfiltered membership source. "
                "No other board, slug or ATS-wide enumeration was used."
            ),
            "ui_pagination": "Single unfiltered board listing; meta.total equals the row count. No page/cursor parameter exists on this endpoint.",
            "initial_api_rows": len(initial_rows),
            "final_api_rows": len(final_rows),
            "meta_total_initial": (initial_payload.get("meta") or {}).get("total"),
            "meta_total_final": (final_payload.get("meta") or {}).get("total") if final_payload else None,
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": [str(row["id"]) for row in self._ui_order(initial_rows)],
            "final_visible_ids": [str(row["id"]) for row in self._ui_order(final_rows)],
            "source_rows_changed_between_snapshots": changed_rows or [],
            "representative_exact_detail_checks": representative or {},
            "representative_exact_detail_errors": detail_errors or [],
            "description_source": (
                "Each exact published board row's full `content` HTML (byte-identical to the same job's "
                "board detail endpoint for the two representative IDs). Native devrev.ai/job/<id> pages were "
                "unreachable from this network (Vercel Security Checkpoint, HTTP 429). "
                "Description source URL recorded per job is its exact board detail endpoint."
            ),
            "salary_policy": "The published board rows expose no structured compensation field (metadata is null on all current rows); salary stays null everywhere — nothing inferred from prose.",
            "job_attachment_links": {
                str(row["id"]): _job_attachment_links(row.get("content"))
                for row in final_rows if _job_attachment_links(row.get("content"))
            },
            "collection_wall_seconds": round(time.perf_counter() - self.result._started_clock, 6),
            "initial_and_final_listing_requests": 2,
            "native_detail_page_opened": bool(entry_probe.get("status") == 200),
        }
        return meta


def rep_ok(check):
    return (check.get("title_match") and check.get("content_match")
            and check.get("first_published_match") and check.get("updated_at_match"))
