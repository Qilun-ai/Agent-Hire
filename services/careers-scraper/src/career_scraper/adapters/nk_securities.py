"""NK Securities' own open-positions page and the exact Greenhouse board it requests.

The company page publishes one inline careers renderer. That renderer calls
``api.greenhouse.io/v1/boards/nksecuritiesresearch`` for departments and for the
complete content-bearing job rows, then renders all returned postings in one
unfiltered list. Membership therefore comes from that company-scoped board, not
from a guessed board slug or a standalone ATS search. The exact published job
pages are fetched individually to verify every stored description.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
from html import unescape
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter


OFFICIAL_ENTRY = "https://www.nksecurities.com/open-positions.html"
BOARD_API = "https://api.greenhouse.io/v1/boards/nksecuritiesresearch"
JOBS_API = BOARD_API + "/jobs?content=true"
DEPARTMENTS_API = BOARD_API + "/departments"
DETAIL_HOST = "job-boards.eu.greenhouse.io"
DETAIL_PATH = re.compile(r"^/nksecuritiesresearch/jobs/(\d+)$")
BOARD_TOKEN = "nksecuritiesresearch"
COMPANY_NAME = "NK Securities Research"
PAGE_TITLE = "Open Positions - NK Securities Research"
VERIFICATION_REVISION = "nk-securities.official-open-positions.greenhouse-board.1"
DEPARTMENT_PREFIX = re.compile(r"^\s*\d+\.\s*")
BLOCK_TAGS = {"p", "div", "section", "article", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}
UI_IDS = ("gh-jobs-count", "gh-jobs-board", "ghSearch", "ghLocation", "ghDepartments", "ghLevels")
ESCAPED_WRAPPER = re.compile(
    r"^\s*&(?:amp;)*lt;(?:p|div|section|article|ul|ol|h[1-6])\b", re.I)


def normalized(value: str | None) -> str:
    """Visible-text/normalised identity form used for comparisons and exports."""
    value = unicodedata.normalize("NFC", value or "")
    return re.sub(r"\s+", " ", value).strip()


def compact_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return normalized(soup.get_text(" ", strip=True))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def decode_content(value: str) -> str:
    """Unescape exactly the escaped HTML wrapper emitted by the board payload.

    The whole description HTML is escaped once by Greenhouse. Unescaping only
    while the complete value still starts with an escaped block wrapper keeps
    literal code such as ``&lt;T&gt;`` from turning into markup.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("NK Securities Greenhouse content is missing")
    for _ in range(2):
        if not ESCAPED_WRAPPER.match(value):
            break
        value = unescape(value)
    soup = BeautifulSoup(value, "lxml")
    if not any(tag.name in BLOCK_TAGS for tag in soup.find_all(True)):
        raise ValueError("NK Securities full description HTML wrapper is missing")
    if not normalized(soup.get_text(" ", strip=True)):
        raise ValueError("NK Securities full description body is empty")
    return value


def department_label(name: str) -> str:
    return DEPARTMENT_PREFIX.sub("", name or "").strip()


def natural_key(value: str):
    return tuple(int(part) if part.isdigit() else part.casefold()
                 for part in re.split(r"(\d+)", value or ""))


def _url_identity(value: str) -> tuple[str, str]:
    parts = urlsplit(value or "")
    match = DETAIL_PATH.fullmatch(parts.path)
    if (parts.scheme != "https" or parts.netloc != DETAIL_HOST
            or parts.query or parts.fragment or match is None):
        raise ValueError(f"NK Securities published job URL is not its exact company board detail: {value}")
    return parts.path, match.group(1)


