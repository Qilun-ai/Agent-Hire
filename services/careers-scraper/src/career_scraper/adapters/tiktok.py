"""TikTok's official Life at TikTok careers portal.

Official journey: https://lifeattiktok.com/search (TikTok's own #LifeAtTikTok
careers site). The site's shipped Next.js client chunk publishes the exact API
client used by the search page:

    base      https://api.lifeattiktok.com/api/v1/public/supplier
    origin    https://lifeattiktok.com
    header    website-path: tiktok
    routes    /config/job/filters, /search/job/posts, /job/posts/{id}

The HTML host returns HTTP 503 to this worker's egress, but the company-scoped
API host is reachable. The unfiltered API count (4,307) and first page IDs
reconcile with the independently rendered official listing (4,307 roles, 12
per page, 359 pages). The adapter never uses a guessed ATS slug, a parent
company board, a keyword search or an aggregator.

The API does not expose a posting date for these roles; date_posted and
valid_through stay null. Salary is parsed only from the published pay
transparency field; absent values stay null.
"""
from __future__ import annotations

import asyncio
from copy import copy
from html import escape
import hashlib
import re
import time
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError


ENTRY_URL = "https://lifeattiktok.com/search"
API_ROOT = "https://api.lifeattiktok.com/api/v1/public/supplier"
FILTERS_URL = API_ROOT + "/config/job/filters"
SEARCH_URL = API_ROOT + "/search/job/posts"
DETAIL_URL = API_ROOT + "/job/posts/{job_id}"
PAGE_SIZE = 12

API_HEADERS = {
    "Content-Type": "application/json",
    "accept-language": "en-US",
    "Origin": "https://lifeattiktok.com",
    "website-path": "tiktok",
}
BLANK_FILTERS = {
    "recruitment_id_list": [],
    "job_category_id_list": [],
    "subject_id_list": [],
    "location_code_list": [],
    "keyword": "",
}
PAY_FIELD_MARKER = "pay transparency"

# The first sentence of the published US pay-transparency field, e.g.
# "The base salary range for this position in the selected city is
#  $218400 - $480000 annually."
SALARY_RANGE = re.compile(
    r"(?:base\s+)?salary\s+range[^.]*?\b(?:is|of|:)\s*"
    r"(?P<currency1>US\$|S\$|HK\$|A\$|C\$|USD|SGD|HKD|AUD|CAD|GBP|EUR|INR|£|€|\$|₹)?\s*"
    r"(?P<min>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?:-|–|—|to)\s*"
    r"(?P<currency2>US\$|S\$|HK\$|A\$|C\$|USD|SGD|HKD|AUD|CAD|GBP|EUR|INR|£|€|\$|₹)?\s*"
    r"(?P<max>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<period>annually|per\s+year|per\s+annum|hourly|per\s+hour|monthly|per\s+month|weekly|per\s+week)?",
    re.I,
)
CURRENCY_NAMES = {
    "$": "USD", "US$": "USD", "USD": "USD",
    "S$": "SGD", "SGD": "SGD",
    "HK$": "HKD", "HKD": "HKD",
    "A$": "AUD", "AUD": "AUD",
    "C$": "CAD", "CAD": "CAD",
    "£": "GBP", "GBP": "GBP",
    "€": "EUR", "EUR": "EUR",
    "₹": "INR", "INR": "INR",
}
PERIOD_NAMES = {
    "annually": "year", "per year": "year", "per annum": "year",
    "hourly": "hour", "per hour": "hour",
    "monthly": "month", "per month": "month",
    "weekly": "week", "per week": "week",
}


def _visible(value: str | None) -> str:
    """Collapse rendered whitespace and zero-width characters only."""
    return re.sub(r"\s+", " ", (value or "").replace("\u200b", " ")).strip()


def _html_text(value: str | None) -> str:
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def _search_body(offset: int) -> dict:
    # The empty lists/keyword describe the unfiltered listing observed in the
    # official UI; no country, seniority or keyword filter is applied.
    return {**BLANK_FILTERS, "limit": PAGE_SIZE, "offset": offset}


