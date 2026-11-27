"""Walmart Global Tech India's official careers page and its published Workday India route.

The official entry ``https://tech.walmart.com/content/walmart-global-tech/en_us/careers.html``
is Walmart Global Tech's own careers page.  Its inline search handler sends the
published "India based" region to the Walmart Workday tenant
``walmart``/``WalmartExternal`` with the India country facet
``c4f78be1a8f14da0ab49ce1162348a5e``.  This module follows exactly that route:
the India-faceted CXS listing (20 rows per page, 197 observed) and the exact
listed ``externalPath`` detail records.  It never enumerates the unfiltered
Walmart board, another country, a guessed tenant or an aggregator.

The Workday CXS detail payload contains the complete employer-authored
``jobPostingInfo.jobDescription`` HTML.  The adapter retains that full HTML,
maps only real source fields (``startDate`` → posting date, ``postedOn`` →
raw relative label, ``jobReqId`` → requisition id), and leaves unknown
dates/salary null.  The rendered detail body was independently compared with
the API HTML for the first and last listed jobs; see the review folder.
"""
from __future__ import annotations

from hashlib import sha256
from html import escape
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://tech.walmart.com/content/walmart-global-tech/en_us/careers.html"
WORKDAY_HOST = "walmart.wd504.myworkdayjobs.com"
WORKDAY_ORIGIN = f"https://{WORKDAY_HOST}"
TENANT = "walmart"
SITE = "WalmartExternal"
INDIA_COUNTRY_ID = "c4f78be1a8f14da0ab49ce1162348a5e"
INDIA_LISTING_URL = f"{WORKDAY_ORIGIN}/{SITE}?q=&locationCountry={INDIA_COUNTRY_ID}"
INDIA_LISTING_REFERER = f"{WORKDAY_ORIGIN}/{SITE}?locationCountry={INDIA_COUNTRY_ID}"
LISTING_API = f"{WORKDAY_ORIGIN}/wday/cxs/{TENANT}/{SITE}/jobs"
DETAIL_API_PREFIX = f"{WORKDAY_ORIGIN}/wday/cxs/{TENANT}/{SITE}"
PAGE_LIMIT = 20
APPLY_SUFFIX = "/apply"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REQ_ID = re.compile(r"^[A-Za-z]{1,4}-?\d[\w-]*$")
JOB_PATH = re.compile(r"^/job/[^?#]+$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
HEADING_TEXT = re.compile(
    r"^(?:position summary|what you(?:'|’)?ll do|what you will do|job description(?: summary)?|"
    r"minimum qualifications|preferred qualifications|primary location|about (?:the )?team|"
    r"about walmart global tech|benefits|belonging|equal opportunity employer|"
    r"responsibilit(?:y|ies)|requirements|qualifications|what you(?:'|’)?ll bring|"
    r"what you will need|what you(?:'|’)?ll need|who you are|about the role|role|skills|experience)\b.*[:.]*$",
    re.I,
)
RESPONSIBILITY_TOKENS = (
    "responsibilit", "what you'll do", "what you’ll do", "what you will do",
    "job description", "role summary", "about the role", "the opportunity",
)
QUALIFICATION_TOKENS = (
    "qualif", "requirement", "what you'll bring", "what you’ll bring",
    "what you will need", "what you’ll need", "who you are", "skills",
    "experience", "education",
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def job_url(external_path: str) -> str:
    if not isinstance(external_path, str) or not JOB_PATH.match(external_path):
        raise ValueError(f"Walmart India listed externalPath is not an exact /job/ path: {external_path!r}")
    return f"{WORKDAY_ORIGIN}/{SITE}{external_path}"


def detail_api_url(external_path: str) -> str:
    job_url(external_path)
    return DETAIL_API_PREFIX + external_path


def apply_url(external_url: str) -> str:
    parsed = urlsplit(external_url or "")
    if (parsed.scheme != "https" or parsed.netloc != WORKDAY_HOST
            or not parsed.path.startswith(f"/{SITE}/job/")):
        raise ValueError(f"Walmart India detail externalUrl is not an exact Workday job URL: {external_url!r}")
    return external_url + APPLY_SUFFIX


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "tech.walmart.com"
            or parsed.path != "/content/walmart-global-tech/en_us/careers.html"):
        raise ValueError("Walmart Global Tech official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Walmart Global Tech" not in title:
        raise ValueError("Walmart Global Tech official careers entry identity changed")
    form = soup.select_one("form#job-search-form")
    region = soup.select_one("select#job-region")
    keyword = soup.select_one("input#job-keyword")
    location = soup.select_one("input#job-location")
    if form is None or region is None or keyword is None:
        raise ValueError("Walmart Global Tech careers search form is missing")
    options = {
        option.get("value") or option.get_text(" ", strip=True): option.get_text(" ", strip=True)
        for option in region.select("option")
    }
    if options.get("India") != "India based":
        raise ValueError("Walmart Global Tech careers page no longer publishes the India region option")
    scripts = "\n".join(script.get_text() for script in soup.select("script"))
    india_template = (
        f"{WORKDAY_ORIGIN}/{SITE}?q=${{jobKeyword}}&locationCountry={INDIA_COUNTRY_ID}"
    )
    if "jobRegion == 'India'" not in scripts or india_template not in scripts:
        raise ValueError("Walmart Global Tech careers page no longer routes India to the inspected Workday listing")
    return {
        "url": response_url,
        "title": title,
        "form_id": form.get("id"),
        "region_options": [{"value": key, "label": value} for key, value in options.items()],
        "india_option_label": options["India"],
        "india_listing_template": india_template,
        "india_listing_url": INDIA_LISTING_URL,
        "keyword_input": keyword.get("id"),
        "location_input": location.get("id") if location is not None else None,
    }


def parse_listing_page(value: dict, offset: int, expected_total: int | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Walmart India CXS listing response is not an object")
    rows = value.get("jobPostings")
    total = value.get("total")
    if not isinstance(rows, list) or not isinstance(total, int) or total < 0:
        raise ValueError("Walmart India CXS listing response is missing jobPostings or total")
    if expected_total is None:
        if offset != 0:
            raise ValueError("Walmart India CXS first listing page must start at offset 0")
        expected_total = total
    elif offset == 0 and total != expected_total:
        raise ValueError(f"Walmart India CXS listing total changed on offset 0: {total} != {expected_total}")
    elif offset > 0 and total not in (0, expected_total):
        raise ValueError(f"Walmart India CXS listing total changed on offset {offset}: {total} != {expected_total}")
    if offset == 0 and total < len(rows):
        raise ValueError("Walmart India CXS first listing total is smaller than its returned rows")
    parsed, seen_paths, seen_reqs, issues = [], set(), set(), []
    for row in rows:
        if not isinstance(row, dict):
            issues.append("walmart_india_listing_row_not_object")
            continue
        path = row.get("externalPath")
        title = normalized_title(row.get("title"))
        location = normalized_title(row.get("locationsText"))
        posted = normalized_title(row.get("postedOn"))
        bullets = row.get("bulletFields")
        if not isinstance(path, str) or not JOB_PATH.match(path):
            issues.append(f"walmart_india_listing_row_bad_path:{path!r}")
            continue
        if not title or not location or not posted:
            issues.append(f"walmart_india_listing_row_fields_missing:{path}")
            continue
        if not isinstance(bullets, list) or not bullets or not isinstance(bullets[0], str) or not REQ_ID.match(bullets[0]):
            issues.append(f"walmart_india_listing_row_requisition_missing:{path}")
            continue
        req = bullets[0]
        if path in seen_paths:
            issues.append(f"walmart_india_listing_duplicate_path:{path}")
            continue
        if req in seen_reqs:
            issues.append(f"walmart_india_listing_duplicate_requisition:{req}")
            continue
        seen_paths.add(path)
        seen_reqs.add(req)
        parsed.append(dict(row))
    expected_rows = min(PAGE_LIMIT, max(0, expected_total - offset))
    if len(rows) != expected_rows:
        issues.append(
            f"walmart_india_listing_page_size_mismatch:offset={offset}:"
            f"expected={expected_rows}:actual={len(rows)}"
        )
    return {
        "rows": parsed,
        "total": expected_total,
        "reported_total": total,
        "offset": offset,
        "limit": PAGE_LIMIT,
        "page_size": len(rows),
        "issues": issues,
    }


def _heading_text(node: Tag) -> str | None:
    value = normalized_title(node.get_text(" ", strip=True))
    if not value:
        return None
    if node.name in HEADING_TAGS:
        return value
    if node.name not in {"p", "div"} or len(value) > 140:
        return None
    bold = node.find(["b", "strong"])
    if bold is not None and normalized_title(bold.get_text(" ", strip=True)) == value:
        return value
    if HEADING_TEXT.match(value):
        return value
    return None


def description_sections(html: str) -> list[dict]:
    """Keep every employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-wmt-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-wmt-body]")
    sections: list[dict] = []
    current: dict | None = None
    for block in (root.contents if root else []):
        if isinstance(block, NavigableString):
            if not str(block).strip():
                continue
            if current is None:
                current = {"heading": None, "html": "", "text": ""}
            current["html"] += escape(str(block))
            continue
        if not isinstance(block, Tag):
            continue
        heading = _heading_text(block)
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        sections.append(current)
    if not sections:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def _matching_sections(sections: list[dict], tokens: tuple[str, ...]) -> list[dict]:
    return [
        section for section in sections
        if section.get("heading") and any(token in section["heading"].lower() for token in tokens)
    ]


def parse_detail(value: dict, expected_row: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("jobPostingInfo"), dict):
        raise ValueError("Walmart India CXS detail response is missing jobPostingInfo")
    info = value["jobPostingInfo"]
    path = expected_row.get("externalPath")
    req = expected_row.get("bulletFields", [None])[0]
    title = normalized_title(info.get("title"))
    if title != normalized_title(expected_row.get("title")):
        raise ValueError(f"Walmart India detail title does not match its exact listing row: {req}")
    if str(info.get("jobReqId") or "") != str(req):
        raise ValueError(f"Walmart India detail requisition does not match its exact listing row: {req}")
    external_url = info.get("externalUrl")
    parsed_url = urlsplit(external_url or "")
    if (parsed_url.scheme != "https" or parsed_url.netloc != WORKDAY_HOST
            or parsed_url.path != f"/{SITE}{path}"):
        raise ValueError(f"Walmart India detail externalUrl does not match its exact listed path: {req}")
    country = info.get("country")
    if not isinstance(country, dict) or country.get("id") != INDIA_COUNTRY_ID or country.get("descriptor") != "India":
        raise ValueError(f"Walmart India detail is not scoped to the published India country facet: {req}")
    description_html = info.get("jobDescription")
    if not isinstance(description_html, str) or not description_html.strip():
        raise ValueError(f"Walmart India detail has an empty jobDescription: {req}")
    normalized = normalized_body_text(description_html)
    if len(normalized) < 100:
        raise ValueError(f"Walmart India detail description is empty or truncated: {req}")
    sections = description_sections(description_html)
    links = [
        {"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
        for anchor in BeautifulSoup(description_html, "lxml").select("a[href]")
    ]
    attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
    start_date = info.get("startDate")
    start_date = start_date if isinstance(start_date, str) and ISO_DATE.match(start_date) else None
    hiring = value.get("hiringOrganization")
    employer = None
    if isinstance(hiring, dict) and normalized_title(hiring.get("name")):
        employer = normalized_title(hiring.get("name"))
    elif normalized_title(info.get("hiringOrganization")):
        employer = normalized_title(info.get("hiringOrganization"))
    location = normalized_title(info.get("location"))
    locations = [location] if location else []
    requisition_location = info.get("jobRequisitionLocation")
    if isinstance(requisition_location, dict) and normalized_title(requisition_location.get("descriptor")):
        descriptor = normalized_title(requisition_location["descriptor"])
        if descriptor not in locations:
            locations.append(descriptor)
    return {
        "title": title,
        "job_req_id": str(info.get("jobReqId")),
        "job_posting_id": info.get("jobPostingId"),
        "external_path": path,
        "external_url": external_url,
        "apply_url": apply_url(external_url),
        "description_html": description_html,
        "description_text": text_html(description_html),
        "description_normalized_chars": len(normalized),
        "description_fnv1a64": fnv1a64(normalized),
        "description_sha256": sha256(normalized.encode()).hexdigest(),
        "description_sections": sections,
        "responsibilities": _matching_sections(sections, RESPONSIBILITY_TOKENS) or None,
        "qualifications": _matching_sections(sections, QUALIFICATION_TOKENS) or None,
        "description_links": links,
        "role_attachments": attachments,
        "locations": locations,
        "location": location or None,
        "country": country,
        "time_type": info.get("timeType"),
        "start_date": start_date,
        "posted_on": info.get("postedOn"),
        "can_apply": info.get("canApply"),
        "posted": info.get("posted"),
        "employer": employer,
        "hiring_organization": hiring if isinstance(hiring, dict) else None,
        "questionnaire_id": info.get("questionnaireId"),
        "secondary_questionnaire_id": info.get("secondaryQuestionnaireId"),
    }


def _listing_payload(offset: int) -> dict:
    return {
        "appliedFacets": {"locationCountry": [INDIA_COUNTRY_ID]},
        "limit": PAGE_LIMIT,
        "offset": offset,
        "searchText": "",
    }


class WalmartGlobalTechIndia(Adapter):
    """Collect only the India-faceted Walmart Global Tech Workday listing."""

    VERIFICATION_REVISION = "walmart-global-tech-india.workday-india-journey.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._detail_parse_seconds = 0.0

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(
            OFFICIAL_ENTRY,
            headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                     "Referer": OFFICIAL_ENTRY},
        )
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
            redirects=[str(item.url) for item in response.history],
        )
        return parsed

    async def _listing_snapshot(self, label: str):
        started = time.perf_counter()
        rows, pages, seen_paths, seen_reqs, issues = [], [], set(), set(), []
        expected_total = None
        offset = 0
        while True:
            if not self.page_guard():
                issues.append(f"walmart_india_{label}_page_budget_exhausted")
                break
            payload = _listing_payload(offset)
            request_started = time.perf_counter()
            response = await self.http.request(
                "POST", LISTING_API, json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json",
                         "Referer": INDIA_LISTING_REFERER, "Origin": WORKDAY_ORIGIN},
            )
            request_seconds = max(0.0, time.perf_counter() - request_started)
            parsed_url = urlsplit(str(response.url))
            if (parsed_url.scheme != "https" or parsed_url.netloc != WORKDAY_HOST
                    or parsed_url.path != f"/wday/cxs/{TENANT}/{SITE}/jobs"):
                raise ValueError("Walmart India listing request redirected away from its exact company-scoped route")
            page_value = parse_listing_page(response.json(), offset, expected_total)
            if expected_total is None:
                expected_total = page_value["total"]
            issues.extend(page_value["issues"])
            for row in page_value["rows"]:
                path = row["externalPath"]
                req = row["bulletFields"][0]
                if path in seen_paths:
                    issues.append(f"walmart_india_{label}_duplicate_path:{path}")
                    continue
                if req in seen_reqs:
                    issues.append(f"walmart_india_{label}_duplicate_requisition:{req}")
                    continue
                seen_paths.add(path)
                seen_reqs.add(req)
                rows.append(row)
            pages.append({
                "offset": offset,
                "reported_total": page_value["reported_total"],
                "expected_total": expected_total,
                "returned_rows": len(page_value["rows"]),
                "page_size": page_value["page_size"],
                "requisitions": [row["bulletFields"][0] for row in page_value["rows"]],
                "request_url": str(response.request.url),
                "final_url": str(response.url),
                "request_seconds": request_seconds,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
                "request_body": payload,
            })
            if expected_total is not None and len(rows) >= expected_total:
                break
            if not page_value["rows"]:
                issues.append(f"walmart_india_{label}_empty_page_at_offset:{offset}")
                break
            offset += len(page_value["rows"])
        if expected_total is None:
            issues.append(f"walmart_india_{label}_total_unknown")
        elif len(rows) != expected_total:
            issues.append(f"walmart_india_{label}_retained_rows_do_not_match_total:{len(rows)}!={expected_total}")
        return {
            "rows": rows,
            "job_ids": [row["bulletFields"][0] for row in rows],
            "count": expected_total,
            "pages": pages,
            "issues": list(dict.fromkeys(issues)),
            "request_seconds": max(0.0, time.perf_counter() - started),
        }

    def _make_job(self, row):
        req = row["bulletFields"][0]
        return self.job(
            {"listing": dict(row)},
            title=row["title"],
            url=job_url(row["externalPath"]),
            job_id=req,
            requisition_id=req,
            apply_url=None,
            description="",
            method=None,
            description_url=None,
            locations=[row["locationsText"]],
            date_posted=None,
            date_posted_raw={"postedOn": row["postedOn"]},
            date_updated=None,
            valid_through=None,
            salary=None,
            employer=None,
            language="en",
        )

    async def _detail(self, job):
        row = job.raw["listing"]
        api = detail_api_url(row["externalPath"])
        started = time.perf_counter()
        response = await self.http.request(
            "GET", api,
            headers={"Accept": "application/json", "Referer": INDIA_LISTING_REFERER},
        )
        parsed_url = urlsplit(str(response.url))
        if (parsed_url.scheme != "https" or parsed_url.netloc != WORKDAY_HOST
                or parsed_url.path != f"/wday/cxs/{TENANT}/{SITE}{row['externalPath']}"):
            raise ValueError("Walmart India detail request redirected away from its exact listed route")
        payload = response.json()
        parse_started = time.perf_counter()
        detail = parse_detail(payload, row)
        self._detail_parse_seconds += max(0.0, time.perf_counter() - parse_started)
        job.title = detail["title"]
        job.description_html = detail["description_html"]
        job.description_method = "workday.jobPostingInfo.jobDescription"
        job.description_source_url = api
        job.requisition_id = detail["job_req_id"]
        job.job_id = detail["job_req_id"]
        job.apply_url = detail["apply_url"]
        job.date_posted = detail["start_date"]
        job.date_posted_raw = {"postedOn": detail["posted_on"], "startDate": detail["start_date"]}
        job.date_updated = None
        job.valid_through = None
        job.locations = detail["locations"]
        job.employment_type = detail["time_type"]
        job.workplace_type = None
        job.salary = None
        job.employer = detail["employer"]
        job.raw = {
            "listing": row,
            "detail": payload,
            "detail_meta": {
                "source_fields": sorted(payload["jobPostingInfo"].keys()),
                "description_sha256": detail["description_sha256"],
                "description_normalized_chars": detail["description_normalized_chars"],
                "description_fnv1a64": detail["description_fnv1a64"],
                "description_sections": detail["description_sections"],
                "description_links": detail["description_links"],
                "role_attachments": detail["role_attachments"],
                "dates": {
                    "startDate": detail["start_date"],
                    "postedOn": detail["posted_on"],
                    "posting_date_mapping": "jobPostingInfo.startDate is the Workday posting start date; postedOn is its relative UI label.",
                    "endDate": None,
                },
                "application_route": {
                    "listing_cta": None,
                    "exact_job_page_url": detail["external_url"],
                    "detail_page_apply_control": "Apply",
                    "detail_page_apply_href": detail["apply_url"],
                    "application_shell_url": detail["apply_url"],
                },
                "country": detail["country"],
                "hiring_organization": detail["hiring_organization"],
                "questionnaire_id": detail["questionnaire_id"],
                "secondary_questionnaire_id": detail["secondary_questionnaire_id"],
                "can_apply": detail["can_apply"],
                "posted": detail["posted"],
                "request_seconds": max(0.0, time.perf_counter() - started),
                "response_sha256": sha256(response.content).hexdigest(),
                "response_bytes": len(response.content),
            },
        }
        if detail["role_attachments"]:
            self.result.errors.append(f"walmart_india_role_attachments_require_exact_review:{detail['job_req_id']}")
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": INDIA_LISTING_URL,
            "listing_authority": "The exact Workday India route published by tech.walmart.com's own careers form (region=India).",
            "listing_membership_request": {
                "method": "POST",
                "path": f"/wday/cxs/{TENANT}/{SITE}/jobs",
                "query_shape": {
                    "appliedFacets": {"locationCountry": [INDIA_COUNTRY_ID]},
                    "limit": PAGE_LIMIT,
                    "offset": "0..180 in 20-row pages",
                    "searchText": "",
                },
                "filters_applied": {"locationCountry": "India"},
                "scope": "Walmart Workday tenant/site only, with the exact India country facet published by the Global Tech careers page; no unfiltered board, other country, keyword or category filter.",
            },
            "detail_method": "One GET per exact listed externalPath to the Workday CXS detail route; full jobPostingInfo.jobDescription retained.",
            "request_scope": "Only the exact India-faceted listing and exact listed job paths; no Workday search/discovery, no other tenant/site.",
        })
        try:
            initial_entry = await self._official_entry()
            initial = await self._listing_snapshot("initial")
        except Exception as exc:
            self.result.errors.append(f"Walmart Global Tech India source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result
        initial_source_seconds = max(0.0, time.perf_counter() - source_started)

        self.result.expected_total = initial["count"]
        self.result.metadata["initial_official_entry"] = initial_entry
        self.result.metadata["initial_listing"] = {key: value for key, value in initial.items() if key != "rows"}
        self.result.metadata["initial_listing_job_ids"] = initial["job_ids"]
        self.result.errors.extend(initial["issues"])
        jobs = [self._make_job(row) for row in initial["rows"]]
        jobs = await self.hydrate(jobs, self._detail)
        self.result.jobs = jobs

        final_entry = None
        final = None
        final_started = time.perf_counter()
        try:
            final_entry = await self._official_entry()
            final = await self._listing_snapshot("final")
        except Exception as exc:
            self.result.errors.append(f"Walmart Global Tech India final source recheck failed: {type(exc).__name__}: {exc}")
        final_seconds = max(0.0, time.perf_counter() - final_started)

        final_matches = False
        if final is not None and final_entry is not None:
            initial_by_id = {row["bulletFields"][0]: row for row in initial["rows"]}
            final_by_id = {row["bulletFields"][0]: row for row in final["rows"]}
            changed_fields = {}
            for req, row in initial_by_id.items():
                other = final_by_id.get(req)
                if other is not None and other != row:
                    changed_fields[req] = sorted(
                        key for key in set(row) | set(other) if row.get(key) != other.get(key)
                    )
            counts_match = initial["count"] == final["count"]
            same_ids = initial["job_ids"] == final["job_ids"]
            entry_same = (
                initial_entry["india_listing_url"] == final_entry["india_listing_url"]
                and initial_entry["region_options"] == final_entry["region_options"]
                and initial_entry["india_option_label"] == final_entry["india_option_label"]
            )
            final_matches = (
                same_ids and counts_match and not changed_fields and entry_same
                and not final["issues"]
            )
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_listing"] = {key: value for key, value in final.items() if key != "rows"}
            self.result.metadata["final_listing_job_ids"] = final["job_ids"]
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["job_ids"],
                "final_ids": final["job_ids"],
                "initial_count": initial["count"],
                "final_count": final["count"],
                "initial_pages": len(initial["pages"]),
                "final_pages": len(final["pages"]),
                "same_ids_and_order": same_ids,
                "all_row_fields_match": not changed_fields,
                "changed_fields_by_job_id": changed_fields,
                "new_job_ids": [req for req in final["job_ids"] if req not in initial_by_id],
                "removed_job_ids": [req for req in initial["job_ids"] if req not in final_by_id],
                "official_india_route_still_exact": entry_same,
                "complete": final_matches,
            }
            if not entry_same:
                self.result.errors.append("walmart_india_official_route_changed_during_collection")
            if not same_ids or not counts_match:
                self.result.errors.append("walmart_india_listing_membership_changed_during_collection")
            if changed_fields:
                self.result.errors.append("walmart_india_source_fields_changed_during_collection")
            self.result.errors.extend(final["issues"])
        else:
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["job_ids"],
                "initial_count": initial["count"],
                "initial_pages": len(initial["pages"]),
                "final_ids": None,
                "final_count": None,
                "complete": False,
            }

        self.result.timings["source_collection_wall_seconds"] = initial_source_seconds
        self.result.timings["final_recheck_wall_seconds"] = final_seconds
        self.result.timings["overall_adapter_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.timings["detail_parse_seconds_sum"] = self._detail_parse_seconds
        self.result.metadata["detail_hydration_mode"] = "One CXS detail GET per exact listed externalPath; full jobPostingInfo.jobDescription retained."
        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.metadata["listing_count"] = initial["count"]
        self.result.metadata["listing_page_count"] = len(initial["pages"])
        self.result.metadata["current_native_membership_reconciled"] = final_matches
        self.result.listing_complete = bool(final_matches and len(jobs) == initial["count"])
        return self.result
