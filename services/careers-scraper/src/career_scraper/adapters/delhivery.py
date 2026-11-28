"""Delhivery's official careers journey: the live /careers landing page publishes
the hosted Darwinbox candidate board, which is followed through its own public
listing and exact-job detail requests only."""
import ast
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urljoin, urlsplit

from .base import Adapter
from ..models import text_html

TASK_ENTRY = "https://www.delhivery.com/company/careers"
OFFICIAL_LANDING = "https://www.delhivery.com/careers"
BOARD_ENTRY = "https://delhivery.darwinbox.in/ms/candidate/careers"
API_BASE = "https://delhivery.darwinbox.in/ms/candidateapi"
LISTING_LIMIT = 10
JS_CHUNK_MAP = re.compile(r"\{([0-9a-f:\",]+)\}\[e\]\+\"\.js\"")
MAP_ENTRY = re.compile(r"(\d+):\"([a-f0-9]+)\"")
SCRIPT_SRCS = re.compile(r'<script[^>]+src="(/_nuxt/[^"]+\.js)"')
HEX_ID = re.compile(r"^[0-9a-f]{14}$")
NUXT_HOST = "www.delhivery.com"
BOARD_HOST = "delhivery.darwinbox.in"
IST = timezone(timedelta(hours=5, minutes=30))


def board_job_url(identity):
    if not HEX_ID.fullmatch(str(identity)):
        raise ValueError(f"Delhivery board job ID is not a 14-char hex ID: {identity!r}")
    return f"{BOARD_ENTRY}/{identity}"


def board_apply_url(identity):
    return board_job_url(identity) + "?apply=1"


def decode_jd(raw_jd):
    """Mirror the candidate detail component's htmlDecode + trusted-HTML render."""
    return unescape(raw_jd or "")


