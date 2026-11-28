"""Qualcomm's own careers site: the official search listing and exact position details.

The official entry https://careers.qualcomm.com/careers is Qualcomm's own
Phenom-hosted careers site (canonical on careers.qualcomm.com, company links in
its footer to qualcomm.com).  Its unfiltered listing is served by
``/api/pcsx/search`` with the site's fixed 10-row pages and reports the visible
company total.  Every posting's complete body is returned by
``/api/pcsx/position_details`` for the exact position id exposed by that
listing.  The company-published sitemap is used only as a completeness
cross-check; it is not the membership authority.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html import unescape
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import now, text_html

OFFICIAL_ENTRY = "https://careers.qualcomm.com/careers"
BASE_HOST = "careers.qualcomm.com"
SEARCH_API = "https://careers.qualcomm.com/api/pcsx/search"
DETAIL_API = "https://careers.qualcomm.com/api/pcsx/position_details"
APPLY_PAGE = "https://careers.qualcomm.com/careers/apply?pid={position_id}"
SITEMAP_INDEX = "https://careers.qualcomm.com/careers/sitemap_index.xml?domain=qualcomm.com"
SITEMAP_URL = "https://careers.qualcomm.com/careers/sitemap.xml?domain=qualcomm.com"
DOMAIN = "qualcomm.com"
PAGE_SIZE = 10
MAX_LISTING_PASSES = 4
DETAIL_BATCH_SIZE = 150
DETAIL_BATCH_PAUSE_SECONDS = 45
JOB_PATH = re.compile(r"^/careers/job/(\d+)(?:-[^/?#]*)?$")
BLOCKING_STATUSES = (403, 405, 429)
SITEMAP_JOB = re.compile(r"https://careers\.qualcomm\.com/careers/job/(\d+)(?:-[^<]*)?$")

SECTION_LABELS = {
    "company": re.compile(r"^company:?$", re.I),
    "job_area": re.compile(r"^job area:?$", re.I),
    "general_summary": re.compile(r"^general summary:?$", re.I),
    "minimum_qualifications": re.compile(r"^minimum qualifications:?$", re.I),
    "preferred_qualifications": re.compile(r"^preferred qualifications:?$", re.I),
    "level_of_responsibility": re.compile(r"^level of responsibility:?$", re.I),
    "responsibilities": re.compile(
        r"^(?:principal duties\s*(?:&|and)\s*responsibilities|principal duties|"
        r"responsibilities(?:\s+include)?|key responsibilities):?$", re.I),
    "type": re.compile(r"^type:?$", re.I),
    "period": re.compile(r"^period:?$", re.I),
    "location": re.compile(r"^location:?$", re.I),
    "last_application_date": re.compile(r"^last application date:?$", re.I),
}
STOP_MARKERS = (
    "pay range", "qualcomm is an equal opportunity employer", "to all staffing",
    "eeo employer", "if you would like more information", "applicants:",
)
DATE_PATTERN = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
SALARY_PATTERN = re.compile(
    r"Pay range\b[\s\S]{0,140}?(\$[\d,]+(?:\.\d+)?\s*[-–]\s*\$[\d,]+(?:\.\d+)?)", re.I)


class QualcommChallenge(RuntimeError):
    """A WAF/CAPTCHA or rate-limit challenge stopped the detail phase."""


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def normalize_body(value: str | None) -> str:
    return clean(value)


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def epoch_date(value) -> str | None:
    """Only the source's own postedTs is used; never request time."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).date().isoformat()


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != BASE_HOST or parsed.path.rstrip("/") != "/careers":
        raise ValueError("Qualcomm official entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None
    description = soup.select_one('meta[name="description"]')
    if "Qualcomm" not in title or canonical_url != OFFICIAL_ENTRY:
        raise ValueError("Qualcomm official entry no longer identifies the inspected careers page")
    return {
        "url": response_url,
        "title": title,
        "canonical_url": canonical_url,
        "description": description.get("content") if description else None,
        "search_headers": [clean(h.get_text(" ", strip=True)) for h in soup.select("h1,h2")][:10],
    }


def parse_sitemap_index(text: str) -> list[str]:
    locs = re.findall(r"<loc>([^<]+)</loc>", text or "")
    if (not locs or locs[0] != SITEMAP_URL
            or any(urlsplit(loc).netloc != BASE_HOST for loc in locs)):
        raise ValueError(f"Qualcomm sitemap index changed: {locs}")
    return locs


def parse_job_sitemap(text: str) -> dict:
    locs = re.findall(r"<loc>([^<]+)</loc>", text or "")
    lastmods = re.findall(r"<lastmod>([^<]+)</lastmod>", text or "")
    rows, issues, seen = [], [], set()
    for loc in locs:
        if "/careers/job/" not in loc:
            continue
        match = SITEMAP_JOB.fullmatch(loc)
        if match is None:
            issues.append(f"sitemap_job_url_shape:{loc}")
            continue
        identity = match.group(1)
        if identity in seen:
            issues.append(f"sitemap_duplicate_id:{identity}")
            continue
        seen.add(identity)
        rows.append({"id": identity, "url": loc})
    return {
        "rows": rows,
        "count": len(rows),
        "lastmod_count": len(lastmods),
        "max_lastmod": max(lastmods) if lastmods else None,
        "issues": issues,
    }


def parse_search_page(value: dict, start: int, expected_count: int | None = None) -> dict:
    if not isinstance(value, dict) or value.get("status") != 200:
        raise ValueError("Qualcomm search API returned no successful status")
    error = value.get("error") or {}
    data = value.get("data")
    if not isinstance(data, dict):
        raise ValueError("Qualcomm search API response has no data object")
    if error.get("message") or error.get("body"):
        raise ValueError(f"Qualcomm search API returned an error: {error}")
    count = data.get("count")
    positions = data.get("positions")
    if not isinstance(count, int) or count < 0 or not isinstance(positions, list):
        raise ValueError("Qualcomm search API count/positions schema changed")
    if expected_count is not None and count != expected_count:
        raise ValueError(f"Qualcomm search total changed from {expected_count} to {count}")
    if len(positions) > PAGE_SIZE:
        raise ValueError("Qualcomm search page returned more than its fixed page size")
    if not positions and start < count:
        raise ValueError(f"Qualcomm search page {start} is empty below the advertised total")
    rows, issues, seen = [], [], set()
    for row in positions:
        if not isinstance(row, dict):
            issues.append("listing_row_not_object")
            continue
        identity = str(row.get("id", ""))
        display = str(row.get("displayJobId", ""))
        position_url = row.get("positionUrl")
        if not re.fullmatch(r"\d+", identity):
            issues.append("listing_row_missing_internal_id")
            continue
        if not re.fullmatch(r"\d+", display):
            issues.append(f"listing_row_missing_display_job_id:{identity}")
            continue
        if not clean(row.get("name")):
            issues.append(f"listing_row_missing_title:{identity}")
            continue
        if not isinstance(position_url, str) or JOB_PATH.fullmatch(urlsplit(position_url).path) is None:
            issues.append(f"listing_row_position_url_shape:{identity}")
            continue
        if JOB_PATH.fullmatch(urlsplit(position_url).path).group(1) != identity:
            issues.append(f"listing_row_position_url_id_mismatch:{identity}")
            continue
        if identity in seen:
            issues.append(f"listing_row_duplicate_id:{identity}")
            continue
        seen.add(identity)
        locations = row.get("locations")
        if not isinstance(locations, list):
            issues.append(f"listing_row_locations_missing:{identity}")
            locations = []
        rows.append(dict(row))
    return {
        "rows": rows,
        "count": count,
        "start": start,
        "page_size": len(positions),
        "sort_by": data.get("sortBy"),
        "issues": issues,
        "filter_def": data.get("filterDef"),
    }


def parse_position_detail(value: dict, position_id: str) -> dict:
    if not isinstance(value, dict) or value.get("status") != 200:
        raise ValueError(f"Qualcomm position {position_id} detail API did not return success")
    data = value.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"Qualcomm position {position_id} detail has no data")
    identity = data.get("id")
    if str(identity) != str(position_id):
        raise ValueError(f"Qualcomm position {position_id} detail id mismatch: {identity}")
    name = clean(data.get("name"))
    if not name:
        raise ValueError(f"Qualcomm position {position_id} detail has no title")
    if not re.fullmatch(r"\d+", str(data.get("displayJobId", ""))):
        raise ValueError(f"Qualcomm position {position_id} detail has no public Job ID")
    description = data.get("jobDescription")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"Qualcomm position {position_id} detail has no jobDescription")
    public_url = data.get("publicUrl") or (
        "https://" + BASE_HOST + str(data.get("positionUrl", ""))
    )
    parsed = urlsplit(str(public_url))
    if parsed.netloc != BASE_HOST or JOB_PATH.fullmatch(parsed.path) is None:
        raise ValueError(f"Qualcomm position {position_id} detail public URL shape changed")
    if JOB_PATH.fullmatch(parsed.path).group(1) != str(position_id):
        raise ValueError(f"Qualcomm position {position_id} detail public URL id mismatch")
    if not isinstance(data.get("postedTs"), int):
        raise ValueError(f"Qualcomm position {position_id} detail has no postedTs")
    return data


