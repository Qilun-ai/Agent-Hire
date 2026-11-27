"""LTM (formerly L&T Infotech / LTIMindtree) own careers journey.

The official entry ``https://www.ltimindtree.com/careers/`` currently answers
with a 301 chain to ``https://www.ltm.com/careers``.  That page publishes five
regional job links, not one global board:

* India                 -> ltm.ripplehire.com token xviy... #list/geo=India
* United States         -> ltm.ripplehire.com token nP1...  #list/geo=Canada,United States
* Canada                -> the same token's Canada subset
* Europe                -> ltm.ripplehire.com token xviy... European country list
* Everywhere else       -> careers.ltm.com/search/ (SuccessFactors Career Site Builder)

The RippleHire links are the company's own candidate portal.  Its SPA calls
``POST /candidate/candidatejobsearch`` with a company token and the published
geo filter; ``GET /candidate/candidatejobdetail`` is the exact detail response.
Both endpoints are constrained to the token's company.  The SuccessFactors
board is the company-published "Everywhere else" list and is parsed from its
unfiltered server-rendered search pages.

Only the full native fields are treated as descriptions: RippleHire's
``jobDesc`` + ``jobSkills`` + ``otherDetails`` + ``compensationRange`` and
SuccessFactors' ``jobdescription`` block.  Footers, cookie text, application
forms and the global "About LTM" panel are never descriptions.
"""
from __future__ import annotations

from datetime import datetime
from html import escape, unescape
import json
import re
import time
import unicodedata
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.ltimindtree.com/careers/"
OFFICIAL_CANONICAL = "https://www.ltm.com/careers"
RIPPLE_ORIGIN = "https://ltm.ripplehire.com"
RIPPLE_CANDIDATE = RIPPLE_ORIGIN + "/candidate/"
RIPPLE_SEARCH_API = RIPPLE_ORIGIN + "/candidate/candidatejobsearch"
RIPPLE_DETAIL_API = RIPPLE_ORIGIN + "/candidate/candidatejobdetail"
SUCCESSFACTORS_ORIGIN = "https://careers.ltm.com"
SUCCESSFACTORS_LISTING = SUCCESSFACTORS_ORIGIN + "/search/"
LCA_NOTICES = "https://lca-notices.ltm.com"
RIPPLE_PAGE_SIZE = 10
SF_PAGE_SIZE = 25
JSON_ACCEPT = "application/json, text/javascript, */*; q=0.01"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
JSON_HEADERS = {"User-Agent": BROWSER_UA, "Accept": JSON_ACCEPT}
HTML_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
RIPPLE_TOKEN_INDIA = "xviyQvbnyYZdGtozXoNm"
RIPPLE_TOKEN_US = "nP1GISU2A024qt3otg3V"
EUROPE_GEO = "Denmark,Finland,Ireland,Luxembourg,Netherlands,Norway,Sweden,Switzerland"
US_CANADA_GEO = "Canada,United States"
COMPANY_CODES = {"LTIMINDIA", "LTIMUSA", "LTIMCANADA", "LTIMEUROPE", "LTIM"}
RIPPLE_DATE = "%d-%b-%Y"
SF_LIST_DATE = "%b %d, %Y"
SF_META_DATE = "%a %b %d %H:%M:%S UTC %Y"
SECTION_MARKERS = (
    "Key Responsibilities", "Roles and Responsibilities", "Responsibilities",
    "Required Skills", "Qualifications", "Desired Experience and Qualifications",
    "Role Summary", "Job Description", "Job Summary", "Skills",
)