def parse_list_field(value):
    """Tolerant parse of Darwinbox stringified lists; arrays pass through."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    return [value]


def posted_date_iso(row):
    """UI postedOn: prefer job_posting_on (Unix seconds, Asia/Kolkata), else created_on."""
    epoch = row.get("job_posting_on")
    if epoch in (None, ""):
        return row.get("created_on") or None
    try:
        seconds = int(str(epoch).strip())
    except (TypeError, ValueError):
        return row.get("created_on") or None
    return datetime.fromtimestamp(seconds, tz=IST).isoformat()


class Delhivery(Adapter):
    """Follow only Delhivery's live careers landing and its published board."""

    VERIFICATION_REVISION = "delhivery.com-careers.darwinbox-candidate-listing-detail.1"

    def _landing_chunk_hash(self, runtime_text):
        """Find chunk 134's JS hash from the runtime bundle's script manifest."""
        hashes = []
        for match in JS_CHUNK_MAP.finditer(runtime_text):
            entries = dict(MAP_ENTRY.findall(match.group(1)))
            if "134" in entries:
                hashes.append(entries["134"])
        if len(hashes) != 1 or not re.fullmatch(r"[a-f0-9]{20}", hashes[0]):
            raise ValueError(
                "Delhivery Nuxt runtime manifest does not expose exactly one JS chunk-134 hash"
            )
        return hashes[0]

    def _assert_landing_chunk(self, chunk_text):
        hrefs = re.findall(r'href:"(https://[^"]+)"', chunk_text)
        summary = {
            "board_hrefs": [h for h in hrefs if h == BOARD_ENTRY],
            "delivery_partner_hrefs": [h for h in hrefs
                                        if h == "https://www.delhivery.com/partner/delivery-partner"],
            "skills_program_hrefs": [h for h in hrefs
                                     if h == "https://www.delhivery.com/skills-development-program"],
            "other_darwinbox_hrefs": [h for h in hrefs if "darwinbox" in h and h != BOARD_ENTRY],
        }
        if (len(summary["board_hrefs"]) != 2 or len(summary["delivery_partner_hrefs"]) != 2
                or len(summary["skills_program_hrefs"]) != 2
                or summary["other_darwinbox_hrefs"] or 'name:"Careers"' not in chunk_text):
            raise ValueError(
                "Delhivery /careers chunk no longer publishes the inspected card hrefs "
                f"{summary}"
            )
        return summary

    def _validate_listing_payload(self, payload, label):
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ValueError(f"Delhivery {label} listing response is not a successful board payload")
        message = payload.get("message")
        if not isinstance(message, dict):
            raise ValueError(f"Delhivery {label} listing response has no message object")
        count = message.get("jobscount")
        rows = message.get("jobs")
        if not isinstance(count, int) or count < 0 or not isinstance(rows, list):
            raise ValueError(f"Delhivery {label} listing response has no jobscount/jobs")
        ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
        if any(not isinstance(identity, str) or not HEX_ID.fullmatch(identity) for identity in ids):
            raise ValueError(f"Delhivery {label} listing contains a missing or invalid job ID")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Delhivery {label} listing repeats a job ID")
        if any(not row.get("title") for row in rows):
            raise ValueError(f"Delhivery {label} listing contains an unidentified job")
        return count, rows

    def _make_job(self, listing_row, detail_row, *, landing_chunk_url, today_employer):
        identity = listing_row["id"]
        detail_url = f"{API_BASE}/job/{identity}"
        if detail_row.get("id") != identity:
            raise ValueError(f"Delhivery detail for {identity} returned a different job ID")
        if detail_row.get("title") != listing_row.get("title"):
            self.result.errors.append(
                f"{identity}: listing title {listing_row.get('title')!r} differs from "
                f"detail title {detail_row.get('title')!r}"
            )
        raw_jd = detail_row.get("jd") or ""
        description_html = decode_jd(raw_jd)
        description_text = text_html(description_html)
        if not description_text.strip():
            self.result.errors.append(f"{identity}: Delhivery public detail jd is empty")
        tooltip_locations = parse_list_field(detail_row.get("tool_tip_locations"))
        if not tooltip_locations:
            tooltip_locations = parse_list_field(listing_row.get("tool_tip_locations"))
        show_location = detail_row.get("officelocation_show_arr") or listing_row.get("officelocation_show_arr")
        locations = [{"location": loc} for loc in tooltip_locations]
        if show_location and not tooltip_locations:
            locations = [{"location": show_location}]
        salary_raw = (detail_row.get("salary_min", ""), detail_row.get("salary_max", ""),
                      detail_row.get("salary_range", ""), detail_row.get("salary_currency", ""))
        salary = None
        if any(str(part).strip() for part in salary_raw):
            salary = {"source_fields": {
                "salary_min": detail_row.get("salary_min"), "salary_max": detail_row.get("salary_max"),
                "salary_range": detail_row.get("salary_range"),
                "salary_currency": detail_row.get("salary_currency"),
                "salary_timeframe": detail_row.get("salary_timeframe"),
            }}
        experience_raw = detail_row.get("experience")
        experience = None
        if isinstance(experience_raw, str) and experience_raw.strip():
            experience = {"source_text": experience_raw}
        posted = posted_date_iso(detail_row)
        posted_raw = detail_row.get("job_posting_on")
        if posted_raw is None or posted_raw == "":
            posted_raw = detail_row.get("created_on")
        posted_raw = str(posted_raw) if posted_raw is not None else None
        raw = {
            "listing": listing_row,
            "detail": detail_row,
            "source_ui": {
                "landing_chunk_url": landing_chunk_url,
                "board_entry": BOARD_ENTRY,
                "detail_page_url": board_job_url(identity),
                "apply_url": board_apply_url(identity),
                "display_location": show_location,
                "tooltip_locations": tooltip_locations,
                "source_flag_is_remote": detail_row.get("is_remote"),
                "derived_workplace_type": "Remote" if str(detail_row.get("is_remote")) == "1" else (
                    "On-site" if str(detail_row.get("is_remote")) == "0" else None
                ),
                "raw_jd": raw_jd,
            },
        }
        workplace = raw["source_ui"]["derived_workplace_type"]
        job = self.job(
            raw,
            title=detail_row.get("title") or listing_row.get("title"),
            url=board_job_url(identity),
            job_id=identity,
            requisition_id=detail_row.get("internal_job_code") or None,
            apply_url=board_apply_url(identity),
            description=description_html,
            method="delhivery.official_careers.darwinbox_job.jd",
            description_url=detail_url,
            locations=locations,
            department=detail_row.get("department") or listing_row.get("department"),
            employment_type=detail_row.get("emp_type") or listing_row.get("emp_type"),
            workplace_type=workplace,
            date_posted=posted,
            date_posted_raw=posted_raw,
            date_updated=None,
            salary=salary,
            experience=experience,
            employer=today_employer,
        )
        job.description_text = description_text
        job.record_identity = f"delhivery:{identity}"
        return job

    async def run(self):
        started = self.result._started_clock
        if self.company.careers_url != TASK_ENTRY or self.company.slug != "delhivery":
            raise ValueError("Delhivery dedicated flow must start from its assigned task entry")
        if not self.page_guard():
            return self.result
        task_response = await self.http.get(TASK_ENTRY)
        task_final_url = str(task_response.url)
        landing = await self.http.get(OFFICIAL_LANDING)
        if str(landing.url) != OFFICIAL_LANDING:
            raise ValueError(f"Delhivery official /careers landing redirected to {landing.url}")
        landing_html = landing.text
        script_srcs = SCRIPT_SRCS.findall(landing_html)
        if not script_srcs:
            raise ValueError("Delhivery /careers shell publishes no Nuxt script bundles")
        chunk_hash = None
        runtime_url = None
        for src in script_srcs:
            script_url = urljoin(OFFICIAL_LANDING, src)
            parsed = urlsplit(script_url)
            if parsed.hostname != NUXT_HOST or not parsed.path.startswith("/_nuxt/"):
                raise ValueError(f"Delhivery landing script left the official site: {script_url}")
            response = await self.http.get(script_url)
            try:
                found = self._landing_chunk_hash(response.text)
            except ValueError:
                continue
            chunk_hash, runtime_url = found, str(response.url)
            break
        if chunk_hash is None:
            raise ValueError("Delhivery landing bundles do not expose the chunk-134 careers card")
        landing_chunk_url = f"https://www.delhivery.com/_nuxt/{chunk_hash}.js"
        chunk_response = await self.http.get(landing_chunk_url)
        if str(chunk_response.url) != landing_chunk_url:
            raise ValueError("Delhivery careers chunk redirected away from its published URL")
        chunk_text = chunk_response.text
        if not chunk_text.startswith("(window.webpackJsonp"):
            raise ValueError("Delhivery careers chunk endpoint no longer serves the inspected module")
        card_summary = self._assert_landing_chunk(chunk_text)

        board_shell = await self.http.get(BOARD_ENTRY)
        if str(board_shell.url) != BOARD_ENTRY:
            raise ValueError(f"Delhivery hosted board redirected to {board_shell.url}")

        board_started = time.perf_counter()
        companyinfo_url = f"{API_BASE}/companyinfo"
        filters_url = f"{API_BASE}/job/filters"
        info_response = await self.http.get(companyinfo_url)
        info_payload = info_response.json()
        try:
            companyinfo = info_payload["message"]["company"]
        except (KeyError, TypeError) as exc:
            raise ValueError("Delhivery companyinfo response is malformed") from exc
        employer = companyinfo.get("company_name")
        if employer != "Delhivery Limited":
            raise ValueError(f"Delhivery board companyinfo names {employer!r}, not Delhivery Limited")
        filters_response = await self.http.get(filters_url)
        filters_payload = filters_response.json()
        try:
            filters_message = filters_payload["message"]
        except (KeyError, TypeError) as exc:
            raise ValueError("Delhivery job/filters response is malformed") from exc
        board_phase_seconds = round(time.perf_counter() - board_started, 6)

        # Initial full pagination with the UI default page size (itemsPerPage=10).
        initial_pages = []
        initial_rows = []
        page = 1
        expected_total = None
        while True:
            if not self.page_guard():
                break
            url = f"{API_BASE}/job?page={page}&limit={LISTING_LIMIT}"
            response = await self.http.get(url)
            count, rows = self._validate_listing_payload(response.json(), f"page {page}")
            if expected_total is None:
                expected_total = count
            elif count != expected_total and rows:
                self.result.errors.append(
                    f"Delhivery jobscount changed during pagination: {expected_total} -> {count} at page {page}"
                )
            initial_pages.append({"page": page, "url": url, "jobscount": count, "rows": len(rows)})
            initial_rows.extend(rows)
            if len(initial_rows) >= expected_total:
                break
            page += 1
        if expected_total is None or len(initial_rows) < expected_total:
            self.result.errors.append("Delhivery listing pagination stopped before jobscount")
            self.result.listing_complete = False
            self.result.expected_total = expected_total or 0
            self.result.pages = len(initial_pages)
            return self.result
        probe_url = f"{API_BASE}/job?page={len(initial_pages) + 1}&limit={LISTING_LIMIT}"
        probe_response = await self.http.get(probe_url)
        probe_count, probe_rows = self._validate_listing_payload(probe_response.json(), "termination probe")
        initial_pages.append({"page": len(initial_pages) + 1, "url": probe_url,
                              "jobscount": probe_count, "rows": len(probe_rows)})
        if probe_rows:
            self.result.errors.append(
                f"Delhivery listing still returns rows at probe page {len(initial_pages)} beyond jobscount"
            )
        initial_by_id = {row["id"]: row for row in initial_rows}
        initial_ids = [row["id"] for row in initial_rows]
        if len(initial_ids) != len(initial_by_id):
            self.result.errors.append("Delhivery listing pagination repeats a job ID")
        if not initial_ids:
            self.result.errors.append("Delhivery board returned zero postings; requires review")
            self.result.listing_complete = False

        # One representative native detail page (first listed ID) confirms route shape.
        detail_started = time.perf_counter()
        representative_url = board_job_url(initial_ids[0]) if initial_ids else None
        if representative_url:
            detail_page = await self.http.get(representative_url)
            if str(detail_page.url) != representative_url:
                self.result.errors.append(
                    f"Delhivery representative detail route redirected: {representative_url} -> {detail_page.url}"
                )
        detail_ui_seconds = round(time.perf_counter() - detail_started, 6)

        # Final membership re-check (page 1) plus history of the probe page.
        final_url = f"{API_BASE}/job?page=1&limit={LISTING_LIMIT}"
        final_response = await self.http.get(final_url)
        final_count, final_rows = self._validate_listing_payload(final_response.json(), "final page 1")
        final_ids = [row["id"] for row in final_rows]
        membership_changed = (final_ids != initial_ids[:len(final_ids)]
                              or (len(initial_ids) <= LISTING_LIMIT
                                  and len(final_ids) != len(initial_ids)))
        if membership_changed:
            self.result.errors.append("delhivery_official_board_membership_changed_between_initial_and_final_snapshots")
        if probe_rows:
            self.result.errors.append("delhivery_termination_probe_page_is_not_empty")

        # Exact-ID detail fetch for every listed ID (including publisher-empty JDs).
        mapping_started = time.perf_counter()
        detail_seconds = 0.0
        completed = 0
        for identity in initial_ids:
            detail_job_started = time.perf_counter()
            url = f"{API_BASE}/job/{identity}"
            response = await self.http.get(url)
            payload = response.json()
            message = payload.get("message") if isinstance(payload, dict) else None
            detail_rows = message.get("job") if isinstance(message, dict) else None
            if (not isinstance(detail_rows, list) or len(detail_rows) != 1
                    or not isinstance(detail_rows[0], dict)):
                self.result.errors.append(f"{identity}: Delhivery detail response has no single job object")
                fallback = dict(initial_by_id[identity])
                fallback.setdefault("jd", "")
                job = self._make_job(
                    initial_by_id[identity], fallback,
                    landing_chunk_url=landing_chunk_url, today_employer=employer,
                )
                self.add(job)
                detail_seconds += time.perf_counter() - detail_job_started
                completed += 1
                continue
            job = self._make_job(
                initial_by_id[identity], detail_rows[0],
                landing_chunk_url=landing_chunk_url, today_employer=employer,
            )
            self.add(job)
            detail_seconds += time.perf_counter() - detail_job_started
            completed += 1
        mapping_seconds = round(time.perf_counter() - mapping_started, 6)

        self.result.expected_total = expected_total
        self.result.pages = len(initial_pages)
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "task_entry_url": TASK_ENTRY,
            "task_entry_final_url": task_final_url,
            "official_landing_url": OFFICIAL_LANDING,
            "landing_runtime_url": runtime_url,
            "landing_chunk_url": landing_chunk_url,
            "landing_card_summary": card_summary,
            "board_entry": BOARD_ENTRY,
            "api_base": API_BASE,
            "companyinfo": companyinfo,
            "filter_vocab": {
                "employmenttypes": filters_message.get("employmenttypes"),
                "locations": filters_message.get("locations"),
                "departments": filters_message.get("departments"),
                "functional_areas": filters_message.get("functional_areas"),
                "capabilities": filters_message.get("capabilities"),
            },
            "membership_authority": (
                "Delhivery /careers landing chunk 134 card hrefs; hosted Darwinbox v1 candidate "
                "board /ms/candidate/careers; its own unfiltered GET job?page=&limit= listing"
            ),
            "listing_limit_per_page": LISTING_LIMIT,
            "listing_pages": initial_pages,
            "final_page1_url": final_url,
            "final_page1_ids": final_ids,
            "initial_visible_ids": initial_ids,
            "final_visible_ids": final_ids,
            "publisher_empty_jd_ids": [
                job.job_id for job in self.result.jobs if not (job.description_text or "").strip()
            ],
            "representative_detail_page_opened": representative_url,
            "description_source": "Each exact listed ID's GET /ms/candidateapi/job/<id> message.job[0].jd",
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings["board_config_phase_seconds"] = board_phase_seconds
        self.result.timings["representative_detail_ui_phase_seconds"] = detail_ui_seconds
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        self.result.timings["detail_mapping_wall_seconds"] = mapping_seconds
        if membership_changed or probe_rows or not self.result.listing_complete:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = not self.result.errors
        return self.result
