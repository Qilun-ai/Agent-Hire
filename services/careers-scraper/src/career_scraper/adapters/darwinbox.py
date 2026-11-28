"""Darwinbox's own careers journey and its company-published Darwinbox board.

The ledger entry ``https://darwinbox.com/careers/`` is a Next.js/Storyblok
marketing page whose hero control ``View Open Positions`` links to the exact
hosted candidate board
``https://dbx.darwinbox.in/ms/candidatev2/main/careers/allJobs``
(tenant ``dbx``, candidate route ``main``, legal name on the board shell
``Darwinbox Digital Solutions Pvt Ltd``). The SPA shell embeds
``apiURL: "/ms/candidateapi/"`` and the listing call
``POST job/alljobs`` with ``{companyId, page, sort_option, limit}``;
details are ``GET job/{id}`` and return ``message.job[0]``. Only that
exact company-exposed board is ever requested.
"""

import hashlib
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import text_html

ENTRY_HOSTS = {"darwinbox.com", "www.darwinbox.com"}
DARWINBOX_HOST = "dbx.darwinbox.in"
DARWINBOX_ENTRY = "https://dbx.darwinbox.in/ms/candidatev2/main/careers/allJobs"
ALLJOBS_API = "https://dbx.darwinbox.in/ms/candidateapi/job/alljobs?companyId=main"
COMPANY_CONFIG_API = (
    "https://dbx.darwinbox.in/ms/candidateapi/getCompanyConfig?is_preview=false&companyId=main"
)
API_BASE = "/ms/candidateapi/"
SHELL_BUNDLE_RE = re.compile(r"main\.[a-f0-9]+\.js")
BOARD_TENANT_LEGAL_NAME = "Darwinbox Digital Solutions Pvt Ltd"


def alljobs_api_url():
    return ALLJOBS_API


def company_config_api_url():
    return COMPANY_CONFIG_API


def job_detail_api_url(identity):
    if not identity:
        raise ValueError("Darwinbox job ID is empty")
    return f"https://{DARWINBOX_HOST}{API_BASE}job/{identity}?companyId=main"


def detail_page_url(identity):
    if not identity:
        raise ValueError("Darwinbox job ID is empty")
    return f"https://{DARWINBOX_HOST}/ms/candidatev2/main/careers/jobDetails/{identity}"