def _iso(value, field: str, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", value):
        raise ValueError(f"NK Securities {field} is not an ISO-8601 source timestamp: {value!r}")
    return value


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "www.nksecurities.com"
            or parts.path != "/open-positions.html" or parts.query or parts.fragment):
        raise ValueError("NK Securities official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != PAGE_TITLE:
        raise ValueError("NK Securities official open-positions title changed")
    canonical = [normalized(link.get("href")) for link in soup.select('link[rel="canonical"]')]
    if canonical != [OFFICIAL_ENTRY]:
        raise ValueError("NK Securities official open-positions canonical URL changed")
    inline = [node.get_text() for node in soup.select("script:not([src])")
              if "greenhouse" in node.get_text().lower()]
    if len(inline) != 1:
        raise ValueError("NK Securities official page no longer publishes exactly one Greenhouse renderer")
    script = inline[0]
    match = re.search(r"BOARD_API\s*=\s*'([^']+)'", script)
    if match is None or match.group(1) != BOARD_API:
        raise ValueError("NK Securities official renderer no longer requests the inspected board API")
    if html.count("api.greenhouse.io") != 1:
        raise ValueError("NK Securities official page references an unexpected number of board APIs")
    if "jobs?content=true" not in script or "/departments" not in script:
        raise ValueError("NK Securities official renderer request shape changed")
    missing = [value for value in UI_IDS if soup.find(id=value) is None]
    if missing:
        raise ValueError(f"NK Securities official listing controls are missing: {missing}")
    text = normalized(soup.get_text(" ", strip=True))
    if "CURRENT OPENINGS" not in text or "All Locations" not in text:
        raise ValueError("NK Securities official open-positions listing heading/filter changed")
    return {
        "entry_url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "page_title": title,
        "canonical_url": canonical[0],
        "board_api": match.group(1),
        "jobs_api": JOBS_API,
        "departments_api": DEPARTMENTS_API,
        "ui_ids": list(UI_IDS),
        "filter_controls": ["search", "location", "departments", "levels"],
        "renderer_sha256": sha256(script.encode()).hexdigest(),
        "meta_description": next((normalized(node.get("content")) for node in soup.select('meta[name="description"]')), None),
    }


def _validate_board_row(row: dict, *, require_content: bool, source: str) -> dict:
    if not isinstance(row, dict):
        raise ValueError(f"NK Securities {source} job row is not an object")
    identity = row.get("id")
    if not isinstance(identity, int) or identity <= 0:
        raise ValueError(f"NK Securities {source} job id is missing/invalid")
    url = row.get("absolute_url")
    _, url_id = _url_identity(url)
    if url_id != str(identity):
        raise ValueError(f"NK Securities {source} job {identity} URL id disagrees with its id")
    title = normalized(row.get("title"))
    if not title:
        raise ValueError(f"NK Securities {source} job {identity} has no title")
    if row.get("company_name") != COMPANY_NAME:
        raise ValueError(f"NK Securities {source} job {identity} has a foreign employer")
    location = row.get("location")
    if not isinstance(location, dict) or not normalized(location.get("name")):
        raise ValueError(f"NK Securities {source} job {identity} has no location")
    _iso(row.get("first_published"), "first_published", nullable=True)
    _iso(row.get("updated_at"), "updated_at", nullable=True)
    _iso(row.get("application_deadline"), "application_deadline", nullable=True)
    if row.get("metadata") is not None and not isinstance(row.get("metadata"), (dict, list)):
        raise ValueError(f"NK Securities {source} job {identity} metadata is not an object/list")
    if row.get("requisition_id") is not None and not isinstance(row.get("requisition_id"), str):
        raise ValueError(f"NK Securities {source} job {identity} requisition id is not a source string")
    if row.get("internal_job_id") is not None and not isinstance(row.get("internal_job_id"), int):
        raise ValueError(f"NK Securities {source} job {identity} internal job id is not an integer")
    if row.get("language") is not None and not isinstance(row.get("language"), str):
        raise ValueError(f"NK Securities {source} job {identity} language is not a string")
    if require_content:
        html = decode_content(row.get("content"))
        if len(compact_text(html)) < 100:
            raise ValueError(f"NK Securities {source} job {identity} full description is empty/short")
        if not isinstance(row.get("departments"), list) or not row["departments"]:
            raise ValueError(f"NK Securities {source} job {identity} has no department membership")
        if not isinstance(row.get("offices"), list) or not row["offices"]:
            raise ValueError(f"NK Securities {source} job {identity} has no office/legal-employer row")
        for department in row["departments"]:
            if (not isinstance(department, dict) or not isinstance(department.get("id"), int)
                    or not normalized(department.get("name"))):
                raise ValueError(f"NK Securities {source} job {identity} department row is invalid")
        for office in row["offices"]:
            if (not isinstance(office, dict) or not normalized(office.get("name"))
                    or not normalized(office.get("location"))):
                raise ValueError(f"NK Securities {source} job {identity} office row is invalid")
    return row


def parse_board_jobs(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("NK Securities company board jobs response is not an object")
    rows = payload.get("jobs")
    meta = payload.get("meta")
    if not isinstance(rows, list) or not rows:
        raise ValueError("NK Securities company board jobs response has no job rows")
    if not isinstance(meta, dict) or meta.get("total") != len(rows):
        raise ValueError("NK Securities company board total does not reconcile to its job rows")
    seen, issues, ordered = set(), [], []
    for row in rows:
        _validate_board_row(row, require_content=True, source="full-content")
        identity = row["id"]
        if identity in seen:
            raise ValueError(f"NK Securities company board repeats job id {identity}")
        seen.add(identity)
        ordered.append(deepcopy(row))
    return {
        "rows": ordered,
        "job_ids": [row["id"] for row in ordered],
        "count": len(ordered),
        "total": meta["total"],
        "issues": issues,
    }


def parse_board_departments(payload: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("departments"), list):
        raise ValueError("NK Securities company board departments response is not an object list")
    departments, job_ids, seen, hidden = [], [], set(), []
    for item in payload["departments"]:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), int)
                or not normalized(item.get("name")) or not isinstance(item.get("jobs"), list)):
            raise ValueError("NK Securities company board department row is invalid")
        jobs = []
        for job in item["jobs"]:
            _validate_board_row(job, require_content=False, source="department")
            identity = job["id"]
            if identity in seen:
                raise ValueError(f"NK Securities company board department repeats job id {identity}")
            seen.add(identity)
            jobs.append(deepcopy(job))
            job_ids.append(identity)
        label = department_label(item["name"])
        if not label:
            raise ValueError(f"NK Securities company board department label is empty: {item['name']!r}")
        entry = {"id": item["id"], "name": normalized(item["name"]), "label": label,
                 "job_ids": [job["id"] for job in jobs], "jobs": jobs}
        departments.append(entry)
        if not jobs:
            hidden.append(entry["name"])
    return {
        "departments": departments,
        "visible_departments": [entry for entry in departments if entry["job_ids"]],
        "hidden_empty_departments": hidden,
        "job_ids": job_ids,
        "count": len(job_ids),
    }


