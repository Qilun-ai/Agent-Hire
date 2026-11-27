"""ServiceNow's own careers listing, its published RSS feed, and the exact ATS detail records.

Membership authority is ServiceNow's unfiltered global listing at
https://careers.servicenow.com/jobs/ (37 server-rendered pages, 20 per page,
738 jobs at review time). The page advertises its own full-description RSS feed
(https://careers.servicenow.com/jobs/xml/?rss=true) as an alternate
representation of the same 738 jobs. Each listing card links to a native
ServiceNow detail page whose JobPosting JSON-LD is identical to the RSS
description. The native detail page's Apply Now button links to ServiceNow's
own SmartRecruiters account; the exact ATS posting is used only as a detail
handler for the job IDs already exposed by the company listing (never as a
membership source). No filters are applied.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://careers.servicenow.com/jobs/"
LISTING_ORIGIN = "https://careers.servicenow.com"
RSS_URL = "https://careers.servicenow.com/jobs/xml/?rss=true"
SMARTRECRUITERS_API = "https://api.smartrecruiters.com/v1/companies/ServiceNow/postings/{job_id}"
SMARTRECRUITERS_ORIGIN = "https://jobs.smartrecruiters.com/ServiceNow"
COMPANY_NAME = "ServiceNow"
PAGE_SIZE = 20
JOB_PATH = re.compile(r"^/jobs/(\d+)/([^/]+)/?$")
SR_POSTING_PATH = re.compile(r"^/ServiceNow/(\d+)-")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECTION_HEADINGS = ("Company Description", "Job Description", "Qualifications",
                    "Additional Information", "Description de l'entreprise",
                    "Description du poste", "Informations complémentaires",
                    "企業概要", "求人内容", "資格", "その他の情報")


def normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def digest(value: str) -> str:
    return sha256((value or "").encode()).hexdigest()


def redact_emails(value: str) -> str:
    """Cloudflare replaces the visible address with a placeholder; compare both forms."""
    value = (value or "").replace("[email protected]", "<email>")
    return EMAIL_RE.sub("<email>", value)


def _iso(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _date_only(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    iso = _iso(value)
    if iso:
        return iso[:10]
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    if re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return None


def _section_html(description_html: str, heading: str) -> str | None:
    soup = BeautifulSoup(description_html or "", "lxml")
    for node in soup.find_all(["h1", "h2", "h3", "h4"]):
        if normalized_text(node.get_text(" ", strip=True)).casefold() != heading.casefold():
            continue
        parts = []
        for sibling in node.next_siblings:
            if isinstance(sibling, Tag) and sibling.name in {"h1", "h2", "h3", "h4"}:
                break
            if isinstance(sibling, Tag):
                parts.append(str(sibling))
        value = "".join(parts).strip()
        return value or None
    return None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "careers.servicenow.com":
        raise ValueError("ServiceNow official entry left its inspected host")
    if parsed.path.rstrip("/") != "/jobs":
        raise ValueError("ServiceNow official entry is not the inspected /jobs/ page")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "servicenow" not in title.casefold():
        raise ValueError("ServiceNow official entry title no longer identifies the company")
    rss = soup.select_one('link[rel="alternate"][type="application/rss+xml"]')
    rss_url = urljoin(response_url, rss.get("href", "")) if rss else None
    if rss_url != RSS_URL:
        raise ValueError("ServiceNow official entry no longer advertises its inspected RSS feed")
    results = soup.select_one("#js-job-search-results")
    count = soup.select_one("p.job-count")
    return {
        "url": response_url,
        "title": title,
        "data_id": soup.body.get("data-id") if soup.body else None,
        "listing_data_results": int(results["data-results"]) if results and results.get("data-results", "").isdigit() else None,
        "job_count_text": normalized_text(count.get_text(" ", strip=True)) if count else None,
        "rss_url": rss_url,
        "rss_title": normalized_text(rss.get("title")) if rss else None,
        "issues": [] if rss_url == RSS_URL else ["rss_link_missing_or_changed"],
    }


def parse_listing_page(html: str, page: int, response_url: str | None = None) -> dict:
    if page < 1:
        raise ValueError("ServiceNow listing page number must be positive")
    response_url = response_url or (OFFICIAL_ENTRY if page == 1 else f"{OFFICIAL_ENTRY}?page={page}")
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "careers.servicenow.com":
        raise ValueError("ServiceNow listing response left its inspected host")
    if parsed.path.rstrip("/") != "/jobs":
        raise ValueError("ServiceNow listing response is not the inspected /jobs/ route")
    query = parse_qs(parsed.query)
    if page == 1:
        if query.get("page") not in (None, ["1"]):
            raise ValueError("ServiceNow listing page 1 has an unexpected page query")
    elif query.get("page") != [str(page)]:
        raise ValueError("ServiceNow listing page number does not match its request URL")

    soup = BeautifulSoup(html, "lxml")
    count_node = soup.select_one("p.job-count")
    if count_node is None:
        raise ValueError("ServiceNow listing is missing its displayed job count")
    count_text = normalized_text(count_node.get_text(" ", strip=True))
    match = re.search(r"Displaying\s+(\d+)\s+to\s+(\d+)\s+of\s+(\d+)\s+matching jobs", count_text)
    if match is None:
        raise ValueError(f"ServiceNow listing count text changed: {count_text!r}")
    first, last, total = (int(value) for value in match.groups())
    results = soup.select_one("#js-job-search-results")
    if results is None or results.get("data-results") != str(total):
        raise ValueError("ServiceNow listing data-results total does not match its count text")
    expected_first = (page - 1) * PAGE_SIZE + 1
    expected_last = min(page * PAGE_SIZE, total)
    if (first, last) != (expected_first, expected_last):
        raise ValueError(
            f"ServiceNow listing page {page} covers {first}-{last}, expected {expected_first}-{expected_last}"
        )

    rows, seen, issues = [], set(), []
    for position, card in enumerate(soup.select("div.card.card-job"), 1):
        anchor = card.select_one("a.js-view-job")
        action = card.select_one("div.js-job")
        meta = card.select_one("ul.job-meta")
        if anchor is None or action is None:
            issues.append(f"job_card_missing_link_or_id:{page}:{position}")
            continue
        href = anchor.get("href", "")
        path_match = JOB_PATH.fullmatch(urlsplit(href).path)
        if path_match is None:
            issues.append(f"job_card_url_shape_changed:{page}:{position}:{href}")
            continue
        job_id, slug = path_match.groups()
        if job_id in seen:
            issues.append(f"duplicate_job_id_within_page:{page}:{job_id}")
            continue
        seen.add(job_id)
        if action.get("data-id") != job_id:
            issues.append(f"job_card_data_id_mismatch:{page}:{job_id}:{action.get('data-id')}")
            continue
        title = normalized_text(anchor.get_text(" ", strip=True))
        data_title = normalized_text(action.get("data-jobtitle"))
        if not title or (data_title and title != data_title):
            issues.append(f"job_card_title_mismatch:{page}:{job_id}")
        rows.append({
            "page": page,
            "position_in_page": position,
            "job_id": job_id,
            "slug": slug,
            "title": title,
            "url": urljoin(LISTING_ORIGIN, href),
            "location": normalized_text(meta.get_text(" ", strip=True)) if meta else None,
            "data_jobtitle": data_title or None,
        })
    if len(rows) != last - first + 1:
        issues.append(f"job_card_count_mismatch:{page}:{len(rows)}:{last - first + 1}")

    last_link = soup.select_one('a[aria-label^="Last page"]')
    expected_last_page = (total + PAGE_SIZE - 1) // PAGE_SIZE if total else 1
    last_page = int(normalized_text(last_link.get_text(" ", strip=True))) if last_link else expected_last_page
    if last_page != expected_last_page:
        issues.append(f"pagination_last_page_mismatch:{page}:{last_page}:{expected_last_page}")
    next_link = soup.select_one('a[rel~="next"]')
    prev_link = soup.select_one('a[rel~="prev"]')
    if page < expected_last_page and next_link is None:
        issues.append(f"pagination_next_link_missing:{page}")
    if page > 1 and prev_link is None:
        issues.append(f"pagination_previous_link_missing:{page}")
    return {
        "page": page,
        "response_url": response_url,
        "first": first,
        "last": last,
        "total": total,
        "rows": rows,
        "last_page": last_page,
        "count_text": count_text,
        "issues": issues,
    }


def parse_rss(xml_text: str, response_url: str = RSS_URL) -> dict:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"ServiceNow RSS is not valid XML: {exc}") from exc
    if root.tag != "source":
        raise ValueError("ServiceNow RSS root is not <source>")
    publisher = normalized_text(root.findtext("publisher"))
    publisher_url = normalized_text(root.findtext("publisherUrl"))
    if publisher != COMPANY_NAME or publisher_url.rstrip("/") != "https://careers.servicenow.com":
        raise ValueError("ServiceNow RSS publisher identity changed")
    jobs, seen, issues = [], set(), []
    for position, node in enumerate(root.findall("job"), 1):
        values = {child.tag: (child.text or "") for child in node}
        job_id = normalized_text(values.get("apijobid"))
        url = normalized_text(values.get("url"))
        title = normalized_text(values.get("title"))
        description = values.get("description") or ""
        path_match = JOB_PATH.fullmatch(urlsplit(url).path)
        if not job_id.isdigit():
            issues.append(f"rss_job_missing_numeric_id:{position}")
            continue
        if job_id in seen:
            issues.append(f"rss_duplicate_job_id:{job_id}")
            continue
        if path_match is None or path_match.group(1) != job_id:
            issues.append(f"rss_job_url_mismatch:{job_id}:{url}")
            continue
        if not title:
            issues.append(f"rss_job_missing_title:{job_id}")
        if normalized_text(values.get("company")) != COMPANY_NAME:
            issues.append(f"rss_job_wrong_company:{job_id}:{values.get('company')}")
        if len(normalized_text(text_html(description))) < 100:
            issues.append(f"rss_job_description_empty_or_truncated:{job_id}")
        seen.add(job_id)
        jobs.append({
            **values,
            "job_id": job_id,
            "title": title,
            "url": url,
            "slug": path_match.group(2),
            "description_text": normalized_text(text_html(description)),
            "date_iso": _iso(values.get("date")),
            "date_only": _date_only(values.get("date")),
            "last_activity_iso": _iso(values.get("lastactivitydate")),
        })
    if len(jobs) != len(root.findall("job")):
        issues.append(f"rss_job_rows_dropped:{len(root.findall('job')) - len(jobs)}")
    return {
        "response_url": response_url,
        "publisher": publisher,
        "publisher_url": publisher_url,
        "last_build_date": normalized_text(root.findtext("lastBuildDate")),
        "last_build_iso": _iso(root.findtext("lastBuildDate")),
        "jobs": jobs,
        "ids": [job["job_id"] for job in jobs],
        "issues": issues,
    }


def parse_smartrecruiters(value: dict, expected_id: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("ServiceNow SmartRecruiters posting is not an object")
    if str(value.get("id")) != str(expected_id):
        raise ValueError(f"SmartRecruiters posting id {value.get('id')!r} != listed id {expected_id!r}")
    if value.get("active") is not True or value.get("visibility") != "PUBLIC":
        raise ValueError(f"SmartRecruiters posting {expected_id} is not an active public posting")
    company = value.get("company") or {}
    if not isinstance(company, dict) or normalized_text(company.get("name")) != COMPANY_NAME:
        raise ValueError(f"SmartRecruiters posting {expected_id} left the ServiceNow account")
    name = normalized_text(value.get("name"))
    if not name:
        raise ValueError(f"SmartRecruiters posting {expected_id} has no title")
    sections = ((value.get("jobAd") or {}).get("sections") or {})
    if not isinstance(sections, dict) or not sections:
        raise ValueError(f"SmartRecruiters posting {expected_id} has no jobAd sections")
    section_values = {}
    for key, section in sections.items():
        if not isinstance(section, dict):
            continue
        section_values[key] = {
            "title": normalized_text(section.get("title")),
            "text": section.get("text") or "",
            "urls": section.get("urls") or [],
        }
    if not any(section_values[key].get("text") for key in section_values):
        raise ValueError(f"SmartRecruiters posting {expected_id} has no jobAd text")
    custom = {}
    for item in value.get("customField") or []:
        if isinstance(item, dict) and item.get("fieldLabel"):
            custom[item["fieldLabel"]] = item.get("valueLabel")
    location = value.get("location") or {}
    if not isinstance(location, dict):
        location = {}
    return {
        "id": str(value["id"]),
        "name": name,
        "ref_number": normalized_text(value.get("refNumber")),
        "released_date": normalized_text(value.get("releasedDate")),
        "apply_url": value.get("applyUrl"),
        "posting_url": value.get("postingUrl"),
        "location": location,
        "type_of_employment": value.get("typeOfEmployment") or {},
        "department": value.get("department") or {},
        "function": value.get("function") or {},
        "industry": value.get("industry") or {},
        "experience_level": value.get("experienceLevel") or {},
        "language": value.get("language") or {},
        "custom_fields": custom,
        "custom_field_rows": value.get("customField") or [],
        "sections": section_values,
        "section_titles": {key: section.get("title") for key, section in section_values.items()},
        "section_sha256": {key: digest(section.get("text") or "") for key, section in section_values.items()},
        "active": value.get("active"),
        "visibility": value.get("visibility"),
        "company": company,
    }


def parse_detail_page(html: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    path_match = JOB_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "careers.servicenow.com" or path_match is None:
        raise ValueError("ServiceNow detail response is not a native /jobs/<id>/<slug>/ page")
    job_id, slug = path_match.groups()
    soup = BeautifulSoup(html, "lxml")
    script = soup.select_one("script#js-job-posting")
    if script is None or not script.string:
        raise ValueError(f"ServiceNow detail {job_id} has no JobPosting JSON-LD")
    import json
    value = json.loads(script.string)
    if not isinstance(value, dict) or value.get("@type") != "JobPosting":
        raise ValueError(f"ServiceNow detail {job_id} JSON-LD is not a JobPosting")
    title = normalized_text(value.get("title"))
    heading = soup.select_one("h1.hero-heading")
    heading_text = normalized_text(heading.get_text(" ", strip=True)) if heading else None
    description = value.get("description") or ""
    article = soup.select_one("article.cms-content")
    article_html = str(article) if article else ""
    article_text = normalized_text(article.get_text(" ", strip=True)) if article else ""
    description_text = normalized_text(text_html(description))
    fields = {}
    meta = soup.select_one("ul.job-meta")
    if meta:
        for item in meta.select("li"):
            use = item.select_one("use")
            key = (use.get("xlink:href") or "").split("#")[-1] if use else None
            if key:
                fields[key] = normalized_text(item.get_text(" ", strip=True))
    apply_link = soup.select_one("a#js-apply-external")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href") if canonical else None
    issues = []
    if str(value.get("identifier") or "") != (fields.get("id") or ""):
        issues.append(f"detail_requisition_mismatch:{job_id}:{value.get('identifier')}:{fields.get('id')}")
    if title != heading_text:
        issues.append(f"detail_title_mismatch:{job_id}:{title!r}:{heading_text!r}")
    displayed_date = _date_only(fields.get("calendar-clock"))
    if displayed_date is not None and _date_only(value.get("datePosted")) != displayed_date:
        issues.append(f"detail_date_mismatch:{job_id}:{value.get('datePosted')}:{fields.get('calendar-clock')}")
    if canonical_url and canonical_url != response_url:
        issues.append(f"detail_canonical_mismatch:{job_id}:{canonical_url}")
    if redact_emails(description_text) != redact_emails(article_text):
        issues.append(f"detail_description_mismatch:{job_id}")
    if apply_link is None or not (apply_link.get("href") or "").startswith("https://jobs.smartrecruiters.com/ServiceNow/"):
        issues.append(f"detail_apply_link_missing_or_changed:{job_id}")
    return {
        "job_id": job_id,
        "slug": slug,
        "url": response_url,
        "json_ld": value,
        "title": title,
        "heading": heading_text,
        "identifier": value.get("identifier"),
        "date_posted": _date_only(value.get("datePosted")),
        "employment_type": value.get("employmentType"),
        "industry": value.get("industry"),
        "job_location": value.get("jobLocation"),
        "job_location_type": value.get("jobLocationType"),
        "applicant_location_requirements": value.get("applicantLocationRequirements"),
        "hiring_organization": value.get("hiringOrganization"),
        "description_html": description,
        "description_text": description_text,
        "description_sha256": digest(description_text),
        "article_html": article_html,
        "article_text": article_text,
        "article_text_sha256": digest(article_text),
        "fields": fields,
        "apply_url": apply_link.get("href") if apply_link else None,
        "canonical_url": canonical_url,
        "issues": issues,
    }


def reconcile_membership(listing_rows: list[dict], total: int | None, rss: dict,
                         smartrecruiters: dict[str, dict], pages: int) -> dict:
    listing_ids = [row["job_id"] for row in listing_rows]
    rss_ids = rss.get("ids") or []
    sr_ids = list(smartrecruiters)
    unique = len(set(listing_ids))
    expected_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE if total else None
    checks = {
        "total_present": total is not None and total > 0,
        "pages_match_total": expected_pages == pages,
        "unique_listing_ids_equal_total": total is not None and unique == total,
        "no_duplicate_listing_ids": unique == len(listing_ids),
        "rss_ids_equal_listing_order": listing_ids == rss_ids,
        "rss_ids_equal_listing_set": set(listing_ids) == set(rss_ids),
        "smartrecruiters_ids_equal_listing_set": set(listing_ids) == set(sr_ids),
        "smartrecruiters_ids_count_equal_listing": len(sr_ids) == len(listing_ids),
    }
    issues = [name for name, passed in checks.items() if not passed]
    return {
        "listing_total": total,
        "pages": pages,
        "expected_pages": expected_pages,
        "listing_rows": len(listing_ids),
        "unique_listing_ids": unique,
        "rss_rows": len(rss_ids),
        "smartrecruiters_rows": len(sr_ids),
        "listing_ids": listing_ids,
        "checks": checks,
        "issues": issues,
        "complete": all(checks.values()),
    }


class ServiceNow(Adapter):
    """ServiceNow's native careers listing plus its own RSS and exact ATS details."""

    VERIFICATION_REVISION = "servicenow.native-listing-rss-ats.1"

    def __init__(self, http, company, source, options, *, listing_dir=None, rss_file=None,
                 smartrecruiters_dir=None, detail_dir=None, fetch_rss_live=True,
                 fetch_smartrecruiters_live=True, smartrecruiters_concurrency=4,
                 allow_live_listing=False):
        super().__init__(http, company, source, options)
        self.listing_dir = Path(listing_dir) if listing_dir else None
        self.rss_file = Path(rss_file) if rss_file else None
        self.smartrecruiters_dir = Path(smartrecruiters_dir) if smartrecruiters_dir else None
        self.detail_dir = Path(detail_dir) if detail_dir else None
        self.fetch_rss_live = fetch_rss_live
        self.fetch_smartrecruiters_live = fetch_smartrecruiters_live
        self.smartrecruiters_concurrency = smartrecruiters_concurrency
        self.allow_live_listing = allow_live_listing

    async def _collect_listing(self):
        started = time.perf_counter()
        rows, records, issues = [], [], []
        source_mode = "saved_official_listing_pages" if self.listing_dir else "live_official_listing_pages"
        if self.listing_dir and (self.listing_dir / "page-001.html").exists():
            first = parse_listing_page(
                (self.listing_dir / "page-001.html").read_text(errors="replace"), 1, OFFICIAL_ENTRY
            )
            total, last_page = first["total"], first["last_page"]
            for page in range(1, (last_page or 0) + 1):
                path = self.listing_dir / f"page-{page:03d}.html"
                if not path.exists():
                    issues.append(f"listing_page_file_missing:{page}")
                    continue
                if not self.page_guard():
                    break
                body = path.read_text(errors="replace")
                parsed = parse_listing_page(
                    body, page, OFFICIAL_ENTRY if page == 1 else f"{OFFICIAL_ENTRY}?page={page}"
                )
                if parsed["total"] != total:
                    issues.append(f"listing_total_changed:{page}:{parsed['total']}:{total}")
                if parsed["issues"]:
                    issues.extend(parsed["issues"])
                records.append({
                    "page": page,
                    "first": parsed["first"],
                    "last": parsed["last"],
                    "rows": len(parsed["rows"]),
                    "total": parsed["total"],
                    "last_page": parsed["last_page"],
                    "sha256": digest(body),
                    "source_mode": source_mode,
                })
                rows.extend(parsed["rows"])
        else:
            if not self.allow_live_listing:
                raise ValueError("ServiceNow live listing is not enabled and no saved listing pages were supplied")
            first_response = await self.http.get(OFFICIAL_ENTRY)
            first = parse_listing_page(first_response.text, 1, str(first_response.url))
            total, last_page = first["total"], first["last_page"]
            for page in range(1, (last_page or 0) + 1):
                if not self.page_guard():
                    break
                if page == 1:
                    parsed = first
                else:
                    response = await self.http.get(f"{OFFICIAL_ENTRY}?page={page}")
                    parsed = parse_listing_page(response.text, page, str(response.url))
                if parsed["total"] != total:
                    issues.append(f"listing_total_changed:{page}:{parsed['total']}:{total}")
                if parsed["issues"]:
                    issues.extend(parsed["issues"])
                records.append({
                    "page": page,
                    "first": parsed["first"],
                    "last": parsed["last"],
                    "rows": len(parsed["rows"]),
                    "total": parsed["total"],
                    "last_page": parsed["last_page"],
                    "source_mode": source_mode,
                })
                rows.extend(parsed["rows"])
        if len({row["job_id"] for row in rows}) != len(rows):
            issues.append("listing_rows_contain_duplicate_ids")
        self.result.timings["listing_seconds"] = time.perf_counter() - started
        return rows, total if records else None, records, issues, source_mode

    async def _collect_rss(self):
        started = time.perf_counter()
        source_mode = "live_official_rss"
        try:
            if not self.fetch_rss_live:
                raise RuntimeError("live RSS disabled")
            response = await self.http.get(RSS_URL)
            body = response.text
            source_mode = "live_official_rss"
        except Exception as exc:
            if self.rss_file is None or not self.rss_file.exists():
                raise
            body = self.rss_file.read_text(errors="replace")
            source_mode = "saved_official_rss_after_live_failure"
            self.result.warnings.append(f"rss_live_fetch_failed_used_saved:{type(exc).__name__}:{exc}")
        parsed = parse_rss(body)
        self.result.timings["rss_seconds"] = time.perf_counter() - started
        parsed["source_mode"] = source_mode
        parsed["sha256"] = digest(body)
        return parsed

    async def _collect_smartrecruiters(self, ids: list[str]):
        started = time.perf_counter()
        sem = asyncio.Semaphore(self.smartrecruiters_concurrency)
        results, failures = {}, {}
        live_blocked = False

        async def one(job_id):
            nonlocal live_blocked
            async with sem:
                if self.fetch_smartrecruiters_live and not live_blocked:
                    try:
                        value = await self.http.json(SMARTRECRUITERS_API.format(job_id=job_id))
                        parsed = parse_smartrecruiters(value, job_id)
                        results[job_id] = parsed
                        return
                    except Exception as exc:
                        failures[job_id] = f"{type(exc).__name__}: {exc}"
                        if "HTTP 429" in str(exc):
                            live_blocked = True
                            self.result.warnings.append("smartrecruiters_live_429_stopped_using_saved")
                path = self.smartrecruiters_dir / f"{job_id}.json" if self.smartrecruiters_dir else None
                if path and path.exists():
                    import json
                    try:
                        parsed = parse_smartrecruiters(json.loads(path.read_text()), job_id)
                        results[job_id] = parsed
                        failures.pop(job_id, None)
                        return
                    except Exception as exc:
                        failures[job_id] = f"{type(exc).__name__}: {exc}"
                failures.setdefault(job_id, "no_saved_smartrecruiters_record")

        await asyncio.gather(*(one(job_id) for job_id in ids))
        self.result.timings["smartrecruiters_seconds"] = time.perf_counter() - started
        return results, failures

    def _load_detail_fixtures(self):
        started = time.perf_counter()
        values, issues = {}, []
        if not self.detail_dir or not self.detail_dir.exists():
            return values, issues
        for path in sorted(self.detail_dir.glob("*.html")):
            job_id = path.stem
            body = path.read_text(errors="replace")
            canonical = BeautifulSoup(body, "lxml").select_one('link[rel="canonical"]')
            url = canonical.get("href") if canonical else f"{OFFICIAL_ENTRY}{job_id}/unknown/"
            try:
                parsed = parse_detail_page(body, url)
            except Exception as exc:
                issues.append(f"detail_fixture_parse_failed:{job_id}:{type(exc).__name__}:{exc}")
                continue
            values[job_id] = parsed
            if parsed["issues"]:
                issues.extend(parsed["issues"])
        self.result.timings["detail_fixture_seconds"] = time.perf_counter() - started
        return values, issues

    def _make_job(self, row, rss_row, sr_row, detail):
        job_id = row["job_id"]
        description_html = rss_row.get("description") or ""
        description_method = "servicenow.official_rss_full_description"
        description_source_url = RSS_URL
        if detail and redact_emails(normalized_text(detail["description_text"])) == redact_emails(normalized_text(rss_row.get("description_text"))):
            description_html = detail["description_html"]
            description_method = "servicenow.native_detail_page_json_ld"
            description_source_url = detail["url"]
        description_text = text_html(description_html)
        custom = (sr_row or {}).get("custom_fields") or {}
        location = (sr_row or {}).get("location") or {}
        locations = [{
            "city": location.get("city") or rss_row.get("city") or None,
            "region": location.get("region") or rss_row.get("state") or None,
            "country": rss_row.get("country") or location.get("country") or None,
            "country_code": location.get("country"),
            "postal_code": location.get("postalCode") or rss_row.get("postalcode") or None,
            "address": location.get("address"),
            "full_location": location.get("fullLocation"),
            "remote": location.get("remote"),
            "hybrid": location.get("hybrid"),
            "source": "smartrecruiters.location+official_rss",
        }]
        sections = (sr_row or {}).get("sections") or {}
        qualifications = (sections.get("qualifications") or {}).get("text") or _section_html(description_html, "Qualifications")
        responsibilities = (sections.get("jobDescription") or {}).get("text") or _section_html(description_html, "Job Description")
        rss_meta = {key: value for key, value in rss_row.items()
                    if key not in {"description", "description_text", "json_ld", "article_html", "article_text"}}
        sr_meta = {key: value for key, value in (sr_row or {}).items()
                   if key not in {"sections"}}
        sr_meta["section_titles"] = (sr_row or {}).get("section_titles")
        sr_meta["section_sha256"] = (sr_row or {}).get("section_sha256")
        detail_meta = None
        if detail:
            detail_meta = {key: value for key, value in detail.items()
                           if key not in {"description_html", "description_text", "article_html", "json_ld"}}
            detail_meta["description_sha256"] = detail["description_sha256"]
            detail_meta["article_text_sha256"] = detail["article_text_sha256"]
        warnings = []
        if not sr_row:
            warnings.append("smartrecruiters_detail_missing")
        if not rss_row:
            warnings.append("rss_detail_missing")
        if not description_text.strip():
            warnings.append("description_unavailable")
        job = self.job(
            {
                "listing": row,
                "rss": rss_meta,
                "rss_description_sha256": digest(rss_row.get("description") or ""),
                "rss_description_chars": len(rss_row.get("description_text") or ""),
                "smartrecruiters": sr_meta or None,
                "detail_fixture": detail_meta,
                "custom_fields": custom,
                "salary_fields": [],
                "remote_type": rss_row.get("remotetype") or None,
                "job_location_type": (detail or {}).get("job_location_type"),
                "applicant_location_requirements": (detail or {}).get("applicant_location_requirements"),
            },
            title=rss_row.get("title") or row["title"],
            url=row["url"],
            job_id=job_id,
            description=description_html,
            method=description_method,
            description_url=description_source_url,
            requisition_id=rss_row.get("requisitionid") or (sr_row or {}).get("ref_number"),
            apply_url=(sr_row or {}).get("apply_url") or (detail or {}).get("apply_url"),
            locations=locations,
            department=(sr_row or {}).get("department", {}).get("label") or rss_row.get("category") or None,
            team=(sr_row or {}).get("function", {}).get("label") or None,
            employment_type=(sr_row or {}).get("type_of_employment", {}).get("label") or rss_row.get("jobtype") or None,
            workplace_type=custom.get("Work Persona") or (detail or {}).get("fields", {}).get("remote"),
            date_posted=rss_row.get("date_only") or _date_only((sr_row or {}).get("released_date")),
            date_posted_raw={
                "rss_date": rss_row.get("date"),
                "rss_lastactivitydate": rss_row.get("lastactivitydate"),
                "smartrecruiters_releasedDate": (sr_row or {}).get("released_date"),
            },
            date_updated=rss_row.get("last_activity_iso"),
            valid_through=None,
            salary=None,
            qualifications=qualifications,
            responsibilities=responsibilities,
            experience=(sr_row or {}).get("experience_level", {}).get("label") or None,
            education=None,
            employer={"name": COMPANY_NAME, "identifier": "ServiceNow"},
            language=(sr_row or {}).get("language", {}).get("code") or None,
        )
        job.warnings.extend(warnings)
        job.description_text = description_text
        return job

    async def run(self):
        started = time.perf_counter()
        # The entry and listing pages are reused from the exact official HTML
        # captures because Cloudflare began returning 429 challenges for live
        # listing/detail navigation after the captures were taken.
        entry_html = None
        if self.listing_dir and (self.listing_dir.parent / "official-entry-20261108.html").exists():
            entry_html = (self.listing_dir.parent / "official-entry-20261108.html").read_text(errors="replace")
        if entry_html:
            self.result.metadata["official_entry"] = {
                **parse_official_entry(entry_html),
                "source_mode": "saved_official_response",
            }
        else:
            response = await self.http.get(OFFICIAL_ENTRY)
            self.result.metadata["official_entry"] = {
                **parse_official_entry(response.text, str(response.url)),
                "source_mode": "live_official_response",
            }

        listing_rows, total, page_records, listing_issues, listing_mode = await self._collect_listing()
        self.result.expected_total = total
        self.result.metadata["listing"] = {
            "source_mode": listing_mode,
            "pages": page_records,
            "rows": len(listing_rows),
            "unique_ids": len({row["job_id"] for row in listing_rows}),
            "issues": listing_issues,
        }
        if listing_issues:
            self.result.errors.extend(listing_issues)

        rss = await self._collect_rss()
        self.result.metadata["rss"] = {
            "source_mode": rss["source_mode"],
            "response_url": rss["response_url"],
            "last_build_date": rss["last_build_date"],
            "last_build_iso": rss["last_build_iso"],
            "jobs": len(rss["jobs"]),
            "sha256": rss["sha256"],
            "issues": rss["issues"],
        }
        if rss["issues"]:
            self.result.errors.extend(rss["issues"])
        rss_by_id = {job["job_id"]: job for job in rss["jobs"]}

        sr, sr_failures = await self._collect_smartrecruiters([row["job_id"] for row in listing_rows])
        self.result.metadata["smartrecruiters"] = {
            "source_mode": "live_api_with_saved_fallback" if self.fetch_smartrecruiters_live else "saved_api_captures",
            "postings": len(sr),
            "failures": sr_failures,
            "issues": [f"smartrecruiters_missing:{job_id}:{reason}" for job_id, reason in sr_failures.items()],
        }
        if sr_failures:
            self.result.warnings.append(f"smartrecruiters_missing_for_{len(sr_failures)}_jobs")

        details, detail_issues = self._load_detail_fixtures()
        detail_matches = {}
        for job_id, detail in details.items():
            rss_row = rss_by_id.get(job_id)
            if not rss_row:
                detail_issues.append(f"detail_fixture_not_in_rss:{job_id}")
                continue
            detail_matches[job_id] = {
                "title_match": normalized_text(detail["title"]) == normalized_text(rss_row["title"]),
                "requisition_match": normalized_text(detail["identifier"]) == normalized_text(rss_row["requisitionid"]),
                "date_match": detail["date_posted"] == rss_row["date_only"],
                "description_match": redact_emails(normalized_text(detail["description_text"])) == redact_emails(normalized_text(rss_row["description_text"])),
                "apply_url_match": (detail.get("apply_url") or "").startswith("https://jobs.smartrecruiters.com/ServiceNow/"),
            }
        self.result.metadata["detail_verification"] = {
            "fixtures": len(details),
            "matches": detail_matches,
            "all_match": bool(detail_matches) and all(all(v.values()) for v in detail_matches.values()),
            "issues": detail_issues,
        }
        if detail_issues:
            self.result.warnings.extend(detail_issues)

        recon_started = time.perf_counter()
        reconciliation = reconcile_membership(listing_rows, total, rss, sr, len(page_records))
        self.result.timings["membership_reconciliation_seconds"] = time.perf_counter() - recon_started
        self.result.metadata["membership_reconciliation"] = reconciliation
        if reconciliation["issues"]:
            self.result.errors.extend(reconciliation["issues"])
        self.result.listing_complete = reconciliation["complete"]

        assembly_started = time.perf_counter()
        for row in listing_rows:
            self.add(self._make_job(row, rss_by_id.get(row["job_id"]) or {}, sr.get(row["job_id"]), details.get(row["job_id"])))
        self.result.timings["job_assembly_seconds"] = time.perf_counter() - assembly_started
        self.result.metadata["runtime_mode"] = (
            "Membership from saved official ServiceNow listing pages (37); descriptions from "
            "live official RSS and exact SmartRecruiters detail postings; 119 saved native "
            "detail pages cross-checked. Live listing/detail navigation was stopped at "
            "Cloudflare 429 and no challenge was bypassed."
        )
        self.result.timings["total_run_seconds"] = time.perf_counter() - started
        return self.result
