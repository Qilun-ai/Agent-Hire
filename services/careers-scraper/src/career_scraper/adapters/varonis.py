"""Varonis' own careers site (careers.varonis.com) and the Jobvite pages it embeds.

The journey observed on 2026-11-08 is:

    https://www.varonis.com/company/careers          (official marketing entry)
      -> https://careers.varonis.com/               (Varonis-branded Angular careersite)
           GET /api/getRequisitions                 (unfiltered company listing, 74 rows)
           GET /api/getSearchCriteria               (country/location/department filters)
      -> https://jobs.jobvite.com/varonis/job/<id>  (exact job page loaded in the site iframe)

The public Jobvite feed for the same company id (qTjaVfw1) is used only to
retain the board-published job date and requisition id, and to cross-check the
description body; membership always comes from Varonis' own listing API.
"""
from __future__ import annotations

from datetime import datetime, timezone
import io
import re
import time
from urllib.parse import urlsplit

import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import Result


OFFICIAL_ENTRY = "https://www.varonis.com/company/careers"
CAREERS_SITE = "https://careers.varonis.com/"
LISTING_API = "https://careers.varonis.com/api/getRequisitions"
FILTER_API = "https://careers.varonis.com/api/getSearchCriteria"
JOBVITE_JOB = "https://jobs.jobvite.com/varonis/job/"
JOBVITE_ORIGIN = "https://jobs.jobvite.com"
FEED_URL = "https://app.jobvite.com/CompanyJobs/Xml.aspx?c=qTjaVfw1"
COMPANY_ID = "qTjaVfw1"
CAREER_SITE_URI = "varonis"
EMPLOYER = "Varonis Systems, Inc."
EXPECTED_ENTRY_HOST = "www.varonis.com"
ENTRY_PATH = "/company/careers"
MAIN_BUNDLE = re.compile(r"(main\.[0-9a-f]+\.js)$")
JOB_PATH = re.compile(r"^/varonis/job/([A-Za-z0-9]+)$")


def normalize_text(value: str | None) -> str:
    """Collapse rendered whitespace without changing visible characters."""
    return re.sub(r"\s+", " ", value or "").strip()


def whitespace_stripped(value: str | None) -> str:
    """Whitespace-insensitive form used to compare get_text() with innerText.

    BeautifulSoup's ``get_text(" ")`` inserts a space between adjacent block
    nodes while the browser's ``innerText`` may not, so the exact-character form
    is compared after dropping every whitespace character.  No other character
    is removed, changed or case-folded.
    """
    return re.sub(r"\s+", "", value or "")


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return normalize_text(value)


def body_fingerprint(html: str | None) -> dict:
    text = normalized_body_text(html)
    stripped = whitespace_stripped(text)
    return {
        "normalized_chars": len(text),
        "normalized_fnv1a64": fnv1a64(text),
        "whitespace_stripped_chars": len(stripped),
        "whitespace_stripped_fnv1a64": fnv1a64(stripped),
    }


def _preloaded_data(html: str) -> dict:
    """Read the careersite ``preloadedData`` constant embedded in a job page."""
    match = re.search(
        r"angular\.module\('preloadedData'[^)]*\)\.constant\('preloadedData',\s*\{(.*?)\}\)\.constant\('i18n'",
        html,
        re.S,
    )
    if not match:
        return {}
    data = {}
    for key, value in re.findall(r"(\w+):\s*'([^']*)'", match.group(1)):
        data[key] = value
    return data


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != EXPECTED_ENTRY_HOST
        or parsed.path.rstrip("/") != ENTRY_PATH
    ):
        raise ValueError("Varonis official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    links = [
        anchor.get("href", "")
        for anchor in soup.select("a[href]")
        if "careers.varonis.com" in anchor.get("href", "")
    ]
    cta_labels = sorted({
        normalize_text(anchor.get_text(" ", strip=True))
        for anchor in soup.select('a[href*="careers.varonis.com"]')
    })
    if not links:
        raise ValueError("Varonis official careers page no longer links to its careers site")
    if not any(href.rstrip("/") == CAREERS_SITE.rstrip("/") for href in links):
        raise ValueError("Varonis official careers page no longer links to https://careers.varonis.com/")
    if "Careers" not in title:
        raise ValueError("Varonis official careers page title changed")
    return {
        "url": response_url,
        "title": title,
        "careers_site_url": CAREERS_SITE,
        "link_count": len(links),
        "link_labels": cta_labels,
        "company_hiring_contact": [
            anchor.get("href")
            for anchor in soup.select('a[href^="mailto:"]')
            if "career" in anchor.get("href", "").lower()
        ],
    }


