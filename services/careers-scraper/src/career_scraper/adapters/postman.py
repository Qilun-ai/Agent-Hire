"""Postman's official careers listing and the exact Workday board it publishes."""
from __future__ import annotations

from hashlib import sha256
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.postman.com/company/careers/"
OFFICIAL_LISTING = "https://www.postman.com/company/careers/open-positions/"
CAREERS_JSON = "https://www.postman.com/_mk-www-next/api-cache/careers-jobs.json"
WORKDAY_ORIGIN = "https://postman.wd108.myworkdayjobs.com"
WORKDAY_SITE = "careers"
WORKDAY_BOARD = WORKDAY_ORIGIN + "/careers"
WORKDAY_API = WORKDAY_ORIGIN + "/wday/cxs/postman/careers"
WORKDAY_JOBS_API = WORKDAY_API + "/jobs"
BOARD_PAGE_SIZE = 20
OFFICIAL_CAREERS_TITLE = "Careers | Postman"
OFFICIAL_LISTING_TITLE = "Open Positions at Postman | Join Our Team"
OFFICIAL_LISTING_H1 = "Become a Postmanaut!"
REQ_ID = re.compile(r"^JR\d+$")
URL_REQ_ID = re.compile(r"_(JR\d+)(?:-\d+)?$")
STAR_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SALARY_SENTENCE = re.compile(
    r"(?i)(?:the reasonably estimated (?:base salary|salary|OTE)[^.]*\.|"
    r"(?:base salary|salary range|compensation range|pay range)[^.]*\.)"
)
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
RESPONSIBILITY_HEADINGS = re.compile(
    r"(?i)(what you.?ll do|responsibilit|how you.?ll contribute|what you.?ll own|"
    r"the role|top priorities|your impact|day to day)"
)
QUALIFICATION_HEADINGS = re.compile(
    r"(?i)(qualif|what we.?re looking for|about you|what you.?ll need|nice[- ]to[- ]have|"
    r"preferred|must[- ]have|requirements|who you are|technical familiarity|bonus skills)"
)
SECTION_HEADINGS = re.compile(
    r"(?i)^(?:who are we|the opportunity|about you|about the team|about the role|about [a-z0-9][a-z0-9 ,&.'’\-]*|"
    r"the role|what you.?ll do|how you.?ll contribute|what you.?ll own|what we.?re looking for|"
    r"what you.?ll need(?: to be successful)?|nice[- ]to[- ]have|must[- ]have qualifications|"
    r"preferred|preferred qualifications|bonus (?:skills|qualifications)[^:]*|why this matters|"
    r"why join us|top priorities|essential qualifications|bonus qualifications|technical familiarity|"
    r"what else|our values|equal opportunity|responsibilities|qualifications|requirements|"
    r"work schedule and coverage)\??:?$"
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_body_text(html: str | None) -> str:
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


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.postman.com"
            or parsed.path.rstrip("/") != "/company/careers"):
        raise ValueError("Postman official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != OFFICIAL_CAREERS_TITLE:
        raise ValueError("Postman official careers page title changed")
    links = []
    for anchor in soup.select("a[href]"):
        target = urljoin(response_url, anchor.get("href") or "")
        if target.rstrip("/") == OFFICIAL_LISTING.rstrip("/"):
            links.append(target)
    if not links:
        raise ValueError("Postman official careers page no longer links to its open-positions listing")
    return {
        "url": response_url,
        "title": title,
        "open_positions_links": sorted(set(links)),
        "official_listing_url": OFFICIAL_LISTING,
    }


def parse_listing_page(html: str, response_url: str = OFFICIAL_LISTING) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.postman.com"
            or parsed.path.rstrip("/") != "/company/careers/open-positions"):
        raise ValueError("Postman open-positions listing redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title != OFFICIAL_LISTING_TITLE:
        raise ValueError("Postman open-positions listing title changed")
    h1 = [normalized_title(node.get_text(" ", strip=True)) for node in soup.select("h1")]
    if h1 != [OFFICIAL_LISTING_H1]:
        raise ValueError("Postman open-positions listing heading changed")
    stats = soup.select_one(".oxOpMq_statsBar")
    stats_text = normalized_title(stats.get_text(" ", strip=True) if stats else "")
    match = re.search(r"(\d+)\s+open positions", stats_text, re.I)
    filters = {}
    for details in soup.select("details"):
        summary = normalized_title(details.summary.get_text(" ", strip=True)) if details.summary else ""
        if summary:
            filters[summary] = [
                normalized_title(label.get_text(" ", strip=True)) for label in details.select("label")
            ]
    workday_links = [
        anchor.get("href") for anchor in soup.select("a[href]")
        if "myworkdayjobs.com" in (anchor.get("href") or "")
        and "/careers/job/" not in (anchor.get("href") or "")
    ]
    job_links = [
        anchor.get("href") for anchor in soup.select("a[href]")
        if "/careers/job/" in (anchor.get("href") or "")
    ]
    return {
        "url": response_url,
        "title": title,
        "h1": h1,
        "stats_text": stats_text,
        "visible_open_positions_text": int(match.group(1)) if match else None,
        "filters": filters,
        "search_inputs": [
            {"id": node.get("id"), "placeholder": node.get("placeholder")}
            for node in soup.select("input[type=text]")
        ],
        "pagination_controls": [
            normalized_title(node.get_text(" ", strip=True))
            for node in soup.select("a,button")
            if re.search(r"^(?:next|previous|load more|show more)$",
                         normalized_title(node.get_text(" ", strip=True)), re.I)
        ],
        "server_rendered_workday_job_links": job_links,
        "workday_alert_urls": sorted(set(workday_links)),
    }


def parse_official_jobs(value: dict, *, response_url: str = CAREERS_JSON) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Postman careers-jobs.json is not an object")
    jobs = value.get("jobs")
    meta = value.get("meta")
    if not isinstance(jobs, list) or not isinstance(meta, dict):
        raise ValueError("Postman careers-jobs.json is missing jobs or meta")
    if meta.get("source") != "workday_cxs" or meta.get("careersUrl") != WORKDAY_BOARD:
        raise ValueError("Postman careers-jobs.json no longer points at its exact Workday board")
    total = meta.get("total")
    if not isinstance(total, int) or total < 0:
        raise ValueError("Postman careers-jobs.json total is missing or invalid")
    rows, seen, issues = [], set(), []
    for row in jobs:
        if not isinstance(row, dict):
            issues.append("postman_official_row_not_object")
            continue
        identity = str(row.get("id") or "")
        title = normalized_title(row.get("title"))
        url = str(row.get("url") or "")
        url_parts = urlsplit(url)
        suffix = URL_REQ_ID.search(url_parts.path)
        expected_prefix = (url_parts.scheme == "https" and url_parts.netloc == "postman.wd108.myworkdayjobs.com"
                           and url_parts.path.startswith("/careers/job/"))
        if not REQ_ID.fullmatch(identity) or not title or not expected_prefix:
            issues.append(f"postman_official_row_identity_missing:{identity or '?'}")
            continue
        if suffix is None or suffix.group(1) != identity:
            issues.append(f"postman_official_row_url_requisition_mismatch:{identity}")
        if identity in seen:
            issues.append(f"postman_official_duplicate_id:{identity}")
            continue
        if not normalized_title(row.get("location")) or not normalized_title(row.get("department")):
            issues.append(f"postman_official_row_location_or_department_missing:{identity}")
        if not isinstance(row.get("updated_at"), str) or not isinstance(row.get("description"), str):
            issues.append(f"postman_official_row_listing_fields_missing:{identity}")
        seen.add(identity)
        rows.append(dict(row))
    if total != len(rows):
        issues.append("postman_official_total_does_not_match_retained_rows")
    return {
        "url": response_url,
        "rows": rows,
        "job_ids": [str(row["id"]) for row in rows],
        "total": total,
        "meta": dict(meta),
        "issues": issues,
    }


def parse_board_page(value: dict, requested_offset: int, limit: int = BOARD_PAGE_SIZE) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Postman Workday board page is not an object")
    total = value.get("total")
    postings = value.get("jobPostings")
    if not isinstance(total, int) or total < 0 or not isinstance(postings, list):
        raise ValueError("Postman Workday board page is missing total or jobPostings")
    if len(postings) > limit:
        raise ValueError("Postman Workday board returned more rows than its requested limit")
    rows, seen, issues = [], set(), []
    for row in postings:
        if not isinstance(row, dict):
            issues.append("postman_board_row_not_object")
            continue
        path = str(row.get("externalPath") or "")
        bullets = row.get("bulletFields")
        identity = str(bullets[0]) if isinstance(bullets, list) and bullets else ""
        suffix = URL_REQ_ID.search(path)
        if (not path.startswith("/job/") or not REQ_ID.fullmatch(identity)
                or suffix is None or suffix.group(1) != identity):
            issues.append(f"postman_board_row_identity_missing:{identity or '?'}")
            continue
        if identity in seen:
            issues.append(f"postman_board_duplicate_id:{identity}")
            continue
        if not normalized_title(row.get("title")) or not normalized_title(row.get("timeType")):
            issues.append(f"postman_board_row_title_or_type_missing:{identity}")
        if not isinstance(row.get("locationsText"), str) or not isinstance(row.get("postedOn"), str):
            issues.append(f"postman_board_row_location_or_posted_text_missing:{identity}")
        seen.add(identity)
        rows.append(dict(row))
    expected_rows = min(limit, max(0, total - requested_offset))
    if len(postings) != expected_rows:
        issues.append("postman_board_page_row_count_mismatch")
    return {
        "rows": rows,
        "job_ids": [str(row["bulletFields"][0]) for row in rows],
        "total": total,
        "offset": requested_offset,
        "limit": limit,
        "issues": issues,
    }


def parse_detail(value: dict, expected: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("jobPostingInfo"), dict):
        raise ValueError("Postman Workday detail response is missing jobPostingInfo")
    info = value["jobPostingInfo"]
    expected_id = str(expected["bulletFields"][0])
    expected_path = str(expected["externalPath"])
    identity = str(info.get("jobReqId") or "")
    issues = []
    if identity != expected_id:
        issues.append(f"postman_detail_requisition_mismatch:{expected_id}:{identity or '?'}")
    if normalized_title(info.get("title")) != normalized_title(expected.get("title")):
        issues.append(f"postman_detail_title_conflict:{expected_id}")
    if info.get("jobPostingSiteId") != WORKDAY_SITE:
        issues.append(f"postman_detail_site_mismatch:{expected_id}")
    expected_url = WORKDAY_ORIGIN + "/careers" + expected_path
    if str(info.get("externalUrl") or "") != expected_url:
        issues.append(f"postman_detail_external_url_mismatch:{expected_id}")
    if info.get("canApply") is not True or info.get("posted") is not True:
        issues.append(f"postman_detail_not_publicly_applicable:{expected_id}")
    if not isinstance(info.get("jobDescription"), str) or len(normalized_body_text(info["jobDescription"])) < 100:
        issues.append(f"postman_detail_description_empty_or_truncated:{expected_id}")
    start_date = info.get("startDate")
    if start_date is not None and (not isinstance(start_date, str) or not STAR_DATE.fullmatch(start_date)):
        issues.append(f"postman_detail_start_date_invalid:{expected_id}")
    if not isinstance(info.get("location"), str) or not normalized_title(info["location"]):
        issues.append(f"postman_detail_location_missing:{expected_id}")
    additional = info.get("additionalLocations")
    if additional is not None and (not isinstance(additional, list)
                                   or any(not isinstance(x, str) or not x.strip() for x in additional)):
        issues.append(f"postman_detail_additional_locations_invalid:{expected_id}")
    employer = value.get("hiringOrganization")
    if (not isinstance(employer, dict) or not normalized_title(employer.get("name"))):
        issues.append(f"postman_detail_hiring_organization_missing:{expected_id}")
    if not normalized_title(info.get("timeType")):
        issues.append(f"postman_detail_time_type_missing:{expected_id}")
    return {
        "id": identity or expected_id,
        "job_req_id": identity or None,
        "title": normalized_title(info.get("title")),
        "external_url": str(info.get("externalUrl") or ""),
        "location": info.get("location"),
        "additional_locations": list(additional or []),
        "start_date": start_date,
        "posted_on": info.get("postedOn"),
        "time_type": info.get("timeType"),
        "job_posting_id": info.get("jobPostingId"),
        "job_posting_site_id": info.get("jobPostingSiteId"),
        "can_apply": info.get("canApply"),
        "posted": info.get("posted"),
        "country": info.get("country"),
        "questionnaire_id": info.get("questionnaireId"),
        "employer": employer,
        "job_description": info.get("jobDescription") or "",
        "info": info,
        "issues": issues,
    }


def description_html_blocks(html: str | None) -> list[dict]:
    """Keep the employer-authored block structure for source evidence."""
    soup = BeautifulSoup(f'<div data-postman-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-postman-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = normalized_title(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find(["strong", "b"], recursive=False)
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return [section for section in sections if section["heading"] or section["text"]]


def description_sections(html: str | None) -> list[dict]:
    """Split the rendered text on the employer's section labels.

    Postman mixes <h1> headings with inline bold labels followed by <br> tags
    (for example ``<b>The Opportunity<br/><br/></b>``), so a text-line split is
    the reliable way to retain every section. Full HTML is always kept on the
    job record; these sections are the qualification/responsibility view.
    """
    lines = [line.strip() for line in text_html(html).splitlines() if line.strip()]
    sections: list[dict] = []
    current = {"heading": None, "text": ""}
    for line in lines:
        heading = line[:-1].strip() if line.endswith(":") else line
        if SECTION_HEADINGS.fullmatch(heading) and len(heading) <= 120:
            if current["heading"] or current["text"].strip():
                sections.append(current)
            current = {"heading": heading, "text": ""}
            continue
        current["text"] = f"{current['text']}\n{line}".strip()
    if current["heading"] or current["text"].strip():
        sections.append(current)
    unique, seen = [], set()
    for section in sections:
        key = (section["heading"], section["text"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(section)
    return unique


def salary_sentences(html: str | None) -> list[str]:
    text = normalized_body_text(html)
    return list(dict.fromkeys(match.group(0).strip() for match in SALARY_SENTENCE.finditer(text)))


def section_payloads(sections: list[dict], pattern: re.Pattern) -> list[dict] | None:
    selected = [
        {"heading": section["heading"], "text": section["text"]}
        for section in sections
        if section["heading"] and pattern.search(section["heading"]) and section["text"]
    ]
    return selected or None


def _api_headers() -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": WORKDAY_ORIGIN,
        "Referer": WORKDAY_BOARD,
    }


class Postman(Adapter):
    """Read Postman's own open-positions list and hydrate the exact Workday jobs."""

    VERIFICATION_REVISION = "postman.official-json-exact-workday-board.1"

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _official_listing(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_LISTING)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_listing_page(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _official_jobs(self):
        started = time.perf_counter()
        response = await self.http.get(CAREERS_JSON, headers={"Accept": "application/json"})
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_jobs(response.json(), response_url=str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content),
                      cache_control=response.headers.get("cache-control"),
                      age=response.headers.get("age"),
                      last_modified=response.headers.get("last-modified"))
        return parsed

    async def _board_snapshot(self):
        started = time.perf_counter()
        rows, pages, seen, issues = [], [], set(), []
        expected_total = None
        offset = 0
        while True:
            if not self.page_guard():
                issues.append("postman_board_page_budget_exhausted")
                break
            request_started = time.perf_counter()
            response = await self.http.request(
                "POST", WORKDAY_JOBS_API,
                json={"appliedFacets": {}, "limit": BOARD_PAGE_SIZE, "offset": offset, "searchText": ""},
                headers=_api_headers(),
            )
            request_seconds = max(0.0, time.perf_counter() - request_started)
            page = parse_board_page(response.json(), offset)
            if expected_total is None:
                expected_total = page["total"]
            elif page["total"] != expected_total:
                issues.append("postman_board_total_changed_between_pages")
            issues.extend(page["issues"])
            for row in page["rows"]:
                identity = str(row["bulletFields"][0])
                if identity in seen:
                    issues.append(f"postman_board_duplicate_id_across_pages:{identity}")
                    continue
                seen.add(identity)
                rows.append(row)
            pages.append({
                "offset": offset,
                "limit": BOARD_PAGE_SIZE,
                "total": page["total"],
                "job_ids": page["job_ids"],
                "request_seconds": request_seconds,
                "status": response.status_code,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
            })
            if expected_total is None or expected_total < 1 or len(rows) >= expected_total:
                break
            if not page["rows"]:
                issues.append("postman_board_empty_page_before_total")
                break
            offset += len(page["rows"])
        if expected_total is not None and len(rows) != expected_total:
            issues.append("postman_board_total_does_not_match_retained_rows")
        return {
            "rows": rows,
            "job_ids": [str(row["bulletFields"][0]) for row in rows],
            "total": expected_total,
            "pages": pages,
            "issues": list(dict.fromkeys(issues)),
            "request_seconds": max(0.0, time.perf_counter() - started),
        }

    async def _snapshot(self):
        entry = await self._official_entry()
        listing = await self._official_listing()
        official = await self._official_jobs()
        board = await self._board_snapshot()
        return entry, listing, official, board

    def _make_job(self, identity: str, official_row: dict | None, board_row: dict | None):
        title = (official_row or {}).get("title") or (board_row or {}).get("title") or ""
        path = (board_row or {}).get("externalPath")
        url = (official_row or {}).get("url") or (WORKDAY_ORIGIN + "/careers" + path if path else "")
        locations = []
        if official_row and official_row.get("location"):
            locations.append(official_row["location"])
        if board_row and board_row.get("locationsText") and board_row["locationsText"] not in locations:
            locations.append(board_row["locationsText"])
        job = self.job(
            {"official_listing": dict(official_row or {}), "workday_board_listing": dict(board_row or {})},
            title=title,
            url=url,
            job_id=identity,
            requisition_id=identity,
            apply_url=None,
            description="",
            method=None,
            description_url=None,
            locations=locations,
            department=(official_row or {}).get("department"),
            employment_type=(board_row or {}).get("timeType"),
            workplace_type=None,
            date_posted=None,
            date_posted_raw={
                "workday_board_postedOn": (board_row or {}).get("postedOn"),
                "official_listing_updated_at": (official_row or {}).get("updated_at"),
            },
            date_updated=None,
            valid_through=None,
            salary=None,
            employer=None,
            language="en",
        )
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_LISTING,
            "listing_authority": ("Postman's own open-positions page renders its company-owned "
                                  "careers-jobs.json (meta.source=workday_cxs) and links every job "
                                  "to the exact postman/careers Workday site."),
            "listing_membership_request": {
                "method": "GET",
                "url": CAREERS_JSON,
                "scope": "Postman's own company publish cache, which reports the exact Workday board.",
            },
            "workday_board_url": WORKDAY_BOARD,
            "workday_board_request": {
                "method": "POST",
                "url": WORKDAY_JOBS_API,
                "body": {"appliedFacets": {}, "limit": BOARD_PAGE_SIZE, "offset": "page", "searchText": ""},
                "filters_applied": False,
                "scope": "postman/careers only; no other Workday tenant or site is queried.",
            },
            "detail_method": "workday.cxs.jobPostingInfo.jobDescription",
            "request_scope": "Only the official Postman listing, its own JSON cache, and the exact postman/careers Workday jobs exposed there.",
        })
        snapshot_started = time.perf_counter()
        try:
            entry, listing, official, board = await self._snapshot()
            initial_snapshot_seconds = max(0.0, time.perf_counter() - snapshot_started)
        except Exception as exc:
            self.result.errors.append(f"Postman source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.expected_total = official["total"]
        self.result.metadata["initial_official_entry"] = entry
        self.result.metadata["initial_listing_page"] = listing
        self.result.metadata["initial_official_jobs"] = {
            key: value for key, value in official.items() if key not in ("rows",)
        }
        self.result.metadata["initial_board_snapshot"] = {
            key: value for key, value in board.items() if key not in ("rows",)
        }
        if listing["server_rendered_workday_job_links"]:
            self.result.warnings.append("postman_listing_now_server_renders_job_links")
        if listing["visible_open_positions_text"] not in (None, official["total"]):
            self.result.errors.append("postman_listing_visible_count_differs_from_official_json")
        if listing["workday_alert_urls"] and listing["workday_alert_urls"] != [WORKDAY_BOARD]:
            self.result.errors.append("postman_listing_workday_board_link_changed")
        self.result.errors.extend(official["issues"])
        self.result.errors.extend(board["issues"])
        official_ids = official["job_ids"]
        board_ids = board["job_ids"]
        if set(official_ids) != set(board_ids):
            self.result.errors.append("postman_official_json_and_workday_board_membership_mismatch")
        if official["total"] != board["total"]:
            self.result.errors.append("postman_official_json_and_workday_board_total_mismatch")

        official_by_id = {str(row["id"]): row for row in official["rows"]}
        board_by_id = {str(row["bulletFields"][0]): row for row in board["rows"]}
        ordered_ids = list(official_ids) + [identity for identity in board_ids if identity not in set(official_ids)]
        jobs = []
        for identity in ordered_ids:
            job = self._make_job(identity, official_by_id.get(identity), board_by_id.get(identity))
            if not job.url:
                self.result.errors.append(f"postman_membership_row_has_no_job_url:{identity}")
                continue
            jobs.append(job)
        if len(jobs) != official["total"]:
            self.result.errors.append("postman_retained_rows_do_not_match_official_listing_count")

        async def detail(job):
            identity = str(job.job_id)
            board_row = job.raw["workday_board_listing"]
            path = board_row.get("externalPath")
            if not path:
                official_url = str(job.raw["official_listing"].get("url") or job.url)
                path = urlsplit(official_url).path
                if path.startswith("/careers/job/"):
                    path = path[len("/careers"):]
                board_row = {
                    "bulletFields": [identity],
                    "externalPath": path,
                    "title": job.raw["official_listing"].get("title"),
                }
                job.warnings.append("postman_detail_expected_row_recovered_from_official_listing")
            url = WORKDAY_API + path
            request_started = time.perf_counter()
            response = await self.http.get(url, headers=_api_headers())
            seconds = max(0.0, time.perf_counter() - request_started)
            parsed = parse_detail(response.json(), board_row)
            if parsed["job_req_id"] and str(parsed["job_req_id"]) != identity:
                self.result.errors.append(f"postman_detail_requisition_mismatch:{identity}")
            self.result.errors.extend(parsed["issues"])
            sections = description_sections(parsed["job_description"])
            html_blocks = description_html_blocks(parsed["job_description"])
            links = [
                {"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
                for anchor in BeautifulSoup(parsed["job_description"], "lxml").select("a[href]")
            ]
            attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
            salaries = salary_sentences(parsed["job_description"])
            footer_headings = {"what else?", "our values", "equal opportunity"}
            role_sections = [s for s in sections if (s["heading"] or "").strip().lower() not in footer_headings]
            job.title = parsed["title"] or job.title
            job.description_html = parsed["job_description"]
            job.description_method = "workday.cxs.jobPostingInfo.jobDescription"
            job.description_source_url = url
            job.requisition_id = parsed["job_req_id"] or identity
            job.apply_url = parsed["external_url"] + "/apply" if parsed["external_url"] else None
            job.date_posted = parsed["start_date"]
            job.date_posted_raw = {
                "workday_startDate": parsed["start_date"],
                "workday_postedOn": parsed["posted_on"],
                "official_listing_updated_at": job.raw["official_listing"].get("updated_at"),
            }
            job.locations = [parsed["location"]] + parsed["additional_locations"] if parsed["location"] else parsed["additional_locations"]
            job.employment_type = parsed["time_type"]
            job.employer = (parsed["employer"] or {}).get("name")
            job.salary = salaries[0] if len(salaries) == 1 else (salaries or None)
            job.responsibilities = section_payloads(role_sections, RESPONSIBILITY_HEADINGS)
            job.qualifications = section_payloads(role_sections, QUALIFICATION_HEADINGS)
            normalized = normalized_body_text(parsed["job_description"])
            job.raw["detail"] = {
                "request_url": url,
                "request_seconds": seconds,
                "status": response.status_code,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
                "jobPostingInfo": parsed["info"],
                "hiringOrganization": parsed["employer"],
                "source_fields": {
                    "jobReqId": parsed["job_req_id"],
                    "startDate": parsed["start_date"],
                    "postedOn": parsed["posted_on"],
                    "timeType": parsed["time_type"],
                    "location": parsed["location"],
                    "additionalLocations": parsed["additional_locations"],
                    "externalUrl": parsed["external_url"],
                    "jobPostingSiteId": parsed["job_posting_site_id"],
                    "canApply": parsed["can_apply"],
                    "posted": parsed["posted"],
                    "country": parsed["country"],
                },
                "description_sha256": sha256(parsed["job_description"].encode()).hexdigest(),
                "description_normalized_chars": len(normalized),
                "description_fnv1a64": fnv1a64(normalized),
                "description_sections": sections,
                "description_html_blocks": html_blocks,
                "description_links": links,
                "role_attachments": attachments,
                "salary_sentences": salaries,
                "footer_section_headings_excluded_from_role_fields": sorted(footer_headings),
                "apply_route": {
                    "rendered_control": "Apply",
                    "control_href_suffix": "/apply",
                    "apply_url": job.apply_url,
                },
                "dates": {
                    "posting_date_field": "startDate",
                    "posting_date": parsed["start_date"],
                    "relative_ui_text_not_used_as_date": parsed["posted_on"],
                    "end_date": parsed["info"].get("endDate"),
                },
            }
            return job

        jobs = await self.hydrate(jobs, detail)
        self.result.jobs = jobs

        final = None
        final_listing = final_entry = final_official = final_board = None
        final_started = time.perf_counter()
        try:
            final_entry, final_listing, final_official, final_board = await self._snapshot()
            final_snapshot_seconds = max(0.0, time.perf_counter() - final_started)
            final = {
                "entry": final_entry,
                "listing": final_listing,
                "official": final_official,
                "board": final_board,
            }
        except Exception as exc:
            final_snapshot_seconds = max(0.0, time.perf_counter() - final_started)
            self.result.errors.append(f"Postman final source recheck failed: {type(exc).__name__}: {exc}")

        initial_official_by_id = {str(row["id"]): row for row in official["rows"]}
        initial_board_by_id = {str(row["bulletFields"][0]): row for row in board["rows"]}
        changed_fields = {}
        if final is not None:
            final_official_by_id = {str(row["id"]): row for row in final_official["rows"]}
            final_board_by_id = {str(row["bulletFields"][0]): row for row in final_board["rows"]}
            for identity in set(initial_official_by_id) | set(final_official_by_id):
                before, after = initial_official_by_id.get(identity), final_official_by_id.get(identity)
                if before is None or after is None:
                    changed_fields[identity] = ["membership"]
                    continue
                changed = sorted(
                    key for key in ("title", "url", "location", "department")
                    if before.get(key) != after.get(key)
                )
                if changed:
                    changed_fields[identity] = changed
            for identity in set(initial_board_by_id) | set(final_board_by_id):
                before, after = initial_board_by_id.get(identity), final_board_by_id.get(identity)
                if before is None or after is None:
                    changed_fields.setdefault(identity, []).append("board_membership")
                    continue
                changed = sorted(
                    key for key in ("title", "externalPath", "timeType", "locationsText")
                    if before.get(key) != after.get(key)
                )
                if changed:
                    changed_fields.setdefault(identity, []).extend(f"board_{key}" for key in changed)
            official_same = (official["job_ids"] == final_official["job_ids"]
                             and official["total"] == final_official["total"])
            board_same = (board["job_ids"] == final_board["job_ids"]
                          and board["total"] == final_board["total"])
            listing_same = (listing["visible_open_positions_text"] == final_listing["visible_open_positions_text"]
                            and listing["workday_alert_urls"] == final_listing["workday_alert_urls"])
            filters_same = listing["filters"] == final_listing["filters"]
            complete = (not changed_fields and official_same and board_same and listing_same
                        and set(official["job_ids"]) == set(board["job_ids"])
                        and len(jobs) == official["total"] and not final_official["issues"]
                        and not final_board["issues"])
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_listing_page"] = final_listing
            self.result.metadata["final_official_jobs"] = {
                key: value for key, value in final_official.items() if key != "rows"
            }
            self.result.metadata["final_board_snapshot"] = {
                key: value for key, value in final_board.items() if key != "rows"
            }
            self.result.metadata["membership_reconciliation"] = {
                "initial_official_ids": official["job_ids"],
                "final_official_ids": final_official["job_ids"],
                "initial_board_ids": board["job_ids"],
                "final_board_ids": final_board["job_ids"],
                "initial_official_total": official["total"],
                "final_official_total": final_official["total"],
                "initial_board_total": board["total"],
                "final_board_total": final_board["total"],
                "official_and_board_same_set": set(official["job_ids"]) == set(board["job_ids"]),
                "official_order_is_alphabetical_display": True,
                "board_order_is_recency_display": True,
                "same_official_ids_and_order": official_same,
                "same_board_ids_and_order": board_same,
                "same_listing_visible_count_and_board_link": listing_same,
                "filters_unchanged": filters_same,
                "changed_fields_by_job_id": changed_fields,
                "new_job_ids": [i for i in final_official["job_ids"] if i not in set(official["job_ids"])],
                "removed_job_ids": [i for i in official["job_ids"] if i not in set(final_official["job_ids"])],
                "complete": complete,
            }
            if not official_same or not board_same or changed_fields:
                self.result.errors.append("postman_listing_membership_changed_during_collection")
            if not listing_same:
                self.result.errors.append("postman_listing_page_changed_during_collection")
            if not filters_same:
                self.result.warnings.append("postman_listing_filters_changed_during_collection")
            self.result.errors.extend(final_official["issues"])
            self.result.errors.extend(final_board["issues"])
        else:
            self.result.metadata["membership_reconciliation"] = {
                "initial_official_ids": official["job_ids"],
                "initial_board_ids": board["job_ids"],
                "initial_official_total": official["total"],
                "initial_board_total": board["total"],
                "official_and_board_same_set": set(official["job_ids"]) == set(board["job_ids"]),
                "complete": False,
            }
            complete = False

        self.result.metadata["detail_hydration_mode"] = (
            "Each exact Workday job URL exposed by the official listing is hydrated through its "
            "public CxS detail JSON; jobDescription is the full employer-authored body."
        )
        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.metadata["official_jobs_count"] = official["total"]
        self.result.metadata["board_jobs_count"] = board["total"]
        self.result.metadata["jobs_collected"] = len(jobs)
        self.result.metadata["all_descriptions_verified"] = all(job.description_verified for job in jobs)
        self.result.metadata["current_native_membership_reconciled"] = bool(complete)
        self.result.listing_complete = bool(complete and len(jobs) == official["total"])
        self.result.timings["initial_source_snapshot_seconds"] = initial_snapshot_seconds
        self.result.timings["final_source_recheck_seconds"] = final_snapshot_seconds
        self.result.timings["source_collection_wall_seconds"] = initial_snapshot_seconds + final_snapshot_seconds
        self.result.timings["overall_run_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.errors = list(dict.fromkeys(self.result.errors))
        self.result.warnings = list(dict.fromkeys(self.result.warnings))
        return self.result
