"""Oracle's own careers site: www.oracle.com/careers -> careers.oracle.com jobsearch.

The journey inspected on 2026-11-08 is Oracle's first-party Oracle Cloud HCM
Candidate Experience site (site CX_45001 on eeho.fa.us2.oraclecloud.com), not an
aggregator or a guessed ATS slug:

* https://www.oracle.com/careers/ links "Search jobs at Oracle" to
  https://careers.oracle.com/en/sites/jobsearch/jobs (the link pre-selects a US
  location; the site's own unfiltered view is the global membership).
* The rendered listing loads
  /hcmRestApi/resources/latest/recruitingCEJobRequisitions with
  finder=findReqs;siteNumber=CX_45001,limit=14,sortBy=POSTING_DATES_DESC and
  "Show More Results" requests the same finder with offset += 14.
* Each card links https://careers.oracle.com/en/sites/jobsearch/job/<Id>; the
  rendered detail requests
  /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails with
  finder=ById;Id="<Id>",siteNumber=CX_45001&expand=all.
* The Apply Now control opens <job url>/apply/email (the job application form).
* The site's own job-postings sitemap is an independent membership surface.

The listing advertises TotalJobsCount=2836 while the listing and the official
sitemap both expose 2834 distinct IDs (2 counted-but-invisible). The adapter
records that discrepancy explicitly instead of inventing records.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
from html import unescape
import re
import time
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.oracle.com/careers/"
LISTING_URL = "https://careers.oracle.com/en/sites/jobsearch/jobs"
SITE_NAME = "Oracle"
PAGE_SIZE = 100
SORT_BY = "POSTING_DATES_DESC"
EXPAND = (
    "requisitionList.workLocation,requisitionList.otherWorkLocations,"
    "requisitionList.secondaryLocations,flexFieldsFacet.values,"
    "requisitionList.requisitionFlexFields"
)
FACETS = "WORK_LOCATIONS;WORKPLACE_TYPES;TITLES;CATEGORIES;ORGANIZATIONS;POSTING_DATES;FLEX_FIELDS;LOCATIONS"
JOBS_API_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAIL_API_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
SETTINGS_API_PATH = "/hcmRestApi/CandidateExperience/en/siteSettings"
SITEMAP_PATH = "/hcmUI/CandidateExperience/sitemaps/jobpostings"
# Oracle's Akamai edge answers the shared scraper UA with HTTP 403 while the
# public site serves a normal browser UA; this is not a CAPTCHA or bot challenge.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"),
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
JOB_PATH = "/en/sites/jobsearch/job/{job_id}"
APPLY_SUFFIX = "/apply/email"
DESCRIPTION_METHOD = "oracle.careers.cx45001.job_requisition_details.external_sections"

# The rendered detail page prints these headings above the matching API field.
DESCRIPTION_SECTIONS = (
    ("Job Description", "ExternalDescriptionStr"),
    ("Responsibilities", "ExternalResponsibilitiesStr"),
    ("Qualifications", "ExternalQualificationsStr"),
    ("About Us", "CorporateDescriptionStr"),
)
SALARY_RE = re.compile(r"Hiring Range in (?P<currency>[A-Z]{3})\s+(?P<detail>.*?)(?=\.\s|\.$)", re.S)
AMOUNT_RE = re.compile(
    r"(?:from:?\s*)?(?P<from>\$[\d,]+(?:\.\d+)?)"
    r"(?:\s*to\s*(?P<to>\$[\d,]+(?:\.\d+)?))?"
    r"\s*per\s*(?P<period>hour|annum|year|month)",
    re.I,
)


def job_url(job_id: str) -> str:
    return "https://careers.oracle.com" + JOB_PATH.format(job_id=job_id)


def apply_url(job_id: str) -> str:
    return job_url(job_id) + APPLY_SUFFIX


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.oracle.com"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Oracle official careers entry redirected away from www.oracle.com/careers")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Oracle" not in title:
        raise ValueError("Oracle official careers page no longer identifies Oracle")
    links = []
    for anchor in soup.select("a[href]"):
        href = urljoin(response_url, anchor.get("href", ""))
        link = urlsplit(href)
        if (link.scheme == "https" and link.netloc == "careers.oracle.com"
                and link.path.rstrip("/") == "/en/sites/jobsearch/jobs"):
            links.append({"href": href, "text": anchor.get_text(" ", strip=True)})
    if not links:
        raise ValueError("Oracle official careers page no longer links its jobsearch listing")
    # The published CTA carries a United States location filter; membership is
    # the site's own unfiltered global listing, so the filter is deliberately
    # dropped and recorded as observed evidence.
    return {
        "url": response_url,
        "title": title,
        "listing_url": LISTING_URL,
        "observed_listing_links": links[:4],
        "observed_listing_link_query": urlsplit(links[0]["href"]).query,
    }


def parse_listing_shell(html: str, response_url: str = LISTING_URL) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "careers.oracle.com"
            or parsed.path.rstrip("/") != "/en/sites/jobsearch/jobs"):
        raise ValueError("Oracle listing shell redirected away from careers.oracle.com jobsearch")
    hosts = set(re.findall(r"https://([a-z0-9.-]+\.oraclecloud\.com)(?::443)?/hcmRestApi/", html))
    sites = set(re.findall(r"siteNumber=(CX_\d+)", html))
    if len(hosts) != 1 or len(sites) != 1:
        raise ValueError("Oracle listing shell no longer exposes one HCM API host and site number")
    if "static.oracle.com/cdn/fa/oj-hcm-ce/" not in html:
        raise ValueError("Oracle listing shell is no longer the Oracle HCM Candidate Experience app")
    soup = BeautifulSoup(html, "lxml")
    return {
        "url": response_url,
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "api_host": hosts.pop(),
        "site_number": sites.pop(),
    }


def parse_site_settings(value: dict, *, expected_host: str, expected_site: str) -> dict:
    app = value.get("app") or {}
    if app.get("siteName") != SITE_NAME:
        raise ValueError("Oracle site settings no longer identify siteName=Oracle")
    if app.get("siteCode") != expected_site or app.get("siteNumber") != expected_site:
        raise ValueError("Oracle site settings site code changed from the listing shell")
    api = urlsplit(app.get("apiBaseUrl") or "")
    if api.scheme != "https" or api.hostname != expected_host:
        raise ValueError("Oracle site settings API host changed from the listing shell")
    if app.get("siteURLName") != "jobsearch":
        raise ValueError("Oracle site settings URL name changed from jobsearch")
    return {
        "site_name": app["siteName"],
        "site_code": app["siteCode"],
        "site_number": app["siteNumber"],
        "site_lang": app.get("siteLang"),
        "site_url_name": app.get("siteURLName"),
        "api_base_url": app.get("apiBaseUrl"),
        "ui_base_url": app.get("uiBaseUrl"),
        "cdn_url": app.get("cdnUrl"),
        "languages": app.get("languages") or [],
        "templates": app.get("templates") or {},
    }


def listing_params(site_number: str, offset: int, limit: int = PAGE_SIZE) -> dict:
    finder = (
        f"findReqs;siteNumber={site_number},facetsList={FACETS},"
        f"limit={limit},offset={offset},sortBy={SORT_BY}"
    )
    return {"onlyData": "true", "expand": EXPAND, "finder": finder,
            "limit": str(limit), "offset": str(offset)}


def detail_params(site_number: str, job_id: str) -> dict:
    return {"onlyData": "true", "expand": "all",
            "finder": f'ById;Id="{job_id}",siteNumber={site_number}'}


def parse_listing_page(value: dict, *, offset: int, limit: int, site_number: str) -> dict:
    items = value.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("Oracle listing response no longer has exactly one result block")
    block = items[0]
    if block.get("SiteNumber") != site_number:
        raise ValueError("Oracle listing response site number changed")
    if int(block.get("Offset", -1)) != offset or int(block.get("Limit", -1)) != limit:
        raise ValueError("Oracle listing response offset/limit changed from the request")
    if block.get("SortBy") != SORT_BY:
        raise ValueError("Oracle listing response sort changed from POSTING_DATES_DESC")
    rows = block.get("requisitionList")
    if not isinstance(rows, list):
        raise ValueError("Oracle listing response omitted requisitionList")
    ids = []
    for row in rows:
        job_id = str(row.get("Id") or "")
        if not job_id or not (row.get("Title") or "").strip():
            raise ValueError("Oracle listing row lost its Id or Title")
        ids.append(job_id)
    if len(ids) != len(set(ids)):
        raise ValueError("Oracle listing page repeats a requisition Id")
    total = int(block.get("TotalJobsCount"))
    return {
        "total": total,
        "rows": rows,
        "ids": ids,
        "returned": len(rows),
        "offset": offset,
        "limit": limit,
        "sort_by": block.get("SortBy"),
        "has_more": bool(value.get("hasMore", offset + len(rows) < total)),
    }


def parse_detail(value: dict, *, job_id: str) -> dict:
    items = value.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError(f"Oracle detail for {job_id} no longer has exactly one record")
    row = items[0]
    if str(row.get("Id")) != str(job_id):
        raise ValueError(f"Oracle detail returned a different posting than {job_id}")
    if not (row.get("Title") or "").strip():
        raise ValueError(f"Oracle detail for {job_id} lost its Title")
    return row


def parse_job_sitemap(xml_text: str) -> dict:
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", xml_text, re.S)
    if not locs:
        raise ValueError("Oracle job-postings sitemap has no <loc> entries")
    ids, locales = [], Counter()
    for loc in locs:
        parsed = urlsplit(unescape(loc).strip())
        if parsed.scheme != "https" or parsed.netloc != "careers.oracle.com":
            raise ValueError("Oracle job-postings sitemap links outside careers.oracle.com")
        match = re.fullmatch(r"/([a-z]{2})/job/(\d+)", parsed.path)
        if not match:
            raise ValueError("Oracle job-postings sitemap route changed")
        locales[match.group(1)] += 1
        ids.append(match.group(2))
    unique = sorted(set(ids))
    return {
        "url_count": len(locs),
        "unique_ids": unique,
        "locales": dict(sorted(locales.items())),
        "locale_duplicate_urls": len(locs) - len(unique),
    }


def flex_fields(row: dict) -> list[dict]:
    return [
        {"prompt": item.get("Prompt"), "value": item.get("Value")}
        for item in row.get("requisitionFlexFields") or []
        if isinstance(item, dict)
    ]


def flex_value(fields: list[dict], prompt: str) -> str | None:
    for item in fields:
        if item.get("prompt") == prompt:
            return item.get("value")
    return None


def locations(row: dict) -> list[str]:
    values = [row.get("PrimaryLocation")]
    for key in ("secondaryLocations", "otherWorkLocations", "workLocation"):
        for item in row.get(key) or []:
            if isinstance(item, dict):
                values.append(item.get("Name") or item.get("LocationName"))
    return list(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))


def salary_from_text(text: str) -> dict | None:
    match = SALARY_RE.search(text or "")
    if not match:
        return None
    detail = match.group("detail")
    ranges = [
        {"from": amount.group("from"), "to": amount.group("to"),
         "period": amount.group("period").lower()}
        for amount in AMOUNT_RE.finditer(detail)
    ]
    if not ranges:
        return None
    return {
        "currency": match.group("currency"),
        "ranges": ranges,
        "raw": match.group(0).strip(),
        "source_field": "ExternalQualificationsStr",
    }


def description_sections(row: dict) -> list[dict]:
    sections = []
    for heading, field in DESCRIPTION_SECTIONS:
        html_value = row.get(field) or ""
        text = text_html(html_value)
        if not text:
            continue
        sections.append({
            "heading": heading,
            "field": field,
            "html": html_value,
            "text": text,
            "text_sha256": sha256(text.encode()).hexdigest(),
        })
    return sections


def compose_description(row: dict) -> dict:
    sections = description_sections(row)
    html = "\n".join(f"<h2>{section['heading']}</h2>\n{section['html']}" for section in sections)
    return {
        "html": html,
        "text": text_html(html),
        "sections": sections,
        "available": bool(sections),
    }


class OracleCorporation(Adapter):
    """Collect Oracle's own CX_45001 job listing and every exact detail record."""

    VERIFICATION_REVISION = "oracle.careers.cx45001.1"

    async def _get(self, url: str, *, params: dict | None = None, label: str) -> dict:
        started = time.perf_counter()
        response = await self.http.request("GET", url, params=params, headers=BROWSER_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        return {
            "url": url,
            "request_url": str(response.request.url),
            "final_url": str(response.url),
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "response": response,
            "label": label,
        }

    async def _official_entry(self) -> dict:
        capture = await self._get(OFFICIAL_ENTRY, label="company_official_careers_entry")
        parsed = parse_official_entry(capture["response"].text, capture["final_url"])
        parsed.update({key: capture[key] for key in (
            "request_url", "final_url", "status", "content_type", "seconds",
            "response_bytes", "response_sha256")})
        return parsed

    async def _listing_shell(self) -> dict:
        capture = await self._get(LISTING_URL, label="company_jobsearch_spa_shell")
        parsed = parse_listing_shell(capture["response"].text, capture["final_url"])
        parsed.update({key: capture[key] for key in (
            "request_url", "final_url", "status", "content_type", "seconds",
            "response_bytes", "response_sha256")})
        return parsed

    async def _site_settings(self, shell: dict) -> dict:
        url = f"https://{shell['api_host']}{SETTINGS_API_PATH}/{shell['site_number']}"
        capture = await self._get(url, label="company_site_settings")
        parsed = parse_site_settings(capture["response"].json(),
                                     expected_host=shell["api_host"],
                                     expected_site=shell["site_number"])
        parsed.update({key: capture[key] for key in (
            "request_url", "final_url", "status", "content_type", "seconds",
            "response_bytes", "response_sha256")})
        return parsed

    async def _sitemap(self, shell: dict) -> dict:
        url = f"https://{shell['api_host']}{SITEMAP_PATH}"
        capture = await self._get(url, label="company_jobpostings_sitemap")
        parsed = parse_job_sitemap(capture["response"].text)
        parsed.update({key: capture[key] for key in (
            "request_url", "final_url", "status", "content_type", "seconds",
            "response_bytes", "response_sha256")})
        return parsed

    async def _enumerate(self, shell: dict, label: str) -> dict:
        api = f"https://{shell['api_host']}{JOBS_API_PATH}"
        site = shell["site_number"]
        rows, ids, pages, issues = [], [], [], []
        seen: set[str] = set()
        total = None
        offset = 0
        elapsed = 0.0
        while True:
            if not self.page_guard():
                issues.append("oracle_listing_page_budget_exhausted")
                break
            params = listing_params(site, offset)
            capture = await self._get(api, params=params, label=f"{label}_listing_page")
            elapsed += capture["seconds"]
            page = parse_listing_page(capture["response"].json(), offset=offset,
                                      limit=PAGE_SIZE, site_number=site)
            if total is None:
                total = page["total"]
            elif page["total"] != total:
                issues.append(f"oracle_advertised_total_changed:{total}->{page['total']}")
            for row, job_id in zip(page["rows"], page["ids"]):
                if job_id in seen:
                    issues.append(f"oracle_duplicate_listing_id:{job_id}")
                    continue
                seen.add(job_id)
                rows.append(row)
                ids.append(job_id)
            pages.append({
                "offset": page["offset"],
                "limit": page["limit"],
                "returned": page["returned"],
                "total": page["total"],
                "job_ids": page["ids"],
                "request_url": capture["request_url"],
                "final_url": capture["final_url"],
                "status": capture["status"],
                "seconds": capture["seconds"],
                "response_bytes": capture["response_bytes"],
                "response_sha256": capture["response_sha256"],
            })
            if page["returned"] == 0:
                break
            offset += page["returned"]
            if offset >= page["total"]:
                break
        return {
            "label": label,
            "api_url": api,
            "site_number": site,
            "advertised_total": total,
            "rows": rows,
            "ids": ids,
            "pages": pages,
            "issues": list(dict.fromkeys(issues)),
            "seconds": round(elapsed, 6),
        }

    def _make_job(self, row: dict):
        job_id = str(row["Id"])
        return self.job(
            {"listing": row},
            title=row.get("Title") or "",
            url=job_url(job_id),
            apply_url=apply_url(job_id),
            job_id=job_id,
            date_posted=row.get("PostedDate"),
            date_posted_raw=row.get("PostedDate"),
            locations=locations(row),
            employment_type=row.get("JobType") or row.get("JobSchedule") or row.get("WorkerType"),
            workplace_type=row.get("WorkplaceTypeCode") or row.get("WorkplaceType") or None,
            language=row.get("Language"),
        )

    async def run(self):
        started = time.perf_counter()
        official = await self._official_entry()
        shell = await self._listing_shell()
        settings = await self._site_settings(shell)
        self.result.metadata["official_entry"] = official
        self.result.metadata["listing_shell"] = shell
        self.result.metadata["site_settings"] = settings

        initial = await self._enumerate(shell, "initial")
        self.result.timings["initial_listing_seconds"] = initial["seconds"]
        self.result.expected_total = len(initial["ids"])
        self.result.metadata["initial_listing"] = {
            key: value for key, value in initial.items() if key != "rows"
        }
        if initial["advertised_total"] is not None and initial["advertised_total"] != len(initial["ids"]):
            self.result.warnings.append(
                f"source TotalJobsCount={initial['advertised_total']} but the public listing exposes "
                f"{len(initial['ids'])} distinct published IDs "
                f"({initial['advertised_total'] - len(initial['ids'])} counted-but-invisible)")
        self.result.errors.extend(initial["issues"])

        for row in initial["rows"]:
            self.add(self._make_job(row))
        if not self.result.jobs:
            self.result.errors.append("oracle_listing_empty")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
            return self.result

        async def detail(job):
            api = f"https://{shell['api_host']}{DETAIL_API_PATH}"
            params = detail_params(shell["site_number"], job.job_id)
            response = await self.http.request("GET", api, params=params, headers=BROWSER_HEADERS)
            row = parse_detail(response.json(), job_id=job.job_id)
            job.raw["detail"] = row
            fields = flex_fields(row)
            description = compose_description(row)
            job.raw["description_sections"] = [
                {key: section[key] for key in ("heading", "field", "text_sha256")}
                for section in description["sections"]
            ]
            if not description["available"]:
                job.description_unavailable = True
                job.warnings.extend(["source_description_missing", "description_not_full_field"])
                self.result.metadata["oracle_confirmed_empty_roles"] = (
                    self.result.metadata.get("oracle_confirmed_empty_roles", 0) + 1)
            else:
                job.description_html = description["html"]
                job.description_text = description["text"]
            job.description_source_url = api + "?" + urlencode(params)
            job.description_method = DESCRIPTION_METHOD
            job.title = row.get("Title") or job.title
            job.requisition_id = str(row["RequisitionId"]) if row.get("RequisitionId") else job.requisition_id
            job.date_posted = row.get("ExternalPostedStartDate") or job.date_posted
            job.date_posted_raw = row.get("ExternalPostedStartDate") or job.date_posted_raw
            job.date_updated = None
            job.valid_through = row.get("ExternalPostedEndDate")
            job.locations = locations(row) or job.locations
            job.department = row.get("Category") or row.get("Department") or row.get("JobFunction")
            job.employment_type = (flex_value(fields, "Job Type") or row.get("JobType")
                                   or row.get("JobSchedule") or row.get("WorkerType")
                                   or job.employment_type)
            job.workplace_type = row.get("WorkplaceTypeCode") or row.get("WorkplaceType") or None
            job.qualifications = row.get("ExternalQualificationsStr") or None
            job.responsibilities = row.get("ExternalResponsibilitiesStr") or None
            job.experience = flex_value(fields, "Years")
            job.education = row.get("StudyLevel")
            job.employer = row.get("LegalEmployer") or None
            job.language = row.get("ContentLocale") or job.language
            job.salary = salary_from_text(text_html(row.get("ExternalQualificationsStr")))
            return job

        await self.hydrate(self.result.jobs, detail)

        final = await self._enumerate(shell, "final")
        self.result.timings["final_listing_seconds"] = final["seconds"]
        sitemap = await self._sitemap(shell)
        self.result.timings["sitemap_seconds"] = sitemap["seconds"]
        self.result.metadata["final_listing"] = {
            key: value for key, value in final.items() if key != "rows"
        }
        self.result.metadata["sitemap"] = {
            key: value for key, value in sitemap.items() if key != "unique_ids"
        }
        self.result.errors.extend(final["issues"])

        initial_ids = set(initial["ids"])
        final_ids = set(final["ids"])
        sitemap_ids = set(sitemap["unique_ids"])
        removed = sorted(initial_ids - final_ids)
        added = [job_id for job_id in final["ids"] if job_id not in initial_ids]
        sitemap_only = sorted(sitemap_ids - final_ids)
        listing_only = sorted(final_ids - sitemap_ids)

        if added:
            final_rows = {str(row["Id"]): row for row in final["rows"]}
            new_jobs = []
            for job_id in added:
                row = final_rows.get(job_id)
                if row is None:
                    continue
                job = self._make_job(row)
                self.add(job)
                new_jobs.append(job)
            if new_jobs:
                await self.hydrate(new_jobs, detail)

        collected_ids = {str(job.job_id) for job in self.result.jobs}
        complete = (
            not self.result.errors
            and not removed
            and not added
            and not sitemap_only
            and not listing_only
            and final_ids == sitemap_ids == collected_ids
        )
        self.result.listing_complete = complete
        self.result.metadata["membership_reconciliation"] = {
            "advertised_total": final["advertised_total"],
            "listing_distinct_ids": len(final_ids),
            "sitemap_url_count": sitemap["url_count"],
            "sitemap_distinct_ids": len(sitemap_ids),
            "sitemap_locales": sitemap["locales"],
            "counted_but_invisible": (final["advertised_total"] or 0) - len(final_ids),
            "duplicate_listing_ids": len(final["ids"]) - len(final_ids),
            "duplicate_sitemap_ids": sitemap["url_count"] - len(sitemap_ids),
            "listing_and_sitemap_ids_match": final_ids == sitemap_ids,
            "initial_ids": initial["ids"],
            "final_ids": final["ids"],
            "sitemap_only_ids": sitemap_only,
            "listing_only_ids": listing_only,
            "added_ids": added,
            "removed_ids": removed,
            "collected_ids": sorted(collected_ids),
            "initial_pages": len(initial["pages"]),
            "final_pages": len(final["pages"]),
            "complete": complete,
        }
        self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
        return self.result
