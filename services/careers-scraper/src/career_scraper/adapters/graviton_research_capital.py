"""Graviton's own iframe and first-party generated open-positions JSON feed."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.gravitonresearch.com/careers"
OFFICIAL_FINAL = "https://www.gravitontrading.com/careers"
IFRAME_URL = "https://www.gravitontrading.com/greenhouse-embed-local.html"
IFRAME_SCRIPT_URL = "https://www.gravitontrading.com/javascripts/greenhouse-embed-local.js"
DATA_URL = "https://www.gravitontrading.com/data/greenhouse_jobs.json"
BOARD_URL = "https://boards.greenhouse.io/embed/job_board?for=gravitonresearchcapital"
DETAIL_HOST = "job-boards.greenhouse.io"
DETAIL_PATH = re.compile(r"^/gravitonresearchcapital/jobs/(\d+)$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def rendered_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _split_locations(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(";") if part.strip()]


def _description_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(f'<div data-graviton-description="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-graviton-description]")

    def walk(node):
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            if child.name in HEADING_TAGS or child.name in {"p", "ul", "ol", "blockquote"}:
                yield child
            else:
                yield from walk(child)

    blocks = list(walk(root)) if root else []
    sections, current = [], None
    for block in blocks:
        heading = block.get_text(" ", strip=True) if block.name in HEADING_TAGS else ""
        if block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text
        if heading:
            if current is not None:
                current["text"] = text_html(current["html"])
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
        else:
            if current is None:
                current = {"heading": None, "html": "", "text": ""}
            current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    return sections


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    if response_url.rstrip("/") != OFFICIAL_FINAL.rstrip("/"):
        raise ValueError("Graviton official careers entry redirected away from the inspected final page")
    soup = BeautifulSoup(html, "lxml")
    iframe = soup.select_one("iframe#grnhse_iframe")
    if (iframe is None or iframe.get("title") != "Current job openings"
            or urljoin(response_url, iframe.get("src", "")) != IFRAME_URL):
        raise ValueError("Graviton official careers page no longer embeds the inspected openings iframe")
    return {
        "entry_url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "iframe_id": iframe.get("id"),
        "iframe_title": iframe.get("title"),
        "iframe_url": urljoin(response_url, iframe["src"]),
        "official_entry_title": soup.title.get_text(" ", strip=True) if soup.title else None,
    }


def parse_iframe_page(html: str, response_url: str = IFRAME_URL) -> dict:
    if response_url.rstrip("/") != IFRAME_URL.rstrip("/"):
        raise ValueError("Graviton openings iframe redirected away from the company-owned page")
    soup = BeautifulSoup(html, "lxml")
    if (not soup.title or soup.title.get_text(" ", strip=True) != "Current Job Openings"
            or not any(normalized_title(h.get_text(" ", strip=True)) == "Current Job Openings"
                       for h in soup.select("h1,h2,h3"))):
        raise ValueError("Graviton company openings page heading changed")
    scripts = [urljoin(response_url, script.get("src", "")) for script in soup.select("script[src]")]
    if IFRAME_SCRIPT_URL not in scripts:
        raise ValueError("Graviton iframe no longer loads its inspected first-party renderer script")
    ifs = soup.select("#departments-select, #offices-select")
    if len(ifs) != 2:
        raise ValueError("Graviton local iframe no longer exposes the department and office filters")
    return {
        "url": response_url,
        "title": soup.title.get_text(" ", strip=True),
        "employer_heading": soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else None,
        "script_url": IFRAME_SCRIPT_URL,
        "filters": [node.get("id") for node in ifs],
        "initial_filter_values": {
            node.get("id"): [option.get_text(" ", strip=True) for option in node.select("option")]
            for node in ifs
        },
    }


def parse_company_json(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Graviton first-party job JSON is not an object")
    board, source, jobs, counts = (value.get(key) for key in ("board", "source", "jobs", "counts"))
    if (not isinstance(board, dict) or board.get("name") != "Graviton Research Capital LLP"
            or board.get("title") != "Current openings at Graviton Research Capital LLP"
            or not isinstance(source, dict) or source.get("board_url") != BOARD_URL
            or not isinstance(jobs, list) or not isinstance(counts, dict)):
        raise ValueError("Graviton first-party JSON no longer matches its company board identity/schema")
    if (counts.get("jobs") != len(jobs) or counts.get("details_fetched") != len(jobs)
            or counts.get("details_errors") != 0):
        raise ValueError("Graviton first-party JSON count/detail-fetch report is incomplete")
    departments, offices = board.get("departments"), board.get("offices")
    sections = board.get("sections")
    if not isinstance(departments, dict) or not isinstance(offices, dict) or not isinstance(sections, list):
        raise ValueError("Graviton first-party board maps or department sections are missing")
    section_ids = [str(job_id) for section in sections for job_id in section.get("gh_jids", [])]
    job_ids, rows, issues = [], [], []
    for row in jobs:
        details = row.get("details")
        identity = str(row.get("gh_jid", ""))
        url = row.get("url")
        parts = urlsplit(url or "")
        match = DETAIL_PATH.fullmatch(parts.path)
        if (not re.fullmatch(r"\d+", identity) or match is None or match[1] != identity
                or parts.scheme != "https" or parts.netloc != DETAIL_HOST or parts.query or parts.fragment):
            issues.append(f"graviton_row_missing_exact_company_job_url:{identity or 'missing-id'}")
            continue
        if identity in job_ids:
            issues.append(f"graviton_duplicate_job_id:{identity}")
            continue
        if not normalized_title(row.get("title")) or not isinstance(details, dict):
            issues.append(f"graviton_row_identity_or_details_missing:{identity}")
            continue
        detail_id = str(details.get("gh_jid", ""))
        if (detail_id != identity or details.get("url") != url
                or normalized_title(details.get("title")) != normalized_title(row.get("title"))):
            issues.append(f"graviton_detail_identity_mismatch:{identity}")
        description_html = details.get("description_html")
        description_text = details.get("description_text")
        if (not isinstance(description_html, str) or len(rendered_text(description_html)) < 100
                or not isinstance(description_text, str) or len(rendered_text(description_text)) < 100):
            issues.append(f"graviton_full_detail_missing:{identity}")
        elif rendered_text(description_text) != rendered_text(description_html):
            issues.append(f"graviton_description_text_html_mismatch:{identity}")
        if row.get("department_id") is not None:
            department_id = str(row["department_id"])
            if department_id not in departments or departments[department_id] != row.get("department"):
                issues.append(f"graviton_department_map_mismatch:{identity}")
        if row.get("office_id") is not None:
            office_id = str(row["office_id"])
            if office_id not in offices or offices[office_id] != row.get("office"):
                issues.append(f"graviton_office_map_mismatch:{identity}")
        row_links = row.get("source", {}).get("board_url") if isinstance(row.get("source"), dict) else None
        if row_links != BOARD_URL:
            issues.append(f"graviton_job_row_source_board_mismatch:{identity}")
        job_ids.append(identity)
        rows.append(dict(row))
    if sorted(section_ids) != sorted(job_ids) or len(section_ids) != len(set(section_ids)):
        issues.append("graviton_board_sections_do_not_reconcile_to_job_ids")
    return {
        "rows": rows,
        "job_ids": job_ids,
        "count": len(rows),
        "source_scraped_at": value.get("scraped_at"),
        "source_board_url": source.get("board_url"),
        "board": board,
        "counts": counts,
        "issues": list(dict.fromkeys(issues)),
    }


def parse_detail_reference(html: str, row: dict):
    identity = str(row.get("gh_jid", ""))
    detail = row.get("details", {})
    url = row.get("url")
    parts = urlsplit(url or "")
    match = DETAIL_PATH.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.netloc != DETAIL_HOST or match is None
            or match[1] != identity or parts.query or parts.fragment):
        raise ValueError("Graviton representative detail is not the exact first-party-list URL")
    soup = BeautifulSoup(html, "lxml")
    heading = soup.select_one("main .job-post h1") or soup.select_one("h1")
    body = soup.select_one("main .job__description") or soup.select_one(".job__description")
    actual_title = normalized_title(heading.get_text(" ", strip=True)) if heading else ""
    api_title = normalized_title(detail.get("title"))
    if not heading or actual_title != api_title or not body:
        raise ValueError(f"Graviton exact detail title/full body missing: {identity}")
    expected_text = rendered_text(detail.get("description_html"))
    page_text = rendered_text(str(body))
    if page_text != expected_text:
        raise ValueError(f"Graviton exact detail body differs from first-party JSON: {identity}")
    form = soup.select_one("form#application-form")
    apply_button = next((button for button in soup.select("button")
                         if "apply" in button.get_text(" ", strip=True).lower()), None)
    form_action = form.get("action") if form else None
    expected_action = parts.path
    if form_action and form_action.rstrip("/") != expected_action.rstrip("/"):
        raise ValueError(f"Graviton exact detail application form action changed: {identity}")
    return {
        "url": url,
        "id": identity,
        "title": actual_title,
        "description_html": str(body),
        "description_chars": len(page_text),
        "description_fnv1a64": fnv1a64(page_text),
        "description_matches_first_party_json": True,
        "apply_control": apply_button.get_text(" ", strip=True) if apply_button else None,
        "form_method": form.get("method") if form else None,
        "application_form_action": form_action,
        "apply_url": url if apply_button and form_action else None,
        "date_fields": {"date_posted": detail.get("jobposting_jsonld", {}).get("datePosted") if isinstance(detail.get("jobposting_jsonld"), dict) else None,
                        "valid_through": detail.get("jobposting_jsonld", {}).get("validThrough") if isinstance(detail.get("jobposting_jsonld"), dict) else None},
    }


class GravitonResearchCapital(Adapter):
    """Use the exact first-party JSON file rendered by Graviton's official iframe."""

    VERIFICATION_REVISION = "graviton.first-party-local-greenhouse-json.1"

    def __init__(self, http, company, source, options, *, initial_capture: dict | None = None):
        super().__init__(http, company, source, options)
        self.initial_capture = initial_capture

    async def _get_response(self, url: str):
        started = time.perf_counter()
        response = await self.http.get(url)
        duration = max(0.0, time.perf_counter() - started)
        return response, duration

    async def _fresh_snapshot(self):
        entry_response, entry_seconds = await self._get_response(OFFICIAL_ENTRY)
        if urlsplit(str(entry_response.url)).scheme != "https" or str(entry_response.url).rstrip("/") != OFFICIAL_FINAL.rstrip("/"):
            raise ValueError("Graviton official entry did not reach the inspected careers page")
        entry = parse_official_entry(entry_response.text, str(entry_response.url))
        entry.update({"status": entry_response.status_code, "seconds": entry_seconds,
                      "response_bytes": len(entry_response.content), "response_sha256": sha256(entry_response.content).hexdigest()})

        iframe_response, iframe_seconds = await self._get_response(IFRAME_URL)
        if str(iframe_response.url) != IFRAME_URL:
            raise ValueError("Graviton exact iframe route redirected away from the official company page")
        iframe = parse_iframe_page(iframe_response.text, str(iframe_response.url))
        iframe.update({"status": iframe_response.status_code, "seconds": iframe_seconds,
                       "response_bytes": len(iframe_response.content), "response_sha256": sha256(iframe_response.content).hexdigest()})

        if not self.page_guard():
            raise ValueError("Graviton first-party JSON page budget exhausted")
        data_url = urljoin(IFRAME_URL, "data/greenhouse_jobs.json")
        data_response, data_seconds = await self._get_response(data_url)
        if str(data_response.url) != DATA_URL:
            raise ValueError("Graviton first-party jobs data request left the exact iframe publisher path")
        try:
            parsed = parse_company_json(data_response.json())
        except ValueError as exc:
            raise ValueError(f"Graviton first-party jobs JSON failed validation: {exc}") from exc
        parsed.update({"request_url": data_url, "final_url": str(data_response.url),
                       "status": data_response.status_code, "seconds": data_seconds,
                       "response_bytes": len(data_response.content), "response_sha256": sha256(data_response.content).hexdigest(),
                       "reused_source_capture": False})
        return entry, iframe, parsed, data_response.json()

    def _reuse_snapshot(self):
        capture = self.initial_capture
        entry = parse_official_entry(capture["official_html"], capture["entry_metadata"]["final_url"])
        entry.update(capture["entry_metadata"])
        iframe = parse_iframe_page(capture["iframe_html"], capture["iframe_metadata"]["final_url"])
        iframe.update(capture["iframe_metadata"])
        data = capture["json"]
        parsed = parse_company_json(data)
        parsed.update(capture["data_metadata"])
        parsed["reused_source_capture"] = True
        return entry, iframe, parsed, data

    def _make_job(self, row, source_url):
        identity = str(row["gh_jid"])
        detail = row["details"]
        description_html = detail["description_html"]
        description_text = detail["description_text"]
        sections = _description_sections(description_html)
        responsibility_terms = ("responsibilit", "what you will do", "what you’ll do", "what you'll do")
        qualification_terms = ("requirement", "qualif", "what you will need", "who you are", "experience")
        responsibilities = [s for s in sections if s["heading"] and any(t in s["heading"].lower() for t in responsibility_terms)]
        qualifications = [s for s in sections if s["heading"] and any(t in s["heading"].lower() for t in qualification_terms)]
        soup = BeautifulSoup(description_html, "lxml")
        description_links = [{"text": a.get_text(" ", strip=True), "url": a.get("href")}
                             for a in soup.select("a[href]")]
        attachments = [link for link in description_links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
        ld = detail.get("jobposting_jsonld")
        date_posted = ld.get("datePosted") or None if isinstance(ld, dict) else None
        valid_through = ld.get("validThrough") or None if isinstance(ld, dict) else None
        form_action = urljoin(row["url"], detail.get("container_data_attrs", {}).get("action", "")) if isinstance(detail.get("container_data_attrs"), dict) else None
        apply_url = detail.get("apply_url") or row["url"]
        job = self.job(
            {"listing": dict(row), "first_party_board": self.result.metadata.get("board")},
            title=normalized_title(row["title"]),
            url=row["url"],
            job_id=identity,
            requisition_id=None,
            apply_url=apply_url,
            description=description_html,
            method="graviton.first_party_greenhouse_json.details.description_html",
            description_url=source_url,
            locations=_split_locations(row.get("location_text")),
            department=row.get("department"),
            employment_type=None,
            workplace_type=None,
            date_posted=date_posted,
            date_posted_raw=date_posted,
            valid_through=valid_through,
            employer="Graviton Research Capital LLP",
            language="en",
            responsibilities=responsibilities or None,
            qualifications=qualifications or None,
        )
        job.description_text = description_text
        job.company_scope_verified = self.company.scope == "company"
        job.raw["detail"] = {
            **detail,
            "description_sections": sections,
            "description_links": description_links,
            "role_attachments": attachments,
            "description_sha256": sha256(description_html.encode()).hexdigest(),
            "normalized_description_chars": len(rendered_text(description_html)),
            "normalized_description_fnv1a64": fnv1a64(rendered_text(description_html)),
            "source_data_url": source_url,
            "application_form_action_source": form_action,
        }
        return job

    async def run(self):
        started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_final_url": OFFICIAL_FINAL,
            "first_party_iframe_url": IFRAME_URL,
            "first_party_json_url": DATA_URL,
            "listing_authority": "Exact first-party JSON file fetched by the JavaScript loaded in Graviton's official embedded job board.",
            "membership_request_method": "GET same-origin data/greenhouse_jobs.json from the published iframe script.",
            "request_scope": "Only the current rows and exact job URLs in the first-party Graviton JSON; no Greenhouse listing/API enumeration.",
        })
        try:
            if self.initial_capture is None:
                initial_entry, initial_iframe, initial, _initial_data = await self._fresh_snapshot()
            else:
                if not self.page_guard():
                    raise ValueError("Graviton reused initial JSON page budget exhausted")
                initial_entry, initial_iframe, initial, _initial_data = self._reuse_snapshot()
        except Exception as exc:
            self.result.errors.append(f"Graviton source capture failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
            return self.result

        self.result.expected_total = initial["counts"].get("jobs")
        self.result.metadata["initial_official_entry"] = initial_entry
        self.result.metadata["initial_iframe"] = initial_iframe
        self.result.metadata["initial_first_party_json"] = {k: v for k, v in initial.items() if k != "rows"}
        self.result.metadata["filter_options"] = {
            "departments": initial["board"].get("departments", {}),
            "offices": initial["board"].get("offices", {}),
            "filters": initial["board"].get("filters", {}),
        }
        self.result.metadata["source_scraped_at"] = initial.get("source_scraped_at")
        self.result.metadata["source_detail_capture_times"] = {
            str(row["gh_jid"]): row["details"].get("fetched_at") for row in initial["rows"]
        }
        if initial["issues"]:
            self.result.errors.extend(initial["issues"])

        jobs = []
        initial_by_id = {str(row["gh_jid"]): row for row in initial["rows"]}
        detail_started = time.perf_counter()
        for row in initial["rows"]:
            identity = str(row["gh_jid"])
            job = self._make_job(row, DATA_URL)
            if job.raw["detail"]["role_attachments"]:
                self.result.errors.append(f"graviton_role_attachments_require_review:{identity}")
            if job.raw["detail"]["normalized_description_chars"] < 100:
                self.result.errors.append(f"graviton_description_too_short:{identity}")
            jobs.append(job)
        detail_parse_seconds = max(0.0, time.perf_counter() - detail_started)

        try:
            final_entry, final_iframe, final, _final_data = await self._fresh_snapshot()
        except Exception as exc:
            self.result.errors.append(f"Graviton final source recheck failed: {type(exc).__name__}: {exc}")
            final_entry = final_iframe = final = None

        if final is None or final_entry is None or final_iframe is None:
            final_matches = False
        else:
            initial_ids = initial["job_ids"]
            final_ids = final["job_ids"]
            final_by_id = {str(row["gh_jid"]): row for row in final["rows"]}
            changed_fields = {}
            for identity in initial_ids:
                if identity in final_by_id:
                    before = deepcopy(initial_by_id[identity])
                    after = deepcopy(final_by_id[identity])
                    before.get("details", {}).pop("fetched_at", None)
                    after.get("details", {}).pop("fetched_at", None)
                    if before != after:
                        changed_fields[identity] = ["source_job_row_or_detail_changed"]
            same_ids = initial_ids == final_ids
            same_count = initial["counts"].get("jobs") == final["counts"].get("jobs")
            same_board = (initial["board"] == final["board"]
                          and initial["source_board_url"] == final["source_board_url"])
            entry_same = final_entry["iframe_url"] == initial_entry["iframe_url"] == IFRAME_URL
            iframe_same = final_iframe["script_url"] == initial_iframe["script_url"] == IFRAME_SCRIPT_URL
            final_matches = same_ids and same_count and same_board and entry_same and iframe_same and not changed_fields and not final["issues"]
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_iframe"] = final_iframe
            self.result.metadata["final_first_party_json"] = {k: v for k, v in final.items() if k != "rows"}
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial_ids,
                "final_ids": final_ids,
                "initial_count": initial["counts"].get("jobs"),
                "final_count": final["counts"].get("jobs"),
                "initial_pages": 1,
                "final_pages": 1,
                "same_ids_and_order": same_ids,
                "counts_match": same_count,
                "all_source_fields_except_capture_timestamps_match": not changed_fields,
                "source_capture_time_changes_by_id_not_treated_as_job_dates": sorted(
                    identity for identity in initial_ids if initial_by_id.get(identity, {}).get("details", {}).get("fetched_at")
                    != final_by_id.get(identity, {}).get("details", {}).get("fetched_at")
                ),
                "changed_fields_by_job_id": changed_fields,
                "new_job_ids": [x for x in final_ids if x not in initial_ids],
                "removed_job_ids": [x for x in initial_ids if x not in final_ids],
                "board_filters_and_sections_same": same_board,
                "official_iframe_same": entry_same and iframe_same,
                "complete": final_matches,
            }
            if not same_ids or not same_count:
                self.result.errors.append("graviton_first_party_listing_membership_changed_during_collection")
            if changed_fields:
                self.result.errors.append("graviton_first_party_job_fields_changed_during_collection")
            if not same_board or not entry_same or not iframe_same:
                self.result.errors.append("graviton_official_careers_journey_changed_during_collection")
            self.result.errors.extend(final["issues"])

        self.result.jobs = jobs
        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.metadata["retained_total"] = len(jobs)
        self.result.metadata["current_native_membership_reconciled"] = final_matches
        self.result.listing_complete = bool(final_matches and len(jobs) == initial["counts"].get("jobs"))
        self.result.timings["source_data_parse_seconds"] = detail_parse_seconds
        self.result.timings["detail_phase_seconds"] = detail_parse_seconds
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
        return self.result
