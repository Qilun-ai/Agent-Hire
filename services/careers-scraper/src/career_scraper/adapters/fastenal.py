"""Fastenal's own Careers page and native Fastenal Careers search/detail flow."""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
import math
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


ENTRY = "https://careers.fastenal.com/"
LISTING = "https://jobs.fastenal.com/jobs"
JOBS_ORIGIN = "https://jobs.fastenal.com"
LOAD_JOBS = "https://jobs.fastenal.com/load-jobs"
SCRIPT_NAME = "scripts-2026.6.0-min.js"
PAGE_SIZE = 100  # Native DataTables menu choice confirmed in the published UI.
SUPPORTED_PAGE_SIZES = (10, 25, 50, 100)
EXPECTED_DETAIL_FIELDS = (
    "Job ID", "Title", "Type", "Location", "Departments", "Date Published", "End Date",
)
ROLE_ATTACHMENT = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?|pptx?)(?:$|[?#])", re.I)


def _compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _canonical_hash(value) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(body.encode("utf-8")).hexdigest()


def _date_iso(value: str | None, formats: tuple[str, ...]) -> str | None:
    value = _compact(value)
    if not value:
        return None
    for pattern in formats:
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _csrf(soup: BeautifulSoup) -> tuple[str, str]:
    header = soup.select_one('meta[name="_csrf_header"]')
    token = soup.select_one('meta[name="_csrf"]')
    if not header or not token or not header.get("content") or not token.get("content"):
        raise ValueError("Fastenal listing no longer exposes the same-session CSRF meta fields")
    return header["content"].strip(), token["content"].strip()


def _detail_table(soup: BeautifulSoup) -> tuple[list[dict[str, str]], dict[str, str]]:
    expected = set(EXPECTED_DETAIL_FIELDS)
    matches = []
    for table in soup.select("table"):
        pairs = []
        fields = {}
        for row in table.select("tr"):
            cells = row.select("th, td")
            if len(cells) != 2:
                continue
            label = _compact(cells[0].get_text(" ", strip=True))
            value = _compact(cells[1].get_text(" ", strip=True))
            if not label:
                continue
            pairs.append({"label": label, "value": value})
            if label in fields:
                raise ValueError(f"Fastenal detail repeats table field {label!r}")
            fields[label] = value
        if expected.issubset(fields):
            matches.append((pairs, fields))
    if len(matches) != 1:
        raise ValueError(f"Fastenal detail must expose one native job-details table, found {len(matches)}")
    return matches[0]


def _salary_text(description_text: str) -> list[str]:
    # Retain only exact pay sentences found in the role body; do not infer units
    # or a currency from the symbol alone.
    snippets = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", description_text):
        normalized = _compact(sentence)
        if normalized and re.search(r"\b(?:base pay|salary|compensation|pay range|bonus)\b", normalized, re.I):
            snippets.append(normalized)
    return list(dict.fromkeys(snippets))


def _listing_form(draw: int, start: int) -> dict[str, str]:
    return {
        "draw": str(draw),
        "start": str(start),
        "length": str(PAGE_SIZE),
        "order[0][column]": "0",
        "order[0][dir]": "asc",
        "search[value]": "",
        "search[regex]": "false",
        "sortColumn": "0",
        "sortDir": "asc",
        "query": "",
    }


def _exact_detail_url(job_id: str | int) -> str:
    identity = str(job_id)
    if not re.fullmatch(r"[0-9]+", identity):
        raise ValueError(f"Fastenal published a non-numeric job ID: {identity!r}")
    return urljoin(JOBS_ORIGIN + "/", f"details/{identity}")


class Fastenal(Adapter):
    """Collect the unfiltered DataTables listing and exact native job pages."""

    VERIFICATION_REVISION = "fastenal-careers-native-load-jobs-details.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.csrf_header: str | None = None
        self.csrf_token: str | None = None
        self.listing_html = ""
        self.listing_script_url: str | None = None
        self.listing_script_sha256: str | None = None
        self.draw = 0
        self.requested_ids: set[str] = set()
        # A company-owned runner may supply the two full detail pages already
        # archived during source research. Values are (HTML, request metadata).
        self.saved_detail_pages: dict[str, tuple[str, dict]] = {}
        self._research_reused_ids: list[str] = []
        self._network_detail_ids: list[str] = []

    async def _discover_entry_and_listing(self):
        if self.company.slug != "fastenal" or self.company.careers_url != ENTRY:
            raise ValueError("Fastenal dedicated flow must start at the assigned Fastenal Careers entry")
        started = time.perf_counter()
        entry = await self.http.get(ENTRY)
        if str(entry.url) != ENTRY:
            raise ValueError(f"Fastenal official careers entry redirected to {entry.url}")
        entry_soup = BeautifulSoup(entry.text, "lxml")
        published_links = []
        for anchor in entry_soup.select("a[href]"):
            label = _compact(anchor.get_text(" ", strip=True) or anchor.get("aria-label", ""))
            target = urljoin(ENTRY, anchor["href"])
            if label.casefold() == "search careers" and target == LISTING:
                published_links.append((label, target))
        if not published_links or {target for _, target in published_links} != {LISTING}:
            raise ValueError("Fastenal official Careers page must publish Search Careers links only to jobs.fastenal.com/jobs")

        listing = await self.http.get(LISTING)
        if str(listing.url) != LISTING:
            raise ValueError(f"Fastenal native listing redirected to {listing.url}")
        self.listing_html = listing.text
        soup = BeautifulSoup(self.listing_html, "lxml")
        table = soup.select_one("table#jobTable")
        if table is None:
            raise ValueError("Fastenal Jobs page no longer exposes the native #jobTable")
        form = soup.select_one("form#findJobsForm")
        if form is None or form.get("method", "").casefold() != "post":
            raise ValueError("Fastenal native Find Jobs form changed its POST behavior")
        advanced = soup.select_one(".advanced-search")
        if advanced is not None and not (
            "d-none" in (advanced.get("class") or [])
            or "display:none" in (advanced.get("style", "").replace(" ", "").casefold())
        ):
            raise ValueError("Fastenal advanced search is open; refusing to collect a filtered result set")
        self.csrf_header, self.csrf_token = _csrf(soup)

        script_tags = []
        for script in soup.select("script[src]"):
            script_url = urljoin(LISTING, script["src"])
            parsed = urlsplit(script_url)
            if parsed.hostname == "jobs.fastenal.com" and parsed.path.endswith("/" + SCRIPT_NAME):
                script_tags.append(script_url)
        if len(script_tags) != 1:
            raise ValueError(f"Fastenal listing must load exactly one {SCRIPT_NAME}; found {len(script_tags)}")
        self.listing_script_url = script_tags[0]
        script_response = await self.http.get(self.listing_script_url)
        if str(script_response.url) != self.listing_script_url:
            raise ValueError("Fastenal DataTables script redirected away from its published URL")
        script = script_response.text
        required = (
            "initJobTable:function()", "serverSide:true", 'url:"/load-jobs"',
            'type:"POST"', 'href="details/', "d.jobId+", 
        )
        missing = [value for value in required if value not in script]
        if missing:
            raise ValueError(f"Fastenal native listing script changed its observed request/detail flow: {missing}")
        self.listing_script_sha256 = sha256(script_response.content).hexdigest()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry": str(entry.url),
            "published_search_link": published_links[0][0],
            "published_search_url": published_links[0][1],
            "native_listing_url": str(listing.url),
            "listing_script_url": self.listing_script_url,
            "listing_script_sha256": self.listing_script_sha256,
            "listing_table": "#jobTable",
            "server_side": True,
            "selected_native_page_length": PAGE_SIZE,
            "ui_supported_page_lengths": list(SUPPORTED_PAGE_SIZES),
            "listing_request": {
                "method": "POST",
                "url": LOAD_JOBS,
                "csrf_header_name": self.csrf_header,
                "csrf_token_recorded": False,
                "session": "same HTTP session as GET /jobs; CSRF header/token read from that response",
                "unfiltered_form_template": _listing_form(1, 0),
                "form_parameters": list(_listing_form(1, 0)),
                "advanced_search": "closed; no query/filter/jobId parameters sent",
            },
            "detail_route": {
                "template": "https://jobs.fastenal.com/details/{jobId}",
                "basis": "current published DataTables script link template: details/{jobId}",
                "apply_route": "native Apply Now /careers/application/{jobId}",
            },
        })
        self.result.timings["official_entry_and_listing_shell_seconds"] = round(time.perf_counter() - started, 6)

    async def _listing_page(self, start: int, *, label: str, total_expected: int | None):
        if not self.page_guard():
            raise ValueError("Fastenal listing pagination budget exhausted")
        self.draw += 1
        form = _listing_form(self.draw, start)
        response = await self.http.request(
            "POST", LOAD_JOBS, data=form,
            headers={
                self.csrf_header: self.csrf_token,
                "Referer": LISTING,
                "Origin": JOBS_ORIGIN,
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        if str(response.url) != LOAD_JOBS:
            raise ValueError(f"Fastenal listing POST redirected to {response.url}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Fastenal listing page at start={start} returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError(f"Fastenal listing page at start={start} has an unknown response schema")
        total = payload.get("recordsTotal")
        filtered = payload.get("recordsFiltered")
        rows = payload["data"]
        if payload.get("draw") != self.draw:
            raise ValueError(f"Fastenal listing draw mismatch at start={start}: {payload.get('draw')!r}")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0 or filtered != total:
            raise ValueError(f"Fastenal unfiltered listing count is invalid at start={start}: {total!r}/{filtered!r}")
        if total_expected is not None and total != total_expected:
            raise ValueError(f"Fastenal listing total changed during {label}: {total_expected} -> {total}")
        expected_count = min(PAGE_SIZE, max(0, total - start))
        if len(rows) != expected_count:
            raise ValueError(f"Fastenal page start={start} returned {len(rows)} rows, expected {expected_count} of {total}")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Fastenal listing page start={start} contains a non-object row")
        if any(row.get("jobId") in (None, "") or not row.get("title") for row in rows):
            raise ValueError(f"Fastenal listing page start={start} contains a missing ID or title")
        response_hash = sha256(response.content).hexdigest()
        self.result.metadata.setdefault("listing_http_requests", []).append({
            "pass": label,
            "draw": self.draw,
            "start": start,
            "length": PAGE_SIZE,
            "form_without_csrf": form,
            "response_sha256": response_hash,
            "records_total": total,
            "records_filtered": filtered,
            "returned": len(rows),
        })
        return rows, total, response_hash

    async def _listing_snapshot(self, *, label: str):
        started = time.perf_counter()
        rows_all = []
        counts, page_hashes, response_hashes, starts, totals = [], [], [], [], []
        page_first_ids, page_last_ids = [], []
        expected_total = None
        start = 0
        while expected_total is None or start < expected_total:
            page_rows, total, response_hash = await self._listing_page(
                start, label=label, total_expected=expected_total,
            )
            if expected_total is None:
                expected_total = total
            if not page_rows and start < expected_total:
                raise ValueError(f"Fastenal listing stopped early at offset {start} of {expected_total}")
            rows_all.extend(page_rows)
            counts.append(len(page_rows))
            page_first_ids.append(str(page_rows[0]["jobId"]) if page_rows else None)
            page_last_ids.append(str(page_rows[-1]["jobId"]) if page_rows else None)
            page_hashes.append(_canonical_hash(page_rows))
            response_hashes.append(response_hash)
            starts.append(start)
            totals.append(total)
            start += PAGE_SIZE
        ids = [str(row["jobId"]) for row in rows_all]
        if len(ids) != len(set(ids)):
            duplicates = sorted({identity for identity in ids if ids.count(identity) > 1})
            raise ValueError(f"Fastenal {label} listing contains duplicate IDs: {duplicates[:10]}")
        expected_pages = math.ceil(expected_total / PAGE_SIZE) if expected_total else 0
        if len(rows_all) != expected_total or len(counts) != expected_pages:
            raise ValueError(f"Fastenal {label} listing failed to reconcile all {expected_total} rows")
        snapshot = {
            "name": label,
            "expected_total": expected_total,
            "page_size": PAGE_SIZE,
            "pages": len(counts),
            "page_counts": counts,
            "page_sha256": page_hashes,
            "response_sha256": response_hashes,
            "starts": starts,
            "page_first_ids": page_first_ids,
            "page_last_ids": page_last_ids,
            "ordered_ids": ids,
            "row_sha256": {str(row["jobId"]): _canonical_hash(row) for row in rows_all},
            "duration_seconds": round(time.perf_counter() - started, 6),
        }
        self.result.metadata.setdefault("listing_snapshots", []).append(snapshot)
        self.result.timings[f"{label}_listing_seconds"] = snapshot["duration_seconds"]
        return rows_all, snapshot

    def _new_job(self, row):
        identity = str(row["jobId"])
        url = _exact_detail_url(identity)
        job = self.job(
            {"listing": row, "source_ui": {"detail_link_relative": f"details/{identity}"}},
            title=str(row["title"]), job_id=identity, url=url,
            date_posted=None, date_posted_raw=None,
            locations=[{
                "location": None,
                "city": row.get("city"),
                "state": row.get("state"),
                "zip": row.get("zip"),
                "street_address": row.get("streetAddress"),
            }],
            department=row.get("department") or None,
        )
        job.record_identity = sha256(
            f"{self.company.name}|fastenal|{identity}|{url}|{row['title']}".encode("utf-8")
        ).hexdigest()
        return job

    def _apply_detail_html(self, job, html: str, *, detail_url: str, response_sha256: str,
                          fetched_at: str | None, reused_from_research: bool = False):
        if detail_url != job.url:
            raise ValueError(f"Fastenal detail source URL does not match exact listing link for {job.job_id}")
        parsed_url = urlsplit(detail_url)
        if parsed_url.hostname != "jobs.fastenal.com" or parsed_url.path != f"/details/{job.job_id}":
            raise ValueError(f"Fastenal detail left the exact native job route: {detail_url}")
        soup = BeautifulSoup(html, "lxml")
        pairs, fields = _detail_table(soup)
        table_id = fields["Job ID"]
        if table_id != str(job.job_id):
            raise ValueError(f"Fastenal detail table ID {table_id!r} differs from listing ID {job.job_id}")
        detail_title = fields["Title"]
        if detail_title != job.title:
            raise ValueError(f"Fastenal detail title differs from listing for {job.job_id}: {detail_title!r}")
        title_heading = _compact((soup.select_one("h2") or soup).get_text(" ", strip=True))
        if title_heading and title_heading != f"Position Details - {detail_title}":
            raise ValueError(f"Fastenal detail heading identity changed for {job.job_id}: {title_heading!r}")
        description = soup.select_one(".cms-job-description")
        if description is None:
            raise ValueError(f"Fastenal detail {job.job_id} has no complete .cms-job-description")
        description_html = description.decode_contents(formatter="html")
        description_text = text_html(description_html)
        if len(_compact(description_text)) < 100:
            raise ValueError(f"Fastenal detail {job.job_id} has an empty or truncated role body")

        apply_links = []
        apply_labels = []
        for anchor in soup.select("a[href]"):
            label = _compact(anchor.get_text(" ", strip=True))
            href = anchor["href"].strip()
            parsed_href = urlsplit(urljoin(detail_url, href))
            is_application_route = parsed_href.path.startswith("/careers/application/")
            if label.casefold() != "apply now" and not is_application_route:
                continue
            apply_labels.append(label)
            if parsed_href.hostname == "jobs.fastenal.com" and parsed_href.path == f"/careers/application/{job.job_id}":
                apply_links.append(parsed_href.geturl())
            else:
                raise ValueError(f"Fastenal detail {job.job_id} exposes a noncanonical Apply Now link: {parsed_href.geturl()}")
        if len(apply_links) > 1:
            raise ValueError(f"Fastenal detail {job.job_id} repeats its native Apply Now link")
        apply_url = apply_links[0] if apply_links else None

        listing = job.raw["listing"]
        detail_location = fields["Location"] or None
        location = {
            "location": detail_location,
            "city": listing.get("city"),
            "state": listing.get("state"),
            "zip": listing.get("zip"),
            "street_address": listing.get("streetAddress"),
        }
        posted_raw = fields["Date Published"] or None
        end_raw = fields["End Date"] or None
        posted = _date_iso(posted_raw, ("%B %d, %Y", "%b %d, %Y", "%m-%d-%Y", "%m/%d/%Y"))
        end_date = _date_iso(end_raw, ("%m-%d-%Y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"))
        if posted_raw and posted is None:
            self.result.errors.append(f"{job.job_id}:unparsed_source_date_published:{posted_raw}")
        if end_raw and end_date is None:
            self.result.errors.append(f"{job.job_id}:unparsed_source_end_date:{end_raw}")
        description_links = [
            {"url": urljoin(detail_url, anchor["href"].strip()),
             "text": _compact(anchor.get_text(" ", strip=True))}
            for anchor in description.select("a[href]")
        ]
        role_attachments = [link for link in description_links if ROLE_ATTACHMENT.search(link["url"])]
        raw_detail = {
            "table_fields": fields,
            "table_field_rows": pairs,
            "detail_heading": title_heading,
            "detail_url": detail_url,
            "apply_url": apply_url,
            "apply_link_status": "native_exact_link" if apply_url else "not_exposed_on_exact_detail_page",
            "apply_link_labels": apply_labels,
            "date_published_raw": posted_raw,
            "end_date_raw": end_raw,
            "detail_page_html_sha256": response_sha256,
            "detail_fetched_at": fetched_at,
            "reused_from_research_html": reused_from_research,
            "source_page_html_is_archived": True,
            "description_links": description_links,
            "role_bearing_attachment_links": role_attachments,
        }
        job.raw["detail_page"] = raw_detail
        job.raw["source_ui"].update({
            "apply_url": apply_url,
            "apply_link_status": "native_exact_link" if apply_url else "not_exposed_on_exact_detail_page",
            "description_page_selector": ".cms-job-description",
            "description_page_html_sha256": response_sha256,
            "description_links": description_links,
        })
        job.description_html = description_html
        job.description_text = description_text
        job.warnings = [warning for warning in job.warnings if warning != "detail_fetch_failed"]
        job.description_method = "fastenal.native_job_detail.cms-job-description"
        job.description_source_url = detail_url
        job.apply_url = apply_url
        job.requisition_id = None  # Fastenal exposes Job ID; no separate requisition value was published.
        job.locations = [location] if any(value for value in location.values()) else []
        job.department = fields["Departments"] or None
        job.employment_type = fields["Type"] or None
        job.workplace_type = None
        job.date_posted = posted
        job.date_posted_raw = posted_raw
        job.date_updated = None
        job.valid_through = end_date
        job.salary = {"source_text": _salary_text(description_text)} if _salary_text(description_text) else None
        job.employer = self.company.name if title_heading else None
        job.qualifications = None
        job.responsibilities = None
        job.education = None
        return job

    async def _detail(self, job):
        identity = str(job.job_id)
        if identity not in self.requested_ids:
            raise ValueError(f"Fastenal detail {identity} was not in the exact native listing")
        url = _exact_detail_url(identity)
        if url != job.url:
            raise ValueError(f"Fastenal detail URL changed for listing ID {identity}")
        if identity in self.saved_detail_pages:
            html, source = self.saved_detail_pages[identity]
            digest = sha256(html.encode("utf-8")).hexdigest()
            expected_digest = source.get("sha256")
            if expected_digest and expected_digest != digest:
                raise ValueError(f"Saved Fastenal research HTML digest changed for {identity}")
            self._research_reused_ids.append(identity)
            return self._apply_detail_html(
                job, html, detail_url=url, response_sha256=digest,
                fetched_at=source.get("fetched_at"), reused_from_research=True,
            )
        response = await self.http.get(url)
        if str(response.url) != url:
            raise ValueError(f"Fastenal exact detail page redirected for {identity}: {response.url}")
        self._network_detail_ids.append(identity)
        return self._apply_detail_html(
            job, response.text, detail_url=url,
            response_sha256=sha256(response.content).hexdigest(),
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

    async def run(self):
        await self._discover_entry_and_listing()
        initial_rows, initial = await self._listing_snapshot(label="initial")
        self.result.expected_total = initial["expected_total"]
        self.result.metadata.update({
            "expected_total": initial["expected_total"],
            "initial_listing_ids": initial["ordered_ids"],
            "initial_listing_first_row": initial_rows[0] if initial_rows else None,
            "initial_listing_last_row": initial_rows[-1] if initial_rows else None,
        })
        self.requested_ids = set(initial["ordered_ids"])
        if self.options.max_jobs and len(initial_rows) > self.options.max_jobs:
            self.result.warnings.append("sample_limit")
            selected_rows = initial_rows[:self.options.max_jobs]
            self.result.listing_complete = False
        else:
            selected_rows = initial_rows
        jobs = [self._new_job(row) for row in selected_rows]
        self.result.jobs.extend(jobs)
        self.result.dirty_jobs.extend(jobs)
        # The runner checkpoints the complete first membership before it begins
        # detail hydration, and can report the true terminal row to the parent.
        if self.options.checkpoint:
            self.options.checkpoint(self.result)
        if self.options.max_jobs and len(initial_rows) > self.options.max_jobs:
            self.requested_ids = {str(job.job_id) for job in jobs}
        self.result.jobs = await self.hydrate(self.result.jobs, self._detail)
        self.result.metadata["research_detail_html_reused_ids"] = list(self._research_reused_ids)
        self.result.metadata["network_detail_ids"] = list(self._network_detail_ids)
        self.result.metadata["network_detail_request_count"] = len(self._network_detail_ids)
        self.result.metadata["detail_pages_without_native_apply_cta"] = [
            str(job.job_id) for job in self.result.jobs if not job.apply_url
        ]
        if "sample_limit" not in self.result.warnings:
            final_rows, final = await self._listing_snapshot(label="final")
            initial_by_id = {str(row["jobId"]): row for row in initial_rows}
            final_by_id = {str(row["jobId"]): row for row in final_rows}
            self.result.metadata.update({
                "final_listing_ids": final["ordered_ids"],
                "final_listing_first_row": final_rows[0] if final_rows else None,
                "final_listing_last_row": final_rows[-1] if final_rows else None,
            })
            if initial["ordered_ids"] != final["ordered_ids"]:
                raise ValueError("Fastenal initial and final ordered listing IDs differ")
            if initial_by_id != final_by_id:
                raise ValueError("Fastenal initial and final listing full fields differ")
            if initial["expected_total"] != final["expected_total"]:
                raise ValueError("Fastenal initial and final publisher totals differ")
            self.result.listing_complete = True
        listing_seconds = (
            self.result.timings.get("initial_listing_seconds", 0.0)
            + self.result.timings.get("final_listing_seconds", 0.0)
        )
        shell_seconds = self.result.timings.get("official_entry_and_listing_shell_seconds", 0.0)
        self.result.timings["source_and_company_collection_seconds"] = round(shell_seconds + listing_seconds, 6)
        if self.result.errors:
            self.result.listing_complete = False
        return self.result
