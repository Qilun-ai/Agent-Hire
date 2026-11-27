"""Razorpay's official careers journey and its company-published Greenhouse board.

Observed journey (see companies/razorpay/journey-research.md):

- The ledger's official entry https://razorpay.com/jobs/ redirects (301) to
  https://razorpay.com/careers/. That Framer page is Razorpay's own careers
  site and publishes exactly one job-listing destination: six responsive
  ``SEE ALL JOBS`` anchors to
  https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited
  ("Current openings at Razorpay Software Private Limited").
- The exact board page SSRs 22 rows (17 department groups, one page) and embeds
  its listing state as ``"jobPosts": {count, page, total, total_pages, data}``.
  The board-scoped JSON API
  https://boards-api.greenhouse.io/v1/boards/razorpaysoftwareprivatelimited/jobs?content=true
  returns the same 22 jobs with the full description content and meta.total.
  No other board, slug or ATS-wide discovery is queried.
- Each published job page is
  https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited/jobs/<id>.
  Its ``div.job__description`` body is the complete role description; Apply is
  an in-page form (``#application-form``), not an external ATS link.
- Every collected role's stored description is checked against its own rendered
  job page (title, location and full normalised body text), not only against the
  listing API payload.

Dates are source-only: ``first_published`` is the posting date and
``updated_at`` is the source update date. ``application_deadline`` is null on
all current rows, so ``valid_through`` stays null. No compensation field exists
in the source; salary stays null and is never inferred from prose.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

OFFICIAL_ENTRY = "https://razorpay.com/jobs/"
CAREERS_URL = "https://razorpay.com/careers/"
BOARD_TOKEN = "razorpaysoftwareprivatelimited"
BOARD_PUBLIC_URL = f"https://job-boards.greenhouse.io/{BOARD_TOKEN}"
BOARD_API_URL = (
    f"https://boards-api.greenhouse.io/v1/boards/{BOARD_TOKEN}/jobs?content=true"
)
JOB_PAGE_BASE = f"{BOARD_PUBLIC_URL}/jobs"
DETAIL_API_BASE = f"https://boards-api.greenhouse.io/v1/boards/{BOARD_TOKEN}/jobs"
EMPLOYER = "Razorpay Software Private Limited"
GREENHOUSE_ID = re.compile(r"^\d{7,13}$")
JOB_PAGE_RE = re.compile(re.escape(JOB_PAGE_BASE) + r"/(\d{7,13})$")
JOB_COUNT_RE = re.compile(r"^\s*(\d+)\s+jobs?\s*$", re.I)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_body_text(html: str | None) -> str:
    return text_html(html)


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def job_page_url(identity) -> str:
    identity = str(identity)
    if not GREENHOUSE_ID.fullmatch(identity):
        raise ValueError(f"Razorpay job ID is not a Greenhouse ID: {identity!r}")
    return f"{JOB_PAGE_BASE}/{identity}"


def detail_api_url(identity) -> str:
    identity = str(identity)
    if not GREENHOUSE_ID.fullmatch(identity):
        raise ValueError(f"Razorpay job ID is not a Greenhouse ID: {identity!r}")
    return f"{DETAIL_API_BASE}/{identity}"


def _validate_board_url(url, label):
    parsed = urlsplit(str(url))
    if (parsed.scheme != "https" or parsed.hostname != "boards-api.greenhouse.io"
            or parsed.path != f"/v1/boards/{BOARD_TOKEN}/jobs"
            or parsed.query != "content=true"):
        raise ValueError(f"Razorpay {label} source left the inspected company-published board: {url}")


def parse_official_entry(html: str, response_url: str = CAREERS_URL) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname != "razorpay.com"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError(f"Razorpay official careers entry did not end on razorpay.com/careers: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Razorpay Careers" not in title:
        raise ValueError("Razorpay official careers page title changed")
    if soup.select("iframe"):
        raise ValueError("Razorpay official careers page now embeds an unexpected iframe")
    board_links = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip().rstrip("/")
        if href == BOARD_PUBLIC_URL.rstrip("/"):
            board_links.append({
                "text": normalized_title(anchor.get_text(" ", strip=True)),
                "aria_label": anchor.get("aria-label"),
                "target": anchor.get("target"),
            })
    if not board_links:
        raise ValueError("Razorpay official careers page no longer publishes its company board link")
    if not any(link["text"].upper() == "SEE ALL JOBS" for link in board_links):
        raise ValueError("Razorpay official careers page board CTA changed")
    return {
        "url": response_url,
        "page_title": title,
        "board_public_url": BOARD_PUBLIC_URL,
        "board_link_count": len(board_links),
        "board_links": board_links,
        "iframes": [],
    }


def _embedded_listing(html: str) -> dict:
    marker = '"jobPosts":'
    index = html.find(marker)
    if index < 0:
        raise ValueError("Razorpay board page no longer embeds its jobPosts listing state")
    start = index + len(marker)
    while start < len(html) and html[start] in " \t\r\n":
        start += 1
    try:
        value, _ = json.JSONDecoder().raw_decode(html, start)
    except json.JSONDecodeError as exc:
        raise ValueError("Razorpay board page embedded jobPosts state is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Razorpay board page embedded jobPosts state is not an object")
    return value


def parse_board_page(html: str, response_url: str = BOARD_PUBLIC_URL) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname != "job-boards.greenhouse.io"
            or parsed.path.rstrip("/") != f"/{BOARD_TOKEN}"):
        raise ValueError(f"Razorpay board listing came from an unexpected route: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    page_title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if page_title != f"Jobs at {EMPLOYER}":
        raise ValueError(f"Razorpay board page title changed: {page_title!r}")
    heading = soup.select_one("h1")
    if not heading or normalized_title(heading.get_text(" ", strip=True)) != f"Current openings at {EMPLOYER}":
        raise ValueError("Razorpay board page heading changed")
    count_node = soup.select_one('[data-testid="job-count-header"]')
    if not count_node:
        raise ValueError("Razorpay board page no longer shows a visible job count")
    count_match = JOB_COUNT_RE.match(count_node.get_text(" ", strip=True))
    if not count_match:
        raise ValueError("Razorpay board page visible job count changed format")
    visible_count = int(count_match.group(1))

    rows, seen, issues = [], set(), []
    groups = []
    for group in soup.select("div.job-posts--table--department"):
        path_node = group.select_one("div.job-posts--department-path p")
        group_path = normalized_title(path_node.get_text(" ", strip=True)) if path_node else None
        heading_node = group.select_one("h3")
        group_name = normalized_title(heading_node.get_text(" ", strip=True)) if heading_node else None
        if not group_name:
            issues.append("razorpay_board_department_heading_missing")
            continue
        department = f"{group_path} / {group_name}" if group_path and group_path != group_name else group_name
        group_rows = group.select("tr.job-post")
        groups.append({"department": department, "visible_rows": len(group_rows)})
        for tr in group_rows:
            anchor = tr.select_one("a[href]")
            if anchor is None:
                issues.append("razorpay_board_row_missing_link")
                continue
            href = (anchor.get("href") or "").strip()
            match = JOB_PAGE_RE.match(href)
            if not match:
                issues.append(f"razorpay_board_row_unexpected_link:{href}")
                continue
            identity = match.group(1)
            if identity in seen:
                issues.append(f"razorpay_board_duplicate_row:{identity}")
                continue
            paragraphs = tr.select("p")
            if not paragraphs:
                issues.append(f"razorpay_board_row_missing_text:{identity}")
                continue
            title_node = paragraphs[0]
            badge_node = title_node.select_one("span.tag-text")
            badge = normalized_title(badge_node.get_text(" ", strip=True)) if badge_node else None
            for tag in title_node.select("span.tag-container"):
                tag.decompose()
            title = normalized_title(title_node.get_text(" ", strip=True))
            location = None
            if len(paragraphs) > 1:
                location = normalized_title(paragraphs[1].get_text(" ", strip=True))
            if not title:
                issues.append(f"razorpay_board_row_missing_title:{identity}")
                continue
            seen.add(identity)
            rows.append({
                "id": identity,
                "title": title,
                "location": location,
                "url": href,
                "department": department,
                "department_path": group_path,
                "department_name": group_name,
                "badge": badge,
                "ui_index": len(rows) + 1,
            })

    embedded = _embedded_listing(html)
    embedded_rows = embedded.get("data")
    if not isinstance(embedded_rows, list):
        issues.append("razorpay_board_embedded_listing_missing_data")
        embedded_rows = []
    embedded_ids = []
    embedded_rows_by_id = {}
    for row in embedded_rows:
        if not isinstance(row, dict) or not str(row.get("id", "")).isdigit():
            issues.append("razorpay_board_embedded_row_invalid")
            continue
        embedded_ids.append(str(row["id"]))
        embedded_rows_by_id[str(row["id"])] = row
        if row.get("absolute_url") != job_page_url(row["id"]):
            issues.append(f"razorpay_board_embedded_unexpected_url:{row['id']}")
    for row in rows:
        row["embedded"] = embedded_rows_by_id.get(row["id"])
    visible_ids = [row["id"] for row in rows]
    if int(embedded.get("count") or -1) != len(rows):
        issues.append("razorpay_board_visible_row_count_differs_from_embedded_count")
    if int(embedded.get("total") or -1) != len(rows):
        issues.append("razorpay_board_visible_row_count_differs_from_embedded_total")
    if embedded_ids != visible_ids:
        issues.append("razorpay_board_embedded_row_order_differs_from_visible_rows")
    if visible_count != len(rows):
        issues.append("razorpay_board_count_header_differs_from_visible_rows")
    return {
        "url": response_url,
        "page_title": page_title,
        "heading": normalized_title(heading.get_text(" ", strip=True)),
        "visible_count_header": visible_count,
        "rows": rows,
        "job_ids": visible_ids,
        "departments": groups,
        "embedded": {
            "count": embedded.get("count"),
            "page": embedded.get("page"),
            "total": embedded.get("total"),
            "total_pages": embedded.get("total_pages"),
            "ids": embedded_ids,
        },
        "filters": {
            "search": bool(soup.select_one("#keyword-filter")),
            "department": bool(soup.select_one("#department-filter")),
            "office": bool(soup.select_one("#office-filter")),
            "pagination_controls": [
                normalized_title(node.get_text(" ", strip=True))
                for node in soup.select("button,a")
                if re.search(r"\b(?:next|previous|load more)\b",
                             node.get_text(" ", strip=True), re.I)
            ],
        },
        "issues": issues,
    }


def parse_board_api(payload: dict, response_url: str = BOARD_API_URL) -> dict:
    _validate_board_url(response_url, "listing API")
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError("Razorpay board API response has no jobs array")
    meta = payload.get("meta")
    total = meta.get("total") if isinstance(meta, dict) else None
    if not isinstance(total, int) or total < 0:
        raise ValueError("Razorpay board API response has no integer meta.total")
    rows, seen, issues = [], set(), []
    for row in payload["jobs"]:
        if not isinstance(row, dict):
            raise ValueError("Razorpay board API row is not an object")
        identity = row.get("id")
        if not isinstance(identity, int) or not GREENHOUSE_ID.fullmatch(str(identity)):
            raise ValueError("Razorpay board API row has a missing or invalid exact job ID")
        if str(identity) in seen:
            raise ValueError(f"Razorpay board API repeats job ID {identity}")
        if row.get("title") is None or not normalized_title(row.get("title")):
            raise ValueError(f"Razorpay board API job {identity} has no title")
        if row.get("absolute_url") != job_page_url(identity):
            raise ValueError(f"Razorpay job {identity} absolute_url is not its published job page: {row.get('absolute_url')}")
        if row.get("company_name") != EMPLOYER:
            raise ValueError(f"Razorpay job {identity} employer changed: {row.get('company_name')!r}")
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"Razorpay job {identity} board row has no full description content")
        if len(normalized_body_text(content)) < 100:
            raise ValueError(f"Razorpay job {identity} published description is implausibly short")
        location = row.get("location")
        if not isinstance(location, dict) or not normalized_title(location.get("name")):
            raise ValueError(f"Razorpay job {identity} has no published location")
        if not isinstance(row.get("departments", []), list) or not isinstance(row.get("offices", []), list):
            raise ValueError(f"Razorpay job {identity} departments/offices schema changed")
        seen.add(str(identity))
        rows.append(row)
    if total != len(rows):
        issues.append(f"razorpay_board_api_meta_total_mismatch:{total}!={len(rows)}")
    return {
        "url": response_url,
        "total": total,
        "rows": rows,
        "job_ids": [str(row["id"]) for row in rows],
        "issues": issues,
    }


def parse_detail_page(html: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != "job-boards.greenhouse.io":
        raise ValueError(f"Razorpay job detail came from an unexpected host: {response_url}")
    match = JOB_PAGE_RE.match(f"https://{parsed.netloc}{parsed.path}")
    if match is None:
        raise ValueError(f"Razorpay job detail came from an unexpected route: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    title_node = soup.select_one("div.job__title h1")
    if title_node is None:
        raise ValueError(f"Razorpay job page has no title: {response_url}")
    location_node = soup.select_one("div.job__location div")
    description = soup.select_one("div.job__description")
    if description is None:
        raise ValueError(f"Razorpay job page has no description container: {response_url}")
    description_html = description.decode_contents()
    description_text = normalized_body_text(description_html)
    return {
        "url": response_url,
        "job_id": match.group(1),
        "title": normalized_title(title_node.get_text(" ", strip=True)),
        "location": normalized_title(location_node.get_text(" ", strip=True)) if location_node else None,
        "description_html": description_html,
        "description_text": description_text,
        "description_chars": len(description_text),
        "description_sha256": hashlib.sha256(description_text.encode()).hexdigest(),
        "description_fnv1a64": fnv1a64(description_text),
        "apply_button": bool(soup.select_one('button[aria-label="Apply"]')),
        "application_form_present": bool(soup.select_one("#application-form")),
        "page_title": normalized_title(soup.title.get_text(" ", strip=True) if soup.title else ""),
    }


def attachment_links(description_html: str) -> list[dict]:
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": normalized_title(anchor.get_text(" ", strip=True)) or None})
    return values


def metadata_map(row: dict) -> dict:
    values = {}
    for item in row.get("metadata") or []:
        if isinstance(item, dict) and item.get("name"):
            values[item["name"]] = item.get("value")
    return values


class Razorpay(Adapter):
    """Follow Razorpay's official careers entry and its exact published board."""

    VERIFICATION_REVISION = "razorpay.official-careers-greenhouse-board.1"

    async def _get(self, url, label):
        response = await self.http.get(url)
        if str(response.url) != url:
            raise ValueError(f"Razorpay {label} request redirected away from its inspected source: {response.url}")
        return response

    async def _entry(self):
        response = await self.http.get(self.company.careers_url)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed["request_url"] = self.company.careers_url
        parsed["status"] = int(response.status_code)
        parsed["redirects"] = [
            {"status": int(item.status_code), "location": item.headers.get("location")}
            for item in response.history
        ]
        parsed["response_sha256"] = hashlib.sha256(response.content).hexdigest()
        parsed["response_bytes"] = len(response.content)
        return parsed

    async def _board_snapshot(self, label):
        page_response = await self._get(BOARD_PUBLIC_URL, f"{label} board page")
        page = parse_board_page(page_response.text, str(page_response.url))
        api_response = await self._get(BOARD_API_URL, f"{label} board API")
        _validate_board_url(str(api_response.url), f"{label} board API")
        try:
            payload = api_response.json()
        except ValueError as exc:
            raise ValueError(f"Razorpay {label} board API response is not JSON") from exc
        api = parse_board_api(payload, str(api_response.url))
        return {
            "page": page,
            "api": api,
            "page_response": {
                "status": int(page_response.status_code),
                "sha256": hashlib.sha256(page_response.content).hexdigest(),
                "bytes": len(page_response.content),
            },
            "api_response": {
                "status": int(api_response.status_code),
                "sha256": hashlib.sha256(api_response.content).hexdigest(),
                "bytes": len(api_response.content),
            },
        }

    async def _detail(self, identity, ui_row, api_row):
        url = job_page_url(identity)
        started = time.perf_counter()
        try:
            response = await self._get(url, "job page")
            detail = parse_detail_page(response.text, str(response.url))
            api_text = normalized_body_text((api_row or {}).get("content") or "")
            title = normalized_title((api_row or {}).get("title") or (ui_row or {}).get("title"))
            api_location = normalized_title(((api_row or {}).get("location") or {}).get("name"))
            checks = {
                "status": int(response.status_code),
                "final_url": str(response.url),
                "title_match": detail["title"] == title,
                "location_match": detail["location"] == api_location if api_row else None,
                "body_matches_listing_api": detail["description_text"] == api_text if api_row else None,
                "api_content_chars": len(api_text),
                "detail_body_chars": detail["description_chars"],
                "apply_button": detail["apply_button"],
                "application_form_present": detail["application_form_present"],
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
                "request_seconds": round(time.perf_counter() - started, 6),
            }
            if api_row:
                if not checks["title_match"]:
                    checks["issue"] = "detail_title_differs_from_listing"
                elif not checks["location_match"]:
                    checks["issue"] = "detail_location_differs_from_listing"
                elif not checks["body_matches_listing_api"]:
                    checks["issue"] = "detail_body_differs_from_listing_api"
            elif detail["title"] != normalized_title(ui_row["title"]):
                checks["title_match"] = False
                checks["issue"] = "detail_title_differs_from_listing_ui"
            return detail, checks
        except Exception as exc:
            return None, {
                "status": None,
                "final_url": url,
                "error": f"{type(exc).__name__}: {exc}",
                "request_seconds": round(time.perf_counter() - started, 6),
            }

    def _make_job(self, identity, ui_row, api_row, detail, checks):
        api_row = api_row or {}
        ui_row = ui_row or {}
        description_html = ""
        description_method = None
        description_source_url = job_page_url(identity)
        warnings = []
        if detail:
            description_html = detail["description_html"]
            description_method = "razorpay.official_careers.greenhouse_board.job_page.description"
        elif api_row:
            description_html = api_row.get("content") or ""
            description_method = "razorpay.official_careers.greenhouse_board.api_content_without_job_page"
            description_source_url = detail_api_url(identity)
            warnings.append("detail_fetch_failed")
        locations_raw = metadata_map(api_row)
        office_rows = [office for office in api_row.get("offices") or [] if isinstance(office, dict)]
        department_api = [dept.get("name") for dept in api_row.get("departments") or []
                          if isinstance(dept, dict) and dept.get("name")]
        department = ui_row.get("department") or (" / ".join(department_api) if department_api else None)
        title = normalized_title(api_row.get("title") or ui_row.get("title"))
        location_name = normalized_title(((api_row.get("location") or {}).get("name"))) or ui_row.get("location")
        raw = {
            "listing_api_row": api_row,
            "listing_ui_row": ui_row,
            "listing_embedded_row": ui_row.get("embedded"),
            "board_api_url": BOARD_API_URL,
            "board_detail_api_url": detail_api_url(identity),
            "job_page_url": job_page_url(identity),
            "detail_page_checks": checks,
            "metadata_fields": locations_raw,
            "offices": office_rows,
            "departments_api": api_row.get("departments") or [],
            "employment_raw": api_row.get("employment"),
            "application_deadline_raw": api_row.get("application_deadline"),
            "ai_disclaimer_raw": api_row.get("ai_disclaimer"),
            "attachment_links": attachment_links(description_html),
            "ui_badge": ui_row.get("badge"),
        }
        job = self.job(
            raw,
            title=title,
            url=job_page_url(identity),
            job_id=identity,
            requisition_id=api_row.get("requisition_id"),
            apply_url=job_page_url(identity) + "#application-form",
            description=description_html,
            method=description_method,
            description_url=description_source_url,
            locations=[location_name] if location_name else [],
            department=department,
            employment_type=None,
            workplace_type=None,
            date_posted=api_row.get("first_published") if isinstance(api_row.get("first_published"), str) else None,
            date_posted_raw=api_row.get("first_published"),
            date_updated=api_row.get("updated_at") if isinstance(api_row.get("updated_at"), str) else None,
            valid_through=api_row.get("application_deadline") if isinstance(api_row.get("application_deadline"), str) else None,
            salary=None,
            employer=api_row.get("company_name") or EMPLOYER,
            language=api_row.get("language"),
        )
        job.description_text = detail["description_text"] if detail else normalized_body_text(description_html)
        job.record_identity = f"razorpay:{identity}"
        if checks.get("issue"):
            warnings.append(checks["issue"])
        if not detail:
            warnings.append("description_from_listing_api_only")
        if job.raw["attachment_links"]:
            warnings.append("role_attachment_links_present_without_full_attachment_text")
        job.warnings.extend(warnings)
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        parts = urlsplit(entry_url)
        if (parts.scheme != "https" or parts.hostname not in {"razorpay.com", "www.razorpay.com"}
                or parts.path.rstrip("/") not in {"/jobs", "/careers"}):
            raise ValueError("Razorpay dedicated flow must start from an official HTTPS /jobs or /careers entry")
        if not self.page_guard():
            return self.result

        entry_started = time.perf_counter()
        entry = await self._entry()
        entry_seconds = time.perf_counter() - entry_started
        if not entry.get("redirects") and parts.path.rstrip("/") == "/jobs":
            self.result.errors.append("razorpay /jobs entry no longer redirects to /careers")

        initial_started = time.perf_counter()
        initial = await self._board_snapshot("initial")
        initial_seconds = time.perf_counter() - initial_started
        for issue in initial["page"]["issues"] + initial["api"]["issues"]:
            self.result.errors.append(f"initial board reconciliation: {issue}")
        initial_ui_ids = list(initial["page"]["job_ids"])
        initial_api_ids = list(initial["api"]["job_ids"])
        if set(initial_ui_ids) != set(initial_api_ids):
            self.result.errors.append(
                "razorpay initial board UI/API membership differs: "
                f"ui_only={sorted(set(initial_ui_ids) - set(initial_api_ids))} "
                f"api_only={sorted(set(initial_api_ids) - set(initial_ui_ids))}"
            )

        final_started = time.perf_counter()
        final = await self._board_snapshot("final")
        final_seconds = time.perf_counter() - final_started
        for issue in final["page"]["issues"] + final["api"]["issues"]:
            self.result.errors.append(f"final board reconciliation: {issue}")

        final_ui_ids = list(final["page"]["job_ids"])
        final_api_ids = list(final["api"]["job_ids"])
        final_ui_by_id = {row["id"]: row for row in final["page"]["rows"]}
        final_api_by_id = {str(row["id"]): row for row in final["api"]["rows"]}
        initial_ui_by_id = {row["id"]: row for row in initial["page"]["rows"]}
        initial_api_by_id = {str(row["id"]): row for row in initial["api"]["rows"]}

        membership_changed = initial_ui_ids != final_ui_ids or initial_api_ids != final_api_ids
        if membership_changed:
            self.result.errors.append(
                "razorpay company-published board membership changed between initial and final snapshots"
            )
        if set(final_ui_ids) != set(final_api_ids):
            self.result.errors.append(
                "razorpay final board UI/API membership differs: "
                f"ui_only={sorted(set(final_ui_ids) - set(final_api_ids))} "
                f"api_only={sorted(set(final_api_ids) - set(final_ui_ids))}"
            )
        if final["page"]["embedded"].get("total_pages") not in (None, 1):
            self.result.errors.append(
                f"razorpay board UI has {final['page']['embedded'].get('total_pages')} listing pages; "
                "only the unfiltered page 1 was reconciled"
            )

        all_ids = list(dict.fromkeys(final_ui_ids + [i for i in final_api_ids if i not in final_ui_ids]))
        if not all_ids:
            self.result.expected_total = 0
            self.result.errors.append(
                "Razorpay company-published board returned zero jobs; empty state requires independent review"
            )
            self.result.metadata.update(self._metadata(entry, initial, final, {}, {}, entry_seconds,
                                                       initial_seconds, final_seconds, 0, 0))
            return self.result

        detail_started = time.perf_counter()
        sem = asyncio.Semaphore(max(1, self.options.detail_concurrency))

        async def one(identity):
            async with sem:
                return identity, await self._detail(
                    identity, final_ui_by_id.get(identity) or initial_ui_by_id.get(identity),
                    final_api_by_id.get(identity) or initial_api_by_id.get(identity),
                )

        detail_results = {}
        for identity, (detail, checks) in await asyncio.gather(*(one(i) for i in all_ids)):
            detail_results[identity] = (detail, checks)
        detail_seconds = time.perf_counter() - detail_started

        mapping_started = time.perf_counter()
        detail_checks = {}
        for identity in all_ids:
            detail, checks = detail_results[identity]
            detail_checks[identity] = checks
            if checks.get("error"):
                self.result.errors.append(f"Razorpay job page {identity} failed: {checks['error']}")
            if checks.get("issue"):
                self.result.errors.append(f"Razorpay job page {identity}: {checks['issue']}")
            if detail is None and not final_api_by_id.get(identity) and not initial_api_by_id.get(identity):
                self.result.errors.append(f"Razorpay job {identity} has neither API content nor a readable job page")
                continue
            initial_row = initial_api_by_id.get(identity)
            final_row = final_api_by_id.get(identity)
            job = self._make_job(
                identity,
                final_ui_by_id.get(identity) or initial_ui_by_id.get(identity),
                final_row or initial_row,
                detail,
                checks,
            )
            if membership_changed and identity not in final_ui_ids:
                job.warnings.append("membership_changed_during_collection")
            if initial_row is not None and final_row is not None and initial_row != final_row:
                job.raw["initial_listing_api_row"] = initial_row
                job.warnings.append("source_row_changed_between_snapshots")
            self.add(job)
        mapping_seconds = time.perf_counter() - mapping_started

        self.result.expected_total = len(all_ids)
        self.result.pages = 1
        self.result.listing_complete = not self.result.errors
        self.result.metadata.update(self._metadata(
            entry, initial, final, detail_checks, detail_results,
            entry_seconds, initial_seconds, final_seconds, detail_seconds, mapping_seconds,
        ))
        self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = round(initial_seconds + final_seconds, 6)
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        self.result.timings["detail_mapping_phase_seconds"] = round(mapping_seconds, 6)
        self.result.timings["source_collection_wall_seconds"] = round(time.perf_counter() - started, 6)
        return self.result

    def _metadata(self, entry, initial, final, detail_checks, detail_results,
                  entry_seconds, initial_seconds, final_seconds, detail_seconds, mapping_seconds):
        final_page = final.get("page") or {}
        final_api = final.get("api") or {}
        initial_page = initial.get("page") or {}
        initial_api = initial.get("api") or {}
        details = []
        for identity, (detail, checks) in detail_results.items():
            details.append({
                "id": identity,
                "checks": checks,
                "detail": None if detail is None else {
                    "title": detail["title"],
                    "location": detail["location"],
                    "description_chars": detail["description_chars"],
                    "description_sha256": detail["description_sha256"],
                    "description_fnv1a64": detail["description_fnv1a64"],
                    "apply_button": detail["apply_button"],
                    "application_form_present": detail["application_form_present"],
                },
            })
        return {
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": self.company.careers_url,
            "official_entry": entry,
            "listing_url": BOARD_PUBLIC_URL,
            "listing_api_url": BOARD_API_URL,
            "listing_authority": (
                "Razorpay's own careers page (razorpay.com/careers, reached by the official "
                "razorpay.com/jobs entry) publishes exactly one job listing: the "
                f"'{EMPLOYER}' Greenhouse board {BOARD_PUBLIC_URL}. Its unfiltered SSR rows, "
                "embedded jobPosts state and board-scoped API meta.total are the membership source. "
                "No guessed slug, parent-wide board or ATS-wide discovery was used."
            ),
            "membership": {
                "initial_ui_visible_rows": len(initial_page.get("rows") or []),
                "initial_ui_count_header": initial_page.get("visible_count_header"),
                "initial_embedded_count": (initial_page.get("embedded") or {}).get("count"),
                "initial_embedded_total": (initial_page.get("embedded") or {}).get("total"),
                "initial_embedded_total_pages": (initial_page.get("embedded") or {}).get("total_pages"),
                "initial_api_total": initial_api.get("total"),
                "initial_ui_ids": initial_page.get("job_ids") or [],
                "initial_api_ids": initial_api.get("job_ids") or [],
                "final_ui_visible_rows": len(final_page.get("rows") or []),
                "final_ui_count_header": final_page.get("visible_count_header"),
                "final_embedded_count": (final_page.get("embedded") or {}).get("count"),
                "final_embedded_total": (final_page.get("embedded") or {}).get("total"),
                "final_embedded_total_pages": (final_page.get("embedded") or {}).get("total_pages"),
                "final_api_total": final_api.get("total"),
                "final_ui_ids": final_page.get("job_ids") or [],
                "final_api_ids": final_api.get("job_ids") or [],
                "ui_and_api_ids_match": set(final_page.get("job_ids") or []) == set(final_api.get("job_ids") or []),
                "initial_final_id_order_matches": (initial_page.get("job_ids") == final_page.get("job_ids")
                                                   and initial_api.get("job_ids") == final_api.get("job_ids")),
                "ui_department_groups": final_page.get("departments") or [],
                "filters": final_page.get("filters") or {},
                "pagination": (
                    f"Single unfiltered board page: visible '{final_page.get('visible_count_header')} jobs', "
                    f"embedded jobPosts {{count={((final_page.get('embedded') or {}).get('count'))}, "
                    f"total={((final_page.get('embedded') or {}).get('total'))}, "
                    f"page={((final_page.get('embedded') or {}).get('page'))}, "
                    f"total_pages={((final_page.get('embedded') or {}).get('total_pages'))}}} and "
                    f"API meta.total={final_api.get('total')} all agree. The Search/Department/Office "
                    "controls filter the same in-memory rows; no page/cursor/load-more control exists."
                ),
            },
            "detail_method": (
                "Every collected job's own published job page was opened and its "
                "div.job__description body compared (normalised text and length) with the "
                "same row's board API content; titles and locations were compared too. "
                "Stored description comes from the rendered job page body."
            ),
            "detail_checks": detail_checks,
            "detail_pages": details,
            "dates": {
                "date_posted": "source first_published",
                "date_updated": (
                    "source API updated_at (per job). The board page's embedded jobPosts "
                    "updated_at is a shared page-render timestamp (identical on every row) and "
                    "is retained only as raw UI evidence, never used as a job date."
                ),
                "valid_through": "source application_deadline; null on all current rows",
                "unknown_dates_remain_null": True,
            },
            "salary_policy": (
                "The board exposes no structured compensation field; every row's salary stays "
                "null and no amount is inferred from prose."
            ),
            "employment_policy": (
                "The API's `employment` value is a Greenhouse system value "
                "('employment_required'), not a displayed employment type; it is retained raw "
                "and employment_type stays null rather than being guessed."
            ),
            "request_scope": (
                "Only razorpay.com/careers, the exact company-published board page, the exact "
                "board-scoped API and that board's own job pages are requested."
            ),
            "timings": {
                "entry_seconds": round(entry_seconds, 6),
                "initial_listing_seconds": round(initial_seconds, 6),
                "final_listing_seconds": round(final_seconds, 6),
                "all_job_pages_seconds": round(detail_seconds, 6),
                "job_mapping_seconds": round(mapping_seconds, 6),
            },
            "requests": {
                "entry_pages": 1,
                "board_pages": 2,
                "board_api_listings": 2,
                "job_pages": len(detail_results),
            },
        }
