"""Walmart Labs (Walmart Global Tech) US technology jobs on Walmart's own careers site.

The official entry ``https://tech.walmart.com/content/walmart-global-tech/en_us/careers.html``
is Walmart Global Tech's own careers page.  Its published search journey sends
US searches to ``careers.walmart.com``.  The company-scoped technology listing
used here is the careers site's Technology career area (the page explicitly
branded "Walmart Global Tech"), whose "See all technology roles" link resolves
to ``/us/en/results?searchQuery=All&careerareas=Technology``.  The unfiltered
parent-wide US board, the Canada board and the India Workday board are not used
by this module.

Membership comes from the same hybrid search request the careers frontend uses
for the technology query/filter (661 unique jobs at review time), cross-checked
against the AI listing's advertised total.  The AI ten-card pagination is not
used as membership authority because repeated pages and re-ranking made a
sequential capture produce only 419 unique IDs against 661 advertised; the
complete hybrid response is stable as a set and returns all 661 IDs in one
request.

Each detail page is server-rendered and carries the complete employer-authored
``jobPostingDescription`` in ``__NEXT_DATA__.props.pageProps.jobDetails``.
Descriptions are never taken from cards, previews, footers or application
questions.  A listed job whose detail route answers the exact
``/us/en/job-not-found`` redirect is recorded as an exact failure, not replaced
with an empty description.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from html import unescape
import json
import re
import time
import unicodedata
import uuid
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import Result, now, text_html


OFFICIAL_ENTRY = "https://tech.walmart.com/content/walmart-global-tech/en_us/careers.html"
CAREERS_ORIGIN = "https://careers.walmart.com"
TECHNOLOGY_AREA_URL = CAREERS_ORIGIN + "/us/en/home/careers-areas/technology"
LISTING_URL = CAREERS_ORIGIN + "/us/en/results?searchQuery=All&careerareas=Technology"
GRAPHQL_URL = CAREERS_ORIGIN + "/api/graphql"
DETAIL_URL = CAREERS_ORIGIN + "/us/en/jobs/{job_id}"
JOB_NOT_FOUND_PATH = "/us/en/job-not-found"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
AI_QUERY_ID = "b0467c1f-f578-4261-9280-0ea4614f251c"       # ChatBasedJobSearchQuery
HYBRID_QUERY_ID = "b09f52ab-9c7f-421f-8a2a-d990b6f288a8"   # HybridCombinedSearchV2Query
LISTING_QUERY_TEXT = "All jobs in Technology"
LISTING_FILTER = "areas IN ['Technology']"
AI_PAGE_SIZE = 10
HYBRID_SIZE = 1000
DETAIL_METHOD = "walmart_careers.next_data.jobPostingDescription"
JOB_ID = re.compile(r"^[A-Za-z]{1,4}-?\d[\w-]*$")
ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
ATTACHMENT = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
HEADINGS = ("h1", "h2", "h3", "h4")
GRAPHQL_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": CAREERS_ORIGIN,
    "Referer": LISTING_URL,
    "User-Agent": BROWSER_UA,
}
PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": BROWSER_UA,
}


class DetailUnavailable(RuntimeError):
    """The exact listed job no longer has a public detail page."""


class RateLimited(RuntimeError):
    """The source answered HTTP 429; stop rather than bypass it."""


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(value: str | None) -> str:
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def canonical_careers_url(href: str) -> str:
    """Resolve an AEM ``/content/careers/...`` link to the public careers URL."""
    absolute = urljoin(CAREERS_ORIGIN + "/", href or "")
    parts = urlsplit(absolute)
    path = parts.path
    if path.startswith("/content/careers"):
        path = path[len("/content/careers"):] or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _date_only(value) -> str | None:
    if not isinstance(value, str):
        return None
    match = ISO_DATE.match(value.strip())
    return match.group(1) if match else None


def _number(value):
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        if not cleaned:
            return None
        try:
            number = float(cleaned)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    return None


def _dict_value(value):
    return value.get("value") if isinstance(value, dict) else value


def _location(value: dict, *, primary: bool) -> dict | None:
    if not isinstance(value, dict):
        return None
    banner = value.get("banner") if isinstance(value.get("banner"), dict) else {}
    store_type = value.get("storeType") if isinstance(value.get("storeType"), dict) else {}
    result = {
        "location_id": value.get("locationId") or value.get("id"),
        "store_number": value.get("storeNumber"),
        "location_name": value.get("locationName"),
        "city": value.get("city"),
        "county": value.get("county"),
        "state": value.get("stateCode") or value.get("state"),
        "country": value.get("country"),
        "country_code": value.get("countryCode"),
        "address": value.get("address"),
        "postal_code": value.get("postalCode"),
        "latitude": value.get("latitude"),
        "longitude": value.get("longitude"),
        "banner": banner.get("description"),
        "store_type": store_type.get("description"),
        "is_primary": primary,
    }
    return {key: item for key, item in result.items() if item is not None}


def description_sections(html: str) -> list[dict]:
    """Split the employer body on its real h1–h4 headings, keeping exact text."""
    soup = BeautifulSoup(html or "", "lxml")
    root = soup.body or soup
    sections: list[dict] = []
    current: dict | None = None
    for child in root.children:
        if not isinstance(child, Tag):
            continue
        if child.name in HEADINGS:
            heading = normalized_title(child.get_text(" ", strip=True))
            if not heading:
                continue
            current = {"heading": heading, "html": "", "text": ""}
            sections.append(current)
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
            sections.append(current)
        current["html"] += str(child)
    for section in sections:
        section["text"] = text_html(section["html"]).strip()
    return sections


def section_by_heading(sections: list[dict], prefixes: tuple[str, ...]) -> str | None:
    for section in sections:
        heading = (section.get("heading") or "").casefold()
        if any(heading.startswith(prefix.casefold()) for prefix in prefixes):
            text = (section.get("text") or "").strip()
            return text or None
    return None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "tech.walmart.com"
            or parts.path != "/content/walmart-global-tech/en_us/careers.html"):
        raise ValueError("Walmart Global Tech official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Walmart Global Tech" not in title:
        raise ValueError("Walmart Global Tech official careers entry identity changed")
    form = soup.select_one("form#job-search-form")
    region = soup.select_one("select#job-region")
    keyword = soup.select_one("input#job-keyword")
    if form is None or region is None or keyword is None:
        raise ValueError("Walmart Global Tech careers search form is missing")
    options = {
        (option.get("value") or "").strip(): normalized_title(option.get_text(" ", strip=True))
        for option in region.select("option")
    }
    if options.get("US") != "U.S. based":
        raise ValueError("Walmart Global Tech careers page no longer publishes the US region option")
    scripts = "\n".join(script.get_text() for script in soup.select("script"))
    us_template = f"{CAREERS_ORIGIN}/us/en/results?searchQuery=${{jobKeyword}}"
    india_template = (
        "https://walmart.wd504.myworkdayjobs.com/WalmartExternal"
        "?q=${jobKeyword}&locationCountry=c4f78be1a8f14da0ab49ce1162348a5e"
    )
    if "jobRegion == 'US'" not in scripts or us_template not in scripts:
        raise ValueError("Walmart Global Tech careers page no longer routes US to the inspected careers results")
    if "jobRegion == 'India'" not in scripts or india_template not in scripts:
        raise ValueError("Walmart Global Tech careers page no longer routes India to the inspected Workday listing")
    return {
        "url": response_url,
        "title": title,
        "form_id": form.get("id"),
        "keyword_input": keyword.get("id"),
        "region_options": [{"value": key, "label": label} for key, label in options.items() if key],
        "us_listing_template": us_template,
        "india_listing_template": india_template,
    }


def parse_technology_area(html: str, response_url: str = TECHNOLOGY_AREA_URL) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "careers.walmart.com"
            or parts.path != "/us/en/home/careers-areas/technology"):
        raise ValueError("Walmart Technology career-area page redirected away from its inspected URL")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    headings = [normalized_title(h.get_text(" ", strip=True)) for h in soup.select("h1")]
    body = normalized_title(soup.get_text(" ", strip=True))
    if title != "Technology" or headings[:1] != ["Technology"]:
        raise ValueError("Walmart Technology career-area identity changed")
    if "Walmart Global Tech" not in body or "See all technology roles" not in body:
        raise ValueError("Walmart Technology career-area no longer publishes the Walmart Global Tech technology listing")
    links = []
    for anchor in soup.select("a[href]"):
        label = normalized_title(anchor.get_text(" ", strip=True))
        href = anchor.get("href") or ""
        if label in ("See all technology roles", "See all open roles") and "careerareas=Technology" in href:
            links.append({"label": label, "href": href, "url": canonical_careers_url(href)})
    if not links:
        raise ValueError("Walmart Technology career-area has no 'See all technology roles' listing link")
    urls = {item["url"] for item in links}
    if urls != {LISTING_URL}:
        raise ValueError(f"Walmart Technology career-area listing link changed: {sorted(urls)}")
    return {
        "url": response_url,
        "title": title,
        "listing_links": links,
        "listing_url": LISTING_URL,
        "walmart_global_tech_named": True,
    }


def parse_ai_count(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Walmart AI listing response is not an object")
    if value.get("errors"):
        raise ValueError(f"Walmart AI listing returned GraphQL errors: {value['errors']}")
    assistant = (value.get("data") or {}).get("jobSearchAssistant")
    if not isinstance(assistant, dict):
        raise ValueError("Walmart AI listing response is missing jobSearchAssistant")
    messages = assistant.get("tool_messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("Walmart AI listing response has no tool message")
    message = messages[0]
    if not isinstance(message, dict):
        raise ValueError("Walmart AI listing tool message is not an object")
    artifact = message.get("artifact")
    if not isinstance(artifact, dict):
        raise ValueError("Walmart AI listing tool message has no artifact")
    content = message.get("content")
    content_data = {}
    if isinstance(content, str):
        try:
            content_data = json.loads(content)
        except ValueError:
            content_data = {}
    if not isinstance(content_data, dict):
        content_data = {}
    total = artifact.get("total_jobs", content_data.get("total_jobs"))
    if not isinstance(total, int) or total < 0:
        raise ValueError("Walmart AI listing response has no integer total_jobs")
    filters = artifact.get("filters") or content_data.get("job_filters") or ""
    if "areas IN" not in str(filters) or "Technology" not in str(filters):
        raise ValueError(f"Walmart AI listing response is not scoped to Technology: {filters!r}")
    page = artifact.get("job_page_number")
    if page != 0:
        raise ValueError(f"Walmart AI listing first page is not page 0: {page!r}")
    jobs = artifact.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("Walmart AI listing response has no jobs list")
    if len(jobs) > total:
        raise ValueError("Walmart AI listing first page has more rows than total_jobs")
    page_size = artifact.get("page_size")
    return {
        "total_jobs": total,
        "job_page_number": page,
        "page_size": page_size,
        "filters": filters,
        "jobs": jobs,
        "refined_query": artifact.get("refined_query") or content_data.get("refined_query"),
    }


def parse_membership(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Walmart membership response is not an object")
    if value.get("errors"):
        raise ValueError(f"Walmart membership returned GraphQL errors: {value['errors']}")
    hybrid = (value.get("data") or {}).get("hybridCombinedSearchV2")
    if not isinstance(hybrid, dict):
        raise ValueError("Walmart membership response is missing hybridCombinedSearchV2")
    rows = hybrid.get("jobs")
    if not isinstance(rows, list):
        raise ValueError("Walmart membership response has no jobs list")
    parsed: list[dict] = []
    seen: set[str] = set()
    issues: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            issues.append("walmart_membership_row_not_object")
            continue
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            issues.append("walmart_membership_row_missing_metadata")
            continue
        identity = metadata.get("jobId")
        title = normalized_title(metadata.get("jobPostingTitle"))
        brand = normalized_title(metadata.get("brand"))
        if not isinstance(identity, str) or not JOB_ID.match(identity):
            issues.append(f"walmart_membership_row_bad_job_id:{identity!r}")
            continue
        if not title or not brand:
            issues.append(f"walmart_membership_row_missing_title_or_brand:{identity}")
            continue
        if identity in seen:
            issues.append(f"walmart_membership_duplicate_job_id:{identity}")
            continue
        seen.add(identity)
        parsed.append({
            "source_row_id": row.get("id"),
            "job_id": identity,
            "brand": brand,
            "title": title,
            "city": metadata.get("primaryLocationCity"),
            "state": metadata.get("primaryLocationState"),
            "workgroup_desc": metadata.get("workgroupDesc"),
            "metadata": metadata,
        })
    return {
        "rows": parsed,
        "job_ids": [row["job_id"] for row in parsed],
        "count": len(parsed),
        "issues": issues,
    }


def _salary_from_detail(detail: dict) -> dict | None:
    ranges = []
    for item in detail.get("payRange") or []:
        if not isinstance(item, dict):
            continue
        minimum, maximum = _number(item.get("min")), _number(item.get("max"))
        if not minimum and not maximum:
            continue
        ranges.append({
            "location": item.get("location"),
            "code": item.get("code"),
            "min": minimum,
            "max": maximum,
            "min_raw": item.get("min"),
            "max_raw": item.get("max"),
        })
    if not ranges:
        return None
    plan = detail.get("payPlanData") if isinstance(detail.get("payPlanData"), dict) else {}
    currency = None
    reference = plan.get("currencyReference") if isinstance(plan.get("currencyReference"), dict) else {}
    if reference.get("currencyId"):
        currency = reference["currencyId"]
    return {
        "currency": currency,
        "frequency": detail.get("payFrequency"),
        "ranges": ranges,
        "source_field": "payRange",
    }


def _qualifications(detail: dict, sections: list[dict]) -> dict | None:
    minimum = detail.get("minimumQualification")
    preferred = detail.get("preferredQualification")
    minimum = minimum if isinstance(minimum, list) else ([minimum] if isinstance(minimum, str) else [])
    preferred = preferred if isinstance(preferred, list) else ([preferred] if isinstance(preferred, str) else [])
    minimum_text = section_by_heading(sections, ("Minimum Qualifications", "Minimum qualifications"))
    preferred_text = section_by_heading(sections, ("Preferred Qualifications", "Preferred qualifications"))
    if not (minimum or preferred or minimum_text or preferred_text):
        return None
    return {
        "minimum": minimum,
        "preferred": preferred,
        "minimum_text": minimum_text,
        "preferred_text": preferred_text,
    }


def _responsibilities(sections: list[dict]) -> str | None:
    text = section_by_heading(
        sections,
        ("What you'll do", "What you will do", "Responsibilities", "Role summary"),
    )
    if text and len(text) >= 40:
        return text
    return None


def _employer(detail: dict) -> dict | None:
    organization = detail.get("organizationData")
    company = organization.get("company") if isinstance(organization, dict) else None
    description = company.get("description") if isinstance(company, dict) else None
    if isinstance(description, dict):
        for key in ("101", "102", "103"):
            value = description.get(key)
            if isinstance(value, str) and value.strip():
                return {"name": value.strip(), "source_field": f"organizationData.company.description.{key}"}
        for value in description.values():
            if isinstance(value, str) and value.strip():
                return {"name": value.strip(), "source_field": "organizationData.company.description"}
    brand = detail.get("brand")
    return {"name": brand, "source_field": "brand"} if brand else None


def parse_detail(html: str, response_url: str, listing: dict | None = None) -> dict:
    parts = urlsplit(response_url)
    if parts.netloc != "careers.walmart.com":
        raise ValueError("Walmart detail response came from an unexpected host")
    if parts.path == JOB_NOT_FOUND_PATH or parts.path.endswith("/job-not-found"):
        raise DetailUnavailable("job_not_found_redirect")
    soup = BeautifulSoup(html or "", "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        raise DetailUnavailable("next_data_missing")
    try:
        document = json.loads(script.get_text())
    except ValueError as exc:
        raise DetailUnavailable(f"next_data_invalid_json: {exc}") from exc
    page = ((document.get("props") or {}).get("pageProps") or {})
    detail = page.get("jobDetails")
    if not isinstance(detail, dict):
        raise DetailUnavailable("job_details_missing")
    identity = detail.get("jobId")
    if not isinstance(identity, str) or not identity.strip():
        raise DetailUnavailable("job_id_missing")
    title = normalized_title(detail.get("title") or detail.get("jobPostingTitle"))
    description_html = detail.get("jobPostingDescription") or detail.get("description") or ""
    if not isinstance(description_html, str):
        description_html = ""
    description_text = text_html(description_html)
    sections = description_sections(description_html) if description_html else []
    issues = []
    if not title:
        issues.append("detail_title_missing")
    if len(description_text) < 100:
        issues.append("detail_description_short_or_empty")
    if detail.get("jobPostingDescription") in (None, "") and detail.get("description"):
        issues.append("job_posting_description_missing_used_description")
    listing_row = listing or {}
    listing_title = normalized_title(listing_row.get("title"))
    if listing_title and title and listing_title.casefold() != title.casefold():
        issues.append("detail_title_differs_from_listing")
    listing_brand = normalized_title(listing_row.get("brand"))
    detail_brand = normalized_title(detail.get("brand"))
    if listing_brand and detail_brand and listing_brand.casefold() != detail_brand.casefold():
        issues.append("detail_brand_differs_from_listing")
    detail_date = _date_only(detail.get("jobPostingStartDate"))
    listing_date = None
    listing_ms = listing_row.get("jobPostingStartDate")
    if isinstance(listing_ms, (int, float)):
        listing_date = datetime.fromtimestamp(listing_ms / 1000, tz=timezone.utc).date().isoformat()
    if detail_date and listing_date and detail_date != listing_date:
        issues.append("detail_posting_date_differs_from_listing")
    locations = []
    primary = _location(detail.get("primaryLocation"), primary=True)
    if primary:
        locations.append(primary)
    additional = detail.get("additionalLocations")
    if isinstance(additional, list):
        for item in additional:
            location = _location(item, primary=False)
            if location:
                locations.append(location)
    profile = detail.get("profileMapping")
    areas, categories = [], []
    if isinstance(profile, dict):
        areas = sorted(profile)
        categories = sorted({
            key for value in profile.values() if isinstance(value, dict) for key in value
        })
    business = detail.get("businessSegment")
    business_value = business.get("value") if isinstance(business, dict) else business
    job_type = detail.get("jobType")
    job_type_value = job_type.get("value") if isinstance(job_type, dict) else job_type
    employment_raw = detail.get("employmentTypes")
    employment = []
    if isinstance(employment_raw, list):
        for item in employment_raw:
            value = item.get("value") if isinstance(item, dict) else item
            if value:
                employment.append(value)
    if not employment and job_type_value:
        employment = [job_type_value]
    department = {
        "career_areas": areas,
        "categories": categories,
        "profile_mapping": profile,
        "business_segment": business_value,
    } if (areas or categories or business_value) else None
    return {
        "job_id": identity.strip(),
        "title": title,
        "job_posting_id": detail.get("jobPostingId"),
        "brand": detail_brand or None,
        "active": detail.get("active"),
        "requisition_status": _dict_value(detail.get("requisitionStatus")),
        "position_available": detail.get("positionAvailable"),
        "date_posted": detail_date,
        "date_posted_raw": detail.get("jobPostingStartDate"),
        "date_updated": None,
        "valid_through": None,
        "date_created_raw": detail.get("createdAt"),
        "date_effective_raw": detail.get("effectiveDate"),
        "description_html": description_html,
        "description_text": description_text,
        "description_sections": sections,
        "qualifications": _qualifications(detail, sections),
        "responsibilities": _responsibilities(sections),
        "locations": locations,
        "salary": _salary_from_detail(detail),
        "employment_type": employment or None,
        "job_type": job_type_value,
        "position_worker_type": _dict_value(detail.get("positionWorkerType")),
        "work_shift": _dict_value(detail.get("workShift")),
        "employer": _employer(detail),
        "department": department,
        "apply_url": None,
        "apply_flow": {
            "button_label": "Apply now",
            "route": CAREERS_ORIGIN + "/us/en/jobs/apply",
            "requires_login": True,
            "per_job_url_published": False,
        },
        "attachments": [
            {"url": unescape(anchor.get("href")), "label": normalized_title(anchor.get_text(" ", strip=True)) or None}
            for anchor in soup.select('[data-testid="JobDescription-htmldiv"] a[href]')
            if ATTACHMENT.search(anchor.get("href", ""))
        ],
        "detail": detail,
        "issues": issues,
    }


class WalmartLabs(Adapter):
    """Collect the US Walmart Global Tech technology listing and full job pages."""

    VERIFICATION_REVISION = "walmart-labs.careers-technology-hybrid-nextdata.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rate_limited = False
        self._detail_stop = asyncio.Event()

    @staticmethod
    def _thread_id() -> str:
        return f"S-{int(time.time() * 1000)}-{uuid.uuid4()}"

    def _ai_body(self) -> dict:
        return {
            "queryId": AI_QUERY_ID,
            "variables": {
                "chatRequest": {
                    "messages": [{"role": "user", "content": [{"type": "text", "text": LISTING_QUERY_TEXT}]}],
                    "thread_id": self._thread_id(),
                    "channel": "job_search",
                    "context": {
                        "job_search_context": {
                            "locale": "en_US",
                            "sort": "relevance",
                            "active_tab": "jobs",
                            "management_levels": [],
                            "content_page": 0,
                            "future_roles_page": 0,
                            "job_page": 0,
                        }
                    },
                }
            },
            "headers": {},
        }

    def _membership_body(self) -> dict:
        return {
            "queryId": HYBRID_QUERY_ID,
            "variables": {
                "request": {
                    "query": LISTING_QUERY_TEXT,
                    "isBasicSearch": False,
                    "filter": LISTING_FILTER,
                },
                "locale": "en_US",
                "page": 0,
                "size": HYBRID_SIZE,
            },
            "headers": {},
        }

    async def _graphql(self, body: dict) -> dict:
        response = await self.http.request("POST", GRAPHQL_URL, json=body, headers=GRAPHQL_HEADERS)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"Walmart GraphQL response was not JSON: {response.headers.get('content-type')}") from exc

    async def _ai_count(self) -> dict:
        started = time.perf_counter()
        body = self._ai_body()
        try:
            value = await self._graphql(body)
            parsed = parse_ai_count(value)
            parsed["request"] = body
            return parsed
        finally:
            self.result.timings["ai_count_request_seconds"] = (
                self.result.timings.get("ai_count_request_seconds", 0.0)
                + max(0.0, time.perf_counter() - started)
            )

    async def _membership(self) -> dict:
        started = time.perf_counter()
        body = self._membership_body()
        try:
            value = await self._graphql(body)
            parsed = parse_membership(value)
            parsed["request"] = body
            return parsed
        finally:
            self.result.timings["membership_request_seconds"] = (
                self.result.timings.get("membership_request_seconds", 0.0)
                + max(0.0, time.perf_counter() - started)
            )

    def _listing_job(self, row: dict, position: int):
        identity = row["job_id"]
        location = None
        if row.get("city") or row.get("state"):
            location = {
                "city": row.get("city"),
                "state": row.get("state"),
                "country": "US",
                "source": "hybrid_search_metadata",
            }
        return self.job(
            {"listing": row, "listing_position": position},
            title=row["title"],
            url=DETAIL_URL.format(job_id=identity),
            job_id=identity,
            method=None,
            description_url=DETAIL_URL.format(job_id=identity),
            locations=[location] if location else [],
            department=None,
            employment_type=None,
            date_posted=None,
            salary=None,
        )

    async def _detail_one(self, job):
        if self._detail_stop.is_set():
            job.warnings.append("detail_skipped_after_rate_limit")
            return job
        try:
            response = await self.http.get(job.url, headers=PAGE_HEADERS)
        except FetchError as exc:
            message = str(exc)
            if "429" in message:
                self._rate_limited = True
                self._detail_stop.set()
                self.result.errors.append(f"Detail {job.url}: HTTP 429; stopped detail phase")
                job.description_unavailable = True
                job.description_method = "unavailable_rate_limited"
                job.description_source_url = job.url
                job.warnings.append("detail_rate_limited")
                return job
            job.description_unavailable = True
            job.description_method = "unavailable_detail_fetch_error"
            job.description_source_url = job.url
            job.warnings.append("detail_fetch_failed")
            self.result.errors.append(f"Detail {job.url}: {type(exc).__name__}: {exc}")
            return job
        final_url = str(response.url)
        job.raw["detail_page"] = {
            "url": job.url,
            "final_url": final_url,
            "status": response.status_code,
            "bytes": len(response.content),
            "sha256": sha256(response.content).hexdigest(),
            "fetched_at": now(),
        }
        if urlsplit(final_url).path == JOB_NOT_FOUND_PATH or "job-not-found" in final_url:
            job.description_unavailable = True
            job.description_method = "unavailable_job_not_found"
            job.description_source_url = job.url
            job.warnings.append("detail_page_job_not_found")
            self.result.errors.append(f"Detail {job.url}: exact /us/en/job-not-found redirect")
            return job
        try:
            parsed = parse_detail(response.text, final_url, job.raw.get("listing"))
        except DetailUnavailable as exc:
            job.description_unavailable = True
            job.description_method = f"unavailable_{exc}"
            job.description_source_url = job.url
            job.warnings.append(f"detail_unavailable:{exc}")
            self.result.errors.append(f"Detail {job.url}: {exc}")
            return job
        except Exception as exc:
            job.description_unavailable = True
            job.description_method = "unavailable_detail_parse_error"
            job.description_source_url = job.url
            job.warnings.append("detail_parse_failed")
            self.result.errors.append(f"Detail {job.url}: {type(exc).__name__}: {exc}")
            return job
        if parsed["job_id"] != job.job_id:
            job.description_unavailable = True
            job.description_method = "unavailable_detail_id_mismatch"
            job.description_source_url = job.url
            job.warnings.append("detail_job_id_mismatch")
            self.result.errors.append(
                f"Detail {job.url}: detail jobId {parsed['job_id']!r} != listed {job.job_id!r}"
            )
            return job
        job.title = parsed["title"] or job.title
        job.requisition_id = parsed["job_posting_id"]
        job.apply_url = parsed["apply_url"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = DETAIL_METHOD
        job.description_source_url = job.url
        job.locations = parsed["locations"] or job.locations
        job.department = parsed["department"]
        job.employment_type = parsed["employment_type"]
        job.workplace_type = None
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.date_updated = None
        job.valid_through = None
        job.salary = parsed["salary"]
        job.qualifications = parsed["qualifications"]
        job.responsibilities = parsed["responsibilities"]
        job.employer = parsed["employer"]
        job.language = "en"
        job.raw["detail"] = parsed["detail"]
        job.raw["description_sections"] = parsed["description_sections"]
        job.raw["apply_flow"] = parsed["apply_flow"]
        job.raw["attachments"] = parsed["attachments"]
        job.raw["detail_field_sources"] = {
            "date_posted": "jobPostingStartDate",
            "date_updated": None,
            "valid_through": None,
            "description": "jobPostingDescription",
            "salary": "payRange" if parsed["salary"] else None,
            "locations": "primaryLocation+additionalLocations",
            "employer": (parsed["employer"] or {}).get("source_field"),
            "qualifications": "minimumQualification+preferredQualification",
            "responsibilities": "jobPostingDescription sections",
        }
        for issue in parsed["issues"]:
            job.warnings.append(issue)
            if issue in ("detail_description_short_or_empty", "detail_title_missing"):
                job.description_unavailable = True
                self.result.errors.append(f"Detail {job.url}: {issue}")
        job.company_scope_verified = True
        return job

    async def _hydrate_details(self, jobs):
        started = time.perf_counter()
        queue: asyncio.Queue = asyncio.Queue()
        for job in jobs:
            queue.put_nowait(job)
        stats = {
            "total": len(jobs),
            "completed": 0,
            "verified": 0,
            "unavailable": 0,
            "failed_requests": 0,
        }
        checkpoint_every = 25

        async def worker():
            while not queue.empty():
                if self._detail_stop.is_set():
                    return
                try:
                    job = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await self._detail_one(job)
                except Exception as exc:
                    job.description_unavailable = True
                    job.description_method = "unavailable_detail_worker_error"
                    job.description_source_url = job.url
                    job.warnings.append("detail_worker_failed")
                    self.result.errors.append(f"Detail {job.url}: {type(exc).__name__}: {exc}")
                finally:
                    queue.task_done()
                    self.result.validate_job(job)
                    job.company_scope_verified = True
                    stats["completed"] += 1
                    stats["verified"] += int(job.description_verified)
                    stats["unavailable"] += int(job.description_unavailable)
                    if job.raw.get("detail_page") is None:
                        stats["failed_requests"] += 1
                    if self.options.checkpoint and stats["completed"] % checkpoint_every == 0:
                        try:
                            self.options.checkpoint(self.result)
                        except Exception as exc:
                            self.result.warnings.append(f"checkpoint_failed: {type(exc).__name__}: {exc}")

        workers = [asyncio.create_task(worker()) for _ in range(max(1, self.options.detail_concurrency))]
        await asyncio.gather(*workers)
        stats["duration_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        stats["rate_limited"] = self._rate_limited
        self.result.metadata["details"] = stats
        self.result.timings["detail_phase_seconds"] = (
            self.result.timings.get("detail_phase_seconds", 0.0) + stats["duration_seconds"]
        )
        return stats

    async def run(self) -> Result:
        entry_started = time.perf_counter()
        try:
            entry_response = await self.http.get(OFFICIAL_ENTRY, headers=PAGE_HEADERS)
            self.entry = parse_official_entry(entry_response.text, str(entry_response.url))
            self.result.metadata["official_entry"] = {
                **self.entry,
                "status": entry_response.status_code,
                "final_url": str(entry_response.url),
            }
        except Exception as exc:
            self.result.errors.append(f"official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.metadata["official_entry"] = {"url": OFFICIAL_ENTRY, "error": str(exc)}
        self.result.timings["official_entry_seconds"] = max(0.0, time.perf_counter() - entry_started)

        area_started = time.perf_counter()
        try:
            area_response = await self.http.get(TECHNOLOGY_AREA_URL, headers=PAGE_HEADERS)
            self.technology_area = parse_technology_area(area_response.text, str(area_response.url))
            self.result.metadata["technology_area"] = {
                **self.technology_area,
                "status": area_response.status_code,
                "final_url": str(area_response.url),
            }
        except Exception as exc:
            self.result.errors.append(f"technology_area_failed: {type(exc).__name__}: {exc}")
            self.result.metadata["technology_area"] = {"url": TECHNOLOGY_AREA_URL, "error": str(exc)}
        self.result.timings["technology_area_seconds"] = max(0.0, time.perf_counter() - area_started)

        ai = None
        try:
            ai = await self._ai_count()
            self.result.metadata["ai_listing_count"] = {
                key: ai[key] for key in ("total_jobs", "job_page_number", "page_size", "filters", "refined_query")
            }
        except Exception as exc:
            self.result.warnings.append(f"ai_listing_count_unavailable: {type(exc).__name__}: {exc}")
            self.result.metadata["ai_listing_count"] = {"error": str(exc)}

        listing_started = time.perf_counter()
        try:
            membership = await self._membership()
        except Exception as exc:
            self.result.errors.append(f"membership_failed: {type(exc).__name__}: {exc}")
            self.result.metadata["membership"] = {"url": LISTING_URL, "error": str(exc)}
            self.result.listing_complete = False
            return self.result
        self.result.timings["listing_phase_seconds"] = max(0.0, time.perf_counter() - listing_started)
        self.result.pages = 1
        rows = sorted(membership["rows"], key=lambda row: row["job_id"])
        ids = [row["job_id"] for row in rows]
        ai_total = ai["total_jobs"] if ai else None
        self.result.expected_total = ai_total if ai_total is not None else len(rows)
        if ai_total is not None and ai_total != len(rows):
            self.result.errors.append(f"membership_count_differs_from_advertised_{len(rows)}_of_{ai_total}")
        if membership["issues"]:
            self.result.warnings.extend(membership["issues"][:50])
        if len(rows) >= HYBRID_SIZE:
            self.result.errors.append("membership_hybrid_response_at_size_cap")
        if not rows:
            self.result.errors.append("walmart_listing_empty")
            self.result.listing_complete = False
            return self.result
        self.result.metadata["membership"] = {
            "listing_url": LISTING_URL,
            "endpoint": GRAPHQL_URL,
            "query_id": HYBRID_QUERY_ID,
            "query": LISTING_QUERY_TEXT,
            "filter": LISTING_FILTER,
            "requested_size": HYBRID_SIZE,
            "count": len(rows),
            "advertised_total": ai_total,
            "unique_ids": len(set(ids)),
            "issues": membership["issues"],
            "ids_sha256": sha256("\n".join(sorted(ids)).encode()).hexdigest(),
            "order": "sorted_by_job_id; hybrid search order changes between identical requests",
            "ai_pagination": {
                "page_size": AI_PAGE_SIZE,
                "advertised_pages": ((ai_total + AI_PAGE_SIZE - 1) // AI_PAGE_SIZE) if ai_total else None,
                "used_for_membership": False,
                "reason": "sequential AI pages repeated prior pages and re-ranked; 670 rows / 419 unique vs 661 advertised",
            },
        }
        # Keep this false until every row is added: checkpoints during the add
        # loop call Result.report(), which would otherwise flag a false total
        # mismatch against the not-yet-complete membership.
        self.result.listing_complete = False
        for position, row in enumerate(rows):
            self.add(self._listing_job(row, position))
        if len(self.result.jobs) != len(rows):
            self.result.errors.append(
                f"walmart_job_add_count_mismatch_{len(self.result.jobs)}_of_{len(rows)}"
            )
        self.result.listing_complete = (
            (ai_total is None or len(rows) == ai_total) and len(self.result.jobs) == len(rows)
        )

        await self._hydrate_details(self.result.jobs)

        recheck_started = time.perf_counter()
        try:
            recheck = await self._membership()
            before, after = set(ids), set(recheck["job_ids"])
            added, removed = sorted(after - before), sorted(before - after)
            self.result.metadata["final_membership_recheck"] = {
                "count": len(after),
                "added_ids": added,
                "removed_ids": removed,
                "changed": bool(added or removed),
                "issues": recheck["issues"],
            }
            if added or removed:
                self.result.errors.append("membership_changed_during_collection")
                self.result.listing_complete = False
        except Exception as exc:
            self.result.errors.append(f"final_membership_recheck_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["final_membership_recheck"] = {"error": str(exc)}
        self.result.timings["final_membership_recheck_seconds"] = max(
            0.0, time.perf_counter() - recheck_started
        )

        self.result.metadata["field_coverage"] = {
            "date_posted": sum(bool(job.date_posted) for job in self.result.jobs),
            "salary": sum(job.salary is not None for job in self.result.jobs),
            "locations": sum(bool(job.locations) for job in self.result.jobs),
            "apply_url": sum(bool(job.apply_url) for job in self.result.jobs),
            "requisition_id": sum(bool(job.requisition_id) for job in self.result.jobs),
            "qualifications": sum(job.qualifications is not None for job in self.result.jobs),
            "responsibilities": sum(bool(job.responsibilities) for job in self.result.jobs),
            "description_verified": sum(job.description_verified for job in self.result.jobs),
        }
        self.result.metadata["unavailable_jobs"] = [
            {"id": job.job_id, "url": job.url, "warnings": job.warnings}
            for job in self.result.jobs if job.description_unavailable
        ]
        return self.result
