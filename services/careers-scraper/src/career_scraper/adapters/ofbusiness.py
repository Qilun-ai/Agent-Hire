"""OfBusiness's own Wix careers site, its unfiltered Jobs CMS listing, and detail pages."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime
from hashlib import sha256
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


# The ledger's https://www.ofbusiness.com/careers currently returns a 410 from
# the corporate site. The corporate homepage's own footer links to this current
# company careers microsite.
LEDGER_URL = "https://www.ofbusiness.com/careers"
CORPORATE_HOME = "https://www.ofbusiness.com/"
OFFICIAL_ENTRY = "https://www.ofbcareers.com/"
LISTING_URL = "https://www.ofbcareers.com/categories"
SITE_ORIGIN = "https://www.ofbcareers.com"
LISTING_QUERY_API = SITE_ORIGIN + "/_api/cloud-data/v2/items/query"
ACCESS_TOKENS_API = SITE_ORIGIN + "/_api/v1/access-tokens"
WIX_DATA_APP_ID = "a20801f2-918d-4ef6-a4c4-83347f6b27a4"
WIX_DATA_TOKEN_APP_ID = "e593b0bd-b783-45b8-97c2-873d42aacaf4"
COLLECTION_ID = "Jobs"
UI_PAGE_SIZE = 4
EMPLOYER = "OFB Tech Pvt. Ltd."
SOURCE_TIMEZONE = ZoneInfo("Asia/Kolkata")
# The shared scraper UA receives HTTP 406 from the corporate site; the public
# careers pages are otherwise ordinary anonymous HTML and were inspected with
# this standard desktop Chrome UA.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec")
SECTION_MAP = (
    ("About the Business", "jobDescription"),
    ("What you will do.", "whatYouWillDo"),
    ("What we are looking for.", "whatWeAreLookingFor"),
    ("What we are offering.", "whatWeAreOffering"),
)


def normalized(value: str | None) -> str:
    value = unicodedata.normalize("NFC", value or "")
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def display_date(value: dict | str | None) -> str | None:
    """Format Wix's CMS creation timestamp exactly as the public cards label it.

    This is retained as display/provenance evidence. It is deliberately not
    copied into ``date_posted``: the underlying field is CMS creation metadata,
    not a separately published posting/deadline date.
    """
    raw = value.get("$date") if isinstance(value, dict) else value
    if not isinstance(raw, str) or not raw:
        return None
    try:
        instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if instant.tzinfo is None:
        return None
    local = instant.astimezone(SOURCE_TIMEZONE)
    return f"{local.day} {MONTHS[local.month - 1]} {local.year}"


def parse_corporate_home(html: str, response_url: str = CORPORATE_HOME) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "www.ofbusiness.com" or parts.path not in ("/", ""):
        raise ValueError("OfBusiness corporate entry redirected away from its inspected homepage")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    links = []
    for anchor in soup.select("a[href]"):
        if urljoin(response_url, anchor.get("href", "")) != OFFICIAL_ENTRY:
            continue
        if normalized(anchor.get_text(" ", strip=True)).lower() == "careers":
            links.append(anchor.get("href"))
    if not links:
        raise ValueError("OfBusiness corporate homepage no longer links to its current careers site")
    return {"url": response_url, "title": title, "careers_links": links}


def parse_careers_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "www.ofbcareers.com" or parts.path not in ("/", ""):
        raise ValueError("OfBusiness careers entry redirected away from its inspected site")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "OfBusiness Careers" not in title:
        raise ValueError("OfBusiness careers site identity changed")
    listing_links = []
    for anchor in soup.select("a[href]"):
        target = urljoin(response_url, anchor.get("href", ""))
        if urlsplit(target).path == "/categories":
            listing_links.append(target)
    if LISTING_URL not in listing_links:
        raise ValueError("OfBusiness careers site no longer exposes its inspected Jobs listing")
    return {
        "url": response_url,
        "title": title,
        "listing_url": LISTING_URL,
        "listing_links": list(dict.fromkeys(listing_links)),
        "contact_links": [a.get("href") for a in soup.select('a[href^="mailto:"]')],
    }


def parse_listing_warmup(html: str, response_url: str = LISTING_URL) -> dict:
    """Read the server-rendered page's own Wix pagination and dataset metadata."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "www.ofbcareers.com" or parts.path != "/categories":
        raise ValueError("OfBusiness listing response came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "OfBusiness Careers" not in title:
        raise ValueError("OfBusiness listing company identity changed")
    script = soup.find("script", id="wix-warmup-data")
    if script is None or not script.string:
        raise ValueError("OfBusiness listing no longer carries its Wix warmup state")
    try:
        warmup = json.loads(script.string)
    except json.JSONDecodeError as exc:
        raise ValueError("OfBusiness listing Wix warmup state is not valid JSON") from exc

    pages = set()
    for update in warmup.get("platform", {}).get("ssrPropsUpdates", []) or []:
        if not isinstance(update, dict):
            continue
        for props in update.values():
            if isinstance(props, dict) and isinstance(props.get("totalPages"), int):
                pages.add(props["totalPages"])
    datasets = {}
    store = warmup.get("appsWarmupData", {}).get("dataBinding", {}).get("dataStore", {})
    for dataset_id, value in (store.get("recordInfosByDatasetId") or {}).items():
        if not isinstance(value, dict):
            continue
        size = value.get("datasetSize") or {}
        if (isinstance(size.get("total"), int) and size.get("loaded") == UI_PAGE_SIZE
                and len(value.get("itemIds") or []) == UI_PAGE_SIZE):
            datasets[dataset_id] = value
    totals = {value["datasetSize"]["total"] for value in datasets.values()}
    if len(pages) != 1 or len(totals) != 1:
        raise ValueError("OfBusiness listing pagination metadata is missing or ambiguous")
    total_pages = next(iter(pages))
    total = next(iter(totals))
    if total_pages < 1 or total < 0:
        raise ValueError("OfBusiness listing pagination metadata is invalid")

    initial_ids = []
    for value in datasets.values():
        if value["datasetSize"]["total"] == total and value["datasetSize"]["loaded"] > 0:
            candidate = value.get("itemIds") or []
            if candidate and not initial_ids:
                initial_ids = [str(item) for item in candidate]
    if not initial_ids:
        raise ValueError("OfBusiness listing warmup state no longer contains its first-page item IDs")
    if len(initial_ids) > UI_PAGE_SIZE:
        raise ValueError("OfBusiness listing first-page row count exceeds the observed page size")

    rendered_rows = len([node for node in soup.select('[role="listitem"]')])
    if rendered_rows != len(initial_ids):
        raise ValueError("OfBusiness rendered first-page cards do not match warmup item IDs")
    return {
        "url": response_url,
        "title": title,
        "total_pages": total_pages,
        "total": total,
        "page_size": UI_PAGE_SIZE,
        "initial_ids": initial_ids,
        "rendered_rows": rendered_rows,
        "pagination_controls": [
            node.get("aria-label")
            for node in soup.select('[aria-label="Pagination"] a[aria-label], [aria-label="Pagination"] button[aria-label]')
        ],
        "filter_labels": [
            node.get_text(" ", strip=True)
            for node in soup.select("label")
            if normalized(node.get_text(" ", strip=True))
        ],
    }


def parse_access_token(value: dict) -> str:
    apps = value.get("apps") if isinstance(value, dict) else None
    app = apps.get(WIX_DATA_TOKEN_APP_ID) if isinstance(apps, dict) else None
    token = app.get("accessToken") if isinstance(app, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("OfBusiness Wix Data access-token response changed")
    return token


def query_payload(offset: int, limit: int) -> dict:
    return {
        "dataCollectionId": COLLECTION_ID,
        "query": {"filter": {}, "paging": {"offset": offset, "limit": limit}, "fields": []},
        "referencedItemOptions": [{"fieldName": "category"}],
        "returnTotalCount": True,
        "environment": "LIVE",
        "appId": WIX_DATA_APP_ID,
    }


def encoded_query(offset: int, limit: int) -> str:
    raw = json.dumps(query_payload(offset, limit), separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_jobs_query(value: dict, offset: int, limit: int) -> dict:
    if not isinstance(value, dict) or value.get("errors"):
        raise ValueError(f"OfBusiness Jobs query returned errors: {value.get('errors') if isinstance(value, dict) else value}")
    items = value.get("dataItems")
    meta = value.get("pagingMetadata")
    if not isinstance(items, list) or not isinstance(meta, dict):
        raise ValueError("OfBusiness Jobs query response is missing dataItems or pagingMetadata")
    count, total, actual_offset = (meta.get(key) for key in ("count", "total", "offset"))
    if not all(isinstance(number, int) and number >= 0 for number in (count, total, actual_offset)):
        raise ValueError("OfBusiness Jobs query pagination metadata is invalid")
    if actual_offset != offset:
        raise ValueError("OfBusiness Jobs query returned a different offset")
    if len(items) > limit:
        raise ValueError("OfBusiness Jobs query returned more rows than requested")
    rows, ids, issues = [], [], []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("data"), dict):
            issues.append("jobs_query_row_missing_data")
            continue
        identity = item.get("id")
        row = item["data"]
        if not isinstance(identity, str) or not identity:
            issues.append("jobs_query_row_missing_id")
            continue
        if str(row.get("_id")) != identity:
            issues.append(f"jobs_query_row_id_mismatch:{identity}")
        if identity in ids:
            issues.append(f"jobs_query_duplicate_id:{identity}")
            continue
        ids.append(identity)
        rows.append(row)
    if len(rows) != len(items):
        issues.append("jobs_query_invalid_rows_present")
    if total < count or count < len(rows):
        issues.append("jobs_query_count_metadata_mismatch")
    expected = min(limit, max(0, total - offset))
    if len(items) != expected:
        issues.append("jobs_query_page_row_count_mismatch")
    return {
        "rows": rows,
        "job_ids": ids,
        "count": count,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_next": meta.get("hasNext"),
        "issues": issues,
    }


def parse_detail_page(html: str, response_url: str) -> dict:
    """Parse the company-published detail page's role sections and header fields."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "www.ofbcareers.com"
            or not parts.path.startswith("/jobs/")):
        raise ValueError("OfBusiness detail URL is outside the inspected /jobs/ route")
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    rich = soup.select('div[data-testid="richTextElement"]')
    texts = [node.get_text(" ", strip=True) for node in rich]
    if len(texts) < 10:
        raise ValueError("OfBusiness detail page is missing its rich-text role fields")

    label_index = {}
    for index, text in enumerate(texts):
        key = normalized(text).rstrip(".").lower()
        for label, _field in SECTION_MAP:
            if key == label.rstrip(".").lower():
                label_index[label] = index
    missing = [label for label, _field in SECTION_MAP if label not in label_index]
    if missing:
        raise ValueError(f"OfBusiness detail page is missing sections: {missing}")
    indices = [label_index[label] for label, _field in SECTION_MAP]
    if indices != sorted(indices) or any(index + 1 >= len(texts) for index in indices):
        raise ValueError("OfBusiness detail page sections are out of order or empty")

    sections = {}
    for label, field in SECTION_MAP:
        index = label_index[label]
        body = rich[index + 1]
        text = normalized(body.get_text(" ", strip=True))
        if not text:
            raise ValueError(f"OfBusiness detail page section is empty: {label}")
        sections[label] = {
            "field": field,
            "html": str(body),
            "text": text,
            "normalized_chars": len(text),
            "fnv1a64": fnv1a64(text),
        }

    about_index = min(indices)
    header = texts[:about_index]
    job_id_index = next((i for i, text in enumerate(header) if normalized(text).rstrip(":").lower() == "job id"), None)
    if job_id_index is None or job_id_index + 1 >= len(header):
        raise ValueError("OfBusiness detail page is missing its visible Job ID")
    before = header[:job_id_index]
    if len(before) < 5:
        raise ValueError("OfBusiness detail page header fields are incomplete")
    title, department, employment_type, experience, location = before[:5]
    requisition_id = normalized(header[job_id_index + 1])
    if not title or not location:
        raise ValueError("OfBusiness detail page title or location is empty")

    form = soup.find("form")
    body_text = normalized(soup.get_text(" ", strip=True))
    canonical = "\n".join(f"{label}\n{sections[label]['text']}" for label, _field in SECTION_MAP)
    return {
        "url": response_url,
        "page_title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "title": normalized(title),
        "department": normalized(department),
        "employment_type": normalized(employment_type),
        "experience": normalized(experience),
        "location": normalized(location),
        "requisition_id": requisition_id,
        "sections": sections,
        "canonical_description_text": canonical,
        "description_normalized_chars": len(normalized(canonical)),
        "description_fnv1a64": fnv1a64(normalized(canonical)),
        "visible_body_normalized_chars": len(body_text),
        "visible_body_fnv1a64": fnv1a64(body_text),
        "application_form_present": form is not None and "Application Form" in body_text,
        "file_inputs": [node.get("accept") for node in soup.select('input[type="file"]')],
    }


def canonical_description_html(row: dict) -> str:
    blocks = []
    for heading, field in SECTION_MAP:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            blocks.append(f"<h3>{heading}</h3>\n{value}")
    return "\n".join(blocks)


def section_values(row: dict) -> list[dict]:
    values = []
    for heading, field in SECTION_MAP:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            values.append({"heading": heading, "field": field, "html": value, "text": text_html(value)})
    return values


def detail_matches_row(detail: dict, row: dict) -> bool:
    return all((
        normalized(detail.get("title")) == normalized(row.get("jobTitle")),
        normalized(detail.get("location")) == normalized(row.get("location")),
        normalized(detail.get("experience")) == normalized(row.get("experienceRequiredRangeyrs")),
        normalized(detail.get("requisition_id")) == normalized(row.get("empId")),
    ))


def detail_sections_match_row(detail: dict, row: dict) -> bool:
    for heading, field in SECTION_MAP:
        page_text = normalized((detail.get("sections", {}).get(heading) or {}).get("text"))
        record_text = normalized(text_html(row.get(field) if isinstance(row.get(field), str) else ""))
        if page_text != record_text:
            return False
    return True


class OfBusiness(Adapter):
    """Collect only the unpublished-filter Jobs collection behind OfBusiness's own listing."""

    VERIFICATION_REVISION = "ofbusiness.ofbcareers.wix-jobs-collection.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._access_token = None

    async def _query(self, token: str, offset: int, limit: int) -> tuple[dict, float]:
        started = time.perf_counter()
        response = await self.http.request(
            "GET", LISTING_QUERY_API,
            params={".r": encoded_query(offset, limit)},
            headers={
                **BROWSER_HEADERS,
                "Authorization": token,
                "x-wix-brand": "wix",
                "Accept": "application/json, text/plain, */*",
                "Referer": LISTING_URL,
            },
        )
        seconds = max(0.0, time.perf_counter() - started)
        path = urlsplit(str(response.url)).path
        if path != "/_api/cloud-data/v2/items/query":
            raise ValueError("OfBusiness Jobs query redirected away from its company listing route")
        return parse_jobs_query(response.json(), offset, limit), seconds

    async def _details(self, urls: list[str]) -> dict[str, dict]:
        sem = asyncio.Semaphore(self.options.detail_concurrency)
        started = time.perf_counter()

        async def one(url):
            async with sem:
                try:
                    response = await self.http.get(url, headers=BROWSER_HEADERS)
                    return url, parse_detail_page(response.text, str(response.url)), None
                except Exception as exc:
                    return url, None, f"{type(exc).__name__}: {exc}"

        try:
            pairs = await asyncio.gather(*(one(url) for url in urls))
        finally:
            self.result.timings["detail_phase_seconds"] = max(0.0, time.perf_counter() - started)
        details, failures = {}, {}
        for url, detail, error in pairs:
            if detail is not None:
                details[url] = detail
            else:
                failures[url] = error or "unknown detail error"
        return {"details": details, "failures": failures}

    def _make_job(self, row: dict, detail: dict | None, *, shared: dict | None,
                  api_sha: str, page_number: int, page_offset: int):
        identity = str(row["_id"])
        link = row.get("link-jobs-jobTitle")
        url = urljoin(SITE_ORIGIN, link) if isinstance(link, str) and link else None
        category = row.get("category") if isinstance(row.get("category"), dict) else {}
        sections = section_values(row)
        description_html = canonical_description_html(row)
        display = display_date(row.get("_createdDate"))
        created = (row.get("_createdDate") or {}).get("$date") if isinstance(row.get("_createdDate"), dict) else None
        updated = (row.get("_updatedDate") or {}).get("$date") if isinstance(row.get("_updatedDate"), dict) else None
        if detail is not None:
            method = "ofbcareers.detail_page+wix_data.jobs_collection"
            description_url = url
        else:
            method = "ofbcareers.wix_data.jobs_collection"
            description_url = LISTING_QUERY_API
        job = self.job(
            {
                "listing_record": row,
                "api_item_id": identity,
                "listing_page": page_number,
                "listing_offset": page_offset,
                "api_response_sha256": api_sha,
                "display_posting_date": {
                    "label": "Posting",
                    "display": display,
                    "source_field": "_createdDate",
                    "source_value": created,
                    "certified_date_posted": False,
                    "note": "The public card labels this Wix CMS creation timestamp as Posting; it is not copied to date_posted because the underlying field is creation metadata, not a separately published posting date.",
                },
                "collection_dates": {"created": created, "updated": updated},
                "job_code": row.get("jobCode"),
                "shared_detail_url": shared,
                "detail_page": detail,
            },
            title=normalized(row.get("jobTitle")),
            url=url,
            job_id=identity,
            requisition_id=normalized(row.get("empId")) or None,
            apply_url=url,
            description=description_html,
            method=method,
            description_url=description_url,
            locations=[row["location"]] if row.get("location") else [],
            department=category.get("category"),
            team=None,
            employment_type=row.get("jobType"),
            workplace_type=None,
            date_posted=None,
            date_posted_raw={
                "display": display,
                "source_field": "_createdDate",
                "source_value": created,
                "label": "Posting",
                "certified_date_posted": False,
            },
            date_updated=None,
            valid_through=None,
            salary=None,
            qualifications=[section for section in sections
                             if section["field"] == "whatWeAreLookingFor"] or None,
            responsibilities=[section for section in sections
                               if section["field"] == "whatYouWillDo"] or None,
            experience=row.get("experienceRequiredRangeyrs"),
            education=None,
            employer=EMPLOYER,
            language="en",
        )
        job.description_text = text_html(description_html)
        if shared:
            job.warnings.append("shared_detail_url_resolves_to_another_record")
        if detail is None and not shared:
            job.warnings.append("detail_fetch_failed")
        elif detail is not None and not detail_matches_row(detail, row):
            job.warnings.append("detail_page_resolves_to_another_record")
        elif detail is not None and not detail_sections_match_row(detail, row):
            job.warnings.append("detail_page_section_mismatch")
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.http.client.headers.update(BROWSER_HEADERS)
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "ledger_url": LEDGER_URL,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_authority": "The Jobs collection on OfBusiness's own Wix careers microsite, linked from the corporate homepage footer.",
            "listing_membership_request": {
                "method": "GET",
                "path": "/_api/cloud-data/v2/items/query",
                "query_parameter": ".r",
                "decoded_shape": query_payload(0, UI_PAGE_SIZE),
                "filters_applied": False,
                "scope": "Jobs collection only; no ATS board or parent-company board is queried.",
                "ui_page_size": UI_PAGE_SIZE,
            },
            "detail_method": "Wix Data Jobs records carry all four employer-authored sections; every unique company detail URL is fetched and its visible sections are compared.",
        })
        try:
            corporate = await self.http.get(CORPORATE_HOME)
            corporate_entry = parse_corporate_home(corporate.text, str(corporate.url))
            entry = await self.http.get(OFFICIAL_ENTRY)
            entry_parsed = parse_careers_entry(entry.text, str(entry.url))
            listing = await self.http.get(LISTING_URL)
            listing_parsed = parse_listing_warmup(listing.text, str(listing.url))
        except Exception as exc:
            self.result.errors.append(f"official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.metadata.update({
            "corporate_home": {"url": str(corporate.url), "status": corporate.status_code,
                                "title": corporate_entry["title"], "careers_links": corporate_entry["careers_links"]},
            "careers_entry": entry_parsed,
            "listing": listing_parsed,
            "ledger_status": None,
        })

        try:
            # The ledger path is retained as a failure/redirect observation, but
            # the current official link discovered above is authoritative.
            try:
                ledger = await self.http.client.get(LEDGER_URL, follow_redirects=True)
                self.result.metadata["ledger_status"] = ledger.status_code
                self.result.metadata["ledger_final_url"] = str(ledger.url)
            except Exception as exc:
                self.result.metadata["ledger_status"] = "fetch_failed"
                self.result.metadata["ledger_error"] = f"{type(exc).__name__}: {exc}"

            token_response = await self.http.client.get(
                ACCESS_TOKENS_API,
                headers={"Accept": "application/json, text/plain, */*", "x-wix-brand": "wix"},
            )
            token_response.raise_for_status()
            token = parse_access_token(token_response.json())
            self._access_token = token
            self.result.metadata["access_token"] = {
                "app_id": WIX_DATA_TOKEN_APP_ID,
                "status": token_response.status_code,
                "response_sha256": sha256(token_response.content).hexdigest(),
                "body_not_retained": True,
            }

            full, full_seconds = await self._query(token, 0, 100)
            api_sha = sha256(json.dumps(full, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            self.result.metadata["full_query"] = {
                "offset": 0, "limit": 100, "seconds": full_seconds,
                "count": full["count"], "total": full["total"], "rows": len(full["rows"]),
                "issues": full["issues"], "api_sha256": api_sha,
            }

            page_rows, page_ids, page_issues, page_records = [], [], [], []
            for page_number in range(1, listing_parsed["total_pages"] + 1):
                if not self.page_guard():
                    page_issues.append("page_budget_exhausted")
                    break
                offset = (page_number - 1) * UI_PAGE_SIZE
                page_value, seconds = await self._query(token, offset, UI_PAGE_SIZE)
                page_issues.extend(page_value["issues"])
                page_rows.extend(page_value["rows"])
                page_ids.extend(page_value["job_ids"])
                page_records.append({
                    "page": page_number, "offset": offset, "count": page_value["count"],
                    "total": page_value["total"], "job_ids": page_value["job_ids"],
                    "seconds": seconds,
                })
            expected_ids = full["job_ids"]
            pages_match = (
                len(page_records) == listing_parsed["total_pages"]
                and page_ids == expected_ids
                and page_rows == full["rows"]
                and all(row["count"] == len(row["job_ids"]) for row in page_records)
                and page_records[0]["job_ids"] == listing_parsed["initial_ids"]
            )
            totals_match = (
                full["total"] == listing_parsed["total"] == len(full["rows"]) == len(page_rows)
                and full["count"] == len(full["rows"])
            )
            if not pages_match:
                self.result.errors.append("listing_page_membership_mismatch")
            if not totals_match:
                self.result.errors.append("listing_total_mismatch")
            if full["issues"] or page_issues:
                self.result.errors.extend(list(dict.fromkeys(full["issues"] + page_issues)))

            empty_rows = [row for row in full["rows"]
                          if not row.get("jobTitle") or not row.get("link-jobs-jobTitle")]
            valid_rows = [row for row in full["rows"] if row not in empty_rows]
            url_groups: dict[str, list[dict]] = {}
            for row in valid_rows:
                link = row.get("link-jobs-jobTitle")
                url = urljoin(SITE_ORIGIN, link) if isinstance(link, str) else ""
                url_groups.setdefault(url, []).append(row)
            duplicate_groups = {
                url: [str(row["_id"]) for row in rows]
                for url, rows in url_groups.items() if len(rows) > 1
            }
            self.result.timings["source_listing_seconds"] = max(0.0, time.perf_counter() - source_started)
            detail_result = await self._details(sorted(url_groups))
            details, detail_failures = detail_result["details"], detail_result["failures"]

            page_number_by_id = {}
            for page in page_records:
                for identity in page["job_ids"]:
                    page_number_by_id[identity] = page["page"]
            for row in valid_rows:
                identity = str(row["_id"])
                link = row.get("link-jobs-jobTitle")
                url = urljoin(SITE_ORIGIN, link)
                group = url_groups[url]
                primary = group[0]
                shared = None
                if primary["_id"] != identity:
                    shared = {"primary_id": str(primary["_id"]), "url": url,
                              "primary_title": primary.get("jobTitle"),
                              "primary_location": primary.get("location")}
                detail = details.get(url)
                if detail is not None and not detail_matches_row(detail, row):
                    detail = None
                job = self._make_job(
                    row, detail, shared=shared, api_sha=api_sha,
                    page_number=page_number_by_id.get(identity),
                    page_offset=((page_number_by_id.get(identity) or 1) - 1) * UI_PAGE_SIZE,
                )
                self.add(job)

            self.result.expected_total = len(valid_rows)
            self.result.listing_complete = bool(pages_match and totals_match and not self.result.errors)
            self.result.metadata["membership_reconciliation"] = {
                "complete": bool(pages_match and totals_match and not self.result.errors),
                "warmup_total": listing_parsed["total"],
                "warmup_total_pages": listing_parsed["total_pages"],
                "warmup_initial_ids": listing_parsed["initial_ids"],
                "full_query_total": full["total"],
                "full_query_count": full["count"],
                "full_query_rows": len(full["rows"]),
                "full_query_ids": full["job_ids"],
                "ui_page_count": len(page_records),
                "ui_page_size": UI_PAGE_SIZE,
                "ui_page_ids": page_ids,
                "ui_page_ids_match_full_query": page_ids == expected_ids,
                "ui_rows_match_full_query": page_rows == full["rows"],
                "first_page_ids_match_warmup": page_records[0]["job_ids"] if page_records else [],
                "empty_cms_record_ids": [str(row["_id"]) for row in empty_rows],
                "empty_cms_records": len(empty_rows),
                "roles": len(valid_rows),
                "unique_detail_urls": len(url_groups),
                "duplicate_detail_url_groups": duplicate_groups,
                "detail_urls_requested": len(url_groups),
                "detail_pages_fetched": len(details),
                "detail_failures": detail_failures,
                "pages": page_records,
            }
            self.result.metadata["detail_audit"] = {
                "unique_urls": len(url_groups),
                "fetched": len(details),
                "failed": len(detail_failures),
                "matched_current_record": sum(
                    1 for row in valid_rows
                    if details.get(urljoin(SITE_ORIGIN, row.get("link-jobs-jobTitle") or "")) is not None
                    and detail_matches_row(details[urljoin(SITE_ORIGIN, row.get("link-jobs-jobTitle") or "")], row)
                ),
                "shared_secondary_records": sum(len(group) - 1 for group in url_groups.values() if len(group) > 1),
                "section_mismatches": sum(
                    1 for row in valid_rows
                    if (detail := details.get(urljoin(SITE_ORIGIN, row.get("link-jobs-jobTitle") or ""))) is not None
                    and detail_matches_row(detail, row) and not detail_sections_match_row(detail, row)
                ),
            }
            if detail_failures:
                self.result.warnings.append("some_detail_pages_failed")
            if empty_rows:
                self.result.warnings.append("listing_contains_empty_cms_record")
            if duplicate_groups:
                self.result.warnings.append("shared_detail_urls")
        except Exception as exc:
            self.result.errors.append(f"{type(exc).__name__}: {exc}")
            self.result.listing_complete = False

        self.result.timings["source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