def _search_page_url(offset: int) -> str:
    query = urlencode([
        ("recruitment_id_list", ""),
        ("job_category_id_list", ""),
        ("subject_id_list", ""),
        ("location_code_list", ""),
        ("keyword", ""),
        ("limit", str(PAGE_SIZE)),
        ("offset", str(offset)),
    ])
    return ENTRY_URL + "?" + query


def _apply_url(job_id: str) -> str:
    # Observed on the rendered detail page and in the page's
    # jobCommonSettings.applyJobWebsite template.
    return f"https://careers.tiktok.com/resume/{job_id}/apply"


def _role_html(description: str | None, requirement: str | None) -> str:
    """Wrap the exact plain-text role fields in inert paragraph HTML."""
    parts = []
    for value in (description, requirement):
        if not value:
            continue
        lines = [escape(line) for line in str(value).splitlines() if line.strip()]
        if lines:
            parts.append("\n".join(f"<p>{line}</p>" for line in lines))
    return "\n".join(parts)


def _parse_listing_payload(payload, offset: int, expected_total: int | None = None):
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError("TikTok listing response is not code 0")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("TikTok listing response has no data object")
    rows = data.get("job_post_list")
    total = data.get("count")
    if (not isinstance(rows, list) or not isinstance(total, int)
            or isinstance(total, bool) or total < 0):
        raise ValueError("TikTok listing response has no valid count/job_post_list")
    if expected_total is not None and total != expected_total:
        raise ValueError("TikTok listing count changed during pagination")
    expected_rows = min(PAGE_SIZE, max(0, total - offset))
    if len(rows) != expected_rows:
        raise ValueError(
            f"TikTok listing page length {len(rows)} != expected {expected_rows} at offset {offset}"
        )
    if total > 0 and not rows:
        raise ValueError(f"TikTok listing returned an unexpected empty page at offset {offset}")
    return rows, total


def _parse_detail_payload(payload, listing_row: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError("TikTok detail response is not code 0")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("TikTok detail response has no data object")
    detail = data.get("job_post_detail")
    if not isinstance(detail, dict):
        raise ValueError("TikTok detail response has no job_post_detail")
    listing_id = str(listing_row.get("id") or "")
    detail_id = str(detail.get("id") or "")
    if not listing_id or detail_id != listing_id:
        raise ValueError(
            f"TikTok detail id {detail_id!r} does not match listed id {listing_id!r}"
        )
    if _visible(detail.get("title")) != _visible(listing_row.get("title")):
        raise ValueError("TikTok detail title does not match the listed title")
    listing_code = listing_row.get("code")
    detail_code = detail.get("code")
    if listing_code and detail_code and str(listing_code) != str(detail_code):
        raise ValueError("TikTok detail code does not match the listed job code")
    for field in ("description", "requirement"):
        value = detail.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"TikTok detail {field} is not text")
    return detail


def _salary_from_detail(detail: dict) -> list | None:
    info = detail.get("job_post_info") or {}
    items = info.get("job_post_object_value_list") or []
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        field_name = (item.get("field_name") or {})
        label = (field_name.get("en_name") or field_name.get("i18n_name")
                 or field_name.get("name") or "")
        text = _html_text(item.get("field_value"))
        if not text:
            continue
        haystack = f"{label} {text}"
        if not re.search(r"pay\s+transparency|salary|compensation", haystack, re.I):
            continue
        record = {"field": label or None, "source_text": text}
        match = SALARY_RANGE.search(text)
        if match:
            raw_currency = match.group("currency1") or match.group("currency2")
            raw_period = match.group("period")
            record.update({
                "min": float(match.group("min").replace(",", "")),
                "max": float(match.group("max").replace(",", "")),
                "currency": CURRENCY_NAMES.get((raw_currency or "").upper(), raw_currency),
                "raw_currency": raw_currency,
                "period": PERIOD_NAMES.get((raw_period or "").lower().replace("  ", " "), raw_period),
                "raw_period": raw_period,
            })
        results.append(record)
    return results or None


def _city_nodes(value) -> list:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [node for node in value if isinstance(node, dict)]
    return []


