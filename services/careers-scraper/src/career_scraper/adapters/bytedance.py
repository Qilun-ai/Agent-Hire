"""ByteDance's official careers portals.

The international listing is the company-published joinbytedance.com search
page linked from jobs.bytedance.com. The separately linked Mainland China
portal (jobs.bytedance.com/experienced) publishes its own all-roles board; its
search endpoint is signed by the page, so membership is captured from the
company's rendered pagination and archived as page payloads, while each exact
role is verified through the portal's public detail API.
"""
from __future__ import annotations

from copy import copy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import re
import time
from pathlib import PurePosixPath
from urllib.parse import urlencode, urljoin, urlsplit
from html import escape

from bs4 import BeautifulSoup

from .base import Adapter
from ..attachments import extract_attachment
from ..http import FetchError
from ..models import text_html


ENTRY_URL = "https://jobs.bytedance.com/en/position"
SEARCH_PAGE = "https://joinbytedance.com/search"
API_ROOT = "https://jobs.bytedance.com/api/v1/public/supplier"
FILTERS_URL = API_ROOT + "/config/job/filters"
POSITIONS_URL = API_ROOT + "/search/job/posts"
CHINA_ENTRY_URL = "https://jobs.bytedance.com/experienced"
CHINA_LISTING_URL = "https://jobs.bytedance.com/experienced/position"
CHINA_DETAIL_PAGE = "https://jobs.bytedance.com/experienced/position/{id}/detail"
CHINA_DETAIL_API = (
    "https://jobs.bytedance.com/api/v1/job/posts/{id}?portal_type=2&with_recommend=false"
)
CHINA_PAGE_SIZE = 10
CHINA_DISPLAY_CAP = 10000
PAGE_SIZE = 12

API_HEADERS = {
    "Content-Type": "application/json",
    "accept-language": "en-US",
    "website-path": "en",
    "x-tt-env": "boe_epam_api",
    "Origin": "https://joinbytedance.com",
}
BLANK_FILTERS = {
    "recruitment_id_list": [],
    "job_category_id_list": [],
    "subject_id_list": [],
    "location_code_list": [],
    "keyword": "",
}
ROLE_SECTIONS = ("description", "requirement")
ATTACHMENT_SUFFIXES = {".pdf", ".doc", ".docx", ".rtf"}
SECTION_LABELS = (
    "Responsibilities",
    "Qualifications",
    "Job Information",
)
CHINA_API_HEADERS = {
    "accept-language": "zh-CN,zh;q=0.9",
    "website-path": "society",
    "Referer": CHINA_LISTING_URL,
    "Origin": "https://jobs.bytedance.com",
}
CAMPUS_ENTRY_URL = "https://jobs.bytedance.com/campus"
CAMPUS_LISTING_URL = "https://jobs.bytedance.com/campus/position"
CAMPUS_DETAIL_PAGE = "https://jobs.bytedance.com/campus/position/{id}/detail"
CAMPUS_DETAIL_API = (
    "https://jobs.bytedance.com/api/v1/job/posts/{id}?portal_type=3&with_recommend=false"
)
CAMPUS_PAGE_SIZE = 10
CAMPUS_DISPLAYED_TOTAL = 6698
CAMPUS_TERMINAL_PAGE = 670
CAMPUS_TERMINAL_ROWS = 8
CAMPUS_API_HEADERS = {
    "accept-language": "zh-CN,zh;q=0.9",
    "website-path": "campus",
    "Referer": CAMPUS_LISTING_URL,
    "Origin": "https://jobs.bytedance.com",
}
CHINA_BLANK_FILTERS = {
    "keyword": "",
    "limit": CHINA_PAGE_SIZE,
    "offset": 0,
    "job_category_id_list": [],
    "tag_id_list": [],
    "location_code_list": [],
    "subject_id_list": [],
    "recruitment_id_list": [],
    "portal_type": 2,
    "job_function_id_list": [],
    "storefront_id_list": [],
    "portal_entrance": 1,
}