def compact_body_hash(html):
    """NFC/whitespace-free body hash, for future rendered-reference checks."""
    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s﻿]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _epoch_to_date(value):
    """Darwinbox epoch seconds or ISO timestamp -> ``YYYY-MM-DD`` or None."""
    if value in (None, "", 0):
        return None
    if isinstance(value, str):
        text = value.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}", text):
            return text[:10]
        try:
            value = int(float(text))
        except ValueError:
            return None
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _locations(row):
    """Prefer the listing's clean location list, fall back to raw strings."""
    listed = row.get("officelocation_show_arr_list")
    if isinstance(listed, list):
        values = [str(item).strip() for item in listed if str(item).strip()]
        if values:
            return [{"location": item, "address": None} for item in values]
    for key in ("locations", "officelocation_show_arr"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return [{"location": value.strip(), "address": None}]
        if isinstance(value, list):
            values = [str(item).strip() for item in value if str(item).strip()]
            if values:
                return [{"location": item, "address": None} for item in values]
    return []


def _attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _title(row):
    for key in (
        "designation_display_name",
        "title",
        "designation_title_name",
        "designation_name",
    ):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_alljobs_payload(payload, snapshot_name):
    """Validate one ``job/alljobs`` response and return ``(rows, job_counts)``."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError(f"Darwinbox {snapshot_name} listing response has no data array")
    rows = payload["data"]
    counts = payload.get("job_counts")
    if counts is not None and not isinstance(counts, int):
        raise ValueError(f"Darwinbox {snapshot_name} listing job_counts is not an integer")
    ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
    if any(not identity for identity in ids):
        raise ValueError(f"Darwinbox {snapshot_name} listing contains a row without an exact job ID")
    if len(ids) != len(set(ids)):
        raise ValueError(f"Darwinbox {snapshot_name} listing repeats a company-published ID")
    if counts is not None and counts < len(rows):
        raise ValueError(f"Darwinbox {snapshot_name} listing job_counts is smaller than its page")
    return rows, counts


def parse_detail_payload(payload, identity):
    """Unwrap ``GET job/{id}`` and return its single job object."""
    row = None
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, dict) and isinstance(message.get("job"), list) and message["job"]:
            row = message["job"][0]
        elif payload.get("id") and ("jd" in payload or "title" in payload):
            row = payload
    if not isinstance(row, dict) or not row:
        raise ValueError(f"Darwinbox detail response for {identity} has no job object")
    if str(row.get("id")) != str(identity):
        raise ValueError(f"Darwinbox detail response ID differs from requested ID {identity}")
    return row


class Darwinbox(Adapter):
    """Follow Darwinbox's official entry to its own hosted board only."""

    VERIFICATION_REVISION = "darwinbox.official-careers-dbx-alljobs.1"

    @staticmethod
    def _check_entry(page_url, html):
        parsed = urlsplit(page_url)
        if parsed.scheme != "https" or parsed.hostname not in ENTRY_HOSTS:
            raise ValueError(f"Darwinbox official entry left the company HTTPS site: {page_url}")
        soup = BeautifulSoup(html, "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        open_position_links = []
        for anchor in soup.select("a[href]"):
            href = urljoin(page_url, anchor.get("href") or "")
            if href == DARWINBOX_ENTRY:
                open_position_links.append(href)
        canonical = None
        canonical_node = soup.select_one('link[rel="canonical"]')
        if canonical_node:
            canonical = canonical_node.get("href")
        return {
            "title": title,
            "canonical": canonical,
            "open_positions_links": open_position_links,
            "open_positions_link_count": len(open_position_links),
        }

    @staticmethod
    def _check_shell(page_url, html):
        parsed = urlsplit(page_url)
        if parsed.scheme != "https" or parsed.hostname != DARWINBOX_HOST:
            raise ValueError(f"Darwinbox company-published board left its tenant host: {page_url}")
        soup = BeautifulSoup(html, "lxml")
        base = soup.base.get("href") if soup.base else None
        scripts = [node.get("src") or "" for node in soup.select("script[src]")]
        bundle = next((src for src in scripts if SHELL_BUNDLE_RE.fullmatch(src)), None)
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if base == "/ms/candidatev2/" and bundle is not None:
            kind = "app_shell"
        elif base is None and bundle is None and title == BOARD_TENANT_LEGAL_NAME:
            # Scripted HTTP receives Darwinbox's crawler/OG shell instead of
            # the app shell; it still proves the exact board URL.
            kind = "og_shell"
        else:
            raise ValueError("Darwinbox board shell changed from its observed app/OG shell")
        return {
            "kind": kind,
            "title": title,
            "base_href": base,
            "main_bundle": urljoin(page_url, bundle) if bundle else None,
        }

    async def _post_listing(self, api_url, payload, snapshot_name):
        try:
            response = await self.http.request("POST", api_url, json=payload)
        except FetchError as exc:
            raise FetchError(
                f"Darwinbox {snapshot_name} listing request blocked by WAF: {exc}"
            ) from exc
        if str(response.url) != api_url:
            raise ValueError(f"Darwinbox {snapshot_name} listing request redirected away from its source")
        try:
            parsed = response.json()
        except ValueError as exc:
            raise ValueError(f"Darwinbox {snapshot_name} listing response is not JSON") from exc
        parse_alljobs_payload(parsed, snapshot_name)
        return parsed

    def _make_job(self, row, *, detail_row=None, api_url):
        identity = str(row["id"])
        # Listing row is the tile/metadata authority; the exact detail
        # object confirms the full jd and may supply a missing title.
        description_html = (detail_row or {}).get("jd") or row.get("jd") or ""
        description_text = text_html(description_html) if description_html else ((detail_row or {}).get("jd_summary") or row.get("jd_summary") or "")
        if not description_html.strip():
            self.result.errors.append(f"{identity}: Darwinbox detail jd is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{identity}: Darwinbox detail jd is implausibly short")
        title = _title(detail_row or {}) or _title(row)
        date_posted_raw = row.get("posted_on", row.get("created_on"))
        if detail_row and detail_row.get("posted_on") is not None and row.get("posted_on") is None:
            date_posted_raw = detail_row.get("posted_on")
        date_posted = _epoch_to_date(row.get("posted_on") or row.get("created_on") or (detail_row or {}).get("posted_on"))
        salary = row.get("salary_range") or (detail_row or {}).get("salary_range") or None
        if salary == "":
            salary = None
        raw = {
            "listing": row,
            "source_ui": {
                "listing_api_url": api_url,
                "native_detail_page": detail_page_url(identity),
                "detail_api_url": job_detail_api_url(identity),
                "attachment_links": _attachment_links(description_html),
                "salary_from_detail": salary,
                "board_tenant_legal_name": BOARD_TENANT_LEGAL_NAME,
            },
        }
        if detail_row is not None and detail_row != row:
            raw["detail"] = detail_row
        workplace = row.get("is_remote", (detail_row or {}).get("is_remote"))
        job = self.job(
            raw,
            title=title,
            url=detail_page_url(identity),
            job_id=identity,
            requisition_id=str(row.get("internal_job_code") or (detail_row or {}).get("internal_job_code") or "") or None,
            apply_url=detail_page_url(identity),
            description=description_html,
            method="darwinbox.official_careers.dbx_job.jd",
            description_url=api_url,
            locations=_locations(row) or _locations(detail_row or {}),
            department=row.get("department_name") or (detail_row or {}).get("department"),
            team=row.get("functional_area_name") or (detail_row or {}).get("functional_area_name"),
            employment_type=row.get("emp_type_name") or (detail_row or {}).get("emp_type") or (detail_row or {}).get("emp_type_name"),
            workplace_type=("Remote" if workplace in (1, True, "1") else None),
            experience=row.get("experience") or (detail_row or {}).get("experience"),
            salary=salary,
            date_posted=date_posted,
            date_posted_raw=date_posted_raw,
        )
        job.description_text = description_text
        job.record_identity = f"darwinbox:{identity}"
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (
            entry_parts.scheme != "https"
            or entry_parts.hostname not in ENTRY_HOSTS
            or entry_parts.path.rstrip("/") != "/careers"
        ):
            raise ValueError("Darwinbox dedicated flow must start from its official HTTPS /careers/ entry")

        entry_started = time.perf_counter()
        entry = None
        try:
            entry = await self.http.get(entry_url)
        except FetchError as exc:
            self.result.errors.append(f"Darwinbox official entry fetch failed: {exc}")
        entry_seconds = time.perf_counter() - entry_started
        entry_observation = {}
        board_link_ok = False
        if entry is not None:
            try:
                entry_observation = self._check_entry(str(entry.url), entry.text)
                if not entry_observation["open_positions_links"]:
                    self.result.errors.append(
                        "Darwinbox official entry lost its exact View Open Positions board link"
                    )
                else:
                    board_link_ok = True
            except ValueError as exc:
                self.result.errors.append(f"Darwinbox official entry check failed: {exc}")
        self.result.metadata["official_entry_observation"] = entry_observation

        shell_started = time.perf_counter()
        shell_observation = {}
        try:
            shell = await self.http.get(DARWINBOX_ENTRY)
            shell_observation = self._check_shell(str(shell.url), shell.text)
        except (FetchError, ValueError) as exc:
            self.result.errors.append(
                f"Darwinbox company-published board unreachable: {type(exc).__name__}: {exc}"
            )
        shell_seconds = time.perf_counter() - shell_started
        self.result.metadata["darwinbox_shell_observation"] = shell_observation

        listing_started = time.perf_counter()
        listing_error = None
        initial_payload = final_payload = None
        if not board_link_ok and entry is not None:
            listing_error = "official entry does not publish the exact board link; listing not requested"
            self.result.errors.append(listing_error)
        else:
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
        listing_seconds = time.perf_counter() - listing_started

        api_url = ALLJOBS_API
        if listing_error is not None or initial_payload is None or final_payload is None:
            self.result.expected_total = None
            self.result.pages = 1
            self.result.listing_complete = False
            self.result.metadata.update({
                "verification_revision": self.VERIFICATION_REVISION,
                "membership_authority": (
                    "Darwinbox official /careers/ page's View Open Positions control -> exact hosted "
                    "candidate board https://dbx.darwinbox.in/ms/candidatev2/main/careers/allJobs; "
                    "its POST /ms/candidateapi/job/alljobs?companyId=main listing request"
                ),
                "membership_observable_via_http": False,
                "listing_error": listing_error,
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
                "initial_and_final_listing_requests": 2,
            })
            self.result.timings["official_entry_phase_seconds"] = round(entry_seconds, 6)
            self.result.timings["darwinbox_shell_phase_seconds"] = round(shell_seconds, 6)
            self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
            return self.result

        initial_rows, initial_counts = parse_alljobs_payload(initial_payload, "initial")
        final_rows, final_counts = parse_alljobs_payload(final_payload, "final")

        page_reconciliation = [
            {"page": 1, "snapshot": "initial", "job_counts": initial_counts, "rows": len(initial_rows),
             "ids": [str(row["id"]) for row in initial_rows]},
            {"page": 1, "snapshot": "final", "job_counts": final_counts, "rows": len(final_rows),
             "ids": [str(row["id"]) for row in final_rows]},
        ]
        all_rows = list(final_rows)
        seen = {str(row["id"]) for row in all_rows}
        total = final_counts if final_counts is not None else len(all_rows)
        page = 2
        pages_fetched = 2
        pagination_error = None
        while len(all_rows) < total and page <= 100:
            try:
                payload = await self._post_listing(
                    ALLJOBS_API, {"companyId": "main", "page": page, "sort_option": "new", "limit": 10}, f"page{page}"
                )
            except (FetchError, ValueError) as exc:
                pagination_error = f"page {page}: {type(exc).__name__}: {exc}"
                self.result.errors.append(f"Darwinbox Darwinbox listing pagination failed: {pagination_error}")
                break
            rows, counts = parse_alljobs_payload(payload, f"page{page}")
            pages_fetched += 1
            page_reconciliation.append({
                "page": page, "snapshot": f"page{page}", "job_counts": counts,
                "rows": len(rows), "ids": [str(row["id"]) for row in rows],
            })
            if not rows:
                self.result.errors.append("Darwinbox listing ended before job_counts was reached")
                break
            new_rows = [row for row in rows if str(row["id"]) not in seen]
            if not new_rows:
                self.result.errors.append("Darwinbox listing repeated IDs before job_counts was reached")
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
                "darwinbox_official_listing_membership_changed_between_initial_and_final_snapshots"
            )

        detail_started = time.perf_counter()
        detail_error = None
        for row in all_rows:
            identity = str(row["id"])
            detail_row = None
            try:
                detail = await self.http.get(job_detail_api_url(identity))
                try:
                    detail_payload = detail.json()
                except ValueError as exc:
                    raise ValueError("Darwinbox detail response is not JSON") from exc
                detail_row = parse_detail_payload(detail_payload, identity)
            except (FetchError, ValueError) as exc:
                detail_error = f"{identity}: {type(exc).__name__}: {exc}"
                self.result.errors.append(f"Darwinbox exact detail failed: {detail_error}")
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
            "darwinbox_entry_url": DARWINBOX_ENTRY,
            "listing_api_url": api_url,
            "board_tenant_legal_name": BOARD_TENANT_LEGAL_NAME,
            "membership_authority": (
                "Darwinbox official /careers/ page's View Open Positions control -> exact hosted "
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
            "page_reconciliation": page_reconciliation,
            "description_source": "Each exact listed row's jd field, confirmed against GET job/{id} detail objects (message.job[0]).",
            "representative_detail_ids_inspected": [str(row["id"]) for row in all_rows[:2]],
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "initial_and_final_listing_requests": 2,
            "membership_observable_via_http": True,
        })
        self.result.timings["official_entry_phase_seconds"] = round(entry_seconds, 6)
        self.result.timings["darwinbox_shell_phase_seconds"] = round(shell_seconds, 6)
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        return self.result