def _location_from_node(node: dict) -> dict | None:
    name = node.get("en_name") or node.get("i18n_name") or node.get("name")
    if not name:
        return None
    parent = node.get("parent") if isinstance(node.get("parent"), dict) else {}
    grandparent = parent.get("parent") if isinstance(parent.get("parent"), dict) else {}
    return {
        "city": name,
        "state": parent.get("en_name") or parent.get("i18n_name") or parent.get("name"),
        "country": grandparent.get("en_name") or grandparent.get("i18n_name") or grandparent.get("name"),
        "code": node.get("code"),
        "location_type": node.get("location_type"),
    }


def _locations(detail: dict, listing: dict) -> list:
    nodes = []
    for source in (detail, listing):
        for key in ("city_info", "city_list", "city_info_list_for_delivery"):
            nodes.extend(_city_nodes(source.get(key)))
    locations, seen = [], set()
    for node in nodes:
        location = _location_from_node(node)
        if not location:
            continue
        identity = location.get("code") or (location.get("city"), location.get("state"), location.get("country"))
        if identity in seen:
            continue
        seen.add(identity)
        locations.append(location)
    return locations


def _department(detail: dict, listing: dict) -> dict | None:
    node = listing.get("job_category")
    if not isinstance(node, dict):
        node = detail.get("job_category")
    if not isinstance(node, dict):
        return None
    name = node.get("en_name") or node.get("i18n_name") or node.get("name")
    if not name:
        return None
    parent = node.get("parent") if isinstance(node.get("parent"), dict) else {}
    return {
        "name": name,
        "parent": parent.get("en_name") or parent.get("i18n_name") or parent.get("name"),
        "id": node.get("id"),
    }


def _employment_type(detail: dict, listing: dict) -> dict | None:
    node = detail.get("recruit_type")
    if not isinstance(node, dict):
        node = listing.get("recruit_type")
    if not isinstance(node, dict):
        return None
    name = node.get("en_name") or node.get("i18n_name") or node.get("name")
    if not name:
        return None
    info = detail.get("job_post_info") or {}
    category = info.get("recruitment_type") if isinstance(info.get("recruitment_type"), dict) else {}
    category = category.get("parent") if isinstance(category.get("parent"), dict) else {}
    return {
        "name": name,
        "id": node.get("id"),
        "category": category.get("en_name") or category.get("i18n_name") or category.get("name"),
    }


def _page_sections(detail: dict) -> dict:
    info = detail.get("job_post_info") or {}
    sections = {}
    for item in info.get("job_post_object_value_list") or []:
        if not isinstance(item, dict):
            continue
        field_name = item.get("field_name") or {}
        label = (field_name.get("en_name") or field_name.get("i18n_name")
                 or field_name.get("name") or "field")
        text = _html_text(item.get("field_value"))
        if text:
            sections[label] = text
    return sections


def _detail_fields(detail: dict, listing: dict) -> dict:
    description = detail.get("description") or ""
    requirement = detail.get("requirement") or ""
    listing_description = listing.get("description") or ""
    listing_requirement = listing.get("requirement") or ""
    warnings = []
    method = "tiktok.official-lifeattiktok-api.detail-description-requirement.1"
    source_url = DETAIL_URL.format(job_id=detail.get("id") or listing.get("id"))
    if not description and listing_description:
        description = listing_description
        method = "tiktok.official-lifeattiktok-api.listing-full-role-fields"
        source_url = SEARCH_URL
        warnings.append("detail_description_empty_used_listing")
    if not requirement and listing_requirement:
        requirement = listing_requirement
        if method.endswith("detail-description-requirement.1"):
            method = "tiktok.official-lifeattiktok-api.listing-full-role-fields"
            source_url = SEARCH_URL
        warnings.append("detail_requirement_empty_used_listing")
    if description and listing_description and description != listing_description:
        warnings.append("listing_detail_description_mismatch")
    if requirement and listing_requirement and requirement != listing_requirement:
        warnings.append("listing_detail_requirement_mismatch")
    if not description:
        warnings.append("missing_responsibilities_field")
    if not requirement:
        warnings.append("missing_qualifications_field")
    if not description and not requirement:
        warnings.append("description_unavailable")
    job_id = str(detail.get("id") or listing.get("id") or "")
    return {
        "title": detail.get("title") or listing.get("title") or "",
        "description": description,
        "requirement": requirement,
        "description_html": _role_html(description, requirement),
        "responsibilities": description or None,
        "qualifications": requirement or None,
        "locations": _locations(detail, listing),
        "department": _department(detail, listing),
        "employment_type": _employment_type(detail, listing),
        "salary": _salary_from_detail(detail),
        "requisition_id": detail.get("code") or listing.get("code"),
        "apply_url": _apply_url(job_id),
        "date_posted": None,
        "date_posted_raw": None,
        "date_updated": None,
        "valid_through": None,
        "employer": "TikTok",
        "language": "en",
        "description_method": method,
        "description_source_url": source_url,
        "description_unavailable": not description and not requirement,
        "warnings": warnings,
        "page_sections": _page_sections(detail),
    }