def title_norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().casefold()


def extract_sections(html: str) -> dict:
    """Verbatim JD sections and explicitly labelled source facts only."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    lines = [clean(line) for line in soup.get_text("\n").splitlines()]
    lines = [line for line in lines if line]
    labels: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        key = next((name for name, pattern in SECTION_LABELS.items() if pattern.match(line)), None)
        if key:
            labels.append((index, key))

    def block(key: str, single: bool = False) -> str | None:
        index = next((i for i, (_line, name) in enumerate(labels) if name == key), None)
        if index is None:
            return None
        start = labels[index][0]
        end = labels[index + 1][0] if index + 1 < len(labels) else len(lines)
        for marker in STOP_MARKERS:
            found = next(
                (i for i in range(start + 1, end)
                 if lines[i].casefold().startswith(marker)),
                None,
            )
            if found is not None:
                end = found
                break
        values = lines[start + 1:end]
        if single:
            values = values[:1]
        text = "\n".join(values).strip()
        return text if len(text) >= 2 else None

    text = "\n".join(lines)
    salary = SALARY_PATTERN.search(text)
    salary_text = clean(salary.group(1)) if salary else None
    minimum = block("minimum_qualifications")
    preferred = block("preferred_qualifications")
    responsibilities = block("responsibilities")
    last_application = block("last_application_date", single=True)
    valid_through = None
    if last_application:
        date = DATE_PATTERN.search(last_application)
        valid_through = date.group(1) if date else None
    return {
        "company": block("company", single=True),
        "job_area": block("job_area", single=True),
        "general_summary": block("general_summary"),
        "minimum_qualifications": minimum,
        "preferred_qualifications": preferred,
        "responsibilities": responsibilities,
        "level_of_responsibility": block("level_of_responsibility"),
        "employment_type": block("type", single=True),
        "period": block("period", single=True),
        "location_text": block("location", single=True),
        "last_application_date": last_application,
        "valid_through": valid_through,
        "salary_text": salary_text,
    }


class Qualcomm(Adapter):
    """Only Qualcomm's own unfiltered search listing and exact position details."""

    VERIFICATION_REVISION = "qualcomm.pcsx-search-and-position-details.1"
    expected_count = None
    blocked = None

    def search_url(self, start: int, sort_by: str = "relevance") -> str:
        return (f"{SEARCH_API}?domain={DOMAIN}&query=&location=&start={start}"
                f"&sort_by={sort_by}")

    def detail_url(self, position_id: str) -> str:
        return f"{DETAIL_API}?position_id={position_id}&domain={DOMAIN}&hl=en"

    async def listing(self, start: int, sort_by: str = "relevance") -> dict:
        if not self.page_guard():
            raise ValueError("Qualcomm listing page budget exhausted")
        value = await self.http.json(
            self.search_url(start, sort_by),
            headers={"Referer": OFFICIAL_ENTRY},
        )
        return parse_search_page(value, start, self.expected_count)

    async def sitemap_cross_check(self) -> dict:
        started = time.perf_counter()
        index = await self.http.get(SITEMAP_INDEX, headers={"Referer": OFFICIAL_ENTRY})
        index_urls = parse_sitemap_index(index.text)
        pages = []
        for url in index_urls:
            response = await self.http.get(url, headers={"Referer": OFFICIAL_ENTRY})
            parsed = parse_job_sitemap(response.text)
            pages.append({"url": str(response.url), "requested_url": url, **parsed})
        rows = [row for page in pages for row in page["rows"]]
        return {
            "index_url": SITEMAP_INDEX,
            "index_urls": index_urls,
            "pages": [{k: v for k, v in page.items() if k != "rows"} for page in pages],
            "rows": rows,
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def detail(self, job):
        if self.blocked:
            raise QualcommChallenge(
                f"detail phase stopped after HTTP {self.blocked['status']} challenge"
            )
        position_id = str(job.raw["internal_position_id"])
        url = self.detail_url(position_id)
        requests_done = getattr(self, "detail_network_requests", 0)
        if requests_done and requests_done % DETAIL_BATCH_SIZE == 0:
            self.result.metadata["detail_batch_pauses"] = (
                self.result.metadata.get("detail_batch_pauses", 0) + 1
            )
            await asyncio.sleep(DETAIL_BATCH_PAUSE_SECONDS)
        self.detail_network_requests = requests_done + 1
        try:
            value = await self.http.json(url, headers={"Referer": job.url})
        except FetchError as exc:
            message = str(exc)
            for status in BLOCKING_STATUSES:
                if f"HTTP {status}" in message:
                    self.blocked = {
                        "status": status,
                        "url": url,
                        "message": message,
                        "detected_at": now(),
                    }
                    self.result.metadata["blocked"] = self.blocked
                    raise QualcommChallenge(
                        f"HTTP {status} challenge at {url}; no further detail requests"
                    ) from exc
            raise
        detail = parse_position_detail(value, position_id)
        listing = job.raw["listing"]
        if title_norm(detail.get("name")) != title_norm(listing.get("name")):
            job.warnings.append("title_mismatch_listing_detail")
        job.title = clean(detail.get("name"))
        job.url = detail.get("publicUrl") or job.url
        job.job_id = str(detail.get("displayJobId"))
        job.requisition_id = str(detail.get("atsJobId") or detail.get("displayJobId"))
        job.apply_url = APPLY_PAGE.format(position_id=position_id)
        job.locations = [clean(loc) for loc in detail.get("locations") or [] if clean(loc)]
        job.department = detail.get("department") or listing.get("department")
        job.workplace_type = detail.get("workLocationOption")
        job.date_posted = epoch_date(detail.get("postedTs"))
        job.date_posted_raw = detail.get("postedTs")
        sections = extract_sections(detail["jobDescription"])
        job.employer = sections["company"]
        job.team = sections["job_area"]
        job.employment_type = sections["employment_type"]
        job.valid_through = sections["valid_through"]
        job.salary = sections["salary_text"]
        qualifications = {
            key: sections[key]
            for key in ("minimum_qualifications", "preferred_qualifications")
            if sections.get(key)
        }
        job.qualifications = qualifications or None
        job.responsibilities = sections.get("responsibilities")
        job.description_html = detail["jobDescription"]
        job.description_text = text_html(detail["jobDescription"])
        job.description_method = "qualcomm.pcsx.position_details.jobDescription"
        job.description_source_url = url
        job.language = "en"
        job.raw.update({
            "detail": detail,
            "detail_url": url,
            "detail_fetched": True,
            "sections": sections,
            "listing": listing,
        })
        return job

    async def run(self):
        self.result.metadata["verification_revision"] = self.VERIFICATION_REVISION
        entry_started = time.perf_counter()
        entry = await self.http.get(OFFICIAL_ENTRY)
        entry_parsed = parse_official_entry(entry.text, str(entry.url))
        self.result.metadata["official_entry"] = {
            **entry_parsed,
            "status": entry.status_code,
            "final_url": str(entry.url),
            "seconds": round(max(0.0, time.perf_counter() - entry_started), 6),
        }

        sitemap = await self.sitemap_cross_check()
        self.result.metadata["sitemap"] = {
            k: v for k, v in sitemap.items() if k != "rows"
        }
        self.result.metadata["sitemap"]["ids"] = [row["id"] for row in sitemap["rows"]]

        listing_started = time.perf_counter()
        first = await self.listing(0)
        self.expected_count = first["count"]
        self.result.expected_total = self.expected_count
        passes, union, page_records = 0, {}, []
        while passes < MAX_LISTING_PASSES:
            passes += 1
            sort_by = "relevance" if passes == 1 else "timestamp"
            starts = range(0, self.expected_count, PAGE_SIZE)
            for start in starts:
                parsed = first if (passes == 1 and start == 0) else await self.listing(start, sort_by)
                new_ids = 0
                for row in parsed["rows"]:
                    identity = str(row["id"])
                    if identity not in union:
                        union[identity] = row
                        new_ids += 1
                page_records.append({
                    "pass": passes, "sort_by": sort_by, "start": start,
                    "rows": len(parsed["rows"]), "new_ids": new_ids,
                    "count": parsed["count"], "issues": parsed["issues"],
                })
                if parsed["issues"]:
                    self.result.errors.extend(
                        f"listing:{issue}" for issue in parsed["issues"]
                    )
            if len(union) == self.expected_count:
                break
            self.result.warnings.append(
                f"listing_pass_{passes}_incomplete:{len(union)}_of_{self.expected_count}"
            )
        listing_seconds = max(0.0, time.perf_counter() - listing_started)
        self.result.pages = len(page_records)
        self.result.metadata["listing"] = {
            "search_api": SEARCH_API,
            "request_shape": "domain, query, location, start, sort_by (order-only; no filters)",
            "page_size": PAGE_SIZE,
            "advertised_count": self.expected_count,
            "pages_requested": len(page_records),
            "max_page_records": page_records[:2] + page_records[-2:],
            "passes": passes,
            "unique_ids": len(union),
            "seconds": round(listing_seconds, 6),
            "search_sort": "relevance for stable pagination; order-only, membership equals count",
        }
        if len(union) != self.expected_count:
            self.result.errors.append(
                f"listing_membership_incomplete:{len(union)}_of_{self.expected_count}"
            )
            self.result.listing_complete = False
        else:
            self.result.listing_complete = True

        sitemap_ids = {row["id"] for row in sitemap["rows"]}
        union_ids = set(union)
        self.result.metadata["sitemap_reconciliation"] = {
            "sitemap_job_urls": len(sitemap_ids),
            "listing_unique_ids": len(union_ids),
            "listing_ids_absent_from_sitemap": sorted(union_ids - sitemap_ids),
            "sitemap_ids_absent_from_listing": sorted(sitemap_ids - union_ids),
            "note": ("Sitemap is a secondary company-published index (max lastmod "
                     f"{self.result.metadata['sitemap'].get('max_lastmod')}); membership "
                     "is the visible unfiltered search count and its exact rows."),
        }

        jobs = []
        for order, identity in enumerate(union):
            row = union[identity]
            url = "https://" + BASE_HOST + row["positionUrl"]
            job = self.job(
                raw={
                    "internal_position_id": identity,
                    "listing": row,
                    "listing_order": order,
                    "position_url": url,
                },
                title=row["name"],
                url=url,
                job_id=str(row.get("displayJobId")),
                description="",
                method=None,
                description_url=None,
                locations=[clean(loc) for loc in row.get("locations") or []],
                department=row.get("department"),
                workplace_type=row.get("workLocationOption"),
                date_posted=epoch_date(row.get("postedTs")),
                date_posted_raw=row.get("postedTs"),
            )
            jobs.append(job)
        self.result.jobs = await self.hydrate(jobs, self.detail)
        if self.blocked:
            challenge_errors = [e for e in self.result.errors if "QualcommChallenge" in e]
            other_errors = [e for e in self.result.errors if "QualcommChallenge" not in e]
            unverified = sum(not job.description_verified for job in self.result.jobs)
            self.result.errors = other_errors + [
                f"detail_phase_blocked: HTTP {self.blocked['status']} challenge at "
                f"{self.blocked['url']}; {unverified} jobs remain unverified; stopped without retry"
            ]
            self.result.warnings.append("challenge_stopped_detail_phase")
            self.result.metadata["blocked"]["challenge_responses"] = len(challenge_errors)
            self.result.metadata["blocked"]["jobs_left_unverified"] = unverified
        self.result.metadata["details"] = {
            **self.result.metadata.get("details", {}),
            "detail_api": DETAIL_API,
            "detail_request_shape": "position_id, domain, hl",
            "detail_method": "full jobDescription field from the exact position detail API",
            "detail_network_requests": getattr(self, "detail_network_requests", 0),
            "detail_batch_size": DETAIL_BATCH_SIZE,
            "detail_batch_pause_seconds": DETAIL_BATCH_PAUSE_SECONDS,
            "detail_batch_pauses": self.result.metadata.get("detail_batch_pauses", 0),
        }
        self.result.timings["source_collection_seconds"] = round(
            self.result.metadata["official_entry"]["seconds"] + listing_seconds + sitemap["seconds"], 6
        )
        self.result.timings["listing_seconds"] = round(listing_seconds, 6)
        self.result.timings["sitemap_seconds"] = round(sitemap["seconds"], 6)
        self.result.finish()
        return self.result