def parse_careers_site(html: str, response_url: str = CAREERS_SITE) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "careers.varonis.com" or parsed.path not in ("", "/"):
        raise ValueError("Varonis careers site response came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    author = soup.select_one('meta[name="author"]')
    description = soup.select_one('meta[name="description"]')
    base = soup.select_one("base[href]")
    scripts = [script.get("src", "") for script in soup.select("script[src]")]
    bundle = next((src for src in scripts if MAIN_BUNDLE.search(src)), None)
    if title != "Varonis Careers":
        raise ValueError(f"Varonis careers site title changed: {title!r}")
    if author is None or author.get("content") != "Varonis":
        raise ValueError("Varonis careers site author meta changed")
    if base is None or base.get("href") != "/":
        raise ValueError("Varonis careers site base href changed")
    if bundle is None:
        raise ValueError("Varonis careers site no longer loads its Angular bundle")
    return {
        "url": response_url,
        "title": title,
        "author": author.get("content"),
        "description": description.get("content") if description else None,
        "base_href": base.get("href"),
        "bundle_url": CAREERS_SITE + bundle.lstrip("./"),
        "scripts": [src for src in scripts if src.startswith(("main.", "runtime.", "polyfills."))],
        "app_root_present": "<app-root" in html,
    }


def parse_careers_bundle(js: str, response_url: str) -> dict:
    """Extract the careersite's own runtime configuration from its bundle."""
    config = {}
    for key in ("baseHost", "careerSiteUri", "limitRequisitionsDefault", "iframeJsFileUri", "jobviteDomainUri"):
        match = re.search(rf'{key}:\s*"([^"]*)"', js)
        if match:
            config[key] = match.group(1)
    match = re.search(r'baseHost:"([^"]*)"', js)
    if match:
        config.setdefault("baseHost", match.group(1))
    config["numbers"] = {}
    limit = re.search(r"limitRequisitionsDefault:(\d+)", js)
    if limit:
        config["numbers"]["limitRequisitionsDefault"] = int(limit.group(1))
    config["listing_api_path"] = "/api/getRequisitions" if 'get("/api/getRequisitions")' in js else None
    config["filter_api_path"] = "/api/getSearchCriteria" if 'get("/api/getSearchCriteria")' in js else None
    if config.get("careerSiteUri") != CAREER_SITE_URI:
        raise ValueError("Varonis careers bundle careerSiteUri changed")
    if config.get("listing_api_path") != "/api/getRequisitions":
        raise ValueError("Varonis careers bundle no longer requests the inspected listing endpoint")
    if "careersite-function-prod.azurewebsites.net" not in config.get("baseHost", ""):
        raise ValueError("Varonis careers bundle API host changed")
    if "iframe" not in str(config.get("iframeJsFileUri", "")):
        raise ValueError("Varonis careers bundle no longer embeds Jobvite detail iframes")
    return {"url": response_url, **config}


def parse_requisitions(payload: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Varonis listing API payload no longer carries a data list")
    rows, issues, seen = [], [], set()
    for raw in payload["data"]:
        identity = str(raw.get("eId") or "").strip()
        if not identity:
            issues.append("listing_row_without_eId")
            continue
        if identity in seen:
            issues.append(f"duplicate_listing_eId:{identity}")
            continue
        seen.add(identity)
        locations = []
        for location in raw.get("jobLocations") or []:
            locations.append({
                "location_eid": location.get("eId"),
                "name": location.get("name"),
                "city": location.get("city"),
                "state": location.get("state"),
                "country": location.get("country"),
                "postal_code": location.get("postalCode"),
                "address": location.get("address"),
                "address2": location.get("address2"),
                "detail_url": location.get("jobDetailsUrl"),
                "apply_url": location.get("applyUrl"),
            })
        if raw.get("locationsCount") != len(locations):
            issues.append(f"location_count_mismatch:{identity}:{raw.get('locationsCount')}!={len(locations)}")
        if not raw.get("title"):
            issues.append(f"listing_row_without_title:{identity}")
        rows.append({
            "id": identity,
            "title": normalize_text(raw.get("title")),
            "department": raw.get("department"),
            "sub_department": raw.get("subDepartment"),
            "job_classification": raw.get("jobClassification"),
            "locations_count": raw.get("locationsCount"),
            "primary_city": raw.get("locationCity"),
            "primary_country": raw.get("locationCountry"),
            "locations": locations,
            "raw": raw,
        })
    if not rows:
        raise ValueError("Varonis company listing exposed no requisitions")
    departments: dict[str, int] = {}
    for row in rows:
        departments[row["department"]] = departments.get(row["department"], 0) + 1
    return {
        "rows": rows,
        "count": len(rows),
        "ids": [row["id"] for row in rows],
        "departments": departments,
        "locations_total": sum(len(row["locations"]) for row in rows),
        "multi_location_rows": sum(1 for row in rows if len(row["locations"]) > 1),
        "issues": issues,
    }


def parse_search_criteria(payload: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("filters"), list):
        raise ValueError("Varonis filter criteria payload no longer carries filters")
    filters = [
        {"title": item.get("title"), "values": list(item.get("values") or [])}
        for item in payload["filters"]
    ]
    if not filters:
        raise ValueError("Varonis filter criteria payload is empty")
    dependency = payload.get("filterDependencyDescription") or []
    return {
        "filters": filters,
        "titles": [item["title"] for item in filters],
        "values": {item["title"]: len(item["values"]) for item in filters},
        "dependency_rows": len(dependency),
        "applied": [],
    }


def parse_feed_date(value: str | None) -> str | None:
    """Jobvite's XML `<date>` is published as MM/DD/YYYY (e.g. 9/28/2026)."""
    value = (value or "").strip()
    if not value:
        return None
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
    if not match:
        return None
    month, day, year = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc).date().isoformat()
    except ValueError:
        return None


def parse_feed(xml_bytes: bytes, response_url: str = FEED_URL) -> dict:
    parsed = urlsplit(response_url)
    if parsed.netloc != "app.jobvite.com" or parsed.path != "/CompanyJobs/Xml.aspx":
        raise ValueError("Varonis Jobvite feed response came from an unexpected route")
    if f"c={COMPANY_ID}" not in response_url:
        raise ValueError("Varonis Jobvite feed company id changed")
    entries, base_ids, variants, issues = {}, [], [], []
    department_tag = "department_x0020__x0028_business_x0020_sub-unit_x0029_"
    for _, node in ET.iterparse(io.BytesIO(xml_bytes), events=("end",)):
        if node.tag != "job":
            continue
        fields = {child.tag: (child.text or "") for child in node}
        # The rendered body is stored once as description_html; the feed also
        # carries an internal hiring-manager name that is not a job property.
        raw_fields = {
            key: value for key, value in fields.items()
            if key not in ("description", "hiring_x0020_manager")
        }
        identity = fields.get("id", "").strip()
        parent = fields.get("parentId", "").strip() or None
        record = {
            "id": identity,
            "parent_id": parent,
            "external_job_posting_id": fields.get("externalJobPostingId"),
            "title": normalize_text(fields.get("title")),
            "requisition_id": fields.get("requisitionid"),
            "category": fields.get("category"),
            "job_type": fields.get("jobtype"),
            "location": fields.get("location"),
            "date_raw": fields.get("date"),
            "date_posted": parse_feed_date(fields.get("date")),
            "detail_url": (fields.get("detail-url") or "").strip() or None,
            "apply_url": (fields.get("apply-url") or "").strip() or None,
            "department_business_subunit": fields.get(department_tag),
            "description_html": fields.get("description") or "",
            "raw_fields": raw_fields,
        }
        record["description_sha256"] = fnv1a64(record["description_html"])
        node.clear()
        if not identity:
            issues.append("feed_entry_without_id")
            continue
        if identity in entries:
            issues.append(f"duplicate_feed_id:{identity}")
            continue
        if parent:
            variants.append(identity)
        else:
            base_ids.append(identity)
        entries[identity] = record
    if not base_ids:
        raise ValueError("Varonis Jobvite feed carried no base requisition ids")
    return {
        "url": response_url,
        "company_id": COMPANY_ID,
        "entries": entries,
        "base_ids": base_ids,
        "variant_ids": variants,
        "base_count": len(base_ids),
        "variant_count": len(variants),
        "issues": issues,
    }


def _meta_parts(meta: Tag | None) -> list[str]:
    """Split the job-page meta paragraph on Jobvite's inline separator spans."""
    if meta is None:
        return []
    parts, current = [], []
    for child in meta.children:
        classes = child.get("class") or [] if isinstance(child, Tag) else []
        if isinstance(child, Tag) and "jv-inline-separator" in classes:
            value = _clean_meta(" ".join(current))
            if value:
                parts.append(value)
            current = []
        else:
            current.append(child.get_text(" ", strip=True) if isinstance(child, Tag) else str(child))
    value = _clean_meta(" ".join(current))
    if value:
        parts.append(value)
    return parts


def _clean_meta(value: str) -> str:
    value = re.sub(r"\s+([,;:!?])", r"\1", value or "")
    return normalize_text(value)


def parse_detail(html: str, response_url: str, requested_id: str) -> dict:
    parsed = urlsplit(response_url)
    if parsed.netloc != "jobs.jobvite.com":
        raise ValueError(f"Varonis job detail left jobs.jobvite.com: {response_url}")
    if "/careers/varonis/jobs" in parsed.path:
        raise ValueError(f"Varonis job detail {requested_id} no longer exists (Jobvite error redirect)")
    match = JOB_PATH.fullmatch(parsed.path)
    if match is None:
        raise ValueError(f"Varonis job detail path changed: {parsed.path}")
    soup = BeautifulSoup(html, "lxml")
    preloaded = _preloaded_data(html)
    header = soup.select_one("article.jv-page-body h2.jv-header")
    meta = soup.select_one("article.jv-page-body p.jv-job-detail-meta")
    container = soup.select_one("div.jv-job-detail-description")
    if header is None or container is None:
        raise ValueError(f"Varonis job detail {requested_id} lost its header or description container")
    copy = BeautifulSoup(str(container), "lxml").select_one("div.jv-job-detail-description")
    heading = copy.find("h3")
    if heading is not None and normalize_text(heading.get_text(" ", strip=True)).lower() == "description":
        heading.decompose()
    description_html = "".join(str(child) for child in copy.contents).strip()
    description_text = normalized_body_text(description_html)
    if len(description_text) < 100:
        raise ValueError(f"Varonis job detail {requested_id} description is empty")
    apply_links = [
        anchor.get("href")
        for anchor in soup.select("a.jv-button-apply[href]")
    ]
    apply_url = None
    for href in apply_links:
        absolute = href if href.startswith("http") else JOBVITE_ORIGIN + href
        if f"/job/{match.group(1)}/apply" in absolute:
            apply_url = absolute.split("?")[0]
            break
    if apply_url is None and apply_links:
        absolute = apply_links[0] if apply_links[0].startswith("http") else JOBVITE_ORIGIN + apply_links[0]
        apply_url = absolute.split("?")[0]
    meta_lines = _meta_parts(meta)
    location_lines = meta_lines[1:] if meta_lines else []
    fingerprint = body_fingerprint(description_html)
    return {
        "url": response_url,
        "canonical_url": f"{JOBVITE_JOB}{match.group(1)}",
        "requested_id": requested_id,
        "job_id": preloaded.get("jobId") or match.group(1),
        "title": normalize_text(header.get_text(" ", strip=True)),
        "meta_department": meta_lines[0] if meta_lines else None,
        "meta_locations": location_lines,
        "job_req_id": preloaded.get("jobReqId"),
        "job_category_name": preloaded.get("jobCategoryName"),
        "job_category_id": preloaded.get("jobCategoryId"),
        "job_location_id": preloaded.get("jobLocationId"),
        "company_eid": preloaded.get("companyEId"),
        "careersite_name": preloaded.get("careersiteName"),
        "base_url": preloaded.get("baseUrl"),
        "apply_url": apply_url,
        "description_html": description_html,
        "description_text": description_text,
        "description_fingerprint": fingerprint,
        "section_headings": _section_headings(description_html),
    }


def _section_headings(description_html: str) -> list[str]:
    soup = BeautifulSoup(description_html or "", "lxml")
    headings = []
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "strong", "b", "p"]):
        text = normalize_text(node.get_text(" ", strip=True))
        if 3 <= len(text) <= 80 and re.search(r"(?:role|responsib|requirement|qualif|about|what you|who you|duties|benefit|compensation)", text, re.I):
            if text.endswith(":") or len(node.find_all(recursive=False)) == 0:
                headings.append(text.rstrip(":"))
    return list(dict.fromkeys(headings))[:12]