def reconcile_membership(jobs: dict, departments: dict) -> dict:
    if sorted(jobs["job_ids"]) != sorted(departments["job_ids"]):
        missing = sorted(set(jobs["job_ids"]) - set(departments["job_ids"]))
        extra = sorted(set(departments["job_ids"]) - set(jobs["job_ids"]))
        raise ValueError(f"NK Securities board job/department membership mismatch: missing={missing} extra={extra}")
    by_id = {row["id"]: row for row in jobs["rows"]}
    for department in departments["departments"]:
        for listed in department["jobs"]:
            row = by_id[listed["id"]]
            for field in ("title", "absolute_url", "company_name", "requisition_id",
                          "internal_job_id", "first_published", "updated_at",
                          "application_deadline", "language"):
                if listed.get(field) != row.get(field):
                    raise ValueError(
                        f"NK Securities department job {row['id']} field {field} disagrees with the full board")
            if listed["location"] != row["location"]:
                raise ValueError(f"NK Securities department job {row['id']} location disagrees with the full board")
    # The page shows only non-empty departments, sorted numerically by name.
    visible = sorted(departments["visible_departments"], key=lambda item: natural_key(item["name"]))
    order, seen = [], set()
    for department in visible:
        for listed in sorted(department["jobs"], key=lambda item: normalized(item["title"])):
            if listed["id"] in seen:
                continue
            seen.add(listed["id"])
            order.append(listed["id"])
    if len(order) != len(jobs["job_ids"]):
        raise ValueError("NK Securities visible department ordering does not cover the complete board")
    return {
        "complete": True,
        "job_ids": jobs["job_ids"],
        "count": len(jobs["job_ids"]),
        "department_labels": [department["label"] for department in visible],
        "hidden_empty_departments": departments["hidden_empty_departments"],
        "ui_listing_order": order,
    }