class TikTok(Adapter):
    VERIFICATION_REVISION = "tiktok.official-lifeattiktok-api.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        # A single attempt is made per request so a 403/429/503 is recorded and
        # left alone instead of being retried by the shared HTTP client.
        self.safe_http = copy(http)
        self.safe_http.retries = 0
        self.source_blocker = None

    def _mark_source_blocker(self, exc, *, url=None):
        text = str(exc)
        match = re.search(r"\bHTTP\s+(403|405|429|503)\b", text, re.I)
        if not match and not re.search(
            r"human verification|verify you are human|captcha", text, re.I
        ):
            return
        self.source_blocker = {
            "status": int(match.group(1)) if match else None,
            "url": url,
            "error": text,
            "retry": False,
        }
        self.result.metadata["source_blocker"] = self.source_blocker

    async def _request(self, method: str, url: str, **kwargs):
        if self.source_blocker:
            raise FetchError(
                "TikTok request stopped after a public-source block; no further request sent"
            )
        last = None
        for attempt in range(3):
            try:
                return await self.safe_http.request(method, url, **kwargs)
            except FetchError as exc:
                text = str(exc)
                if re.search(r"\bHTTP\s+(403|405|429|503)\b", text, re.I):
                    self._mark_source_blocker(exc, url=url)
                    raise
                if re.search(r"\bHTTP\s+4\d\d\b", text, re.I):
                    raise
                last = exc
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))
        raise last

    async def _filters(self):
        try:
            response = await self._request(
                "POST", FILTERS_URL, json={}, headers=API_HEADERS
            )
            if urlsplit(str(response.url)).hostname != "api.lifeattiktok.com":
                raise ValueError("TikTok filter config redirected away from its official API host")
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if payload.get("code") != 0 or not isinstance(data, dict):
                raise ValueError("TikTok filter config response is not code 0 data")
            self.result.metadata["filter_config"] = {
                "url": str(response.url),
                "job_type_count": len(data.get("job_type_list") or []),
                "city_count": len(data.get("city_list") or []),
                "recruitment_type_count": len(data.get("recruitment_type_list") or []),
                "subject_count": len(data.get("job_subject_list") or []),
                "tag_count": len(data.get("tag_list") or []),
                "job_types": [
                    item.get("en_name") or item.get("i18n_name")
                    for item in data.get("job_type_list") or []
                    if isinstance(item, dict)
                ],
                "recruitment_types": [
                    item.get("en_name") or item.get("i18n_name")
                    for item in data.get("recruitment_type_list") or []
                    if isinstance(item, dict)
                ],
            }
            return data
        except (FetchError, ValueError) as exc:
            self.result.warnings.append("filter_config_unavailable")
            self.result.metadata["filter_config_error"] = f"{type(exc).__name__}: {exc}"
            return None

    async def listing_pass(self):
        """Walk every unfiltered native page once, retaining exact returned rows."""
        started = time.perf_counter()
        rows, page_records, seen_pages = {}, [], set()
        pass_index = len(self.result.metadata.get("native_enumerations", [])) + 1
        expected_total = None
        offset = 0
        complete = True

        while True:
            if not self.page_guard():
                complete = False
                break
            try:
                response = await self._request(
                    "POST", SEARCH_URL, json=_search_body(offset), headers=API_HEADERS
                )
                payload = response.json()
            except (FetchError, ValueError) as exc:
                self.result.errors.append(
                    f"tiktok_listing_request_failed_offset_{offset}: {exc}"
                )
                complete = False
                break
            if urlsplit(str(response.url)).hostname != "api.lifeattiktok.com":
                self.result.errors.append(
                    f"tiktok_listing_redirected_offset_{offset}"
                )
                complete = False
                break
            try:
                page_rows, total = _parse_listing_payload(payload, offset, expected_total)
            except ValueError as exc:
                self.result.errors.append(
                    f"tiktok_listing_invalid_response_offset_{offset}: {exc}"
                )
                complete = False
                break
            if expected_total is None:
                expected_total = total
            if offset in seen_pages:
                self.result.errors.append("tiktok_listing_offset_repeated")
                complete = False
                break
            seen_pages.add(offset)

            page_ids = []
            sampled = False
            for row in page_rows:
                identity = row.get("id")
                if not isinstance(identity, (int, str)) or not str(identity).isdigit():
                    self.result.errors.append(
                        f"tiktok_listing_missing_native_id_offset_{offset}"
                    )
                    complete = False
                    continue
                identity = str(identity)
                if identity in rows:
                    self.result.errors.append("tiktok_listing_id_repeated_across_pages")
                    complete = False
                    continue
                if not isinstance(row.get("title"), str) or not row["title"].strip():
                    self.result.errors.append(
                        f"tiktok_listing_missing_title_{identity}"
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
                "url": _search_page_url(offset),
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
                self.result.errors.append("tiktok_listing_pagination_ended_early")
                complete = False
                break
            offset += PAGE_SIZE

        seconds = max(0.0, time.perf_counter() - started)
        self.result.timings["listing_phase_seconds"] = (
            self.result.timings.get("listing_phase_seconds", 0.0) + seconds
        )
        self.result.metadata.setdefault("native_enumerations", []).append({
            "portal": ENTRY_URL,
            "api": SEARCH_URL,
            "total": expected_total,
            "page_size": PAGE_SIZE,
            "pages": page_records,
            "job_ids": list(rows),
            "complete": complete and expected_total is not None and len(rows) == expected_total,
            "duration_seconds": round(seconds, 6),
        })
        if expected_total is not None and self.result.expected_total is None:
            self.result.expected_total = expected_total
        return rows, expected_total, complete and expected_total is not None and len(rows) == expected_total

    def _listing_job(self, row: dict):
        identity = str(row["id"])
        description = row.get("description") or ""
        requirement = row.get("requirement") or ""
        job = self.job(
            {"listing": row},
            title=_visible(row.get("title")),
            url=f"{ENTRY_URL}/{identity}",
            job_id=identity,
            description=_role_html(description, requirement),
            method="tiktok.official-lifeattiktok-api.listing-full-role-fields",
            description_url=SEARCH_URL,
            requisition_id=row.get("code"),
            locations=_locations({}, row),
            department=_department({}, row),
            employment_type=_employment_type({}, row),
            apply_url=_apply_url(identity),
            employer="TikTok",
            language="en",
        )
        job.responsibilities = description or None
        job.qualifications = requirement or None
        job.date_posted = None
        job.date_posted_raw = None
        job.valid_through = None
        job.description_unavailable = not description and not requirement
        if not description:
            job.warnings.append("missing_responsibilities_field")
        if not requirement:
            job.warnings.append("missing_qualifications_field")
        if not description and not requirement:
            job.warnings.append("description_unavailable")
        self.add(job)
        # Listing-only data is not a detail verification; the hydrate phase
        # replaces this record and validates the exact detail response.
        job.description_verified = False
        return job

    async def detail(self, job):
        if self.source_blocker:
            raise FetchError(
                "TikTok detail request stopped after a public-source block; no further request sent"
            )
        listing = (job.raw or {}).get("listing") or {}
        response = await self._request(
            "POST",
            DETAIL_URL.format(job_id=job.job_id),
            json={"job_post_id": job.job_id},
            headers=API_HEADERS,
        )
        payload = response.json()
        detail = _parse_detail_payload(payload, listing)
        fields = _detail_fields(detail, listing)

        job.title = fields["title"]
        job.description_html = fields["description_html"]
        job.description_method = fields["description_method"]
        job.description_source_url = fields["description_source_url"]
        job.responsibilities = fields["responsibilities"]
        job.qualifications = fields["qualifications"]
        job.locations = fields["locations"]
        job.department = fields["department"]
        job.employment_type = fields["employment_type"]
        job.salary = fields["salary"]
        job.requisition_id = fields["requisition_id"]
        job.apply_url = fields["apply_url"]
        job.date_posted = fields["date_posted"]
        job.date_posted_raw = fields["date_posted_raw"]
        job.date_updated = fields["date_updated"]
        job.valid_through = fields["valid_through"]
        job.employer = fields["employer"]
        job.language = fields["language"]
        job.description_unavailable = fields["description_unavailable"]
        job.raw = {
            "listing": listing,
            "detail": detail,
            "page_sections": fields["page_sections"],
            "detail_request": {
                "method": "POST",
                "url": DETAIL_URL.format(job_id=job.job_id),
                "body": {"job_post_id": job.job_id},
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
            },
        }
        job.warnings = [
            warning for warning in job.warnings if warning != "detail_pending"
        ]
        for warning in fields["warnings"]:
            if warning not in job.warnings:
                job.warnings.append(warning)
        return job

    async def run(self):
        await self._filters()

        initial_rows, initial_total, initial_complete = await self.listing_pass()
        if self.source_blocker:
            final_rows, final_total, final_complete = {}, None, False
            self.result.errors.append("tiktok_final_membership_pass_skipped_after_source_block")
        else:
            final_rows, final_total, final_complete = await self.listing_pass()

        same_membership = (
            initial_total is not None
            and final_total is not None
            and initial_total == final_total
            and set(initial_rows) == set(final_rows)
        )
        if not same_membership:
            self.result.errors.append("tiktok_membership_changed_between_passes")
        if initial_complete and final_complete and same_membership:
            rows = final_rows
            complete = True
        elif initial_complete:
            rows = initial_rows
            complete = False
            self.result.errors.append("tiktok_final_membership_pass_not_complete")
        else:
            rows = final_rows or initial_rows
            complete = False
        self.result.metadata["membership_reconciliation"] = {
            "initial_total": initial_total,
            "initial_ids": list(initial_rows),
            "initial_complete": initial_complete,
            "final_total": final_total,
            "final_ids": list(final_rows),
            "final_complete": final_complete,
            "same_membership": same_membership,
            "selected": "final" if rows is final_rows else "initial",
        }

        for row in rows.values():
            self._listing_job(row)

        # Set completeness only after the full job list exists: Adapter.add
        # checkpoints during listing, and a partial list must not be compared
        # with the full expected total.
        self.result.listing_complete = complete and bool(rows)
        self.result.expected_total = final_total if final_total is not None else initial_total

        if self._all_verified_cached():
            # Resumed snapshot: every listed posting already has a verified
            # full detail from the same source. Reuse it without re-fetching or
            # re-checkpointing thousands of unchanged detail records. The
            # listing passes above still re-established membership.
            reused = []
            for listed in self.result.jobs:
                cached = self.options.cached_jobs[listed.url]
                cached.record_identity = listed.key
                cached.company_scope_verified = self.company.scope == "company"
                self.result.validate_job(cached)
                reused.append(cached)
            self.result.jobs = reused
            self.result.metadata["details"] = {
                "total": len(reused),
                "checked": len(reused),
                "fetched_jobs": 0,
                "verified_cache_hits": len(reused),
                "unavailable_cache_hits": 0,
                "failed": 0,
                "duration_seconds": 0.0,
                "reused_all_verified": True,
            }
            self.result.timings["detail_phase_seconds"] = 0.0
        else:
            await self.hydrate(self.result.jobs, self.detail)
        self.result.finish()
        return self.result

    def _all_verified_cached(self) -> bool:
        cached = self.options.cached_jobs
        if not cached or not self.result.jobs:
            return False
        for listed in self.result.jobs:
            previous = cached.get(listed.url)
            if previous is None or not previous.description_verified:
                return False
            if previous.key != listed.key:
                return False
        return True
