"""Salesforce's own careers jobs page and the exact jobs feed it renders."""
from __future__ import annotations

from datetime import date
from hashlib import sha256
import math
import re
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://careers.salesforce.com/en/jobs/"
JOBS_PAGE = "https://www.salesforce.com/company/careers/jobs/"
FEED_BASE = "https://a.sfdcstatic.com/digital/xsf/careers/prod"
JOBS_PRIMARY_URL = FEED_BASE + "/jobs_1.json"
JOBS_FULL_URL = FEED_BASE + "/jobs_2.json"
LOCATIONS_MAP_URL = FEED_BASE + "/locations_map.json"
CAREERS_JS_URL = "https://c.salesforce.com/digital/xsf/components/v1-stable/careers.js"
PAGE_SIZE = 20
JOB_ID_RE = re.compile(r"^JR\d+(?:-\d+)?$")
PLACEHOLDER_DESCRIPTIONS = {
    "", "n.a.", "n.a", "na", "n/a", "tbd", "to be determined",
    "description not available", "description not available.",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
RESPONSIBILITY_TOKENS = (
    "responsibilit", "what you'll do", "what you will do", "what you’ll do",
    "what you'll actually be doing", "what you’ll actually be doing",
    "what you'll actually be building", "what you’ll actually be building",
    "what you'll actually", "what you’ll actually", "what you will actually",
    "what you'll be doing", "what you’ll be doing", "your impact", "key duties",
    "primary responsibilities", "day to day", "day-to-day", "the role",
)
QUALIFICATION_TOKENS = (
    "qualif", "requirement", "you're our person", "you’re our person",
    "who you are", "what you'll need", "what you’ll need", "minimum",
    "preferred", "nice-to-have", "even better", "skills", "about you",
)
BUNDLE_MARKERS = {
    "feed_base_template": "https://a.sfdcstatic.com/digital/xsf/careers/${e}",
    "jobs_primary": "jobs_1.json",
    "jobs_full": "jobs_2.json",
    "locations_map": "locations_map.json",
    "jobs_primary_backup": "jobs_1_backup.json",
    "report_entry": "Report_Entry",
    "jobs_page_path": "/company/careers/jobs",
    "detail_route": "/:jobId/:jobSlug",
    "apply_suffix": '/apply',
    "posting_date_sort_field": "External_Job_Posting_Start_Date",
    "page_size_field": "jobsPerPage",
}


def normalize_text(value: str | None) -> str:
    value = re.sub(r"[\u00a0\u2007\u202f]", " ", value or "")
    value = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def normalized_title(value: str | None) -> str:
    return normalize_text(unicodedata.normalize("NFC", value or ""))


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return normalize_text(soup.get_text(" ", strip=True))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def slugify_job_title(title: str | None) -> str:
    """The exact slug expression used by the careers bundle for detail links."""
    value = unicodedata.normalize("NFKD", title or "")
    value = re.sub(r"[\u0300-\u036f]", "", value).lower().strip()
    value = re.sub(r"[^a-z0-9\s-]", "", value)
    value = re.sub(r"\s+", "-", value)
    value = value.strip("-")
    value = re.sub(r"-+", "-", value)
    return value or "job-details"


def job_detail_url(job_id: str, title: str | None) -> str:
    return f"https://www.salesforce.com/company/careers/jobs/{job_id}/{slugify_job_title(title)}/"


def observed_apply_url(external_job_posting_site: str | None) -> str | None:
    """The bundle appends ``/apply`` unless the publisher URL already ends with it."""
    value = (external_job_posting_site or "").strip()
    if not value:
        return None
    value = value.rstrip("/")
    return value if value.lower().endswith("/apply") else value + "/apply"


def parse_iso_date(value) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def parse_official_entry(html: str, response_url: str = JOBS_PAGE) -> dict:
    parts = urlsplit(response_url)
    query = parse_qs(parts.query)
    if (parts.scheme != "https" or parts.netloc != "www.salesforce.com"
            or parts.path.rstrip("/") != "/company/careers/jobs"
            or set(query) - {"bc"}):
        raise ValueError("Salesforce official careers entry redirected away from its inspected jobs page")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None
    careers_scripts = [s.get("src") for s in soup.find_all("script") if s.get("src")
                       and "careers.js" in s.get("src")]
    env = "PROD" if re.search(r'"env"\s*:\s*"PROD"', html) else None
    if title != "Salesforce Jobs | Salesforce":
        raise ValueError(f"Salesforce careers page title changed: {title!r}")
    if canonical_url not in (JOBS_PAGE, JOBS_PAGE + "?bc=OTH"):
        raise ValueError(f"Salesforce careers canonical URL changed: {canonical_url!r}")
    if not careers_scripts:
        raise ValueError("Salesforce careers page no longer loads its observed careers bundle")
    if env != "PROD":
        raise ValueError("Salesforce careers feed environment is no longer the observed PROD feed")
    return {
        "url": response_url,
        "title": title,
        "canonical_url": canonical_url,
        "careers_scripts": careers_scripts,
        "feed_environment": env,
        "static_body_text_chars_before_client_render": len(soup.get_text(" ", strip=True)),
    }


def parse_careers_bundle(js: str) -> dict:
    missing = [name for name, marker in BUNDLE_MARKERS.items() if marker not in js]
    if missing:
        raise ValueError(f"Salesforce careers bundle contract changed; missing: {missing}")
    if "External_Job_Posting_Site" not in js:
        raise ValueError("Salesforce careers bundle no longer reads External_Job_Posting_Site")
    if "Job_Description" not in js:
        raise ValueError("Salesforce careers bundle no longer reads Job_Description")
    page_size = re.search(r"Math\.ceil\([^)]*?/(\d+)\)", js)
    return {
        "markers": BUNDLE_MARKERS,
        "reads_job_description": True,
        "reads_external_posting_site": True,
        "observed_page_size": int(page_size.group(1)) if page_size else PAGE_SIZE,
        "merge_contract": "jobs_1.json rows merged with jobs_2.json by Job_Requisition_Ref_ID; full-feed fields win",
        "sort_contract": "External_Job_Posting_Start_Date descending; browser order matched the sorted feed",
        "apply_contract": "append /apply to External_Job_Posting_Site unless it already ends with /apply",
    }


def parse_feed_document(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"Salesforce {label} feed is not an object")
    timestamp = value.get("Fetch_Timestamp")
    count = value.get("Count")
    total = value.get("Total_Jobs")
    rows = value.get("Report_Entry")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise ValueError(f"Salesforce {label} feed Fetch_Timestamp is missing")
    if not isinstance(count, int) or not isinstance(total, int) or count < 0 or total < 0:
        raise ValueError(f"Salesforce {label} feed Count/Total_Jobs are missing")
    if not isinstance(rows, list):
        raise ValueError(f"Salesforce {label} feed Report_Entry is missing")
    issues = []
    if count != len(rows):
        issues.append(f"salesforce_{label}_count_does_not_match_report_entry_rows")
    if total != count:
        issues.append(f"salesforce_{label}_total_jobs_does_not_match_count")
    return {
        "label": label,
        "fetch_timestamp": timestamp,
        "count": count,
        "total_jobs": total,
        "rows": rows,
        "row_fields": sorted({key for row in rows if isinstance(row, dict) for key in row}),
        "issues": issues,
    }


def parse_locations_map(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Salesforce locations map is not an object")
    countries = value.get("Countries")
    if not isinstance(countries, list):
        raise ValueError("Salesforce locations map Countries is missing")
    return {
        "fetch_timestamp": value.get("Fetch_Timestamp"),
        "total_countries": value.get("Total_Countries"),
        "countries": countries,
        "country_count": len(countries),
    }


def location_values(row: dict) -> list[str]:
    """Exact primary + additional location merge used by the careers bundle."""
    primary = str(row.get("Job_Requisition_Primary_Location") or "").strip()
    additional = [part.strip() for part in str(
        row.get("Job_Requisition_Additional_Locations") or "").split(";") if part.strip()]
    if primary or additional:
        seen, values = set(), []
        for value in ([primary] if primary else []) + additional:
            key = value.lower()
            if key not in seen:
                seen.add(key)
                values.append(value)
        return values
    return [value.strip() for value in (row.get("Locations") or [])
            if isinstance(value, str) and value.strip()]


def block_values(row: dict, key_fragment: str) -> list[dict]:
    keys = sorted(key for key in row if key_fragment in key.lower()
                  and isinstance(row.get(key), str) and row[key].strip())
    return [{"field": key, "text": row[key]} for key in keys]


def pay_transparency_blocks(row: dict) -> list[dict]:
    return block_values(row, "pay_transparency_text")


def end_date_text_blocks(row: dict) -> list[dict]:
    return block_values(row, "job_posting_end_date_text")


def feed_statement_blocks(row: dict) -> list[dict]:
    return block_values(row, "_statement")


def _row_id(row: dict) -> str:
    return str(row.get("Job_Requisition_Ref_ID") or "").strip()


def merge_feed_documents(primary: dict, full: dict) -> dict:
    issues = list(primary.get("issues", [])) + list(full.get("issues", []))
    primary_by: dict[str, dict] = {}
    full_by: dict[str, dict] = {}
    for label, rows, bucket in (("jobs_1", primary["rows"], primary_by),
                                ("jobs_2", full["rows"], full_by)):
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                issues.append(f"salesforce_{label}_row_{index}_is_not_object")
                continue
            identity = _row_id(row)
            if not JOB_ID_RE.fullmatch(identity):
                issues.append(f"salesforce_{label}_row_{index}_has_unexpected_job_id:{identity!r}")
                continue
            if identity in bucket:
                issues.append(f"salesforce_{label}_duplicate_job_id:{identity}")
                continue
            bucket[identity] = row

    merged, order = [], []
    for row in primary["rows"]:
        if not isinstance(row, dict):
            continue
        identity = _row_id(row)
        if identity not in primary_by or primary_by[identity] is not row:
            continue
        full_row = full_by.get(identity)
        if full_row is None:
            issues.append(f"salesforce_full_feed_missing_row:{identity}")
            merged.append(dict(row))
        else:
            merged.append({**row, **full_row})
        order.append(identity)
    for identity, row in full_by.items():
        if identity not in primary_by:
            issues.append(f"salesforce_primary_feed_missing_row:{identity}")
            merged.append(dict(row))
            order.append(identity)

    def sort_key(row):
        return parse_iso_date(row.get("External_Job_Posting_Start_Date")) or date.min

    merged.sort(key=sort_key, reverse=True)
    ids = [_row_id(row) for row in merged]

    expected_total = full["total_jobs"] if isinstance(full.get("total_jobs"), int) else primary["total_jobs"]
    if expected_total != len(merged):
        issues.append("salesforce_feed_total_does_not_match_retained_rows")
    if len(set(ids)) != len(ids):
        issues.append("salesforce_merged_feed_contains_duplicate_job_ids")

    for row in merged:
        identity = _row_id(row)
        if not normalized_title(row.get("Job_Posting_Title")):
            issues.append(f"salesforce_row_missing_title:{identity}")
        if parse_iso_date(row.get("External_Job_Posting_Start_Date")) is None:
            issues.append(f"salesforce_row_missing_posting_date:{identity}")
        if not location_values(row):
            issues.append(f"salesforce_row_has_no_display_location:{identity}")
        if not isinstance(row.get("Job_Description"), str):
            issues.append(f"salesforce_row_missing_description_field:{identity}")
        site = row.get("External_Job_Posting_Site")
        if not isinstance(site, str) or not site.startswith("https://"):
            issues.append(f"salesforce_row_missing_external_posting_site:{identity}")
    return {
        "rows": merged,
        "job_ids": ids,
        "count": len(merged),
        "expected_total": expected_total,
        "total_pages": math.ceil(len(merged) / PAGE_SIZE) if merged else 0,
        "issues": list(dict.fromkeys(issues)),
        "primary_meta": {k: primary[k] for k in ("fetch_timestamp", "count", "total_jobs", "row_fields", "issues")},
        "full_meta": {k: full[k] for k in ("fetch_timestamp", "count", "total_jobs", "row_fields", "issues")},
    }


def _section_from_block(block: Tag) -> tuple[str | None, str]:
    """Return (heading, body_html) for a top-level description block."""
    if block.name in HEADING_TAGS:
        heading = normalized_title(block.get_text(" ", strip=True))
        return (heading, "") if len(heading) <= 160 else (None, str(block))
    heading = None
    if block.name == "p":
        bold = block.find(["b", "strong"])
        if bold is not None:
            bold_text = normalized_title(bold.get_text(" ", strip=True))
            if 2 <= len(bold_text) <= 120:
                before = ""
                for node in bold.previous_elements:
                    if node is block:
                        break
                    if isinstance(node, NavigableString):
                        before += str(node)
                    elif isinstance(node, Tag) and node.name not in {"span", "br"}:
                        break
                if not before.strip() and normalized_title(
                        block.get_text(" ", strip=True)).startswith(bold_text):
                    clone = BeautifulSoup(str(block), "lxml").find(block.name)
                    clone_bold = clone.find(["b", "strong"])
                    clone_bold.extract()
                    for br in clone.find_all("br", limit=1):
                        br.extract()
                    return bold_text, str(clone)
    return heading, str(block)


def description_sections(html: str | None) -> list[dict]:
    soup = BeautifulSoup(f'<div data-salesforce-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-salesforce-body]")
    sections, current = [], None
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    for block in blocks:
        heading, body_html = _section_from_block(block)
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": body_html, "text": text_html(body_html)}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += body_html
    if current is not None:
        sections.append(current)
    for section in sections:
        section["text"] = text_html(section["html"])
    return sections or [{"heading": None, "html": "", "text": ""}]


def responsibility_sections(sections: list[dict]) -> list[dict] | None:
    rows = [section for section in sections if section.get("heading")
            and any(token in section["heading"].lower() for token in RESPONSIBILITY_TOKENS)]
    return rows or None


def qualification_sections(sections: list[dict]) -> list[dict] | None:
    rows = [section for section in sections if section.get("heading")
            and any(token in section["heading"].lower() for token in QUALIFICATION_TOKENS)]
    return rows or None


def salary_value(row: dict, pay_blocks: list[dict]) -> dict | None:
    def number(value):
        try:
            parsed = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        return int(parsed) if parsed.is_integer() else parsed

    minimum, maximum = number(row.get("EU_Min_Salary")), number(row.get("EU_Max_Salary"))
    has_eu_range = bool(minimum or maximum)
    if not has_eu_range and not pay_blocks:
        return None
    value = {}
    if has_eu_range:
        value.update({
            "eu_min": minimum, "eu_max": maximum,
            "eu_currency": row.get("EU_Salary_Currency"),
            "eu_source_fields": ["EU_Min_Salary", "EU_Max_Salary", "EU_Salary_Currency"],
        })
    if pay_blocks:
        value["pay_transparency_blocks"] = pay_blocks
    return value or None


def description_placeholder_state(description_html: str | None) -> tuple[bool, str | None]:
    text = normalized_body_text(description_html)
    if text.lower() in PLACEHOLDER_DESCRIPTIONS:
        return True, "publisher_body_is_placeholder"
    if not text:
        return True, "publisher_body_is_empty"
    return False, None


class Salesforce(Adapter):
    """Read the exact jobs feed loaded by Salesforce's own careers jobs page."""

    VERIFICATION_REVISION = "salesforce.careers-jobs-feed.1"

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        entry = parse_official_entry(response.text, str(response.url))
        entry.update(
            request_url=OFFICIAL_ENTRY,
            final_url=str(response.url),
            redirect_chain=[str(item.url) for item in response.history],
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return entry

    async def _bundle(self):
        started = time.perf_counter()
        response = await self.http.get(CAREERS_JS_URL)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != CAREERS_JS_URL:
            raise ValueError("Salesforce careers bundle redirected away from its observed URL")
        parsed = parse_careers_bundle(response.text)
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _feed(self, url: str, label: str):
        started = time.perf_counter()
        response = await self.http.get(url)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != url:
            raise ValueError(f"Salesforce {label} feed redirected away from its observed URL")
        document = parse_feed_document(response.json(), label)
        return {"document": document, "url": url, "final_url": str(response.url),
                "status": response.status_code, "seconds": seconds,
                "response_sha256": sha256(response.content).hexdigest(),
                "response_bytes": len(response.content)}

    async def _locations(self):
        started = time.perf_counter()
        response = await self.http.get(LOCATIONS_MAP_URL)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != LOCATIONS_MAP_URL:
            raise ValueError("Salesforce locations map redirected away from its observed URL")
        parsed = parse_locations_map(response.json())
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    def _make_job(self, row: dict, primary_row: dict | None):
        identity = _row_id(row)
        title = str(row.get("Job_Posting_Title") or "").strip()
        description_html = row.get("Job_Description") or ""
        description_text = text_html(description_html)
        normalized = normalized_body_text(description_html)
        sections = description_sections(description_html)
        pay_blocks = pay_transparency_blocks(row)
        end_texts = end_date_text_blocks(row)
        statements = feed_statement_blocks(row)
        unavailable, reason = description_placeholder_state(description_html)
        locations = location_values(row)
        start_date = row.get("External_Job_Posting_Start_Date")
        end_date = row.get("External_Job_Posting_End_Date")
        external_site = row.get("External_Job_Posting_Site")
        detail_url = job_detail_url(identity, title)
        apply_url = observed_apply_url(external_site)
        job = self.job(
            {"feed_row": row, "primary_row": primary_row,
             "source": {"primary_url": JOBS_PRIMARY_URL, "full_url": JOBS_FULL_URL,
                        "primary_file": "jobs_1.json", "full_file": "jobs_2.json"}},
            title=title,
            url=detail_url,
            job_id=identity,
            requisition_id=identity,
            apply_url=apply_url,
            description=description_html,
            method="salesforce.careers.feed.jobs_2.Job_Description",
            description_url=JOBS_FULL_URL,
            locations=locations,
            department=str(row.get("Job_Family_Group") or "").strip() or None,
            employment_type=str(row.get("Time_Type") or "").strip() or None,
            workplace_type=None,
            date_posted=start_date if parse_iso_date(start_date) else None,
            date_posted_raw=start_date,
            date_updated=None,
            valid_through=end_date if parse_iso_date(end_date) else None,
            salary=salary_value(row, pay_blocks),
            employer="Salesforce",
            language=None,
            responsibilities=responsibility_sections(sections),
            qualifications=qualification_sections(sections),
        )
        job.description_text = description_text
        job.description_unavailable = unavailable
        job.raw["detail"] = {
            "source_field": "Job_Description",
            "source_file": "jobs_2.json",
            "description_sha256": sha256(description_html.encode()).hexdigest(),
            "description_normalized_chars": len(normalized),
            "description_fnv1a64": fnv1a64(normalized),
            "description_sections": sections,
            "description_available": not unavailable,
            "description_unavailable_reason": reason,
            "dates": {
                "job_posting_start_date": start_date,
                "job_posting_end_date": end_date,
                "date_posted_mapping": "External_Job_Posting_Start_Date" if parse_iso_date(start_date) else None,
                "valid_through_mapping": "External_Job_Posting_End_Date" if parse_iso_date(end_date) else None,
                "date_updated_mapping": None,
            },
            "apply_route": {
                "external_job_posting_site": external_site,
                "site_apply_href_rule": "append /apply unless already present",
                "computed_apply_url": apply_url,
            },
            "employee_type": row.get("Employee_Type"),
            "time_type": row.get("Time_Type"),
            "structured_locations": {
                "primary": row.get("Job_Requisition_Primary_Location"),
                "additional": row.get("Job_Requisition_Additional_Locations"),
                "cities": row.get("Locations"),
                "countries": row.get("Countries"),
                "regions": row.get("Regions"),
            },
            "pay_transparency_blocks": pay_blocks,
            "job_posting_end_date_texts": end_texts,
            "feed_statements": statements,
            "brand_posting_sites": {
                key: value for key, value in row.items()
                if key.endswith("_site_URL") or key.endswith("_Posting_Start_Date") or key.endswith("_Posting_End_Date")
            },
        }
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "canonical_jobs_page": JOBS_PAGE,
            "listing_authority": "The jobs feed loaded by Salesforce's own www.salesforce.com/company/careers/jobs page.",
            "listing_request_shape": {
                "method": "GET",
                "urls": [JOBS_PRIMARY_URL, JOBS_FULL_URL, LOCATIONS_MAP_URL],
                "query_parameters": None,
                "scope": "Salesforce PROD careers feed; no ATS board, aggregator, or parent-company board is queried.",
                "filters_applied": False,
                "source_bundle_url": CAREERS_JS_URL,
            },
            "membership_merge": "jobs_1.json listing rows merged with jobs_2.json by Job_Requisition_Ref_ID, full-feed fields winning; then sorted by External_Job_Posting_Start_Date descending.",
            "detail_route": "https://www.salesforce.com/company/careers/jobs/{Job_Requisition_Ref_ID}/{slug}/",
            "detail_method": "Full employer Job_Description is inline in the exact jobs_2.json feed requested by the official page; representative detail pages are rendered independently.",
            "apply_route": "External_Job_Posting_Site with the site's /apply suffix rule; no ATS listing enumeration.",
        })
        try:
            initial_started = time.perf_counter()
            entry = await self._official_entry()
            bundle = await self._bundle()
            primary = await self._feed(JOBS_PRIMARY_URL, "jobs_1")
            full = await self._feed(JOBS_FULL_URL, "jobs_2")
            initial_seconds = max(0.0, time.perf_counter() - initial_started)
            locations, locations_error = None, None
            try:
                locations = await self._locations()
            except Exception as exc:
                locations_error = f"{type(exc).__name__}: {exc}"
                self.result.warnings.append("salesforce_locations_map_unavailable")
        except Exception as exc:
            self.result.errors.append(f"Salesforce source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        merged = merge_feed_documents(primary["document"], full["document"])
        self.result.expected_total = merged["expected_total"]
        self.result.metadata.update({
            "initial_official_entry": entry,
            "careers_bundle": {k: v for k, v in bundle.items()},
            "initial_primary_feed": {**primary, "document": primary["document"] | {"rows": None}},
            "initial_full_feed": {**full, "document": full["document"] | {"rows": None}},
            "initial_feed_merge": {k: merged[k] for k in ("job_ids", "count", "expected_total", "total_pages", "issues", "primary_meta", "full_meta")},
            "locations_map": locations,
            "locations_map_error": locations_error,
        })
        if merged["issues"]:
            self.result.errors.extend(merged["issues"])

        primary_by = {_row_id(row): row for row in primary["document"]["rows"] if isinstance(row, dict)}
        detail_started = time.perf_counter()
        jobs = [self._make_job(row, primary_by.get(_row_id(row))) for row in merged["rows"]]
        detail_seconds = max(0.0, time.perf_counter() - detail_started)
        self.result.timings["detail_phase_seconds"] = detail_seconds
        self.result.timings["inline_detail_parse_seconds_sum"] = detail_seconds
        self.result.metadata["detail_hydration_mode"] = (
            f"All {len(jobs)} employer descriptions are inline in the exact jobs_2.json feed; "
            "no per-row detail HTTP fetch is needed. Representative detail pages are checked independently."
        )

        final_started = time.perf_counter()
        final_entry = final_primary = final_full = None
        try:
            final_entry = await self._official_entry()
            final_primary = await self._feed(JOBS_PRIMARY_URL, "jobs_1")
            final_full = await self._feed(JOBS_FULL_URL, "jobs_2")
        except Exception as exc:
            self.result.errors.append(f"Salesforce final source recheck failed: {type(exc).__name__}: {exc}")
        final_seconds = max(0.0, time.perf_counter() - final_started)
        self.result.timings["final_recheck_seconds"] = final_seconds

        final_matches = False
        final_merged = None
        if final_entry is not None and final_primary is not None and final_full is not None:
            final_merged = merge_feed_documents(final_primary["document"], final_full["document"])
            initial_by = {_row_id(row): row for row in merged["rows"]}
            final_by = {_row_id(row): row for row in final_merged["rows"]}
            changed_fields = {}
            for identity in merged["job_ids"]:
                if identity in final_by and initial_by[identity] != final_by[identity]:
                    changed_fields[identity] = sorted(
                        key for key in set(initial_by[identity]) | set(final_by[identity])
                        if initial_by[identity].get(key) != final_by[identity].get(key)
                    )
            counts_match = (merged["count"], merged["expected_total"]) == (
                final_merged["count"], final_merged["expected_total"])
            same_ids = merged["job_ids"] == final_merged["job_ids"]
            def entry_identity(value):
                parts = urlsplit(value["final_url"])
                return (parts.scheme, parts.netloc, parts.path.rstrip("/"),
                        value["canonical_url"], value["feed_environment"])

            entry_same = entry_identity(entry) == entry_identity(final_entry)
            final_matches = bool(same_ids and counts_match and not changed_fields and entry_same
                                 and not final_merged["issues"] and not merged["issues"])
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_feed_merge"] = {
                k: final_merged[k] for k in ("job_ids", "count", "expected_total", "total_pages", "issues")
            }
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": merged["job_ids"],
                "final_ids": final_merged["job_ids"],
                "initial_count": merged["count"],
                "final_count": final_merged["count"],
                "initial_expected_total": merged["expected_total"],
                "final_expected_total": final_merged["expected_total"],
                "same_ids_and_order": same_ids,
                "counts_match": counts_match,
                "all_row_fields_match": not changed_fields,
                "changed_fields_by_job_id": changed_fields,
                "new_job_ids": [i for i in final_merged["job_ids"] if i not in merged["job_ids"]],
                "removed_job_ids": [i for i in merged["job_ids"] if i not in final_merged["job_ids"]],
                "official_entry_still_same": entry_same,
                "complete": final_matches,
                "feed_fetch_timestamps": [merged["full_meta"]["fetch_timestamp"],
                                          final_merged["full_meta"]["fetch_timestamp"]],
            }
            if not same_ids or not counts_match:
                self.result.errors.append("salesforce_listing_membership_changed_during_collection")
            if changed_fields:
                self.result.errors.append("salesforce_source_fields_changed_during_collection")
            if not entry_same:
                self.result.errors.append("salesforce_official_entry_changed_during_collection")
            self.result.errors.extend(final_merged["issues"])

        self.result.jobs = jobs
        self.result.pages = 1
        self.result.metadata["job_ids"] = merged["job_ids"]
        self.result.metadata["visible_ui_total"] = merged["expected_total"]
        self.result.metadata["listing_page_size"] = PAGE_SIZE
        self.result.metadata["listing_page_count"] = merged["total_pages"]
        self.result.metadata["current_native_membership_reconciled"] = final_matches
        self.result.timings["initial_source_collection_seconds"] = initial_seconds
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started)
        self.result.listing_complete = bool(
            final_matches and merged["expected_total"] == len(jobs) and len(jobs) > 0)
        if not self.result.listing_complete and not self.result.errors:
            self.result.errors.append("salesforce_listing_not_reconciled")
        return self.result