def parse_jobs_identity(payload: dict) -> dict:
    """Validate the board's identity-only request used as a second count check."""
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError("NK Securities identity-only jobs response is invalid")
    if not isinstance(payload.get("meta"), dict) or payload["meta"].get("total") != len(payload["jobs"]):
        raise ValueError("NK Securities identity-only jobs total does not reconcile")
    seen = set()
    for row in payload["jobs"]:
        _validate_board_row(row, require_content=False, source="identity-only")
        if row["id"] in seen:
            raise ValueError(f"NK Securities identity-only response repeats job id {row['id']}")
        seen.add(row["id"])
    return {"job_ids": [row["id"] for row in payload["jobs"]], "count": len(payload["jobs"])}


def parse_detail_page(html: str, row: dict, response_url: str | None = None) -> dict:
    identity = str(row["id"])
    url = row["absolute_url"]
    if response_url is not None and response_url != url:
        raise ValueError(f"NK Securities detail page redirected away from its published URL: {url}")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True) if soup.title else "")
    expected_title = f"Job Application for {normalized(row.get('title'))} at {COMPANY_NAME}"
    if title != expected_title:
        raise ValueError(f"NK Securities exact detail title changed: {identity}")
    heading = soup.select_one("h1")
    if heading is None or normalized(heading.get_text(" ", strip=True)) != normalized(row.get("title")):
        raise ValueError(f"NK Securities exact detail h1 disagrees with the board row: {identity}")
    body = soup.select_one("div.job__description")
    if body is None:
        raise ValueError(f"NK Securities exact detail description is missing: {identity}")
    page_text = compact_text(str(body))
    native_text = compact_text(decode_content(row["content"]))
    if page_text != native_text:
        raise ValueError(f"NK Securities exact detail body disagrees with the board content: {identity}")
    form = soup.select_one("form#application-form")
    if (form is None or form.get("method", "get").lower() != "get"
            or form.get("action") != f"/{BOARD_TOKEN}/jobs/{identity}"):
        raise ValueError(f"NK Securities exact application form changed: {identity}")
    return {
        "job_id": identity,
        "url": url,
        "title": normalized(row.get("title")),
        "location": normalized((row.get("location") or {}).get("name")),
        "page_title": title,
        "description_chars": len(page_text),
        "description_sha256": sha256(page_text.encode()).hexdigest(),
        "description_fnv1a64": fnv1a64(page_text),
        "matched_board_content": True,
        "application_form_action": form.get("action"),
        "application_form_id": form.get("id"),
    }


def salary_from_metadata(metadata) -> list[dict] | None:
    """Preserve any explicitly published salary/pay metadata rows, never infer one."""
    items = metadata if isinstance(metadata, list) else ([metadata] if isinstance(metadata, dict) else [])
    found = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = normalized(item.get("name") or item.get("label"))
        value = item.get("value")
        if name and value not in (None, "", []) and re.search(
                r"salary|compensation|pay range|base pay|ctc", name, re.I):
            found.append({"name": name, "value": value})
    return found or None


def job_fields(row: dict) -> dict:
    departments = [normalized(item.get("name")) for item in row.get("departments") or []]
    offices = row.get("offices") or []
    employer = offices[0].get("name") if len({office.get("name") for office in offices}) == 1 else COMPANY_NAME
    return {
        "title": normalized(row.get("title")),
        "job_id": str(row["id"]),
        "requisition_id": row.get("requisition_id"),
        "url": row["absolute_url"],
        "apply_url": row["absolute_url"],
        "locations": [normalized((row.get("location") or {}).get("name"))],
        "department": [department_label(name) for name in departments],
        "team": None,
        "employment_type": None,
        "workplace_type": None,
        "date_posted": row.get("first_published"),
        "date_posted_raw": row.get("first_published"),
        "date_updated": row.get("updated_at"),
        "valid_through": row.get("application_deadline"),
        "salary": salary_from_metadata(row.get("metadata")),
        "qualifications": None,
        "responsibilities": None,
        "experience": None,
        "education": None,
        "employer": employer,
        "language": row.get("language"),
    }