def _visible(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\u200b", " ")).strip()


def _city_values(row: dict) -> list:
    values = []
    for key in ("city_info", "city_list", "other_city_list", "secondary_city_list"):
        value = row.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value:
            values.append(value)
    return values


def china_detail_url(job_id: str) -> str:
    return CHINA_DETAIL_API.format(id=job_id)


def china_detail_page_url(job_id: str) -> str:
    return CHINA_DETAIL_PAGE.format(id=job_id)


def campus_detail_url(job_id: str) -> str:
    return CAMPUS_DETAIL_API.format(id=job_id)


def campus_detail_page_url(job_id: str) -> str:
    return CAMPUS_DETAIL_PAGE.format(id=job_id)


def _epoch_ms_iso(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return None
    if milliseconds <= 0:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()


def parse_china_listing_page(
    payload: dict,
    *,
    expected_total: int | None = None,
    displayed_total: int = CHINA_DISPLAY_CAP,
    page_size: int = CHINA_PAGE_SIZE,
    expected_rows: int | None = None,
    label: str = "China",
):
    """Validate one archived Mainland search page and return (rows, count).

    The signed search request itself is issued by the company page; only its
    response body is retained. Page size 10 and the displayed 10,000 cap were
    observed in the rendered portal. The campus journey reuses this parser
    with its own displayed total (6,698) and an 8-row terminal page.
    """
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError(f"ByteDance {label} listing response is not code 0")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"ByteDance {label} listing response has no data object")
    count = data.get("count")
    rows = data.get("job_post_list")
    if not isinstance(count, int) or count < 0 or count != displayed_total:
        raise ValueError(
            f"ByteDance {label} listing count is not the displayed cap "
            f"{displayed_total}: {count!r}"
        )
    if expected_total is not None and count != expected_total:
        raise ValueError(f"ByteDance {label} listing total changed between pages")
    required = expected_rows if expected_rows is not None else page_size
    if not isinstance(rows, list) or len(rows) != required:
        raise ValueError(f"ByteDance {label} listing page is not {required} rows")
    for row in rows:
        identity = row.get("id") if isinstance(row, dict) else None
        if not isinstance(identity, (int, str)) or not str(identity).isdigit():
            raise ValueError(f"ByteDance {label} listing row has no numeric id")
        if not isinstance(row.get("title"), str) or not row["title"].strip():
            raise ValueError(f"ByteDance {label} listing row {identity} has no title")
        for field in ROLE_SECTIONS:
            if not isinstance(row.get(field), str) or not row[field]:
                raise ValueError(
                    f"ByteDance {label} listing row {identity} has no {field}"
                )
    return rows, count


def parse_campus_listing_page(payload: dict, *, expected_total=None,
                              page_number: int | None = None):
    """Validate one archived campus search page (10 rows, 8 on page 670)."""
    expected_rows = CAMPUS_PAGE_SIZE
    if page_number == CAMPUS_TERMINAL_PAGE:
        expected_rows = CAMPUS_TERMINAL_ROWS
    return parse_china_listing_page(
        payload,
        expected_total=expected_total,
        displayed_total=CAMPUS_DISPLAYED_TOTAL,
        page_size=CAMPUS_PAGE_SIZE,
        expected_rows=expected_rows,
        label="campus",
    )


def parse_china_detail(payload: dict, job_id: str, *, label: str = "China") -> dict:
    """Validate the public Mainland detail payload for one listed job."""
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError(f"ByteDance {label} detail response is not code 0")
    detail = (payload.get("data") or {}).get("job_post_detail")
    if not isinstance(detail, dict):
        raise ValueError(f"ByteDance {label} detail response has no job_post_detail")
    if str(detail.get("id")) != str(job_id):
        raise ValueError(
            f"ByteDance {label} detail id does not match the listed job"
        )
    if not isinstance(detail.get("title"), str) or not detail["title"].strip():
        raise ValueError(f"ByteDance {label} detail has no title")
    for field in ROLE_SECTIONS:
        if not isinstance(detail.get(field), str) or not detail[field]:
            raise ValueError(f"ByteDance {label} detail has no {field}")
    return detail


def parse_campus_detail(payload: dict, job_id: str) -> dict:
    return parse_china_detail(payload, job_id, label="campus")


def china_detail_summary(detail: dict, sha256: str) -> dict:
    """Keep every public detail field without duplicating role bodies."""
    return {
        "id": str(detail.get("id")),
        "title": detail.get("title"),
        "url": china_detail_url(str(detail.get("id"))),
        "source_response_sha256": sha256,
        "code": detail.get("code"),
        "job_id": detail.get("job_id"),
        "publish_time": detail.get("publish_time"),
        "channel_online_status": detail.get("channel_online_status"),
        "job_category": detail.get("job_category"),
        "recruit_type": detail.get("recruit_type"),
        "city_info": detail.get("city_info"),
        "city_list": detail.get("city_list"),
        "tag_list": detail.get("tag_list"),
        "storefront_mode": detail.get("storefront_mode"),
        "storefront_list": detail.get("storefront_list"),
        "process_type": detail.get("process_type"),
        "job_post_info": detail.get("job_post_info"),
        "description_sha256": hashlib.sha256(
            (detail.get("description") or "").encode()).hexdigest(),
        "requirement_sha256": hashlib.sha256(
            (detail.get("requirement") or "").encode()).hexdigest(),
    }


def _search_url(offset: int) -> str:
    # This query order and page size were observed in the company UI. These
    # empty values describe the unfiltered listing; no country or job filters.
    query = [
        ("recruitment_id_list", ""),
        ("job_category_id_list", ""),
        ("subject_id_list", ""),
        ("location_code_list", ""),
        ("keyword", ""),
        ("limit", str(PAGE_SIZE)),
        ("offset", str(offset)),
    ]
    return SEARCH_PAGE + "?" + urlencode(query)


class ByteDance(Adapter):
    VERIFICATION_REVISION = "bytedance.official-international-search-and-exact-details.2"
    UNRESOLVED_JOURNEY = {
        "label": "Careers in China",
        "url": CHINA_ENTRY_URL,
        "scope": "ByteDance",
        "status": "unverified",
        "reason": (
            "The official Mainland portal is separately linked from the international "
            "careers page. Its current membership and pagination have not been reviewed "
            "in a supported browser. One unfiltered direct-client request to its published "
            "search endpoint returned HTTP 405 with an empty body; no retry or signature "
            "reconstruction was attempted."
        ),
        "evidence": "companies/bytedance/source-research.md",
    }

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.source_blocker = None
        self.unattempted_after_block = []

    def _single_attempt_http(self):
        """Share the caller's client/limiter/archive but disable HTTP retries.

        The default shared client retries 429s. This source review requires a
        single attempt so a challenge or rate limit is recorded and left alone.
        A shallow copy avoids changing retry behavior for other company tasks
        that may share the caller's HTTP instance.
        """
        safe_http = copy(self.http)
        safe_http.retries = 0
        return safe_http

    def _mark_source_blocker(self, exc, *, url=None):
        match = re.search(r"\bHTTP\s+(403|405|429)\b", str(exc), re.I)
        if not match and not re.search(r"human verification|verify you are human|captcha", str(exc), re.I):
            return
        self.source_blocker = {
            "status": int(match.group(1)) if match else None,
            "url": url,
            "error": str(exc),
            "retry": False,
        }
        self.result.metadata["source_blocker"] = self.source_blocker

    async def _filters(self):
        response = await self.http.request(
            "POST", FILTERS_URL, json={}, headers=API_HEADERS
        )
        if urlsplit(str(response.url)).hostname != "jobs.bytedance.com":
            raise ValueError("ByteDance filter config redirected away from its official API host")
        payload = response.json()
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise ValueError("ByteDance official filter config response is not code 0 data")
        self.result.metadata["filter_config"] = {
            "url": str(response.url),
            "job_type_count": len(payload["data"].get("job_type_list") or []),
            "city_count": len(payload["data"].get("city_list") or []),
            "recruitment_type_count": len(payload["data"].get("recruitment_type_list") or []),
            "subject_count": len(payload["data"].get("job_subject_list") or []),
            "tag_count": len(payload["data"].get("tag_list") or []),
        }

    async def listing_pass(self):
        """Walk every native page once, retaining the exact returned rows."""
        rows, page_records, seen_pages = {}, [], set()
        pass_index = len(self.result.metadata.get("native_enumerations", [])) + 1
        expected_total = None
        offset = 0
        complete = True

        while True:
            if not self.page_guard():
                complete = False
                break
            body = {**BLANK_FILTERS, "limit": PAGE_SIZE, "offset": offset}
            try:
                response = await self.http.request(
                    "POST", POSITIONS_URL, json=body, headers=API_HEADERS
                )
                payload = response.json()
            except (FetchError, ValueError) as exc:
                if isinstance(exc, FetchError):
                    self._mark_source_blocker(exc, url=POSITIONS_URL)
                self.result.errors.append(
                    f"bytedance_international_request_failed_offset_{offset}: {exc}"
                )
                complete = False
                break
            if urlsplit(str(response.url)).hostname != "jobs.bytedance.com":
                self.result.errors.append(
                    f"bytedance_international_redirected_offset_{offset}"
                )
                complete = False
                break
            data = payload.get("data")
            page_rows = data.get("job_post_list") if isinstance(data, dict) else None
            total = data.get("count") if isinstance(data, dict) else None
            if (payload.get("code") != 0 or not isinstance(page_rows, list)
                    or not isinstance(total, int) or total < 0 or len(page_rows) > PAGE_SIZE):
                self.result.errors.append(
                    f"bytedance_international_invalid_response_offset_{offset}"
                )
                complete = False
                break
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                self.result.errors.append("bytedance_international_total_changed_during_pagination")
                complete = False
                break
            if offset in seen_pages:
                self.result.errors.append("bytedance_international_offset_repeated")
                complete = False
                break
            seen_pages.add(offset)

            expected_rows = min(PAGE_SIZE, max(0, total - offset))
            if len(page_rows) != expected_rows:
                self.result.errors.append(
                    f"bytedance_international_page_length_mismatch_offset_{offset}"
                )
                complete = False
                break
            if total > 0 and not page_rows:
                self.result.errors.append("bytedance_international_unexpected_empty_page")
                complete = False
                break

            page_ids = []
            sampled = False
            for row in page_rows:
                identity = row.get("id")
                if not isinstance(identity, (int, str)) or not str(identity).isdigit():
                    self.result.errors.append(
                        f"bytedance_international_missing_native_id_offset_{offset}"
                    )
                    complete = False
                    continue
                identity = str(identity)
                if identity in rows:
                    self.result.errors.append("bytedance_international_id_repeated_across_pages")
                    complete = False
                    continue
                if not isinstance(row.get("title"), str) or not row["title"].strip():
                    self.result.errors.append(
                        f"bytedance_international_missing_title_{identity}"
                    )
                    complete = False
                    continue
                rows[identity] = row
                page_ids.append(identity)
                if self.options.max_jobs and len(rows) >= self.options.max_jobs:
                    self.result.warnings.append("sample_limit")
                    complete = False
                    sampled = True
                    break
            page_records.append({
                "offset": offset,
                "limit": PAGE_SIZE,
                "url": _search_url(offset),
                "request_url": str(response.request.url),
                "ids": page_ids,
                "count": total,
            })
            progress = getattr(self.options, "source_progress", None)
            if progress:
                progress(
                    self.result,
                    "initial_membership" if pass_index == 1 else "final_membership",
                    offset,
                    total,
                    len(rows),
                )

            if sampled:
                break
            if offset + PAGE_SIZE >= total:
                break
            if not page_ids:
                self.result.errors.append("bytedance_international_pagination_ended_early")
                complete = False
                break
            offset += PAGE_SIZE

        self.result.metadata.setdefault("native_enumerations", []).append({
            "portal": SEARCH_PAGE,
            "total": expected_total,
            "page_size": PAGE_SIZE,
            "pages": page_records,
            "job_ids": list(rows),
            "complete": complete and expected_total is not None and len(rows) == expected_total,
        })
        if expected_total is not None and self.result.expected_total is None:
            self.result.expected_total = expected_total
        return rows, expected_total, complete and expected_total is not None and len(rows) == expected_total

    async def detail(self, job):
        if self.source_blocker:
            self.unattempted_after_block.append(job.job_id)
            raise FetchError(
                "ByteDance detail request stopped after a public-source block; no further request sent"
            )
        try:
            response = await self.http.get(job.url)
        except FetchError as exc:
            self._mark_source_blocker(exc, url=job.url)
            raise
        parsed = urlsplit(str(response.url))
        expected_path = f"/search/{job.job_id}"
        if parsed.hostname != "joinbytedance.com" or parsed.path != expected_path:
            raise ValueError("ByteDance exact job page redirected away from its listed position ID")
        soup = BeautifulSoup(response.content, "lxml")
        body_text = soup.get_text(" ", strip=True)
        if re.search(r"human verification|verify you are human|captcha", body_text, re.I):
            self._mark_source_blocker(
                ValueError("ByteDance detail page returned a human-verification challenge"),
                url=str(response.url),
            )
            raise ValueError("ByteDance detail page returned a human-verification challenge")
        headings = soup.find_all(["h1", "h2"])
        if not headings:
            job.raw["detail_failure"] = {
                "url": str(response.url),
                "source_response_sha256": hashlib.sha256(response.content).hexdigest(),
                "reason": "exact detail page contains no rendered h1/h2 or role body",
                "visible_text": _visible(body_text),
            }
            raise ValueError("ByteDance exact detail page returned no title or role body")
        heading = next(
            (node for node in headings
             if _visible(node.get_text(" ", strip=True)) == _visible(job.title)),
            None,
        )
        if heading is None:
            raise ValueError("ByteDance exact job page title does not match the listed position")
        rendered_title = _visible(heading.get_text(" ", strip=True))

        row = job.raw.get("listing", {})
        if _visible(row.get("title")) != rendered_title:
            raise ValueError("ByteDance exact detail title differs from its raw listing title")
        values = {}
        for block in soup.find_all("div"):
            if not {"flex", "gap-[4px]"}.issubset(block.get("class", [])):
                continue
            labels = block.find_all("p", recursive=False)
            if len(labels) != 2:
                continue
            label = labels[0].get_text(" ", strip=True).rstrip(":").strip()
            values[label] = labels[1].get_text(" ", strip=True)
        if row.get("code") and values.get("Job Code") != str(row["code"]):
            raise ValueError("ByteDance rendered job code does not match its exact listing row")

        visible = _visible(body_text)
        source_fields = {}
        for field_name in ROLE_SECTIONS:
            value = row.get(field_name)
            if value is None or value == "":
                continue
            if not isinstance(value, str) or _visible(text_html(value)) not in visible:
                raise ValueError(
                    f"ByteDance rendered role page does not contain its full {field_name} source field"
                )
            source_fields[field_name] = value
        if not any(len(_visible(value)) >= 100 for value in source_fields.values()):
            raise ValueError("ByteDance exact listing has no substantive full role-description field")

        apply_links = []
        for button in soup.select('button[data-tracking="job-apply-button"]'):
            anchor = button.find_parent("a", href=True)
            if anchor is not None:
                apply_links.append(anchor["href"])
        expected_apply = f"https://jobs.bytedance.com/en/resume/{job.job_id}/apply"
        if not apply_links or any(link != expected_apply for link in apply_links):
            raise ValueError("ByteDance exact detail page has no consistent same-job apply link")

        sections = {}
        for label in SECTION_LABELS:
            title = soup.find("p", class_="bd-title", string=label)
            if title is None:
                if label in {"Responsibilities", "Qualifications"} and row.get(
                    "description" if label == "Responsibilities" else "requirement"
                ):
                    raise ValueError(f"ByteDance exact page is missing its {label} section heading")
                continue
            sibling = title.find_next_sibling()
            sections[label] = str(sibling) if sibling else ""

        job_info_heading = soup.find("p", class_="bd-title", string="Job Information")
        job_info_blocks = []
        if job_info_heading:
            job_info_blocks = [
                child for child in job_info_heading.parent.find_all(recursive=False)
                if child.name == "div"
            ]
        salary_text = []
        if job_info_blocks:
            for paragraph in job_info_blocks[0].select("p"):
                text = paragraph.get_text(" ", strip=True)
                if re.search(r"\b(?:salary|compensation|pay transparency)\b", text, re.I):
                    salary_text.append(text)

        description_html = "\n\n".join(
            source_fields[field_name]
            for field_name in ROLE_SECTIONS
            if field_name in source_fields
        )
        job.description_html = description_html
        job.description_method = "bytedance.international.listing_full_role_fields_verified_on_detail.2"
        job.description_source_url = str(response.url)
        job.responsibilities = source_fields.get("description")
        job.qualifications = source_fields.get("requirement")
        job.apply_url = expected_apply
        job.locations = _city_values(row)
        job.department = row.get("department_info") or row.get("job_category")
        job.team = values.get("Team")
        job.employment_type = values.get("Employment Type") or row.get("recruit_type")
        job.salary = [{"source_text": text, "source_section": "Job Information"}
                      for text in salary_text] or None
        job.language = "en"
        # ByteDance's published list record does not expose a verified posting
        # date. Keep every raw date-like field in the listing record and leave
        # date_posted/valid_through null.
        job.date_posted = None
        job.valid_through = None

        attachment_sources = {}
        for field_name in ROLE_SECTIONS:
            section_label = "Responsibilities" if field_name == "description" else "Qualifications"
            section_html = sections.get(section_label) or row.get(field_name)
            if not isinstance(section_html, str):
                continue
            field_soup = BeautifulSoup(section_html, "lxml")
            for anchor in field_soup.select("a[href]"):
                href = urljoin(job.url, anchor.get("href", ""))
                suffix = PurePosixPath(urlsplit(href).path).suffix.lower()
                if suffix in ATTACHMENT_SUFFIXES:
                    attachment_sources.setdefault(href, []).append(section_label)
        attachments = []
        for url, sources in attachment_sources.items():
            file_response = await self.http.get(url)
            extracted = extract_attachment(
                file_response.content, PurePosixPath(urlsplit(url).path).name
            )
            attachments.append({"url": url, "source_sections": sources, **extracted})
            job.description_html += "\n\n<pre>" + escape(extracted["text"]) + "</pre>"
        job.raw["attachments"] = attachments

        additional_sections = {}
        if job_info_heading:
            for block in job_info_blocks[1:]:
                text = block.get_text("\n", strip=True)
                label = next(
                    (candidate for candidate in (
                        "About Doubao (Seed)",
                        "Why Join ByteDance",
                        "Diversity & Inclusion",
                        "Reasonable Accommodation",
                    ) if candidate in text),
                    None,
                )
                if label:
                    additional_sections[label] = text

        job.raw["detail"] = {
            "id": job.job_id,
            "title": heading.get_text(" ", strip=True),
            "url": str(response.url),
            "source_response_sha256": hashlib.sha256(response.content).hexdigest(),
            "metadata": values,
            "sections": sections,
            "additional_sections": additional_sections,
            "job_information_html": str(job_info_blocks[0]) if job_info_blocks else None,
            "salary_text": salary_text,
            "apply_urls": apply_links,
            "attachment_urls": list(attachment_sources),
        }
        return job

    async def run(self):
        original_http = self.http
        self.http = self._single_attempt_http()
        started = time.perf_counter()
        try:
            self.result.metadata.update(
                verification_revision=self.VERIFICATION_REVISION,
                official_entry=ENTRY_URL,
                international_listing_url=SEARCH_PAGE,
                listing_authority=(
                    "ByteDance official en/position entry redirected to its own "
                    "joinbytedance.com international all-roles search page"
                ),
                page_size=PAGE_SIZE,
                unfiltered_query={**BLANK_FILTERS, "limit": PAGE_SIZE, "offset": 0},
                unresolved_published_journeys=[self.UNRESOLVED_JOURNEY],
            )
            self.result.errors.append("bytedance_china_published_membership_unverified")
            try:
                filter_started = time.perf_counter()
                await self._filters()
                self.result.timings["international_filter_config_seconds"] = (
                    time.perf_counter() - filter_started
                )
                listing_started = time.perf_counter()
                initial, total, initial_complete = await self.listing_pass()
            except (FetchError, ValueError, KeyError, TypeError) as exc:
                if isinstance(exc, FetchError):
                    self._mark_source_blocker(exc, url=FILTERS_URL)
                self.result.errors.append(
                    f"bytedance_international_listing_failed: {type(exc).__name__}: {exc}"
                )
                self.result.listing_complete = False
                self.result.metadata["international_listing_status"] = "failed_or_unclosed"
                self.result.timings["international_collection_seconds"] = time.perf_counter() - started
                self.result.finish()
                return self.result
            self.result.timings["international_initial_listing_seconds"] = (
                time.perf_counter() - listing_started
            )

            if self.result.expected_total is not None and total != self.result.expected_total:
                self.result.errors.append("bytedance_international_total_changed_between_passes")
                initial_complete = False
            for identity, row in initial.items():
                raw = {"listing": row}
                url = f"{SEARCH_PAGE}/{identity}"
                self.add(self.job(
                    raw,
                    # Keep the exact API value in raw.listing; the public card
                    # renders surrounding whitespace as ordinary HTML space.
                    title=_visible(row.get("title")),
                    job_id=identity,
                    requisition_id=str(row["code"]) if row.get("code") is not None else None,
                    url=url,
                    employer="ByteDance",
                    department=row.get("department_info") or row.get("job_category"),
                    employment_type=row.get("recruit_type"),
                    locations=_city_values(row),
                ))

            sampled = "sample_limit" in self.result.warnings
            details_ok = True
            if self.result.jobs and not sampled:
                old_cache = self.options.cached_jobs
                self.options.cached_jobs = {
                    url: cached
                    for url, cached in old_cache.items()
                    if cached.description_method
                    == "bytedance.international.listing_full_role_fields_verified_on_detail.2"
                    and cached.job_id in initial
                    and cached.title == _visible(initial[cached.job_id].get("title"))
                    and cached.raw.get("listing", {}).get("id") in (cached.job_id, int(cached.job_id))
                }
                try:
                    await self.hydrate(self.result.jobs, self.detail)
                except Exception as exc:
                    details_ok = False
                    self.result.errors.append(
                        f"bytedance_international_detail_phase_failed: {type(exc).__name__}: {exc}"
                    )
                finally:
                    self.options.cached_jobs = old_cache
                details_ok = (
                    self.result.metadata.get("details", {}).get("failed", 0) == 0
                    and all(job.description_verified for job in self.result.jobs)
                )
                if self.unattempted_after_block:
                    self.result.metadata["unattempted_details_after_source_block"] = (
                        self.unattempted_after_block
                    )

            final_complete = False
            final = {}
            if not sampled and initial_complete and not self.source_blocker:
                try:
                    final_started = time.perf_counter()
                    final, final_total, final_complete = await self.listing_pass()
                    self.result.timings["international_final_membership_seconds"] = (
                        time.perf_counter() - final_started
                    )
                    same_ids = set(final) == set(initial)
                    same_total = final_total == total
                    self.result.metadata["international_final_membership"] = {
                        "initial_count": len(initial),
                        "final_count": len(final),
                        "initial_total": total,
                        "final_total": final_total,
                        "same_id_set": same_ids,
                        "same_total": same_total,
                        "initial_ids": list(initial),
                        "final_ids": list(final),
                    }
                    if not same_ids:
                        self.result.errors.append("bytedance_international_membership_changed")
                    if not same_total:
                        self.result.errors.append("bytedance_international_total_changed_finally")
                    final_complete = final_complete and same_ids and same_total
                except (FetchError, ValueError, KeyError, TypeError) as exc:
                    self.result.errors.append(
                        f"bytedance_international_final_recheck_failed: {type(exc).__name__}: {exc}"
                    )

            self.result.metadata["company_published_job_ids"] = list(initial)
            self.result.metadata["international_listing_status"] = (
                "complete" if initial_complete and final_complete and details_ok and not sampled
                else "partial"
            )
            self.result.listing_complete = (
                initial_complete and final_complete and details_ok and not sampled
                and len(initial) == total and not self.result.errors
            )
            self.result.timings["international_collection_seconds"] = (
                self.result.timings.get("international_filter_config_seconds", 0.0)
                + self.result.timings.get("international_initial_listing_seconds", 0.0)
                + self.result.timings.get("international_final_membership_seconds", 0.0)
            )
            self.result.finish()
            return self.result
        finally:
            self.http = original_http


class _ByteDanceChinaPortal(Adapter):
    """Shared collector for ByteDance's official Mainland China portals.

    The portals' signed search XHR cannot be reproduced from a plain client,
    so listing membership is read from the exact page payloads captured while
    paginating the company's own rendered page. Each listed role is then
    verified against the portal's public detail API, which is not signed.
    """

    VERIFICATION_REVISION = "bytedance.official-china-portal-listing-and-detail-api.1"
    ENTRY = CHINA_ENTRY_URL
    LISTING = CHINA_LISTING_URL
    DETAIL_PAGE_TMPL = CHINA_DETAIL_PAGE
    DETAIL_API_TMPL = CHINA_DETAIL_API
    PAGE_SIZE = CHINA_PAGE_SIZE
    DISPLAYED_TOTAL = CHINA_DISPLAY_CAP
    API_HEADERS = CHINA_API_HEADERS
    PORTAL_KEY = "mainland_china"
    PORTAL_LABEL = "China"
    LANGUAGE = "zh-CN"
    ERROR_PREFIX = "bytedance_china"
    LISTING_METADATA_KEY = "china_native_listing"
    DETAILS_METADATA_KEY = "china_details"
    DETAIL_METHOD = (
        "bytedance.china.listing_full_role_fields_verified_on_detail_api.1"
    )
    APPLY_NOTE = (
        "The Mainland detail API publishes no static apply URL; the rendered "
        "detail page renders a login-gated 投递 action."
    )
    AUTHORITY = (
        "ByteDance-published Mainland China all-roles board linked from "
        "the international careers footer as Careers in China"
    )

    def __init__(self, http, company, source, options, *, pages_dir=None,
                 responses_dir=None):
        super().__init__(http, company, source, options)
        self.pages_dir = pages_dir
        self.responses_dir = (
            responses_dir if responses_dir is not None
            else (pages_dir.parent / "responses" if pages_dir is not None else None)
        )
        self.source_blocker = None
        self.unattempted_after_block = []

    def detail_page_url(self, job_id: str) -> str:
        return self.DETAIL_PAGE_TMPL.format(id=job_id)

    def detail_api_url(self, job_id: str) -> str:
        return self.DETAIL_API_TMPL.format(id=job_id)

    def load_listing_pages(self, paths=None):
        """Load and reconcile the captured native listing payloads."""
        if paths is None:
            if self.pages_dir is None:
                raise ValueError(
                    f"ByteDance {self.PORTAL_LABEL} listing page directory is required"
                )
            paths = sorted(self.pages_dir.glob("page-*.json"))
        else:
            paths = sorted(paths)
        rows, page_records = {}, []
        expected_total = None
        complete = True
        for path in paths:
            match = re.search(r"page-(\d+)\.json$", str(path))
            page_number = int(match.group(1)) if match else len(page_records) + 1
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                self.result.errors.append(
                    f"{self.ERROR_PREFIX}_listing_page_invalid_page_{page_number}: {exc}"
                )
                complete = False
                break
            count_hint = ((payload.get("data") or {}).get("count")
                          if isinstance(payload, dict) else None)
            expected_rows = self.PAGE_SIZE
            if isinstance(count_hint, int) and count_hint > 0:
                terminal_pages = (
                    count_hint + self.PAGE_SIZE - 1
                ) // self.PAGE_SIZE
                if page_number == terminal_pages:
                    expected_rows = (
                        count_hint - (terminal_pages - 1) * self.PAGE_SIZE
                    )
            try:
                page_rows, count = parse_china_listing_page(
                    payload,
                    expected_total=expected_total,
                    displayed_total=self.DISPLAYED_TOTAL,
                    page_size=self.PAGE_SIZE,
                    expected_rows=expected_rows,
                    label=self.PORTAL_LABEL,
                )
            except (OSError, ValueError, TypeError) as exc:
                self.result.errors.append(
                    f"{self.ERROR_PREFIX}_listing_page_invalid_page_{page_number}: {exc}"
                )
                complete = False
                break
            if expected_total is None:
                expected_total = count
            offset = (page_number - 1) * self.PAGE_SIZE
            if page_number != len(page_records) + 1:
                self.result.errors.append(
                    f"{self.ERROR_PREFIX}_listing_page_sequence_gap"
                )
                complete = False
                break
            page_ids = []
            for row in page_rows:
                identity = str(row["id"])
                if identity in rows:
                    self.result.errors.append(
                        f"{self.ERROR_PREFIX}_listing_id_repeated_across_pages"
                    )
                    complete = False
                    continue
                rows[identity] = row
                page_ids.append(identity)
            expected_for_page = (
                min(self.PAGE_SIZE, expected_total - offset)
                if expected_total is not None else self.PAGE_SIZE
            )
            if len(page_rows) != expected_for_page:
                self.result.errors.append(
                    f"{self.ERROR_PREFIX}_listing_page_length_mismatch_page_{page_number}"
                )
                complete = False
            page_records.append({
                "page": page_number,
                "offset": offset,
                "limit": self.PAGE_SIZE,
                "ids": page_ids,
                "count": count,
                "source_file": str(path),
            })
        terminal_pages = (
            (expected_total + self.PAGE_SIZE - 1) // self.PAGE_SIZE
            if expected_total else 0
        )
        if expected_total is not None and len(page_records) != terminal_pages:
            self.result.errors.append(
                f"{self.ERROR_PREFIX}_listing_page_count_"
                f"{len(page_records)}_expected_{terminal_pages}"
            )
            complete = False
        if expected_total is not None and len(rows) != expected_total:
            self.result.errors.append(
                f"{self.ERROR_PREFIX}_listing_id_count_"
                f"{len(rows)}_expected_{expected_total}"
            )
            complete = False
        self.result.pages = len(page_records)
        self.result.expected_total = expected_total
        self.result.metadata[self.LISTING_METADATA_KEY] = {
            "portal": self.LISTING,
            "displayed_cap": self.DISPLAYED_TOTAL,
            "count_reported_on_every_page": expected_total,
            "page_size": self.PAGE_SIZE,
            "pages": page_records,
            "job_ids": list(rows),
            "complete": complete,
        }
        return rows, expected_total, complete

    def _job_from_row(self, row):
        identity = str(row["id"])
        job = self.job(
            {"listing": row, "portal": self.PORTAL_KEY},
            title=_visible(row.get("title")),
            job_id=identity,
            requisition_id=str(row["code"]) if row.get("code") is not None else None,
            url=self.detail_page_url(identity),
            employer="ByteDance",
            department=row.get("job_category"),
            employment_type=row.get("recruit_type"),
            locations=_city_values(row),
            language=self.LANGUAGE,
        )
        job.date_posted = _epoch_ms_iso(row.get("publish_time"))
        job.date_posted_raw = row.get("publish_time")
        return job

    def _apply_detail(self, job, detail, sha256):
        row = job.raw["listing"]
        if _visible(detail.get("title")) != _visible(row.get("title")):
            raise ValueError(
                f"ByteDance {self.PORTAL_LABEL} detail title does not match "
                "its listing title"
            )
        mismatched = [
            field for field in ROLE_SECTIONS
            if _visible(detail.get(field)) != _visible(row.get(field))
        ]
        if mismatched:
            raise ValueError(
                f"ByteDance {self.PORTAL_LABEL} detail role fields differ from "
                "the listing: " + ", ".join(mismatched)
            )
        job.description_html = "\n\n".join(detail[field] for field in ROLE_SECTIONS)
        job.description_method = self.DETAIL_METHOD
        job.description_source_url = self.detail_api_url(job.job_id)
        job.responsibilities = detail.get("description")
        job.qualifications = detail.get("requirement")
        job.department = detail.get("job_category") or row.get("job_category")
        job.employment_type = detail.get("recruit_type") or row.get("recruit_type")
        job.locations = (
            detail.get("city_list")
            or ([detail["city_info"]] if detail.get("city_info") else _city_values(row))
        )
        info = detail.get("job_post_info") or {}
        salary = {
            key: info.get(key) for key in ("min_salary", "max_salary", "currency")
            if info.get(key) not in (None, "")
        }
        job.salary = salary or None
        job.date_posted = _epoch_ms_iso(detail.get("publish_time")) or job.date_posted
        job.date_posted_raw = detail.get("publish_time") or job.date_posted_raw
        job.raw["detail"] = china_detail_summary(detail, sha256)
        job.raw["detail"]["listing_role_fields_match"] = True
        job.raw["detail"]["apply_url"] = None
        job.raw["detail"]["apply_url_note"] = self.APPLY_NOTE
        return job

    async def detail(self, job):
        if self.source_blocker:
            self.unattempted_after_block.append(job.job_id)
            raise FetchError(
                f"ByteDance {self.PORTAL_LABEL} detail request stopped after "
                "a source block; no further request sent"
            )
        url = self.detail_api_url(job.job_id)
        try:
            response = await self.http.get(url, headers=self.API_HEADERS)
        except FetchError as exc:
            match = re.search(r"\bHTTP\s+(403|405|429)\b", str(exc))
            if match:
                self.source_blocker = {
                    "status": int(match.group(1)), "url": url, "error": str(exc),
                    "retry": False,
                }
                self.result.metadata["source_blocker"] = self.source_blocker
            raise
        detail = parse_china_detail(
            response.json(), job.job_id, label=self.PORTAL_LABEL
        )
        return self._apply_detail(
            job, detail, hashlib.sha256(response.content).hexdigest()
        )

    def build_from_archives(self, manifest_entries):
        """Build a verified snapshot from already-archived detail responses."""
        rows, total, complete = self.load_listing_pages()
        for row in rows.values():
            self.add(self._job_from_row(row))
        by_id = {}
        for entry in manifest_entries:
            identity = str(entry.get("job_id"))
            # A retry that failed must not hide an earlier successful archive.
            if entry.get("ok") or identity not in by_id:
                by_id[identity] = entry
        archived = 0
        for job in self.result.jobs:
            entry = by_id.get(job.job_id)
            if not entry or not entry.get("ok"):
                job.description_unavailable = True
                job.warnings.append("detail_fetch_failed")
                self.result.errors.append(
                    f"{self.ERROR_PREFIX}_detail_missing_{job.job_id}: "
                    f"{(entry or {}).get('error', 'no archive entry')}"
                )
                continue
            archive = None
            if self.responses_dir is not None:
                archive = self.responses_dir / f"{entry['sha256']}.gz"
            if archive is None or not archive.exists():
                job.description_unavailable = True
                job.warnings.append("detail_fetch_failed")
                self.result.errors.append(
                    f"{self.ERROR_PREFIX}_detail_archive_missing_{job.job_id}"
                )
                continue
            with gzip.open(archive, "rb") as handle:
                body = handle.read()
            if hashlib.sha256(body).hexdigest() != entry["sha256"]:
                job.description_unavailable = True
                job.warnings.append("detail_fetch_failed")
                self.result.errors.append(
                    f"{self.ERROR_PREFIX}_detail_archive_hash_mismatch_{job.job_id}"
                )
                continue
            try:
                self._apply_detail(
                    job,
                    parse_china_detail(
                        json.loads(body), job.job_id, label=self.PORTAL_LABEL
                    ),
                    entry["sha256"],
                )
                archived += 1
            except (ValueError, TypeError) as exc:
                job.description_unavailable = True
                job.warnings.append("detail_fetch_failed")
                self.result.errors.append(
                    f"{self.ERROR_PREFIX}_detail_invalid_{job.job_id}: {exc}"
                )
        for job in self.result.jobs:
            self.result.validate_job(job)
        verified = sum(job.description_verified for job in self.result.jobs)
        self.result.metadata[self.DETAILS_METADATA_KEY] = {
            "total": len(self.result.jobs), "archived": archived, "verified": verified,
            "failed": len(self.result.jobs) - verified,
            "source": "archived public detail API responses",
        }
        self.result.listing_complete = (
            complete and verified == len(self.result.jobs) and not self.result.errors
            and total is not None and len(rows) == total
        )
        self.result.finish()
        return self.result

    async def run(self):
        started = time.perf_counter()
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=self.ENTRY,
            listing_url=self.LISTING,
            displayed_total=self.DISPLAYED_TOTAL,
            page_size=self.PAGE_SIZE,
            detail_api=self.DETAIL_API_TMPL.format(id="{id}"),
            listing_authority=self.AUTHORITY,
        )
        try:
            listing_started = time.perf_counter()
            rows, total, complete = self.load_listing_pages()
            self.result.timings["china_listing_load_seconds"] = (
                time.perf_counter() - listing_started
            )
        except (ValueError, OSError, TypeError) as exc:
            self.result.errors.append(
                f"{self.ERROR_PREFIX}_listing_failed: {exc}"
            )
            self.result.listing_complete = False
            self.result.finish()
            return self.result
        for row in rows.values():
            self.add(self._job_from_row(row))
        details_ok = True
        if self.result.jobs:
            try:
                await self.hydrate(self.result.jobs, self.detail)
            except Exception as exc:
                details_ok = False
                self.result.errors.append(
                    f"{self.ERROR_PREFIX}_detail_phase_failed: "
                    f"{type(exc).__name__}: {exc}"
                )
            details_ok = (
                self.result.metadata.get("details", {}).get("failed", 0) == 0
                and all(job.description_verified for job in self.result.jobs)
            )
        self.result.listing_complete = (
            complete and details_ok and total is not None and len(rows) == total
            and not self.result.errors
        )
        self.result.timings["china_collection_seconds"] = time.perf_counter() - started
        self.result.finish()
        return self.result


