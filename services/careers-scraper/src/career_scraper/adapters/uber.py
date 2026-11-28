"""Uber's own careers journey and its company-scoped Oracle Recruiting Cloud site.

Inspected 2026-11-08:

* https://www.uber.com/us/en/careers/list/ answers HTTP 301 to
  https://jobs.uber.com/en/jobs/.
* The jobs.uber.com front-end answers the public request (and headless Chromium)
  with a Cloudflare HTTP 429 ``cf-mitigated: challenge`` page.  No challenge is
  solved or bypassed here.
* The same front-end's application shell references Uber's own Oracle HCM
  Candidate Experience site, https://iaziqy.fa.ocs.oraclecloud.com/hcmUI/
  CandidateExperience/en/sites/UberCareers.  Its site settings identify
  siteName=Uber, siteCode/siteURLName=UberCareers, siteNumber=CX_1, language=en.
* That site's unfiltered listing advertises 827 jobs and its detail pages expose
  the full employer-authored description fields through
  /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails.
* Uber's own https://jobs.uber.com/en/jobs/sitemap.xml independently enumerates
  exactly the same 827 job IDs.

Only the 827 requisitions on Uber's CX_1 site (and the exact IDs exposed by
Uber's jobs sitemap) are requested.  No Oracle-wide search, other Oracle
customer site, Beamery talent community or aggregator is used.
"""
from __future__ import annotations

from hashlib import sha256
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.uber.com/us/en/careers/list/"
OFFICIAL_FRONTEND = "https://jobs.uber.com/en/jobs/"
SITEMAP_INDEX = "https://jobs.uber.com/sitemap.xml"
JOBS_SITEMAP = "https://jobs.uber.com/en/jobs/sitemap.xml"
JOB_URL = "https://jobs.uber.com/en/jobs/{job_id}/"

ORACLE_HOST = "iaziqy.fa.ocs.oraclecloud.com"
ORACLE_SITE_CODE = "UberCareers"
ORACLE_SITE_NUMBER = "CX_1"
ORACLE_BASE = f"https://{ORACLE_HOST}/hcmUI/CandidateExperience/en/sites/{ORACLE_SITE_CODE}"
ORACLE_LISTING_URL = ORACLE_BASE + "/jobs"
ORACLE_JOB_URL = ORACLE_BASE + "/job/{job_id}"
ORACLE_APPLY_URL = ORACLE_BASE + "/job/{job_id}/apply/email"
ORACLE_SETTINGS_URL = (
    f"https://{ORACLE_HOST}/hcmRestApi/CandidateExperience/en/siteSettings/{ORACLE_SITE_NUMBER}"
)
JOBS_API = f"https://{ORACLE_HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
JOBS_API_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAIL_API = f"https://{ORACLE_HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
DETAIL_API_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
SITE_SETTINGS_PATH = f"/hcmRestApi/CandidateExperience/en/siteSettings/{ORACLE_SITE_NUMBER}"

EMPLOYER = "Uber Technologies, Inc."
PAGE_SIZE = 25
SORT_BY = "POSTING_DATES_DESC"
FACETS = ("WORK_LOCATIONS;WORKPLACE_TYPES;TITLES;CATEGORIES;ORGANIZATIONS;"
          "POSTING_DATES;FLEX_FIELDS;LOCATIONS")