def detail_page_reference(
    html: str, listing_row: dict, response_url: str, feed_entry: dict | None = None
) -> dict:
    """Parse one exact job page and compare it with its listing row and feed entry."""
    parsed = parse_detail(html, response_url, listing_row["id"])
    expected_titles = {normalize_text(listing_row["title"]).lower(), normalize_text(parsed["title"]).lower()}
    matches_feed = None
    if feed_entry and feed_entry.get("description_html"):
        matches_feed = (
            body_fingerprint(feed_entry["description_html"])["whitespace_stripped_fnv1a64"]
            == parsed["description_fingerprint"]["whitespace_stripped_fnv1a64"]
        )
    return {
        **parsed,
        "listing_id": listing_row["id"],
        "title_matches_listing": len(expected_titles) == 1,
        "locations_match_listing": len(parsed["meta_locations"]) == len(listing_row["locations"]),
        "description_matches_listing_feed": matches_feed,
    }


class Varonis(Adapter):
    """Varonis' own careersite listing plus its exact Jobvite job pages."""

    async def run(self) -> Result:
        timings = self.result.timings
        started = time.perf_counter()

        collection_started = time.perf_counter()
        entry_response = await self.http.get(OFFICIAL_ENTRY)
        entry = parse_official_entry(entry_response.text, str(entry_response.url))
        step = time.perf_counter()
        timings["official_entry_seconds"] = round(step - collection_started, 6)

        site_response = await self.http.get(CAREERS_SITE)
        site = parse_careers_site(site_response.text, str(site_response.url))
        bundle_response = await self.http.get(site["bundle_url"])
        bundle = parse_careers_bundle(bundle_response.text, str(bundle_response.url))
        timings["careers_site_seconds"] = round(time.perf_counter() - step, 6)

        step = time.perf_counter()
        listing_payload = await self.http.json(LISTING_API)
        listing = parse_requisitions(listing_payload)
        timings["listing_api_seconds"] = round(time.perf_counter() - step, 6)

        step = time.perf_counter()
        criteria_payload = await self.http.json(FILTER_API)
        criteria = parse_search_criteria(criteria_payload)
        timings["filter_api_seconds"] = round(time.perf_counter() - step, 6)

        step = time.perf_counter()
        feed_response = await self.http.get(FEED_URL)
        feed_bytes = feed_response.content
        timings["feed_request_seconds"] = round(time.perf_counter() - step, 6)

        step = time.perf_counter()
        feed = parse_feed(feed_bytes, str(feed_response.url))
        timings["feed_parse_seconds"] = round(time.perf_counter() - step, 6)
        timings["source_collection_seconds"] = round(time.perf_counter() - collection_started, 6)

        feed_by_lower = {identity.lower(): identity for identity in feed["entries"]}
        listing_lower = {identity.lower(): identity for identity in listing["ids"]}
        case_mismatch = {
            row["id"]: feed_by_lower[row["id"].lower()]
            for row in listing["rows"]
            if row["id"] not in feed["entries"] and row["id"].lower() in feed_by_lower
        }
        missing_from_feed = [
            row["id"] for row in listing["rows"]
            if row["id"] not in feed["entries"] and row["id"].lower() not in feed_by_lower
        ]
        extra_feed = [
            identity for identity in feed["entries"] if not feed["entries"][identity].get("parent_id")
            and identity.lower() not in listing_lower
        ]

        jobs = []
        for row in listing["rows"]:
            canonical_id = case_mismatch.get(row["id"], row["id"])
            feed_entry = feed["entries"].get(canonical_id, {})
            raw = {
                "listing": row["raw"],
                "listing_normalized": {key: value for key, value in row.items() if key != "raw"},
                "feed": {
                    key: value for key, value in feed_entry.items() if key != "description_html"
                },
                "source": {
                    "official_entry_url": OFFICIAL_ENTRY,
                    "careers_site_url": CAREERS_SITE,
                    "listing_api_url": LISTING_API,
                    "detail_url": f"{JOBVITE_JOB}{canonical_id}",
                    "feed_url": FEED_URL,
                },
            }
            if feed_entry.get("description_html"):
                raw["feed"]["description_fingerprint"] = body_fingerprint(feed_entry["description_html"])
            if canonical_id != row["id"]:
                raw["case_mismatch"] = {
                    "listing_id": row["id"],
                    "canonical_id": canonical_id,
                    "resolution": "Jobvite detail page 404s for the listing id; the same requisition is published under this casing in Varonis' Jobvite XML feed.",
                }
            job = self.job(
                raw,
                title=row["title"],
                url=f"{JOBVITE_JOB}{canonical_id}",
                job_id=canonical_id,
                requisition_id=feed_entry.get("requisition_id"),
                apply_url=f"{JOBVITE_JOB}{canonical_id}/apply",
                description="",
                method="jobvite_detail_page_description",
                description_url=f"{JOBVITE_JOB}{canonical_id}",
                locations=row["locations"],
                department=row["department"],
                team=row["sub_department"],
                employer=EMPLOYER,
                date_posted=feed_entry.get("date_posted"),
                date_posted_raw=(
                    {
                        "jobvite_feed_date": feed_entry.get("date_raw"),
                        "jobvite_feed_field": "date",
                        "jobvite_feed_url": FEED_URL,
                        "format": "MM/DD/YYYY",
                    }
                    if feed_entry.get("date_raw")
                    else None
                ),
                language="en",
            )
            if canonical_id != row["id"]:
                job.warnings.append(f"listing_id_case_mismatch:{row['id']}->{canonical_id}")
            if not feed_entry:
                job.warnings.append("missing_from_jobvite_feed")
            jobs.append(job)
            self.add(job)

        self.result.expected_total = listing["count"]
        self.result.metadata.update({
            "membership_reconciliation": {
                "listing_api_url": LISTING_API,
                "listing_api_count": listing["count"],
                "listing_ids": listing["ids"],
                "departments": listing["departments"],
                "locations_total": listing["locations_total"],
                "multi_location_rows": listing["multi_location_rows"],
                "listing_issues": listing["issues"],
                "feed_url": FEED_URL,
                "feed_base_requisitions": feed["base_count"],
                "feed_location_variant_entries": feed["variant_count"],
                "feed_entries": len(feed["entries"]),
                "listing_ids_missing_from_feed": missing_from_feed,
                "feed_ids_missing_from_listing": extra_feed,
                "listing_id_case_mismatches": case_mismatch,
                "pagination": "none (single company-scoped JSON response)",
                "filters_applied": [],
                "filter_controls": criteria["titles"],
                "complete": not missing_from_feed and not extra_feed,
            },
            "official_journey": {
                "entry": entry,
                "careers_site": site,
                "bundle": bundle,
                "listing_api": LISTING_API,
                "filter_api": FILTER_API,
                "jobvite_feed": FEED_URL,
                "jobvite_company_id": COMPANY_ID,
            },
            "filters": criteria,
            "employer": EMPLOYER,
            "jobvite_feed_fields": {
                "source_url": FEED_URL,
                "entries": len(feed["entries"]),
                "base_requisitions": feed["base_count"],
                "location_variant_entries": feed["variant_count"],
                "retained_per_job": [
                    "requisitionid", "category", "jobtype", "location", "date",
                    "externalJobPostingId", "detail-url", "apply-url",
                    "department (business sub-unit)", "workflow", "parentId",
                ],
                "excluded_per_job": {
                    "description": "stored once as description_html (identical body, verified per job)",
                    "hiring_x0020_manager": "internal manager name; not a job property",
                },
            },
            "date_semantics": (
                "date_posted comes from Varonis' Jobvite XML feed <date> field (MM/DD/YYYY, "
                "board-published job date). No date is inferred from request time or templates."
            ),
        })

        step = time.perf_counter()
        await self.hydrate(jobs, self.fetch_detail)
        timings["detail_and_reconciliation_seconds"] = round(time.perf_counter() - step, 6)

        verified = sum(job.description_verified for job in jobs)
        mismatches = [job.job_id for job in jobs if "detail_description_differs_from_feed" in job.warnings]
        self.result.metadata["detail_reconciliation"] = {
            "jobs": len(jobs),
            "descriptions_verified": verified,
            "description_matches_feed": sum(
                1 for job in jobs
                if (job.raw.get("detail") or {}).get("description_matches_feed") is True
            ),
            "description_feed_mismatches": mismatches,
            "requisition_ids_from_detail_pages": sum(1 for job in jobs if job.requisition_id),
            "dates_from_feed": sum(1 for job in jobs if job.date_posted),
            "apply_urls": sum(1 for job in jobs if job.apply_url),
        }
        reconciliation = self.result.metadata["membership_reconciliation"]
        self.result.listing_complete = bool(
            reconciliation["complete"]
            and len(jobs) == listing["count"]
            and not missing_from_feed
            and not extra_feed
        )
        if not self.result.listing_complete:
            self.result.errors.append("listing_membership_not_reconciled")
        timings["run_total_seconds"] = round(time.perf_counter() - started, 6)
        return self.result

    async def fetch_detail(self, job):
        detail_url = job.url
        response = await self.http.get(detail_url)
        parsed = parse_detail(response.text, str(response.url), job.job_id)
        if parsed["company_eid"] and parsed["company_eid"] != COMPANY_ID:
            raise ValueError(
                f"Varonis job {job.job_id} belongs to Jobvite company {parsed['company_eid']} instead of {COMPANY_ID}"
            )
        if parsed["company_eid"] != COMPANY_ID:
            job.warnings.append("jobvite_company_id_not_confirmed")
        listing = job.raw["listing_normalized"]
        if normalize_text(parsed["title"]).lower() != normalize_text(listing["title"]).lower():
            job.warnings.append(f"detail_title_differs_from_listing:{parsed['title']}")
        if parsed["job_req_id"]:
            job.requisition_id = parsed["job_req_id"]
        if parsed["apply_url"]:
            job.apply_url = parsed["apply_url"]
        feed_entry = job.raw.get("feed") or {}
        feed_fingerprint = feed_entry.get("description_fingerprint")
        page_fingerprint = parsed["description_fingerprint"]
        matches_feed = None
        if feed_fingerprint:
            matches_feed = (
                feed_fingerprint["whitespace_stripped_fnv1a64"]
                == page_fingerprint["whitespace_stripped_fnv1a64"]
            )
            if not matches_feed:
                job.warnings.append("detail_description_differs_from_feed")
        job.raw["detail"] = {
            "url": parsed["url"],
            "canonical_url": parsed["canonical_url"],
            "title": parsed["title"],
            "meta_department": parsed["meta_department"],
            "meta_locations": parsed["meta_locations"],
            "job_req_id": parsed["job_req_id"],
            "job_category_name": parsed["job_category_name"],
            "job_category_id": parsed["job_category_id"],
            "job_location_id": parsed["job_location_id"],
            "company_eid": parsed["company_eid"],
            "careersite_name": parsed["careersite_name"],
            "apply_url": parsed["apply_url"],
            "description_fingerprint": page_fingerprint,
            "feed_description_fingerprint": feed_fingerprint,
            "description_matches_feed": matches_feed,
            "section_headings": parsed["section_headings"],
            "listing_title_matches_detail": normalize_text(parsed["title"]).lower()
            == normalize_text(listing["title"]).lower(),
            "meta_location_count_matches_listing": len(parsed["meta_locations"]) == len(listing["locations"]),
        }
        if len(parsed["meta_locations"]) != len(listing["locations"]):
            job.warnings.append(
                f"detail_location_count_differs_from_listing:{len(parsed['meta_locations'])}!={len(listing['locations'])}"
            )
        job.description_html = parsed["description_html"]
        job.description_method = "jobvite_detail_page_description"
        job.description_source_url = parsed["canonical_url"]
        job.department = listing["department"]
        job.team = listing["sub_department"]
        job.employer = EMPLOYER
        return job