class ByteDanceChina(_ByteDanceChinaPortal):
    """ByteDance's official Mainland China portal (jobs.bytedance.com/experienced)."""


class ByteDanceCampus(_ByteDanceChinaPortal):
    """ByteDance's official China campus portal (jobs.bytedance.com/campus).

    The campus board is linked from the experienced portal's navigation as
    校园招聘 and publishes 6,698 roles on 670 native pages (8 rows on page 670).
    """

    VERIFICATION_REVISION = "bytedance.official-campus-portal-listing-and-detail-api.1"
    ENTRY = CAMPUS_ENTRY_URL
    LISTING = CAMPUS_LISTING_URL
    DETAIL_PAGE_TMPL = CAMPUS_DETAIL_PAGE
    DETAIL_API_TMPL = CAMPUS_DETAIL_API
    PAGE_SIZE = CAMPUS_PAGE_SIZE
    DISPLAYED_TOTAL = CAMPUS_DISPLAYED_TOTAL
    API_HEADERS = CAMPUS_API_HEADERS
    PORTAL_KEY = "campus"
    PORTAL_LABEL = "campus"
    LANGUAGE = "zh-CN"
    ERROR_PREFIX = "bytedance_campus"
    LISTING_METADATA_KEY = "campus_native_listing"
    DETAILS_METADATA_KEY = "campus_details"
    DETAIL_METHOD = (
        "bytedance.campus.listing_full_role_fields_verified_on_detail_api.1"
    )
    APPLY_NOTE = (
        "The campus detail API publishes no static apply URL; the rendered "
        "detail page renders a login-gated 投递 action."
    )
    AUTHORITY = (
        "ByteDance-published China campus board linked from the experienced "
        "portal navigation as 校园招聘"
    )