EXPAND = (
    "requisitionList.workLocation,requisitionList.otherWorkLocations,"
    "requisitionList.secondaryLocations,flexFieldsFacet.values,"
    "requisitionList.requisitionFlexFields"
)
DESCRIPTION_METHOD = "uber.oracle.cx1.recruitingCEJobRequisitionDetails.external_description_sections"
DESCRIPTION_FIELDS = (
    "ExternalDescriptionStr",
    "ExternalResponsibilitiesStr",
    "ExternalQualificationsStr",
    "CorporateDescriptionStr",
)
ROLE_DESCRIPTION_FIELDS = (
    "ExternalDescriptionStr",
    "ExternalResponsibilitiesStr",
    "ExternalQualificationsStr",
)
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
JSON_HEADERS = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
XML_HEADERS = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "application/xml,text/xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
JOB_SITEMAP_ENTRY = re.compile(r"^/en/jobs/(\d+)/$")
ORACLE_SHELL_PATH = f"/hcmUI/CandidateExperience/en/sites/{ORACLE_SITE_CODE}/jobs"
ORACLE_SHELL_MARKER = "static.oracle.com/cdn/fa/oj-hcm-ce/"
HEADING_RE = re.compile(
    r"^(?:about (?:the )?(?:role|team)(?: and team)?|what you(?:’|')?ll do:?|"
    r"what you will do:?|responsibilities:?|key responsibilities:?|"
    r"basic qualifications:?|preferred qualifications:?|minimum qualifications:?|"
    r"qualifications:?|requirements:?|who you are:?|the role:?|day to day:?|"
    r"what we(?:’|')?re looking for:?|about uber:?)$",
    re.I,
)
LOCATION_SALARY_BLOCK = re.compile(r"(?=For [A-Z][^:]{2,90}-based roles?:)")
BASE_SALARY_RE = re.compile(
    r"base (?:salary|hourly rate)(?: amount)?(?: range)?(?: for this role)? (?:is|of) "
    r"(?:([A-Z]{3})\s*)?\$?\s*([\d,]+(?:\.\d+)?)(?:\s*per\s*(year|hour|month))?\s*-\s*"
    r"(?:([A-Z]{3})\s*)?\$?\s*([\d,]+(?:\.\d+)?)(?:\s*per\s*(year|hour|month))?",
    re.I,
)
BASE_SINGLE_RE = re.compile(
    r"base (?:salary|hourly rate)(?: amount)?(?: range)?(?: for this role)? is "
    r"(?:([A-Z]{3})\s*)?\$?\s*([\d,]+(?:\.\d+)?)\s*per\s*(year|hour|month)",
    re.I,
)
OTE_SALARY_RE = re.compile(
    r"total (?:annualized )?on-target earnings \(OTE\)(?: range)? for this position is "
    r"(?:([A-Z]{3})\s*)?\$?\s*([\d,]+(?:\.\d+)?)\s*-\s*"
    r"(?:([A-Z]{3})\s*)?\$?\s*([\d,]+(?:\.\d+)?)",
    re.I,
)
SALARY_HINT_RE = re.compile(
    r"base (?:salary|hourly rate)|on-target earnings|\bOTE\b|salary range|"
    r"compensation|per year|per hour",
    re.I,
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _sha(value: str | bytes | None) -> str:
    data = value.encode() if isinstance(value, str) else (value or b"")
    return sha256(data).hexdigest()


def parse_official_entry(*, status: int, final_url: str, redirects: list[str],
                         headers: dict, body: str) -> dict:
    """Validate the inspected www.uber.com -> jobs.uber.com redirect and its WAF answer."""
    entry = urlsplit(OFFICIAL_ENTRY)
    final = urlsplit(final_url)
    chain = [str(value) for value in redirects]
    if chain and chain[0] != OFFICIAL_ENTRY:
        raise ValueError("Uber official entry redirect chain does not start at the inspected entry")
    if final.scheme != "https" or final.netloc != "jobs.uber.com" or final.path.rstrip("/") != "/en/jobs":
        raise ValueError("Uber official entry no longer redirects to jobs.uber.com/en/jobs/")
    if entry.scheme != "https" or entry.netloc != "www.uber.com":
        raise ValueError("Uber official entry constant changed")
    soup = BeautifulSoup(body or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    lowered = (body or "").lower()
    mitigated = (headers.get("cf-mitigated") or headers.get("CF-Mitigated") or "").lower()
    challenge = bool(
        status in (403, 429)
        and (mitigated == "challenge" or "just a moment" in lowered
             or "performing security verification" in lowered
             or "challenges.cloudflare.com" in lowered)
    )
    visible_count = None
    for pattern in (r"Job search results\s+([\d,]+)", r"([\d,]+)\s+OPEN JOBS"):
        match = re.search(pattern, body or "", re.I)
        if match:
            visible_count = int(match.group(1).replace(",", ""))
            break
    return {
        "requested_url": OFFICIAL_ENTRY,
        "final_url": final_url,
        "redirect_chain": chain,
        "status": status,
        "title": title,
        "frontend_accessible": status == 200 and not challenge,
        "challenge": ({
            "kind": "cloudflare_challenge",
            "http_status": status,
            "cf_mitigated": mitigated or None,
            "title": title,
            "body_marker": ("performing security verification" if "performing security verification" in lowered
                            else "just a moment" if "just a moment" in lowered else None),
            "bypass_attempted": False,
            "body_bytes": len((body or "").encode()),
            "body_sha256": _sha(body or ""),
        } if challenge else None),
        "visible_count_text": visible_count,
        "body_bytes": len((body or "").encode()),
        "body_sha256": _sha(body or ""),
    }


def parse_sitemap_index(xml_text: str, response_url: str = SITEMAP_INDEX) -> dict:
    locs = [value.strip() for value in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml_text or "", re.S)]
    if not locs:
        raise ValueError("Uber jobs sitemap index has no <loc> entries")
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "jobs.uber.com" or parsed.path != "/sitemap.xml":
        raise ValueError("Uber sitemap index response came from an unexpected URL")
    if JOBS_SITEMAP not in locs:
        raise ValueError("Uber sitemap index no longer lists the jobs sitemap")
    return {"url": response_url, "children": locs, "jobs_sitemap": JOBS_SITEMAP}


def parse_jobs_sitemap(xml_text: str, response_url: str = JOBS_SITEMAP) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "jobs.uber.com" or parsed.path != "/en/jobs/sitemap.xml":
        raise ValueError("Uber jobs sitemap response came from an unexpected URL")
    rows, seen = [], set()
    for match in re.finditer(r"<url>\s*<loc>(.*?)</loc>(?:\s*<lastmod>(.*?)</lastmod>)?\s*</url>",
                             xml_text or "", re.S):
        loc, lastmod = match.group(1).strip(), (match.group(2) or "").strip() or None
        target = urlsplit(loc)
        route = JOB_SITEMAP_ENTRY.fullmatch(target.path)
        if target.scheme != "https" or target.netloc != "jobs.uber.com" or route is None:
            raise ValueError("Uber jobs sitemap contains an unexpected job URL")
        identity = route.group(1)
        if identity in seen:
            raise ValueError(f"Uber jobs sitemap repeats job id {identity}")
        seen.add(identity)
        rows.append({"id": identity, "url": loc, "lastmod": lastmod})
    if not rows:
        raise ValueError("Uber jobs sitemap has no job URLs")
    return {
        "url": response_url,
        "rows": rows,
        "ids": [row["id"] for row in rows],
        "count": len(rows),
        "with_lastmod": sum(bool(row["lastmod"]) for row in rows),
        "lastmod_note": "sitemap lastmod is a crawl/refresh timestamp, not the posting date",
    }


def parse_oracle_shell(html: str, response_url: str = ORACLE_LISTING_URL) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != ORACLE_HOST
            or parsed.path.rstrip("/") != ORACLE_SHELL_PATH):
        raise ValueError("Uber Oracle careers listing shell redirected away from the inspected route")
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    og = soup.select_one('meta[property="og:title"]')
    og_title = (og.get("content") or "").strip() if og else ""
    if "Uber" not in title and "Uber Careers" not in og_title:
        raise ValueError("Uber Oracle careers listing shell no longer identifies Uber Careers")
    if ORACLE_SHELL_MARKER not in (html or ""):
        raise ValueError("Uber Oracle careers listing shell is not the Oracle Candidate Experience app")
    site_numbers = set(re.findall(r"siteNumber=([A-Z]{2}_\d+)", html or ""))
    if site_numbers != {ORACLE_SITE_NUMBER}:
        raise ValueError("Uber Oracle listing shell site number changed")
    return {
        "url": response_url,
        "title": title,
        "og_title": og_title,
        "site_number": ORACLE_SITE_NUMBER,
        "site_code": ORACLE_SITE_CODE,
        "oracle_hcm_marker": ORACLE_SHELL_MARKER,
    }