class NKSecurities(Adapter):
    VERIFICATION_REVISION = VERIFICATION_REVISION

    async def official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        if str(response.url) != OFFICIAL_ENTRY:
            raise ValueError("NK Securities official entry redirected away from its inspected URL")
        entry = parse_official_entry(response.text, str(response.url))
        elapsed = max(0.0, time.perf_counter() - started)
        self.result.timings["official_entry_seconds"] = self.result.timings.get(
            "official_entry_seconds", 0.0) + elapsed
        if "initial_official_entry_seconds" not in self.result.timings:
            self.result.timings["initial_official_entry_seconds"] = elapsed
        else:
            self.result.timings["final_official_entry_seconds"] = elapsed
        return entry

    async def board_listing(self, *, include_identity_check: bool = True):
        started = time.perf_counter()
        departments_payload = await self.http.json(DEPARTMENTS_API)
        jobs_payload = await self.http.json(JOBS_API)
        identity_payload = await self.http.json(BOARD_API + "/jobs") if include_identity_check else None
        departments = parse_board_departments(departments_payload)
        jobs = parse_board_jobs(jobs_payload)
        if identity_payload is not None:
            identity = parse_jobs_identity(identity_payload)
            if sorted(identity["job_ids"]) != sorted(jobs["job_ids"]):
                raise ValueError("NK Securities identity-only board membership disagrees with the full board")
        reconciliation = reconcile_membership(jobs, departments)
        elapsed = max(0.0, time.perf_counter() - started)
        self.result.timings["board_listing_seconds"] = self.result.timings.get(
            "board_listing_seconds", 0.0) + elapsed
        if "initial_board_listing_seconds" not in self.result.timings:
            self.result.timings["initial_board_listing_seconds"] = elapsed
        else:
            self.result.timings["final_board_listing_seconds"] = elapsed
        return jobs, departments, reconciliation

    async def exact_detail_check(self, row: dict):
        response = await self.http.get(row["absolute_url"])
        if str(response.url) != row["absolute_url"]:
            raise ValueError(f"NK Securities exact detail redirected away from its published URL: {row['absolute_url']}")
        return parse_detail_page(response.text, row, str(response.url))

    async def check_details(self, rows):
        started = time.perf_counter()
        sem = asyncio.Semaphore(max(1, int(self.options.detail_concurrency or 1)))
        checks, failures = {}, []
        self.result.metadata["detail_requests"] = len(rows)

        async def one(row):
            async with sem:
                try:
                    checks[str(row["id"])] = await self.exact_detail_check(row)
                except Exception as exc:
                    failures.append(f"NK Securities exact detail {row['absolute_url']}: {type(exc).__name__}: {exc}")
                    self.result.errors.append(failures[-1])
                    self.result.metadata.setdefault("failed_detail_ids", []).append(str(row["id"]))

        await asyncio.gather(*(one(row) for row in rows))
        self.result.timings["detail_phase_seconds"] = self.result.timings.get(
            "detail_phase_seconds", 0.0) + max(0.0, time.perf_counter() - started)
        self.result.metadata["detail_checks"] = checks
        self.result.metadata["detail_failures"] = failures
        return checks

    async def run(self):
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry_url=OFFICIAL_ENTRY,
            listing_url=JOBS_API,
            departments_url=DEPARTMENTS_API,
            listing_authority=(
                "NK Securities' own open-positions page publishes an inline renderer that requests "
                "api.greenhouse.io/v1/boards/nksecuritiesresearch/departments and /jobs?content=true; "
                "only that company-scoped board is enumerated."
            ),
            membership_request_shape={
                "departments": "GET /v1/boards/nksecuritiesresearch/departments",
                "jobs": "GET /v1/boards/nksecuritiesresearch/jobs?content=true",
                "identity_check": "GET /v1/boards/nksecuritiesresearch/jobs",
                "filters_applied": False,
                "pagination": "none; the board returns all rows in one unfiltered response",
            },
            filters_observed=["search", "location", "department", "experience level (client-derived from descriptions)"],
            date_semantics=(
                "first_published is the source posting timestamp, updated_at is the source update, "
                "and a null application_deadline remains null; request time is never used as a posting date."
            ),
            detail_method="Full content HTML from the same company-scoped board content request; every published detail page is fetched and compared.",
            employer_source="Greenhouse offices[0].name (legal entity N.K. Securities Research Private Ltd.)",
            description_sections="Source stores the complete role sections in one HTML field; qualifications/responsibilities are preserved verbatim in description_html rather than invented as separate fields.",
            salary_method="No structured pay range is exposed; literal 'competitive salary' statements remain in the description.",
            education_flag="Greenhouse application-form education flags are preserved in raw.board_job.education, not treated as role requirements.",
            ats_board_scope="only the exact company-scoped nksecuritiesresearch board requested by the official page",
            standalone_ats_requests=0,
            independent_rendered_browser_review="companies/nk-securities/browser-reference-worker.json",
        )

        self.result.timings["source_collection_seconds"] = 0.0
        source_started = time.perf_counter()
        entry = await self.official_entry()
        jobs, departments, reconciliation = await self.board_listing(include_identity_check=True)
        self.result.timings["source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.pages = 1
        self.result.expected_total = jobs["total"]
        self.result.metadata.update(
            official_entry=entry,
            board_job_count=jobs["count"],
            board_job_ids=jobs["job_ids"],
            departments={item["name"]: item["job_ids"] for item in departments["departments"]},
            department_labels=[item["label"] for item in departments["visible_departments"]],
            hidden_empty_departments=departments["hidden_empty_departments"],
            membership_reconciliation={
                "complete": reconciliation["complete"],
                "job_ids": reconciliation["job_ids"],
                "count": reconciliation["count"],
                "department_labels": reconciliation["department_labels"],
                "hidden_empty_departments": reconciliation["hidden_empty_departments"],
                "ui_listing_order": reconciliation["ui_listing_order"],
            },
        )

        by_id = {str(row["id"]): row for row in jobs["rows"]}
        started = time.perf_counter()
        for identity in reconciliation["ui_listing_order"]:
            row = by_id[str(identity)]
            self.add(self.job(
                {"board_job": deepcopy(row), "board_api": BOARD_API, "jobs_api": JOBS_API,
                 "department_names": [item["name"] for item in row["departments"]],
                 "department_labels": [department_label(item["name"]) for item in row["departments"]],
                 "offices": deepcopy(row["offices"])},
                description=decode_content(row["content"]),
                method="nk_securities.official_open_positions.greenhouse_board.content_html",
                description_url=JOBS_API,
                **job_fields(row),
            ))
            if self.limit():
                break
        self.result.timings["native_detail_extraction_seconds"] = max(0.0, time.perf_counter() - started)
        if "sample_limit" in self.result.warnings:
            self.result.listing_complete = False
            return self.result

        checks = await self.check_details([by_id[str(identity)] for identity in reconciliation["ui_listing_order"]])
        for job in self.result.jobs:
            job.raw["detail"] = checks.get(job.job_id, {})
            if job.job_id in checks:
                job.raw["detail_page_verified"] = checks[job.job_id]["matched_board_content"]
            else:
                job.raw["detail_page_verified"] = False
                job.warnings.extend(["detail_page_not_verified", "detail_fetch_failed"])
                job.description_verified = False

        final_started = time.perf_counter()
        try:
            final_entry = await self.official_entry()
            final_jobs, final_departments, final_reconciliation = await self.board_listing(
                include_identity_check=True)
            same_identity = (
                [(row["id"], normalized(row["title"]), row["absolute_url"]) for row in final_jobs["rows"]]
                == [(row["id"], normalized(row["title"]), row["absolute_url"]) for row in jobs["rows"]]
            )
            if not same_identity:
                self.result.errors.append("nk_securities_official_final_membership_identity_changed")
            if final_jobs["rows"] != jobs["rows"]:
                self.result.errors.append("nk_securities_official_final_public_fields_changed")
            if final_entry["renderer_sha256"] != entry["renderer_sha256"]:
                self.result.errors.append("nk_securities_official_renderer_changed")
            self.result.metadata["final_reconciliation"] = {
                "job_ids": final_jobs["job_ids"],
                "count": final_jobs["count"],
                "same_identity": same_identity,
                "department_labels": final_reconciliation["department_labels"],
                "hidden_empty_departments": final_reconciliation["hidden_empty_departments"],
            }
        except Exception as exc:
            self.result.errors.append(f"NK Securities final membership recheck failed: {type(exc).__name__}: {exc}")
        self.result.timings["final_recheck_seconds"] = max(0.0, time.perf_counter() - final_started)
        self.result.listing_complete = (
            not self.result.errors
            and self.result.expected_total == len(self.result.jobs)
            and all(job.description_verified for job in self.result.jobs)
        )
        return self.result