# The links exactly as published by https://www.ltm.com/careers on 2026-11-07.
REGIONS = (
    {
        "key": "india",
        "label": "India",
        "kind": "ripplehire",
        "token": RIPPLE_TOKEN_INDIA,
        "geo": "India",
        "url": (
            RIPPLE_CANDIDATE + "?token=" + RIPPLE_TOKEN_INDIA
            + "&lang=en&source=CAREERSITE#list/geo=India"
        ),
    },
    {
        "key": "us_canada",
        "label": "United States",
        "kind": "ripplehire",
        "token": RIPPLE_TOKEN_US,
        "geo": US_CANADA_GEO,
        "url": (
            RIPPLE_CANDIDATE + "?token=" + RIPPLE_TOKEN_US
            + "&lang=en&source=CAREERSITE#list/geo=Canada%2CUnited%20States"
        ),
    },
    {
        "key": "canada",
        "label": "Canada",
        "kind": "ripplehire",
        "token": RIPPLE_TOKEN_US,
        "geo": "Canada",
        "url": (
            RIPPLE_CANDIDATE + "?token=" + RIPPLE_TOKEN_US
            + "&lang=en&source=CAREERSITE#list/geo=Canada"
        ),
    },
    {
        "key": "europe",
        "label": "Europe",
        "kind": "ripplehire",
        "token": RIPPLE_TOKEN_INDIA,
        "geo": EUROPE_GEO,
        "url": (
            RIPPLE_CANDIDATE + "?token=" + RIPPLE_TOKEN_INDIA
            + "&lang=en&source=CAREERSITE#list/geo="
            + "Denmark%2CFinland%2CIreland%2CLuxembourg%2CNetherlands%2CNorway%2CSweden%2CSwitzerland"
        ),
    },
    {
        "key": "everywhere_else",
        "label": "Everywhere else",
        "kind": "successfactors",
        "url": SUCCESSFACTORS_LISTING,
    },
)
REGION_BY_KEY = {region["key"]: region for region in REGIONS}
COLLECTED_REGION_KEYS = tuple(
    region["key"] for region in REGIONS if region["key"] != "canada"
)


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(value: str | None) -> str:
    """Normalise rendered/source text for a stable body comparison."""
    value = normalize_text(value)
    # Whitespace inserted by HTML tag boundaries before punctuation is not
    # visible in the browser (e.g. <b>Disclaimer</b>: -> "Disclaimer:").
    return re.sub(r"\s+([,;:!?])", r"\1", value)


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _number(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return int(value.strip())
    return None


def _iso_date(value: str | None, formats: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_ripple_date(value: str | None) -> str | None:
    return _iso_date(value, (RIPPLE_DATE,))


def parse_sf_list_date(value: str | None) -> str | None:
    return _iso_date(value, (SF_LIST_DATE,))


def parse_sf_meta_date(value: str | None) -> str | None:
    return _iso_date(value, (SF_META_DATE,))


def ripple_job_url(token: str, job_seq: str) -> str:
    return (
        RIPPLE_CANDIDATE + "?token=" + token
        + "&lang=en&source=CAREERSITE#detail/job/" + str(job_seq)
    )


def ripple_apply_url(token: str, job_seq: str) -> str:
    return (
        RIPPLE_CANDIDATE + "?token=" + token
        + "&lang=en&source=CAREERSITE#apply/job/" + str(job_seq)
    )


def successfactors_job_url(job_id: str) -> str:
    return f"{SUCCESSFACTORS_ORIGIN}/job/_/{job_id}/"


def successfactors_apply_url(job_id: str) -> str:
    return f"{SUCCESSFACTORS_ORIGIN}/talentcommunity/apply/{job_id}/?locale=en_US"


def _canonical_compare_url(value: str) -> str:
    """Compare published links after decoding only the query/fragment text."""
    parts = urlsplit(unescape(value or ""))
    return (
        parts.scheme.lower(), parts.netloc.lower(), parts.path,
        unquote(parts.query), unquote(parts.fragment),
    )


def parse_official_entry(html: str, response_url: str = OFFICIAL_CANONICAL) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.ltm.com"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("LTM official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "LTM" not in title or "Careers" not in title:
        raise ValueError("LTM official careers page title changed")
    if not any("Join us! We are where you are." in normalize_text(
            h.get_text(" ", strip=True)) for h in soup.select("h2,h3")):
        raise ValueError("LTM official careers page lost its regional join-us section")

    links = {}
    all_anchors = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href:
            continue
        absolute = urljoin(response_url, href)
        all_anchors.append({
            "text": normalize_text(anchor.get_text(" ", strip=True)),
            "href": absolute,
        })
    for region in REGIONS:
        target = _canonical_compare_url(region["url"])
        matches = [
            item for item in all_anchors
            if _canonical_compare_url(item["href"]) == target
        ]
        if len(matches) != 1:
            raise ValueError(f"LTM official careers page lost its published {region['label']} link")
        links[region["key"]] = matches[0]
    lca = [item for item in all_anchors if "lca-notices.ltm.com" in item["href"].lower()]
    if not lca:
        raise ValueError("LTM official careers page lost its LCA posting-notices link")
    kolkata = [
        item for item in all_anchors
        if "ripplehire.com" in item["href"].lower() and "location=Kolkata" in unquote(item["href"])
    ]
    return {
        "request_url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "title": title,
        "region_links": links,
        "lca_notices_url": lca[0]["href"],
        "filtered_alias_links": kolkata,
        "is_company_published_regional_journey": True,
    }


def parse_ripple_search(value, *, token: str, geo: str, page: int,
                        page_size: int = RIPPLE_PAGE_SIZE) -> dict:
    if not isinstance(value, dict):
        raise ValueError("RippleHire candidate search response is not an object")
    total = _number(value.get("totalJobCount"))
    start = _number(value.get("startJobIndex"))
    max_size = _number(value.get("maxJobSize"))
    rows = value.get("jobVoList")
    if total is None or start is None or max_size is None or not isinstance(rows, list):
        raise ValueError("RippleHire candidate search response is missing its paging fields")
    if page_size < 1 or page_size > 100:
        raise ValueError("RippleHire page size is outside the observed 1..100 range")
    if start != page * page_size:
        raise ValueError(f"RippleHire search start index {start} does not match page {page}")
    if max_size != page_size:
        raise ValueError(f"RippleHire search page size changed: {max_size} != {page_size}")
    if total < 0 or total > 100_000:
        raise ValueError("RippleHire search total is implausible")
    expected = min(page_size, max(0, total - start))
    if len(rows) != expected:
        raise ValueError(
            f"RippleHire search page {page} returned {len(rows)} rows, expected {expected}"
        )
    parsed, issues, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("RippleHire search row is not an object")
        job_seq = str(row.get("jobSeq") or "")
        if not re.fullmatch(r"\d+", job_seq):
            raise ValueError("RippleHire search row has no numeric jobSeq")
        if job_seq in seen:
            raise ValueError(f"RippleHire search page repeats jobSeq {job_seq}")
        title = normalize_text(row.get("jobTitle"))
        if not title:
            raise ValueError(f"RippleHire search row {job_seq} has no title")
        code = str(row.get("jobCode") or "")
        if not re.fullmatch(r"\d+", code):
            issues.append(f"search_row_missing_numeric_jobCode:{job_seq}")
        if str(row.get("allowCareerSite") or "").upper() != "Y":
            issues.append(f"search_row_not_career_site_public:{job_seq}")
        locations = normalize_text(row.get("locations"))
        if not locations:
            issues.append(f"search_row_missing_location:{job_seq}")
        seen.add(job_seq)
        parsed.append(dict(row))
    return {
        "rows": parsed,
        "total": total,
        "start": start,
        "page": page,
        "page_size": page_size,
        "issues": issues,
    }


def parse_ripple_detail(value, *, token: str, job_seq: str, company_code: str | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("RippleHire candidate detail response is not an object")
    company = value.get("companyVO")
    job = value.get("jobVO")
    if not isinstance(company, dict) or not isinstance(job, dict):
        raise ValueError("RippleHire candidate detail response is missing companyVO/jobVO")
    identity = str(job.get("jobSeq") or "")
    if identity != str(job_seq):
        raise ValueError(f"RippleHire detail jobSeq {identity} does not match requested {job_seq}")
    if value.get("linkDeactivated") is True or value.get("nonErLinkDeactivated") is True:
        raise ValueError(f"RippleHire detail {identity} is deactivated")
    if str(value.get("enableLink") or "Y").upper() != "Y":
        raise ValueError(f"RippleHire detail {identity} no longer accepts the public link")
    if str(job.get("jobId") or identity) != identity:
        raise ValueError(f"RippleHire detail {identity} jobId disagrees with jobSeq")
    company_cd = str(company.get("companyCd") or "")
    if company_cd not in COMPANY_CODES:
        raise ValueError(f"RippleHire detail is outside the LTM company family: {company_cd!r}")
    if company_code and company_cd != company_code:
        raise ValueError(
            f"RippleHire detail company {company_cd!r} does not match token company {company_code!r}"
        )
    if normalize_text(company.get("companyName")) != "LTM":
        raise ValueError("RippleHire detail company name changed")
    title = normalize_text(job.get("jobTitle"))
    if not title:
        raise ValueError(f"RippleHire detail {identity} has no title")
    if str(job.get("allowCareerSite") or "").upper() != "Y":
        raise ValueError(f"RippleHire detail {identity} is not published to the career site")
    status = str(job.get("jobStatus") or "")
    if status and status.upper() != "ACTIVE":
        raise ValueError(f"RippleHire detail {identity} is not ACTIVE: {status}")
    description = job.get("jobDesc")
    if not isinstance(description, str):
        raise ValueError(f"RippleHire detail {identity} has no jobDesc string")
    if not normalized_body_text(description):
        raise ValueError(f"RippleHire detail {identity} has an empty jobDesc")
    code = str(job.get("jobCode") or "")
    if not re.fullmatch(r"\d+", code):
        raise ValueError(f"RippleHire detail {identity} has no numeric jobCode")
    locations = normalize_text(job.get("locations") or job.get("jobLocation"))
    if not locations:
        raise ValueError(f"RippleHire detail {identity} has no location")
    date_posted = parse_ripple_date(job.get("jobPostingDate"))
    return {
        "company": company,
        "job": job,
        "job_seq": identity,
        "company_code": company_cd,
        "company_name": normalize_text(company.get("companyName")),
        "title": title,
        "job_code": code,
        "status": status,
        "locations": locations,
        "date_posted": date_posted,
        "date_posted_raw": job.get("jobPostingDate"),
        "valid_through": parse_ripple_date(job.get("hiringDeadline")),
        "valid_through_raw": job.get("hiringDeadline"),
        "description_html": ripple_description_html(job),
        "issues": ([f"detail_date_unparsed:{identity}:{job.get('jobPostingDate')!r}"]
                   if not date_posted and job.get("jobPostingDate") else []),
    }


def ripple_description_html(job: dict) -> str:
    """Build the exact visible description block from the native detail fields."""
    parts = []
    if isinstance(job.get("jobDesc"), str) and job["jobDesc"].strip():
        parts.append("<h3>Role description</h3>" + job["jobDesc"])
    if isinstance(job.get("jobSkills"), str) and job["jobSkills"].strip():
        parts.append("<h3>Skills</h3>" + job["jobSkills"])
    if isinstance(job.get("otherDetails"), str) and job["otherDetails"].strip():
        parts.append("<h3>Other details</h3>" + job["otherDetails"])
    compensation = job.get("compensationRange")
    if isinstance(compensation, str) and compensation.strip():
        parts.append("<h3>Benefits</h3><p>" + escape(compensation.strip()) + "</p>")
    compensation_info = job.get("compensationInfo")
    if isinstance(compensation_info, str) and compensation_info.strip():
        value = compensation_info if "<" in compensation_info else escape(compensation_info)
        parts.append("<p>" + value + "</p>")
    return "\n".join(parts)


def parse_sf_listing(html: str, response_url: str = SUCCESSFACTORS_LISTING,
                     *, expected_startrow: int = 0) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "careers.ltm.com"
            or parsed.path.rstrip("/") != "/search"):
        raise ValueError("LTM SuccessFactors listing redirected away from careers.ltm.com/search/")
    soup = BeautifulSoup(html or "", "lxml")
    title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "LTM" not in title or "Jobs" not in title:
        raise ValueError("LTM SuccessFactors listing title changed")
    label = None
    page_of = None
    for node in soup.select(".paginationLabel, .srHelp"):
        text = normalize_text(node.get_text(" ", strip=True))
        if "Results" in text:
            label = text
        if re.match(r"Page \d+ of \d+", text):
            page_of = text
    if not label:
        raise ValueError("LTM SuccessFactors listing has no results count")
    match = re.search(
        r"Results\s+(\d+)\s*[–-]\s*(\d+)\s+of\s+(\d+)", label
    )
    if not match:
        raise ValueError(f"LTM SuccessFactors results label changed: {label!r}")
    first, last, total = (int(match.group(i)) for i in (1, 2, 3))
    page_match = re.match(r"Page (\d+) of (\d+)", page_of or "")
    if not page_match:
        raise ValueError("LTM SuccessFactors page-of label is missing")
    page_number, page_count = (int(page_match.group(i)) for i in (1, 2))
    if first != expected_startrow + 1:
        raise ValueError(
            f"LTM SuccessFactors startrow {expected_startrow} has first result {first}"
        )
    expected_rows = min(SF_PAGE_SIZE, max(0, total - expected_startrow))
    if last - first + 1 != expected_rows:
        raise ValueError("LTM SuccessFactors result range does not match page size")
    rows, seen = [], set()
    for tr in soup.select("tr.data-row"):
        anchor = tr.select_one("a.jobTitle-link")
        if anchor is None:
            raise ValueError("LTM SuccessFactors row has no job title link")
        href = urljoin(response_url, anchor.get("href", "").strip())
        url_parts = urlsplit(href)
        match_id = re.fullmatch(r"/job/[^/]+/(\d+)/?", url_parts.path)
        if url_parts.netloc != "careers.ltm.com" or match_id is None:
            raise ValueError(f"LTM SuccessFactors row has an unexpected job URL: {href}")
        job_id = match_id.group(1)
        if job_id in seen:
            raise ValueError(f"LTM SuccessFactors listing repeats job {job_id}")
        location_node = tr.select_one("span.jobLocation")
        date_node = tr.select_one("span.jobDate")
        location = normalize_text(location_node.get_text(" ", strip=True)) if location_node else ""
        date_raw = normalize_text(date_node.get_text(" ", strip=True)) if date_node else ""
        rows.append({
            "job_id": job_id,
            "title": normalize_text(anchor.get_text(" ", strip=True)),
            "url": href,
            "location": location or None,
            "date_raw": date_raw or None,
            "date_posted": parse_sf_list_date(date_raw),
        })
        seen.add(job_id)
    if len(rows) != expected_rows:
        raise ValueError(
            f"LTM SuccessFactors page {page_number} rendered {len(rows)} rows, expected {expected_rows}"
        )
    if page_number * SF_PAGE_SIZE - SF_PAGE_SIZE != expected_startrow:
        raise ValueError("LTM SuccessFactors page number and startrow disagree")
    if page_count != max(1, (total + SF_PAGE_SIZE - 1) // SF_PAGE_SIZE):
        raise ValueError("LTM SuccessFactors page count does not reconcile with total")
    return {
        "rows": rows,
        "total": total,
        "first": first,
        "last": last,
        "page": page_number,
        "pages": page_count,
        "page_size": SF_PAGE_SIZE,
        "startrow": expected_startrow,
        "label": label,
    }


def _prop(soup: BeautifulSoup, name: str):
    node = soup.select_one(f'[data-careersite-propertyid="{name}"]')
    return node


def parse_sf_detail(html: str, response_url: str, *, listing: dict | None = None) -> dict:
    parsed = urlsplit(response_url)
    match_id = re.fullmatch(r"/job/[^/]+/(\d+)/?", parsed.path)
    if (parsed.scheme != "https" or parsed.netloc != "careers.ltm.com"
            or match_id is None):
        raise ValueError("LTM SuccessFactors detail is not an exact careers.ltm.com job URL")
    job_id = match_id.group(1)
    soup = BeautifulSoup(html or "", "lxml")
    title_node = _prop(soup, "title")
    desc_node = _prop(soup, "description")
    if title_node is None or desc_node is None:
        raise ValueError("LTM SuccessFactors detail lost its title or description field")
    title = normalize_text(title_node.get_text(" ", strip=True))
    if not title:
        raise ValueError("LTM SuccessFactors detail has an empty title")
    description_html = desc_node.decode_contents()
    description_text = text_html(description_html)
    if len(normalized_body_text(description_text)) < 40:
        raise ValueError("LTM SuccessFactors detail description is empty or truncated")
    req_node = _prop(soup, "customfield2")
    requisition = normalize_text(req_node.get_text(" ", strip=True)) if req_node else ""
    if requisition and not re.fullmatch(r"\d+", requisition):
        requisition = requisition or ""
    if not requisition:
        requisition = None
    shell = soup.select_one("div.jobDisplayShell")
    if shell is None:
        raise ValueError("LTM SuccessFactors detail lost its job display shell")
    meta = {}
    for node in shell.select("meta[itemprop]"):
        key = node.get("itemprop")
        if key and key not in meta:
            meta[key] = node.get("content")
    date_posted = parse_sf_meta_date(meta.get("datePosted"))
    location = normalize_text(meta.get("streetAddress"))
    organization = normalize_text(meta.get("hiringOrganization"))
    apply_node = soup.select_one("a.dialogApplyBtn[href]")
    apply_url = urljoin(response_url, apply_node.get("href")) if apply_node else None
    segment_node = soup.select_one("span.jobsegments")
    segment = None
    if segment_node:
        segment = normalize_text(segment_node.get_text(" ", strip=True))
        segment = re.sub(r"^Job Segment:\s*", "", segment, flags=re.I) or None
    min_node = _prop(soup, "customfield3")
    max_node = _prop(soup, "customfield4")
    min_salary = normalize_text(min_node.get_text(" ", strip=True)) if min_node else ""
    max_salary = normalize_text(max_node.get_text(" ", strip=True)) if max_node else ""
    salary = None
    if min_salary or max_salary:
        salary = {
            "min": min_salary or None,
            "max": max_salary or None,
            "source_text": " | ".join(x for x in (min_salary, max_salary) if x),
            "source_fields": ["customfield3", "customfield4"],
        }
    if listing:
        if listing.get("job_id") and listing["job_id"] != job_id:
            raise ValueError("LTM SuccessFactors listing/detail job id disagree")
        if not location:
            location = normalize_text(listing.get("location"))
        if not date_posted:
            date_posted = listing.get("date_posted")
    if not location:
        location = None
    return {
        "job_id": job_id,
        "title": title,
        "requisition_id": requisition,
        "description_html": description_html,
        "description_text": description_text,
        "date_posted": date_posted,
        "date_posted_raw": meta.get("datePosted") or (listing or {}).get("date_raw"),
        "location": location,
        "organization": organization or "LTM",
        "apply_url": apply_url,
        "job_segment": segment,
        "salary": salary,
        "meta": meta,
        "issues": (
            [f"detail_date_unparsed:{job_id}"] if not date_posted else []
        ),
    }


def _section_excerpt(text: str, markers: tuple[str, ...], stops: tuple[str, ...]) -> str | None:
    """Return a verbatim, bounded section from the source description text."""
    if not text:
        return None
    for marker in markers:
        match = re.search(r"(?im)^\s*" + re.escape(marker) + r"\s*:?\s*$", text)
        if not match:
            match = re.search(r"(?i)\b" + re.escape(marker) + r"\s*:?", text)
        if not match:
            continue
        start = match.end()
        end = len(text)
        for stop in stops:
            found = re.search(r"(?im)^\s*" + re.escape(stop) + r"\s*:?\s*$", text[start:])
            if found:
                end = min(end, start + found.start())
            else:
                found = re.search(r"(?i)\b" + re.escape(stop) + r"\s*:?", text[start:])
                if found:
                    end = min(end, start + found.start())
        block = text[start:end].strip(" \t\r\n-•")
        if len(normalized_body_text(block)) >= 40:
            return block
    return None


def extract_sections(text: str) -> dict:
    stops = SECTION_MARKERS
    responsibilities = _section_excerpt(
        text,
        ("Roles and Responsibilities", "Key Responsibilities", "Responsibilities", "RESPONSIBILITIES"),
        tuple(m for m in stops if m not in (
            "Roles and Responsibilities", "Key Responsibilities", "Responsibilities", "RESPONSIBILITIES")),
    )
    qualifications = _section_excerpt(
        text,
        ("Desired Experience and Qualifications", "Required Skills  Qualifications",
         "Required Skills Qualifications", "Required Skills", "Qualifications"),
        tuple(m for m in stops if m not in (
            "Desired Experience and Qualifications", "Required Skills  Qualifications",
            "Required Skills Qualifications", "Required Skills", "Qualifications")),
    )
    return {"responsibilities": responsibilities, "qualifications": qualifications}


class LTInfotech(Adapter):
    """Collect the five published regional LTM careers journeys."""

    VERIFICATION_REVISION = "l_t_infotech.published-regions.1"

    async def run(self):
        started = time.monotonic()
        entry_started = time.monotonic()
        response = await self.http.get(OFFICIAL_ENTRY, headers=HTML_HEADERS)
        entry = parse_official_entry(response.text, str(response.url))
        self.result.pages += 1
        self.result.metadata["official_entry"] = entry
        self.result.timings["official_entry_seconds"] = max(0.0, time.monotonic() - entry_started)

        listing_started = time.monotonic()
        region_results = {}
        failed_sources = []
        for region in REGIONS:
            if region["key"] == "canada":
                # The published Canada link is a subset of the published
                # US/Canada request; fetch it below for reconciliation only.
                continue
            try:
                if region["kind"] == "ripplehire":
                    value = await self._collect_ripple_region(region)
                else:
                    value = await self._collect_sf_region(region)
                region_results[region["key"]] = value
            except Exception as exc:
                failed_sources.append({
                    "region": region["key"],
                    "label": region["label"],
                    "url": region["url"],
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                self.result.errors.append(
                    f"Region {region['key']} listing failed: {type(exc).__name__}: {exc}"
                )

        # Canada is a published link and must be a subset of the US/Canada
        # request.  Fetch it for reconciliation but do not duplicate its rows.
        canada_check = None
        try:
            canada = await self._fetch_ripple_listing(
                REGION_BY_KEY["canada"], add=False
            )
            us_ids = {row["jobSeq"] for row in region_results.get("us_canada", {}).get("rows", [])}
            canada_ids = {row["jobSeq"] for row in canada["rows"]}
            canada_check = {
                "total": canada["total"],
                "ids": sorted(canada_ids, key=int),
                "subset_of_us_canada": canada_ids <= us_ids,
                "missing_from_us_canada": sorted(canada_ids - us_ids, key=int),
            }
            if not canada_check["subset_of_us_canada"]:
                self.result.errors.append(
                    "Published Canada list contains jobs absent from the published US/Canada list"
                )
        except Exception as exc:
            canada_check = {"error": f"{type(exc).__name__}: {exc}"}
            self.result.errors.append(f"Canada reconciliation listing failed: {type(exc).__name__}: {exc}")

        self.result.timings["listing_collection_seconds"] = max(0.0, time.monotonic() - listing_started)
        self.result.timings["source_collection_seconds"] = (
            self.result.timings["official_entry_seconds"]
            + self.result.timings["listing_collection_seconds"]
        )
        self.result.metadata["region_counts"] = {
            key: value.get("total") for key, value in region_results.items()
        }
        self.result.metadata["canada_published_subset_check"] = canada_check
        self.result.metadata["failed_sources"] = failed_sources

        expected_parts = [value.get("total") for value in region_results.values()]
        complete_listing = (
            len(region_results) == len(COLLECTED_REGION_KEYS)
            and not failed_sources
            and all(isinstance(value, int) for value in expected_parts)
        )
        self.result.expected_total = sum(expected_parts) if complete_listing else None
        self.result.listing_complete = bool(complete_listing)

        jobs = list(self.result.jobs)
        if jobs:
            self.result.jobs = await self.hydrate(jobs, self._detail)

        audit_started = time.monotonic()
        self._reconcile(region_results, canada_check, failed_sources, expected_parts)
        self.result.timings["membership_audit_seconds"] = max(0.0, time.monotonic() - audit_started)
        self.result.timings["adapter_total_seconds"] = max(0.0, time.monotonic() - started)
        return self.result

    async def _fetch_ripple_listing(self, region: dict, *, add: bool = True) -> dict:
        rows, total, pages = [], None, 0
        page = 0
        while True:
            if not self.page_guard():
                raise ValueError("RippleHire listing page budget exhausted")
            body = {
                "page": page,
                "search": "*:*",
                "token": region["token"],
                "source": "CAREERSITE",
                "pagesize": RIPPLE_PAGE_SIZE,
                "geo": region["geo"],
            }
            value = await self.http.json(
                RIPPLE_SEARCH_API,
                method="POST",
                data={"careerSiteUrlParams": _json_dumps(body), "lang": "en"},
                headers={**JSON_HEADERS, "Referer": region["url"]},
            )
            parsed = parse_ripple_search(
                value, token=region["token"], geo=region["geo"], page=page
            )
            if total is None:
                total = parsed["total"]
            elif parsed["total"] != total:
                raise ValueError("RippleHire total changed between listing pages")
            for issue in parsed["issues"]:
                self.result.warnings.append(f"{region['key']}:{issue}")
            rows.extend(parsed["rows"])
            pages += 1
            if len(rows) >= total:
                break
            page += 1
        if len(rows) != total:
            raise ValueError(
                f"RippleHire {region['key']} listing collected {len(rows)} of {total} rows"
            )
        if add:
            for row in rows:
                self._add_ripple_job(region, row)
        return {
            "key": region["key"],
            "label": region["label"],
            "kind": "ripplehire",
            "total": total,
            "pages": pages,
            "rows": rows,
            "source_url": region["url"],
            "request_url": RIPPLE_SEARCH_API,
        }

    async def _collect_ripple_region(self, region: dict) -> dict:
        return await self._fetch_ripple_listing(region, add=True)

    async def _collect_sf_region(self, region: dict) -> dict:
        rows, total, pages = [], None, 0
        startrow = 0
        while True:
            if not self.page_guard():
                raise ValueError("SuccessFactors listing page budget exhausted")
            url = SUCCESSFACTORS_LISTING + "?q=&sortColumn=referencedate&sortDirection=desc"
            if startrow:
                url += f"&startrow={startrow}"
            response = await self.http.get(url, headers=HTML_HEADERS)
            parsed = parse_sf_listing(
                response.text, str(response.url), expected_startrow=startrow
            )
            if total is None:
                total = parsed["total"]
            elif parsed["total"] != total:
                raise ValueError("SuccessFactors total changed between listing pages")
            rows.extend(parsed["rows"])
            pages += 1
            if len(rows) >= total:
                break
            startrow += SF_PAGE_SIZE
        if len(rows) != total:
            raise ValueError(
                f"SuccessFactors listing collected {len(rows)} of {total} rows"
            )
        for row in rows:
            self._add_sf_job(region, row)
        return {
            "key": region["key"],
            "label": region["label"],
            "kind": "successfactors",
            "total": total,
            "pages": pages,
            "rows": rows,
            "source_url": region["url"],
            "request_url": SUCCESSFACTORS_LISTING,
        }

    def _add_ripple_job(self, region: dict, row: dict):
        job_seq = str(row["jobSeq"])
        token = region["token"]
        job = self.job(
            row,
            title=normalize_text(row.get("jobTitle")),
            url=ripple_job_url(token, job_seq),
            job_id=job_seq,
            requisition_id=str(row.get("jobCode")) if row.get("jobCode") else None,
            locations=[normalize_text(row.get("locations"))] if row.get("locations") else [],
            apply_url=ripple_apply_url(token, job_seq),
            date_posted=parse_ripple_date(row.get("jobPostingDate")),
            date_posted_raw=row.get("jobPostingDate"),
            experience=normalize_text(row.get("jobReqExp")) or None,
            employer="LTM",
        )
        job.raw = {
            "source_kind": "ripplehire",
            "region": region["key"],
            "region_label": region["label"],
            "region_url": region["url"],
            "token": token,
            "geo": region["geo"],
            "listing": row,
            "listing_request_url": RIPPLE_SEARCH_API,
            "detail_api_url": (
                RIPPLE_DETAIL_API + "?token=" + token
                + "&jobSeq=" + job_seq + "&source=CAREERSITE&lang=en"
            ),
        }
        self.add(job)

    def _add_sf_job(self, region: dict, row: dict):
        job = self.job(
            row,
            title=normalize_text(row.get("title")),
            url=row["url"],
            job_id=row["job_id"],
            locations=[row["location"]] if row.get("location") else [],
            apply_url=successfactors_apply_url(row["job_id"]),
            date_posted=row.get("date_posted"),
            date_posted_raw=row.get("date_raw"),
            employer="LTM",
        )
        job.raw = {
            "source_kind": "successfactors",
            "region": region["key"],
            "region_label": region["label"],
            "region_url": region["url"],
            "listing": row,
            "listing_request_url": SUCCESSFACTORS_LISTING,
        }
        self.add(job)

    async def _detail(self, job):
        if job.raw.get("source_kind") == "ripplehire":
            return await self._ripple_detail(job)
        if job.raw.get("source_kind") == "successfactors":
            return await self._sf_detail(job)
        raise ValueError("Unknown LTM job source kind")

    async def _ripple_detail(self, job):
        token = job.raw["token"]
        detail_url = job.raw["detail_api_url"]
        value = await self.http.json(detail_url, headers=JSON_HEADERS)
        parsed = parse_ripple_detail(
            value, token=token, job_seq=job.job_id
        )
        listing = job.raw["listing"]
        if parsed["title"] != normalize_text(listing.get("jobTitle")):
            job.warnings.append("title_mismatch_listing_vs_detail")
        if str(listing.get("jobCode") or "") != parsed["job_code"]:
            job.warnings.append("job_code_mismatch_listing_vs_detail")
        expected_companies = {
            "india": {"LTIMINDIA"},
            "us_canada": {"LTIMUSA", "LTIMCANADA"},
            "canada": {"LTIMCANADA"},
            "europe": {"LTIMEUROPE"},
        }.get(job.raw.get("region"))
        if expected_companies and parsed["company_code"] not in expected_companies:
            raise ValueError(
                f"RippleHire detail company {parsed['company_code']} does not match published region {job.raw.get('region')}"
            )
        for issue in parsed["issues"]:
            job.warnings.append(issue)
        job.title = parsed["title"]
        job.requisition_id = parsed["job_code"]
        job.locations = [parsed["locations"]]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.valid_through = parsed.get("valid_through")
        job.experience = normalize_text(parsed["job"].get("jobReqExp")) or None
        job.employment_type = normalize_text(parsed["job"].get("jobTypeCustom3")) or None
        job.workplace_type = "Remote" if "remote" in parsed["locations"].lower() else None
        job.department = normalize_text(parsed["job"].get("bussinessUnit")) or None
        job.employer = {
            "name": parsed["company_name"],
            "code": parsed["company_code"],
            "entity": normalize_text(parsed["company"].get("multiCompanyName")) or None,
            "parent": parsed["company"].get("parentCode"),
            "currency": parsed["company"].get("currencyCode"),
            "source_field": "companyVO",
        }
        compensation = parsed["job"].get("compensationRange")
        if isinstance(compensation, str) and compensation.strip():
            job.salary = {
                "source_text": compensation.strip(),
                "source_field": "compensationRange",
            }
        else:
            job.salary = None
        sections = extract_sections(job.description_text or text_html(parsed["description_html"]))
        job.responsibilities = sections.get("responsibilities")
        job.qualifications = sections.get("qualifications")
        job.description_html = parsed["description_html"]
        job.description_method = (
            "ltm.ripplehire.candidatejobdetail.jobDesc+jobSkills+otherDetails+compensationRange"
        )
        job.description_source_url = detail_url
        job.raw["detail"] = value
        job.raw["detail_company"] = parsed["company"]
        job.raw["detail_url"] = detail_url
        job.raw["parsed"] = {
            "company_code": parsed["company_code"],
            "company_name": parsed["company_name"],
            "job_code": parsed["job_code"],
            "status": parsed["status"],
            "date_posted": parsed["date_posted"],
            "date_posted_raw": parsed["date_posted_raw"],
        }
        job.raw["job_status"] = parsed["status"]
        job.raw["publish_details"] = parsed["job"].get("publishDetails")
        return job

    async def _sf_detail(self, job):
        response = await self.http.get(job.url, headers=HTML_HEADERS)
        if str(response.url).rstrip("/") != job.url.rstrip("/"):
            raise ValueError("SuccessFactors detail redirected away from its exact job URL")
        parsed = parse_sf_detail(
            response.text, str(response.url), listing=job.raw["listing"]
        )
        if parsed["job_id"] != job.job_id:
            raise ValueError("SuccessFactors detail job id does not match listing")
        if parsed["title"] != job.title:
            job.warnings.append("title_mismatch_listing_vs_detail")
        if not parsed["location"]:
            job.warnings.append("location_missing")
        listing_date = job.raw["listing"].get("date_posted")
        if listing_date and parsed["date_posted"] and listing_date != parsed["date_posted"]:
            job.warnings.append("listing_detail_date_mismatch")
        for issue in parsed["issues"]:
            job.warnings.append(issue)
        job.title = parsed["title"]
        job.requisition_id = parsed["requisition_id"]
        job.apply_url = parsed["apply_url"] or job.apply_url
        job.locations = [parsed["location"]] if parsed["location"] else job.locations
        job.date_posted = parsed["date_posted"] or listing_date
        job.date_posted_raw = parsed["date_posted_raw"]
        job.employer = {
            "name": parsed["organization"],
            "source_field": "meta hiringOrganization",
        }
        job.salary = parsed["salary"]
        job.workplace_type = (
            "Remote" if parsed["location"] and "remote" in parsed["location"].lower()
            else None
        )
        job.description_html = parsed["description_html"]
        job.description_method = "ltm.successfactors.jobdescription"
        job.description_source_url = job.url
        sections = extract_sections(parsed["description_text"])
        job.responsibilities = sections.get("responsibilities")
        job.qualifications = sections.get("qualifications")
        job.raw["detail"] = parsed
        job.raw["detail_url"] = job.url
        job.raw["job_segment"] = parsed["job_segment"]
        return job

    def _reconcile(self, region_results, canada_check, failed_sources, expected_parts):
        jobs = list(self.result.jobs)
        region_ids = {
            key: [str(row["jobSeq"]) for row in value.get("rows", [])]
            for key, value in region_results.items()
            if value.get("kind") == "ripplehire"
        }
        all_ripple_ids = [identity for ids in region_ids.values() for identity in ids]
        cross_region_overlap = sorted(
            {identity for identity in all_ripple_ids if all_ripple_ids.count(identity) > 1},
            key=int,
        )
        sf_ids = [str(row["job_id"]) for value in region_results.values()
                  if value.get("kind") == "successfactors" for row in value.get("rows", [])]
        detail_failures = []
        short_descriptions = []
        date_missing = []
        status_counts = {}
        company_counts = {}
        title_mismatches = []
        code_mismatches = []
        description_lengths = []
        for job in jobs:
            text = normalized_body_text(job.description_text)
            description_lengths.append(len(text))
            if not text or len(text) < 100:
                short_descriptions.append({
                    "id": job.job_id,
                    "url": job.url,
                    "chars": len(text),
                })
            if not job.date_posted:
                date_missing.append({"id": job.job_id, "url": job.url})
            if "detail_fetch_failed" in job.warnings:
                detail_failures.append({
                    "id": job.job_id,
                    "url": job.url,
                    "reason": "detail_fetch_failed",
                })
            if job.raw.get("source_kind") == "ripplehire":
                status = str(job.raw.get("job_status") or "")
                status_counts[status] = status_counts.get(status, 0) + 1
                code = (job.raw.get("parsed") or {}).get("company_code")
                company_counts[code] = company_counts.get(code, 0) + 1
            if "title_mismatch_listing_vs_detail" in job.warnings:
                title_mismatches.append(job.job_id)
            if "job_code_mismatch_listing_vs_detail" in job.warnings:
                code_mismatches.append(job.job_id)
        counts = {key: value.get("total") for key, value in region_results.items()}
        expected = sum(expected_parts) if all(isinstance(x, int) for x in expected_parts) else None
        membership_complete = (
            not failed_sources
            and len(region_results) == len(COLLECTED_REGION_KEYS)
            and expected is not None
            and len(jobs) == expected
            and not cross_region_overlap
        )
        detail_complete = membership_complete and not detail_failures and not short_descriptions
        complete = membership_complete and detail_complete
        # Cross-system title/location matches are informational only: the two
        # published systems use different ids and are separate postings.
        def location_tokens(value: str) -> set[str]:
            return {
                token for token in re.split(r"[^0-9a-z]+", normalize_text(value).casefold())
                if token
            }

        cross_system = {}
        for job in jobs:
            if job.raw.get("source_kind") != "successfactors":
                continue
            title = normalize_text(job.title).casefold()
            sf_tokens = location_tokens(" ".join(job.locations))
            matches = []
            for other in jobs:
                if (other is job or other.raw.get("source_kind") != "ripplehire"
                        or normalize_text(other.title).casefold() != title):
                    continue
                if sf_tokens & location_tokens(" ".join(other.locations)):
                    matches.append(other.job_id)
            if matches:
                cross_system[job.job_id] = sorted(set(matches))[:5]
        self.result.metadata["membership_reconciliation"] = {
            "published_regions": counts,
            "region_ids": region_ids,
            "successfactors_ids": sf_ids,
            "expected_total": expected,
            "jobs_collected": len(jobs),
            "ripplehire_ids": {key: len(ids) for key, ids in region_ids.items()},
            "successfactors_id_count": len(sf_ids),
            "cross_region_duplicate_ids": cross_region_overlap,
            "canada_published_subset": canada_check,
            "detail_failures": detail_failures,
            "short_descriptions": short_descriptions,
            "date_missing": date_missing,
            "ripplehire_status_counts": status_counts,
            "ripplehire_company_counts": company_counts,
            "title_mismatches": title_mismatches,
            "job_code_mismatches": code_mismatches,
            "description_chars": {
                "min": min(description_lengths) if description_lengths else 0,
                "max": max(description_lengths) if description_lengths else 0,
                "total": sum(description_lengths),
            },
            "cross_system_title_location_matches": cross_system,
            "cross_system_title_location_match_count": len(cross_system),
            "failed_sources": failed_sources,
            "membership_complete": bool(membership_complete),
            "detail_complete": bool(detail_complete),
            "complete": bool(complete),
            "excluded_non_job_links": [
                {"url": LCA_NOTICES, "reason": "LCA compliance posting notices, not job vacancies"},
                {
                    "url": "https://ltm.ripplehire.com/candidate/?token=...&source=CAREERSITE#list/geo=India&location=Kolkata",
                    "reason": "filtered example link already covered by the unfiltered India list",
                },
            ],
        }
        if not membership_complete and self.result.listing_complete:
            self.result.listing_complete = False
        if not membership_complete:
            self.result.errors.append("membership_reconciliation_incomplete")
        elif not detail_complete and not self.result.errors:
            self.result.errors.append("detail_verification_incomplete")
        self.result.metadata["description_length_stats"] = self.result.metadata[
            "membership_reconciliation"
        ]["description_chars"]


def _json_dumps(value) -> str:
    return json.dumps(value, separators=(",", ":"))