def parse_site_settings(value: dict) -> dict:
    app = value.get("app") if isinstance(value, dict) else None
    if not isinstance(app, dict):
        raise ValueError("Uber Oracle site settings are missing the app block")
    if app.get("siteName") != "Uber":
        raise ValueError("Uber Oracle site settings no longer identify siteName=Uber")
    if app.get("siteCode") != ORACLE_SITE_CODE or app.get("siteURLName") != ORACLE_SITE_CODE:
        raise ValueError("Uber Oracle site settings site code changed from UberCareers")
    if app.get("siteNumber") != ORACLE_SITE_NUMBER:
        raise ValueError("Uber Oracle site settings site number changed from CX_1")
    if app.get("siteLang") != "en":
        raise ValueError("Uber Oracle site settings language changed from en")
    base = urlsplit(str(app.get("apiBaseUrl") or ""))
    if base.scheme != "https" or base.hostname != ORACLE_HOST:
        raise ValueError("Uber Oracle site settings API host changed")
    return {
        "site_name": app.get("siteName"),
        "site_code": app.get("siteCode"),
        "site_url_name": app.get("siteURLName"),
        "site_number": app.get("siteNumber"),
        "site_lang": app.get("siteLang"),
        "api_base_url": app.get("apiBaseUrl"),
        "cdn_url": app.get("cdnUrl"),
        "languages": app.get("languages") or [],
        "logo_url": (app.get("images") or {}).get("logoImageUrl"),
    }


def listing_params(offset: int) -> dict:
    finder = (f"findReqs;siteNumber={ORACLE_SITE_NUMBER},facetsList={FACETS},"
              f"limit={PAGE_SIZE},sortBy={SORT_BY}")
    if offset:
        finder += f",offset={offset}"
    return {"onlyData": "true", "expand": EXPAND, "finder": finder}


def detail_params(job_id: str) -> dict:
    return {"onlyData": "true", "expand": "all",
            "finder": f'ById;Id="{job_id}",siteNumber={ORACLE_SITE_NUMBER}'}


def parse_listing_page(value: dict, *, offset: int, limit: int = PAGE_SIZE) -> dict:
    items = value.get("items") if isinstance(value, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("Uber Oracle listing response no longer has exactly one result block")
    block = items[0]
    if block.get("SiteNumber") != ORACLE_SITE_NUMBER:
        raise ValueError("Uber Oracle listing response site number changed")
    if int(block.get("Offset", -1)) != offset:
        raise ValueError("Uber Oracle listing response offset changed from the request")
    if int(block.get("Limit", -1)) != limit:
        raise ValueError("Uber Oracle listing response limit changed from the request")
    if block.get("SortBy") != SORT_BY:
        raise ValueError("Uber Oracle listing sort changed from POSTING_DATES_DESC")
    total = block.get("TotalJobsCount")
    if not isinstance(total, int) or total < 0:
        raise ValueError("Uber Oracle listing response lost TotalJobsCount")
    rows = block.get("requisitionList")
    if not isinstance(rows, list):
        raise ValueError("Uber Oracle listing response omitted requisitionList")
    ids = []
    for row in rows:
        identity = str(row.get("Id") or "")
        if not identity or not normalized_title(row.get("Title")):
            raise ValueError("Uber Oracle listing row lost its Id or Title")
        ids.append(identity)
    if len(ids) != len(set(ids)):
        raise ValueError("Uber Oracle listing page repeats a requisition Id")
    if offset + len(rows) > total and total >= 0:
        raise ValueError("Uber Oracle listing page exceeds the advertised total")

    def facet(name):
        values = block.get(name)
        return [
            {"id": item.get("Id"), "name": item.get("Name"), "count": item.get("TotalCount")}
            for item in values or [] if isinstance(item, dict)
        ] if isinstance(values, list) else []

    return {
        "total": total,
        "rows": rows,
        "ids": ids,
        "returned": len(rows),
        "offset": offset,
        "limit": limit,
        "sort_by": block.get("SortBy"),
        "has_more": bool(value.get("hasMore", offset + len(rows) < total)),
        "facets": {
            "organizations": facet("organizationsFacet"),
            "workplace_types": facet("workplaceTypesFacet"),
            "categories": facet("categoriesFacet"),
            "locations": facet("locationsFacet"),
            "work_locations": facet("workLocationsFacet"),
            "posting_dates": facet("postingDatesFacet"),
        },
    }


def parse_detail(value: dict, *, job_id: str) -> dict:
    items = value.get("items") if isinstance(value, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError(f"Uber Oracle detail for {job_id} no longer has exactly one record")
    row = items[0]
    if str(row.get("Id")) != str(job_id):
        raise ValueError(f"Uber Oracle detail returned a different posting than {job_id}")
    if not normalized_title(row.get("Title")):
        raise ValueError(f"Uber Oracle detail for {job_id} lost its Title")
    return row


def _is_placeholder_role_text(text: str) -> bool:
    """True when an employer field is empty or only a placeholder/token."""
    lowered = re.sub(r"\s+", " ", text or "").strip().lower()
    if not lowered:
        return True
    if lowered in {"tbc", "tbd", "tba", "na", "n/a", "todo", "coming soon", "to be confirmed"}:
        return True
    if re.fullmatch(r"tbc[\s~.]*", lowered):
        return True
    if re.fullmatch(r"[\W_~]+", lowered):
        return True
    if len(lowered) < 20:
        return True
    return False


def _is_compensation_only(text: str) -> bool:
    """True when a responsibilities field is only the salary/OTE paragraph."""
    lowered = (text or "").lower()
    if not lowered.strip():
        return True
    role_markers = (
        "about the role", "about the team", "what you", "responsibilit", "qualification",
        "requirement", "who you are", "experience you", "day to day", "the team",
        "ポジション", "主な業務", "求める",
    )
    if any(marker in lowered for marker in role_markers):
        return False
    compensation_markers = (
        "on-target earnings", "base salary", "ote", "compensation", "per year",
        "per hour", "salary range",
    )
    return any(marker in lowered for marker in compensation_markers)


def _role_description_present(row: dict) -> bool:
    """A posting counts as having a role body only when a non-placeholder,
    non-compensation-only role field exists. Corporate boilerplate alone does
    not qualify."""
    external = text_html(row.get("ExternalDescriptionStr") or "").strip()
    if external and not _is_placeholder_role_text(external):
        return True
    qualifications = text_html(row.get("ExternalQualificationsStr") or "").strip()
    if qualifications and not _is_placeholder_role_text(qualifications):
        return True
    responsibilities = text_html(row.get("ExternalResponsibilitiesStr") or "").strip()
    if (responsibilities and not _is_placeholder_role_text(responsibilities)
            and not _is_compensation_only(responsibilities)):
        return True
    return False


def description_blocks(row: dict) -> list[dict]:
    blocks = []
    for field in DESCRIPTION_FIELDS:
        html = row.get(field) or ""
        if not isinstance(html, str) or not html.strip():
            continue
        text = text_html(html)
        blocks.append({
            "field": field,
            "html": html,
            "text": text,
            "chars": len(text),
            "sha256": _sha(html),
        })
    return blocks


def build_description(row: dict) -> tuple[str, str, list[dict]]:
    blocks = description_blocks(row)
    if not blocks:
        return "", "", []
    html = "\n".join(block["html"] for block in blocks)
    return html, text_html(html), blocks


def description_sections(html: str) -> list[dict]:
    """Verbatim employer headings and their text; no synthesised content."""
    text = text_html(html or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    sections: list[dict] = []
    current = None
    for line in lines:
        if HEADING_RE.fullmatch(line):
            if current is not None:
                sections.append(current)
            current = {"heading": line.rstrip(":"), "text": ""}
        elif current is not None:
            current["text"] = (current["text"] + "\n" + line).strip()
    if current is not None:
        sections.append(current)
    return [section for section in sections if section["heading"]]


def _amount(value: str):
    cleaned = re.sub(r"[^\d.]", "", value or "")
    if not cleaned:
        return None
    number = float(cleaned)
    return int(number) if number.is_integer() else round(number, 2)


def parse_salary(row: dict) -> dict | None:
    source = row.get("ExternalResponsibilitiesStr") or ""
    text = text_html(source)
    if not text:
        return None
    # Uber writes EUR/GBP amounts both with a code and with a symbol; keep one form.
    text = text.replace("€", "EUR ").replace("£", "GBP ")
    ranges = []

    def add(kind: str, location: str | None, currency: str | None, low: str,
            high: str, low_period: str | None = None, high_period: str | None = None):
        low_period = (low_period or "").lower() or None
        high_period = (high_period or "").lower() or None
        if low_period is None:
            low_period = high_period
        if high_period is None:
            high_period = low_period
        ranges.append({
            "kind": kind,
            "location": location,
            "currency": currency or "USD",
            "min": _amount(low),
            "max": _amount(high),
            "min_period": low_period,
            "max_period": high_period,
        })

    for chunk in LOCATION_SALARY_BLOCK.split(text):
        match = re.match(r"For ([A-Z][^:]{2,90})-based roles?: (.*)", chunk, re.S)
        if not match:
            continue
        location, body = match.group(1).strip(), match.group(2)
        found_range = False
        for match in BASE_SALARY_RE.finditer(body):
            found_range = True
            add("base_salary", location, match.group(1) or match.group(4), match.group(2),
                match.group(5), match.group(3), match.group(6))
        if not found_range:
            for match in BASE_SINGLE_RE.finditer(body):
                add("base_salary", location, match.group(1), match.group(2),
                    match.group(2), match.group(3), match.group(3))
        for match in OTE_SALARY_RE.finditer(body):
            add("total_on_target_earnings", location, match.group(1) or match.group(3),
                match.group(2), match.group(4))
    if not ranges:
        found_range = False
        for match in BASE_SALARY_RE.finditer(text):
            found_range = True
            add("base_salary", None, match.group(1) or match.group(4), match.group(2),
                match.group(5), match.group(3), match.group(6))
        if not found_range:
            for match in BASE_SINGLE_RE.finditer(text):
                add("base_salary", None, match.group(1), match.group(2),
                    match.group(2), match.group(3), match.group(3))
        for match in OTE_SALARY_RE.finditer(text):
            add("total_on_target_earnings", None, match.group(1) or match.group(3),
                match.group(2), match.group(4))
    if not ranges:
        if not SALARY_HINT_RE.search(text):
            return None
        # Retain the employer's exact compensation paragraph when no range regex applies.
        return {"source_field": "ExternalResponsibilitiesStr", "source_text": text, "ranges": []}
    return {"source_field": "ExternalResponsibilitiesStr", "source_text": text, "ranges": ranges}


def location_names(listing_row: dict, detail: dict) -> list[str]:
    values = []
    primary = detail.get("PrimaryLocation") or listing_row.get("PrimaryLocation")
    if primary:
        values.append(normalized_title(primary))
    for item in detail.get("secondaryLocations") or listing_row.get("secondaryLocations") or []:
        name = normalized_title(item.get("Name")) if isinstance(item, dict) else ""
        if name:
            values.append(name)
    return list(dict.fromkeys(values))


def oracle_job_url(job_id: str) -> str:
    return ORACLE_JOB_URL.format(job_id=job_id)


def oracle_apply_url(job_id: str) -> str:
    return ORACLE_APPLY_URL.format(job_id=job_id)


class Uber(Adapter):
    """Read Uber's CX_1 Oracle careers site and Uber's own jobs sitemap cross-check."""

    VERIFICATION_REVISION = "uber.oracle-cx1-and-jobs-sitemap.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.rows: dict[str, dict] = {}
        self.sitemap_by_id: dict[str, dict] = {}
        self.workplace_type = None
        self.entry = None
        self.site = None

    async def _fetch_once(self, url: str, headers: dict):
        """One recorded observation; intentionally no retry loop (WAF/429 stop rule)."""
        response = await self.http.client.get(url, headers=headers)
        self.http._save(response, "GET", None)
        return response

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self._fetch_once(OFFICIAL_ENTRY, BROWSER_HEADERS)
        redirects = [str(item.url) for item in response.history]
        redirects.append(str(response.url))
        parsed = parse_official_entry(
            status=response.status_code, final_url=str(response.url),
            redirects=redirects, headers=dict(response.headers), body=response.text,
        )
        parsed["request_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        parsed["response_bytes"] = len(response.content)
        parsed["response_sha256"] = _sha(response.content)
        return parsed

    async def _sitemaps(self):
        started = time.perf_counter()
        index_response = await self.http.request("GET", SITEMAP_INDEX, headers=XML_HEADERS)
        index = parse_sitemap_index(index_response.text, str(index_response.url))
        jobs_response = await self.http.request("GET", JOBS_SITEMAP, headers=XML_HEADERS)
        jobs = parse_jobs_sitemap(jobs_response.text, str(jobs_response.url))
        jobs.update({
            "index_response_sha256": _sha(index_response.content),
            "jobs_response_sha256": _sha(jobs_response.content),
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        })
        return index, jobs

    async def _oracle_shell(self):
        started = time.perf_counter()
        shell_response = await self.http.request("GET", ORACLE_LISTING_URL, headers=BROWSER_HEADERS)
        shell = parse_oracle_shell(shell_response.text, str(shell_response.url))
        settings_response = await self.http.request("GET", ORACLE_SETTINGS_URL, headers=JSON_HEADERS)
        settings = parse_site_settings(settings_response.json())
        settings.update({
            "settings_url": str(settings_response.url),
            "settings_response_sha256": _sha(settings_response.content),
            "shell_response_sha256": _sha(shell_response.content),
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        })
        return shell, settings

    async def _listing_snapshot(self):
        started = time.perf_counter()
        rows, seen = [], set()
        pages, issues = [], []
        facets = {}
        total = None
        offset = 0
        while True:
            if not self.page_guard():
                issues.append("uber_listing_page_budget_exhausted")
                break
            request_started = time.perf_counter()
            response = await self.http.request("GET", JOBS_API, params=listing_params(offset),
                                               headers=JSON_HEADERS)
            request_seconds = max(0.0, time.perf_counter() - request_started)
            target = urlsplit(str(response.url))
            if target.scheme != "https" or target.hostname != ORACLE_HOST or target.path != JOBS_API_PATH:
                raise ValueError("Uber Oracle listing request redirected outside the company site")
            page = parse_listing_page(response.json(), offset=offset)
            if offset == 0:
                facets = page["facets"]
            if total is None:
                total = page["total"]
            elif page["total"] != total:
                issues.append("uber_listing_total_changed_during_pagination")
            new_ids = []
            for row in page["rows"]:
                identity = str(row["Id"])
                if identity in seen:
                    issues.append(f"uber_listing_duplicate_id_across_pages:{identity}")
                    continue
                seen.add(identity)
                new_ids.append(identity)
                rows.append(row)
            pages.append({
                "offset": offset,
                "returned": page["returned"],
                "total": page["total"],
                "job_ids": page["ids"],
                "new_ids": len(new_ids),
                "request_seconds": request_seconds,
                "response_bytes": len(response.content),
                "response_sha256": _sha(response.content),
            })
            if total is not None and len(rows) >= total:
                break
            if not page["rows"]:
                issues.append("uber_listing_pagination_stalled")
                break
            offset += len(page["rows"])
        if total is not None and len(rows) != total:
            issues.append("uber_listing_rows_do_not_match_total")
        return {
            "total": total,
            "rows": rows,
            "ids": [str(row["Id"]) for row in rows],
            "pages": pages,
            "page_count": len(pages),
            "facets": facets,
            "issues": list(dict.fromkeys(issues)),
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    async def detail(self, job):
        response = await self.http.request("GET", DETAIL_API, params=detail_params(job.job_id),
                                           headers=JSON_HEADERS)
        target = urlsplit(str(response.url))
        if target.scheme != "https" or target.hostname != ORACLE_HOST or target.path != DETAIL_API_PATH:
            raise ValueError("Uber Oracle detail request redirected outside the company site")
        row = parse_detail(response.json(), job_id=str(job.job_id))
        listing = self.rows.get(str(job.job_id))
        if listing is None:
            raise ValueError(f"Uber detail {job.job_id} has no matching listing row")
        posted = row.get("ExternalPostedStartDate") or ""
        listed = listing.get("PostedDate") or ""
        if posted and listed and posted[:10] != listed:
            # A different posting date is a hard identity problem, not localisation.
            self.result.errors.append(f"uber_detail_posted_date_differs_from_listing:{job.job_id}")
        return self._make_job(listing, row, description_source_url=str(response.request.url))


    def _make_job(self, listing: dict, detail: dict, *, description_source_url: str):
        identity = str(detail["Id"])
        description_html, description_text, blocks = build_description(detail)
        role_present = _role_description_present(detail)
        sections = description_sections(description_html)
        qualifications = [s for s in sections
                          if re.search(r"qualif|requirement|what you(?:’|')?ll need|who you are",
                                       s["heading"], re.I)] or None
        responsibilities = [s for s in sections
                            if re.search(r"what you(?:’|')?ll do|responsibilit|day to day",
                                         s["heading"], re.I)] or None
        salary = parse_salary(detail)
        sitemap_row = self.sitemap_by_id.get(identity) or {}
        job = self.job(
            {
                "listing": listing,
                "detail": detail,
                "description_fields": [
                    {key: block[key] for key in ("field", "chars", "sha256")} for block in blocks
                ],
                "detail_api_url": description_source_url,
                "oracle_job_url": oracle_job_url(identity),
                "official_job_url": JOB_URL.format(job_id=identity),
                "sitemap_lastmod": sitemap_row.get("lastmod"),
                "sitemap_lastmod_is_not_posting_date": True,
                "requisition_id": detail.get("RequisitionId"),
                "description_sections": sections,
                "work_location_names": [
                    item.get("LocationName") for item in detail.get("workLocation") or []
                    if isinstance(item, dict) and item.get("LocationName")
                ],
                "other_work_location_names": [
                    item.get("LocationName") for item in detail.get("otherWorkLocations") or []
                    if isinstance(item, dict) and item.get("LocationName")
                ],
                "employer_source": "Oracle listing organizationsFacet: Uber Technologies, Inc. (827/827)",
                "workplace_type_source": "Oracle listing workplaceTypesFacet: On-site (827/827)",
            },
            title=normalized_title(detail.get("Title") or listing.get("Title")),
            url=JOB_URL.format(job_id=identity),
            job_id=identity,
            requisition_id=(str(detail["RequisitionId"]) if detail.get("RequisitionId") is not None else None),
            apply_url=oracle_apply_url(identity),
            description=description_html,
            method=DESCRIPTION_METHOD,
            description_url=description_source_url,
            locations=location_names(listing, detail),
            department=detail.get("Category") or listing.get("Category"),
            team=detail.get("JobFunction"),
            employment_type=detail.get("JobSchedule"),
            workplace_type=self.workplace_type,
            date_posted=detail.get("ExternalPostedStartDate") or listing.get("PostedDate"),
            date_posted_raw={"listing_PostedDate": listing.get("PostedDate"),
                             "detail_ExternalPostedStartDate": detail.get("ExternalPostedStartDate")},
            date_updated=None,
            valid_through=detail.get("ExternalPostedEndDate") or detail.get("PostingEndDate") or listing.get("PostingEndDate"),
            salary=salary,
            qualifications=qualifications,
            responsibilities=responsibilities,
            experience=None,
            education=detail.get("StudyLevel"),
            employer=EMPLOYER,
            language=detail.get("ContentLocale") or "en",
        )
        job.description_text = description_text
        if not role_present:
            job.description_unavailable = True
            job.warnings.append("role_description_fields_empty_company_boilerplate_only")
        if not blocks:
            job.description_unavailable = True
            job.warnings.append("all_oracle_description_fields_empty")
        job.company_scope_verified = self.company.scope == "company"
        return job

    def _rebuild_from_raw(self):
        """Recompute every record from its retained listing+detail JSON (also on resume)."""
        rebuilt = []
        for job in self.result.jobs:
            listing = self.rows.get(str(job.job_id)) or job.raw.get("listing")
            detail = job.raw.get("detail")
            if not listing or not detail:
                rebuilt.append(job)
                continue
            source_url = job.description_source_url or job.raw.get("detail_api_url")
            fresh = self._make_job(listing, detail, description_source_url=source_url)
            fresh.fetched_at = job.fetched_at
            fresh.record_identity = job.record_identity
            rebuilt.append(fresh)
        self.result.jobs[:] = rebuilt

    def _audit_listing_detail_fields(self):
        """Record listing-vs-detail differences without hiding the detail snapshot."""
        differences = []
        for job in self.result.jobs:
            listing = job.raw.get("listing") or {}
            detail = job.raw.get("detail") or {}
            if not detail:
                continue
            title_matches = normalized_title(listing.get("Title")) == normalized_title(detail.get("Title"))
            location_listing = normalized_title(listing.get("PrimaryLocation"))
            location_detail = normalized_title(detail.get("PrimaryLocation"))
            location_matches = (not location_listing or not location_detail
                                or location_listing == location_detail)
            posted_listing = (listing.get("PostedDate") or "")[:10]
            posted_detail = (detail.get("ExternalPostedStartDate") or "")[:10]
            posted_matches = not posted_listing or not posted_detail or posted_listing == posted_detail
            job.raw["listing_detail_comparison"] = {
                "title_matches": title_matches,
                "primary_location_text_matches": location_matches,
                "posted_date_matches": posted_matches,
                "resolution": "detail API is the job-page authority; listing index may lag or localise fields",
            }
            if not title_matches:
                job.warnings.append("listing_title_differs_from_detail")
                differences.append({
                    "job_id": job.job_id,
                    "field": "Title",
                    "listing": listing.get("Title"),
                    "detail": detail.get("Title"),
                    "resolution": "Retained detail Title; listing index is stale for this ID on the live site.",
                })
            if not location_matches:
                job.warnings.append("listing_primary_location_text_differs_from_detail")
                differences.append({
                    "job_id": job.job_id,
                    "field": "PrimaryLocation",
                    "listing": listing.get("PrimaryLocation"),
                    "detail": detail.get("PrimaryLocation"),
                    "resolution": "Retained detail PrimaryLocation; the listing localises the same place name.",
                })
            if not posted_matches:
                differences.append({
                    "job_id": job.job_id,
                    "field": "PostedDate",
                    "listing": listing.get("PostedDate"),
                    "detail": detail.get("ExternalPostedStartDate"),
                    "resolution": "Recorded as an error; detail and listing disagree on posting date.",
                })
        self.result.metadata["listing_detail_field_differences"] = differences
        for item in differences:
            if item["field"] == "Title":
                self.result.warnings.append(f"uber_listing_title_differs_from_detail:{item['job_id']}")
            elif item["field"] == "PrimaryLocation":
                self.result.warnings.append(f"uber_listing_location_differs_from_detail:{item['job_id']}")
        return differences

    async def run(self):
        started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_frontend_url": OFFICIAL_FRONTEND,
            "listing_url": ORACLE_LISTING_URL,
            "listing_authority": (
                "Uber's own Oracle Recruiting Cloud Candidate Experience site UberCareers (CX_1) on "
                "iaziqy.fa.ocs.oraclecloud.com, referenced by the jobs.uber.com shell; the official "
                "jobs.uber.com sitemap independently enumerates the same 827 job IDs."
            ),
            "listing_request": {
                "method": "GET",
                "path": JOBS_API_PATH,
                "query_shape": {
                    "onlyData": "true",
                    "expand": EXPAND,
                    "finder": f"findReqs;siteNumber={ORACLE_SITE_NUMBER},facetsList={FACETS},"
                              f"limit={PAGE_SIZE},sortBy={SORT_BY}",
                },
                "pagination": "27 observed UI rows/page sizes in 25-step infinite scroll; adapter uses the same finder in 25-row pages.",
                "filters_applied": False,
                "scope": "Only siteNumber=CX_1 (UberCareers); no other Oracle site or board is queried.",
            },
            "detail_method": (
                "Full Employer-authored ExternalDescriptionStr + ExternalResponsibilitiesStr + "
                "ExternalQualificationsStr + CorporateDescriptionStr from Uber's CX_1 "
                "recruitingCEJobRequisitionDetails API; representative rendered pages are checked independently."
            ),
            "request_scope": "Only iaziqy.fa.ocs.oraclecloud.com UberCareers CX_1 and Uber's jobs.uber.com sitemap/entry.",
        })
        try:
            entry = await self._official_entry()
            self.entry = entry
            self.result.metadata["official_entry"] = entry
            self.result.timings["official_entry_seconds"] = entry["request_seconds"]
            if entry["challenge"]:
                self.result.metadata["official_frontend_block"] = entry["challenge"]
                self.result.warnings.append("official_frontend_cloudflare_challenge_recorded")

            sitemap_index, sitemap = await self._sitemaps()
            self.result.timings["sitemap_seconds"] = sitemap["seconds"]
            self.result.metadata["sitemap"] = {
                "index": sitemap_index,
                "jobs": {key: value for key, value in sitemap.items() if key != "rows"},
                "ids": sitemap["ids"],
            }
            self.sitemap_by_id = {row["id"]: row for row in sitemap["rows"]}

            shell, settings = await self._oracle_shell()
            self.site = settings
            self.result.timings["oracle_shell_seconds"] = settings["seconds"]
            self.result.metadata["oracle"] = {"shell": shell, "site_settings": settings}

            initial = await self._listing_snapshot()
            self.result.timings["listing_phase_seconds"] = initial["seconds"]
            self.result.metadata["initial_listing"] = {key: value for key, value in initial.items() if key != "rows"}
            self.result.expected_total = initial["total"]
            if initial["issues"]:
                self.result.errors.extend(initial["issues"])
            if initial["total"] != sitemap["count"]:
                self.result.errors.append("uber_oracle_total_differs_from_jobs_sitemap_count")
            oracle_ids = initial["ids"]
            sitemap_ids = sitemap["ids"]
            if initial["total"] is not None and len(oracle_ids) != initial["total"]:
                self.result.errors.append("uber_oracle_listing_rows_do_not_match_total")
            if set(oracle_ids) != set(sitemap_ids):
                missing_sitemap = sorted(set(oracle_ids) - set(sitemap_ids))
                missing_oracle = sorted(set(sitemap_ids) - set(oracle_ids))
                self.result.errors.append("uber_oracle_and_sitemap_membership_mismatch")
                self.result.metadata["membership_difference"] = {
                    "missing_from_sitemap": missing_sitemap,
                    "missing_from_oracle": missing_oracle,
                }
            facets = initial["facets"]
            organizations = {item["name"]: item["count"] for item in facets.get("organizations", [])}
            workplaces = {item["name"]: item["count"] for item in facets.get("workplace_types", [])}
            if organizations:
                if organizations.get(EMPLOYER) != initial["total"]:
                    self.result.errors.append("uber_organization_facet_no_longer_covers_all_rows")
                self.result.metadata["employer_facet"] = organizations
            if workplaces:
                if len(workplaces) != 1 or next(iter(workplaces.values())) != initial["total"]:
                    self.result.errors.append("uber_workplace_facet_no_longer_single_on_site")
                self.workplace_type = next(iter(workplaces), None)
            self.result.metadata["workplace_type"] = self.workplace_type
            self.result.metadata["facets"] = facets

            self.rows = {str(row["Id"]): row for row in initial["rows"]}
            for identity in oracle_ids:
                if self.limit():
                    break
                row = self.rows[identity]
                self.add(self.job(
                    {"listing": row, "sitemap_lastmod": self.sitemap_by_id.get(identity, {}).get("lastmod")},
                    title=normalized_title(row.get("Title")),
                    url=JOB_URL.format(job_id=identity),
                    job_id=identity,
                    requisition_id=None,
                    apply_url=oracle_apply_url(identity),
                    locations=location_names(row, row),
                    department=row.get("Category"),
                    date_posted=row.get("PostedDate"),
                    date_posted_raw={"listing_PostedDate": row.get("PostedDate")},
                    employer=EMPLOYER,
                ))
        except Exception as exc:
            self.result.errors.append(f"Uber source collection failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
            return self.result

        detail_started = time.perf_counter()
        try:
            await self.hydrate(self.result.jobs, self.detail)
        except Exception as exc:
            self.result.errors.append(f"Uber detail hydration failed: {type(exc).__name__}: {exc}")
        self.result.timings["detail_wall_seconds"] = max(0.0, time.perf_counter() - detail_started)
        self._rebuild_from_raw()
        self._audit_listing_detail_fields()

        final = None
        final_started = time.perf_counter()
        try:
            final_listing = await self._listing_snapshot()
            final_index, final_sitemap = await self._sitemaps()
            final = {"listing": final_listing, "sitemap": final_sitemap, "index": final_index}
        except Exception as exc:
            self.result.errors.append(f"Uber final source recheck failed: {type(exc).__name__}: {exc}")
        self.result.timings["final_recheck_seconds"] = max(0.0, time.perf_counter() - final_started)
        if final is not None:
            final_listing = final["listing"]
            final_ids = final_listing["ids"]
            initial_by_id = self.rows
            final_by_id = {str(row["Id"]): row for row in final_listing["rows"]}
            changed = {}
            for identity, row in initial_by_id.items():
                other = final_by_id.get(identity)
                if other is None:
                    continue
                keys = ("Title", "PostedDate", "PrimaryLocation", "PostingEndDate")
                diff = [key for key in keys if row.get(key) != other.get(key)]
                if diff:
                    changed[identity] = diff
            membership = {
                "initial_count": initial["total"],
                "final_count": final_listing["total"],
                "initial_rows": len(initial["ids"]),
                "final_rows": len(final_ids),
                "initial_sitemap_count": sitemap["count"],
                "final_sitemap_count": final["sitemap"]["count"],
                "same_ids_and_order": final_ids == initial["ids"],
                "same_sitemap_ids_and_order": final["sitemap"]["ids"] == sitemap["ids"],
                "changed_listing_fields_by_job_id": changed,
                "new_job_ids": [identity for identity in final_ids if identity not in initial["ids"]],
                "removed_job_ids": [identity for identity in initial["ids"] if identity not in final_ids],
                "initial_issues": initial["issues"],
                "final_issues": final_listing["issues"],
                "complete": False,
            }
            membership["complete"] = (
                final_listing["total"] == initial["total"]
                and final_ids == initial["ids"]
                and final["sitemap"]["ids"] == sitemap["ids"]
                and not final_listing["issues"]
                and not changed
            )
            self.result.metadata["final_listing"] = {key: value for key, value in final_listing.items() if key != "rows"}
            self.result.metadata["final_sitemap"] = {key: value for key, value in final["sitemap"].items() if key != "rows"}
            self.result.metadata["membership_reconciliation"] = membership
            if not membership["complete"]:
                self.result.errors.append("uber_membership_changed_during_collection")
            if changed:
                self.result.errors.append("uber_listing_fields_changed_during_collection")
            self.result.errors.extend(final_listing["issues"])
        else:
            self.result.metadata["membership_reconciliation"] = {"complete": False,
                                                               "reason": "final recheck failed"}

        detail_rows = sum(1 for job in self.result.jobs if job.raw.get("detail"))
        self.result.metadata["job_ids"] = [job.job_id for job in self.result.jobs]
        self.result.metadata["detail_hydration_mode"] = (
            "Per-job recruitingCEJobRequisitionDetails requests for all 827 company-scoped IDs."
        )
        self.result.metadata["detail_rows"] = detail_rows
        self.result.metadata["current_native_membership_reconciled"] = bool(
            self.result.metadata.get("membership_reconciliation", {}).get("complete")
        )
        self.result.listing_complete = bool(
            self.result.metadata.get("membership_reconciliation", {}).get("complete")
            and self.result.expected_total is not None
            and len(self.result.jobs) == self.result.expected_total
            and not self.result.errors
        )
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
        return self.result
