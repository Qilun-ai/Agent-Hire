"""Mindtree's current careers journey: the LTIMindtree/LTM careers page and its
two company-published job systems (RippleHire for India/US/Canada/Europe and
SuccessFactors Career Site Builder for "Everywhere else").

The adapter is deliberately scoped to what the official careers page publishes:
the RippleHire candidate board reached through the page's region cards (one
company board, 1351 postings when inspected) and the `careers.ltm.com/search/`
SuccessFactors listing linked as "Everywhere else" (48 postings). It never
enumerates another RippleHire account or another SuccessFactors customer; only
the exact tokens/paths linked from the company page are requested.
"""
from __future__ import annotations

from hashlib import sha256
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.ltimindtree.com/careers/"
OFFICIAL_FINAL = "https://www.ltm.com/careers"
RIPPLE_ORIGIN = "https://ltm.ripplehire.com"
RIPPLE_TOKEN = "xviyQvbnyYZdGtozXoNm"
RIPPLE_US_TOKEN = "nP1GISU2A024qt3otg3V"
RIPPLE_SOURCE = "CAREERSITE"
RIPPLE_CANDIDATE_URL = (
    f"{RIPPLE_ORIGIN}/candidate/?token={RIPPLE_TOKEN}&lang=en&source={RIPPLE_SOURCE}"
)
RIPPLE_US_CANDIDATE_URL = (
    f"{RIPPLE_ORIGIN}/candidate/?token={RIPPLE_US_TOKEN}&lang=en&source={RIPPLE_SOURCE}"
)
RIPPLE_SEARCH_API = f"{RIPPLE_ORIGIN}/candidate/candidatejobsearch"
RIPPLE_DETAIL_API = f"{RIPPLE_ORIGIN}/candidate/candidatejobdetail"
RIPPLE_ITEMS_PER_PAGE = 10
RIPPLE_HEADERS = {"Referer": RIPPLE_CANDIDATE_URL, "Origin": RIPPLE_ORIGIN,
                  "Accept": "application/json, text/javascript, */*; q=0.01"}
# The AEM careers page answers non-browser User-Agents with HTTP 406; the
# public page itself is unchanged for an ordinary desktop browser request.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HTML_HEADERS = {"User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"}

SF_ORIGIN = "https://careers.ltm.com"
SF_LISTING_URL = f"{SF_ORIGIN}/search/"
SF_PAGE_SIZE = 25
LCA_NOTICES_URL = "https://Lca-notices.ltm.com"

REGION_LINKS = {
    "Apply here": "https://ltimindtree.ripplehire.com/candidate/?token=xviyQvbnyYZdGtozXoNm&lang=en&source=CAREERSITE#list/geo=India&location=Kolkata",
    "01 India": RIPPLE_CANDIDATE_URL + "#list/geo=India",
    "01 United States": RIPPLE_US_CANDIDATE_URL + "#list/geo=Canada%2CUnited%20States",
    "02 LCA Posting Notices": LCA_NOTICES_URL,
    "03 Canada": RIPPLE_US_CANDIDATE_URL + "#list/geo=Canada",
    "04 Europe": RIPPLE_CANDIDATE_URL
    + "#list/geo=Denmark%2CFinland%2CIreland%2CLuxembourg%2CNetherlands%2CNorway%2CSweden%2CSwitzerland",
    "06 Everywhere else": SF_LISTING_URL,
}

MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
RIPPLE_DATE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
SF_DATE = re.compile(r"^[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+\d{2}:\d{2}:\d{2}\s+UTC\s+(\d{4})$")
SF_RESULTS = re.compile(r"Results\s+(\d+)\s*[–-]\s*(\d+)\s+of\s+(\d+)")
SF_PAGE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)")
SF_JOB_PATH = re.compile(r"^/job/([^/]+)/(\d+)/?$")
SALARY_RANGE = re.compile(
    r"(?P<min>[\d][\d,]*(?:\.\d+)?)\s*(?:to|–|-)\s*(?P<max>[\d][\d,]*(?:\.\d+)?)"
    r"(?:\s*(?P<period>per\s+[A-Za-z]+|annually|a\s+year|/\s*(?:yr|year)))?",
    re.I,
)
CURRENCY = re.compile(r"(?P<symbol>[$€£₹])|(?P<code>USD|CAD|INR|EUR|GBP|AUD|SGD)", re.I)
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
HEADING_HINTS = (
    "job summary", "job description", "roles and responsibilities", "role and responsibilities",
    "responsibilities", "key responsibilities", "qualifications", "requirements",
    "required skills", "skills", "benefits", "about", "experience", "education",
    "what you will do", "what you'll do", "what we offer", "mandatory skills",
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def _json_object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} response is not a JSON object")
    return value


def _json_text(value) -> str:
    return normalized_title(value) if isinstance(value, str) else ""


def parse_official_entry(html: str, response_url: str = OFFICIAL_FINAL) -> dict:
    """Validate the current LTM careers page and its exact published region links."""
    parts = urlsplit(response_url)
    if parts.scheme not in ("http", "https") or parts.netloc != "www.ltm.com" or parts.path.rstrip("/") != "/careers":
        raise ValueError("LTM/Mindtree official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "LTM" not in title or "careers" not in title.lower():
        raise ValueError("LTM careers page title changed")
    links = {}
    for anchor in soup.select("a[href]"):
        label = normalized_title(anchor.get_text(" ", strip=True))
        if label in REGION_LINKS:
            links[label] = urljoin(response_url, anchor.get("href", ""))
    missing = sorted(set(REGION_LINKS) - set(links))
    changed = {label: links[label] for label in links
               if urlsplit(links[label])._replace(scheme=urlsplit(links[label]).scheme.lower(),
                                                   netloc=urlsplit(links[label]).netloc.lower()).geturl()
               != urlsplit(REGION_LINKS[label])._replace(scheme=urlsplit(REGION_LINKS[label]).scheme.lower(),
                                                         netloc=urlsplit(REGION_LINKS[label]).netloc.lower()).geturl()}
    if missing or changed:
        raise ValueError(f"LTM careers region links changed: missing={missing} changed={changed}")
    for label in ("01 India", "01 United States", "03 Canada", "04 Europe"):
        query = parse_qs(urlsplit(links[label]).query)
        if query.get("token", [""])[0] not in (RIPPLE_TOKEN, RIPPLE_US_TOKEN):
            raise ValueError(f"LTM careers region link {label} no longer uses a published RippleHire token")
    return {
        "url": response_url,
        "title": title,
        "region_links": links,
        "lca_notices_url": links["02 LCA Posting Notices"],
        "lca_notices_is_job_listing": False,
        "ripplehire_board_tokens": [RIPPLE_TOKEN, RIPPLE_US_TOKEN],
        "successfactors_listing_url": links["06 Everywhere else"],
    }


def parse_ripple_list_page(value: dict, requested_page: int) -> dict:
    """Parse one RippleHire `candidatejobsearch` JSON page (the UI's own response)."""
    page_value = _json_object(value, "RippleHire job list")
    try:
        start = int(page_value["startJobIndex"])
        items = int(page_value["maxJobSize"])
        total = int(page_value["totalJobCount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("RippleHire job list pagination metadata changed") from exc
    if items != RIPPLE_ITEMS_PER_PAGE or total < 0 or start != requested_page * RIPPLE_ITEMS_PER_PAGE:
        raise ValueError("RippleHire job list page/offset schema changed")
    rows, seen, issues = [], set(), []
    container = page_value.get("jobVoList")
    if not isinstance(container, list):
        raise ValueError("RippleHire job list no longer returns a jobVoList array")
    for node in container:
        if not isinstance(node, dict):
            issues.append("ripplehire_list_row_not_object")
            continue
        identity = _json_text(node.get("jobSeq"))
        if not re.fullmatch(r"\d+", identity):
            issues.append("ripplehire_list_row_missing_numeric_job_seq")
            continue
        if identity in seen:
            issues.append(f"ripplehire_list_duplicate_job_seq:{identity}")
            continue
        seen.add(identity)
        rows.append({
            "jobSeq": identity,
            "jobTitle": _json_text(node.get("jobTitle")),
            "jobCode": _json_text(node.get("jobCode")) or None,
            "locations": _json_text(node.get("locations")),
            "jobReqExp": _json_text(node.get("jobReqExp")) or None,
            "numOfOpening": _json_text(node.get("numOfOpening")) or None,
            "allowCareerSite": _json_text(node.get("allowCareerSite")),
            "jobStatus": _json_text(node.get("jobStatus")) or None,
        })
    expected = min(items, max(0, total - start))
    if len(rows) != expected:
        issues.append(f"ripplehire_page_row_count_mismatch:{len(rows)}!={expected}")
    if any(row["allowCareerSite"] not in ("Y", "") for row in rows):
        issues.append("ripplehire_list_row_not_on_career_site")
    return {"page": requested_page, "start": start, "items": items, "total": total,
            "rows": rows, "issues": issues}


def parse_ripple_detail(value: dict, expected_seq: str) -> dict:
    """Parse one RippleHire `candidatejobdetail` JSON response for an exact jobSeq."""
    detail = _json_object(value, "RippleHire job detail")
    job = detail.get("jobVO")
    company = detail.get("companyVO")
    if not isinstance(job, dict) or not isinstance(company, dict):
        raise ValueError("RippleHire job detail is missing jobVO/companyVO")
    identity = _json_text(job.get("jobSeq")) or _json_text(detail.get("jobSeq"))
    if identity != str(expected_seq):
        raise ValueError(f"RippleHire job detail jobSeq mismatch: {identity} != {expected_seq}")
    title = _json_text(job.get("jobTitle"))
    if not title:
        raise ValueError(f"RippleHire job detail {expected_seq} has no title")
    posting_raw = _json_text(job.get("jobPostingDate"))
    date_posted = None
    if posting_raw:
        match = RIPPLE_DATE.fullmatch(posting_raw)
        if match:
            day, month, year = match.groups()
            number = MONTHS.get(month.lower())
            if number:
                date_posted = f"{year}-{number:02d}-{int(day):02d}"
    fields = {name: _json_text(job.get(name)) or None for name in (
        "jobLocation", "jobLocationList", "locations", "jobReqExp", "numOfOpening", "jobType",
        "jobTypeCustom3", "jobStatus", "jobCode", "jobSkills", "compensationRange",
        "compensationInfo", "hiringDeadline", "openDate", "createDttm", "careerSiteDate",
        "bussinessUnit", "isEeoEnabled", "mobileReqJobExp", "jobPrimarySkills",
        "jobSecondarySkills", "otherDetails",
    )}
    company_fields = {name: _json_text(company.get(name)) or None for name in (
        "companyCd", "companyName", "multiCompanyName", "parentCode", "currencyCode",
        "currencySymbol", "domainName", "parentCompanySeq",
    )}
    publish_details = job.get("publishDetails") if isinstance(job.get("publishDetails"), dict) else None
    return {
        "job_seq": identity,
        "title": title,
        "description_html": job.get("jobDesc") if isinstance(job.get("jobDesc"), str) else "",
        "date_posted": date_posted,
        "date_posted_raw": posting_raw or None,
        "publish_details": publish_details,
        "job_count": detail.get("jobCount"),
        "fields": fields,
        "company": company_fields,
        "job_vo": job,
    }


def parse_sf_list_page(html: str, response_url: str, requested_start: int) -> dict:
    """Parse one SuccessFactors Career Site Builder search page."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "careers.ltm.com" or parts.path != "/search/":
        raise ValueError("LTM SuccessFactors listing redirected away from its inspected route")
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)
    results = SF_RESULTS.search(text)
    page = SF_PAGE.search(text)
    if not results or not page:
        raise ValueError("LTM SuccessFactors result count/pagination text changed")
    low, high, total = (int(value) for value in results.groups())
    page_number, page_count = (int(value) for value in page.groups())
    if low != requested_start + 1 or high < low or total < high:
        raise ValueError("LTM SuccessFactors result range does not match the requested startrow")
    rows, seen, issues = [], set(), []
    for tr in soup.select("tr.data-row"):
        anchor = tr.select_one("a.jobTitle-link[href]")
        if anchor is None:
            issues.append("successfactors_list_row_missing_job_link")
            continue
        target = urljoin(response_url, anchor.get("href", ""))
        match = SF_JOB_PATH.fullmatch(urlsplit(target).path)
        if match is None or urlsplit(target).netloc != "careers.ltm.com":
            issues.append("successfactors_list_row_unexpected_job_url")
            continue
        cells = [normalized_title(td.get_text(" ", strip=True)) for td in tr.select("td")]
        identity = match.group(2)
        if identity in seen:
            issues.append(f"successfactors_list_duplicate_job_id:{identity}")
            continue
        seen.add(identity)
        rows.append({
            "job_id": identity,
            "slug": match.group(1),
            "url": target,
            "title": normalized_title(anchor.get_text(" ", strip=True)),
            "location": cells[1] if len(cells) > 1 else "",
            "date_listed": cells[2] if len(cells) > 2 else "",
            "cells": cells,
        })
    expected = min(SF_PAGE_SIZE, max(0, total - requested_start))
    if len(rows) != expected:
        issues.append(f"successfactors_page_row_count_mismatch:{len(rows)}!={expected}")
    return {"start": requested_start, "low": low, "high": high, "total": total,
            "page": page_number, "page_count": page_count, "rows": rows, "issues": issues}


def parse_sf_detail(html: str, expected_url: str) -> dict:
    """Parse one exact SuccessFactors Career Site Builder job page."""
    parts = urlsplit(expected_url)
    match = SF_JOB_PATH.fullmatch(parts.path)
    if parts.scheme != "https" or parts.netloc != "careers.ltm.com" or match is None:
        raise ValueError("LTM SuccessFactors detail URL is not an exact /job/<slug>/<id>/ page")
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.select_one('h1 span[itemprop="title"]')
    description = soup.select_one('span[itemprop="description"] span.jobdescription')
    title = normalized_title(title_node.get_text(" ", strip=True)) if title_node else ""
    if not title or description is None:
        raise ValueError("LTM SuccessFactors detail title or jobdescription field is missing")
    req = soup.select_one('span[data-careersite-propertyid="customfield2"]')
    address = soup.select_one('span[itemprop="address"] meta[itemprop="streetAddress"]')
    posted = soup.select_one('meta[itemprop="datePosted"]')
    organization = soup.select_one('meta[itemprop="hiringOrganization"]')
    industry = soup.select_one('span[itemprop="industry"]')
    minimum = soup.select_one('span[data-careersite-propertyid="customfield3"]')
    maximum = soup.select_one('span[data-careersite-propertyid="customfield4"]')
    apply_anchor = soup.select_one('a.apply[href]')
    date_posted = date_raw = None
    if posted and posted.get("content"):
        date_raw = normalized_title(posted.get("content"))
        date_match = SF_DATE.fullmatch(date_raw)
        if date_match:
            month, day, year = date_match.groups()
            number = MONTHS.get(month.lower())
            if number:
                date_posted = f"{year}-{number:02d}-{int(day):02d}"
    description_html = description.decode_contents().strip()
    if not normalized_body_text(description_html):
        raise ValueError("LTM SuccessFactors jobdescription field is empty")
    apply_href = apply_anchor.get("href") if apply_anchor else None
    return {
        "job_id": match.group(2),
        "slug": match.group(1),
        "url": expected_url,
        "title": title,
        "requisition_id": normalized_title(req.get_text(" ", strip=True)) if req else None,
        "description_html": description_html,
        "date_posted": date_posted,
        "date_posted_raw": date_raw,
        "location": normalized_title(address.get("content")) if address and address.get("content") else None,
        "organization": normalized_title(organization.get("content")) if organization and organization.get("content") else None,
        "job_segment": normalized_title(industry.get_text(" ", strip=True)) if industry else None,
        "min_salary": normalized_title(minimum.get_text(" ", strip=True)) if minimum else "",
        "max_salary": normalized_title(maximum.get_text(" ", strip=True)) if maximum else "",
        "apply_url": urljoin(expected_url, apply_href) if apply_href else None,
    }


def _is_heading(text: str, bold: bool) -> bool:
    value = normalized_title(text)
    if not value or len(value) > 90:
        return False
    lowered = value.lower().rstrip(":")
    if lowered in HEADING_HINTS:
        return True
    if bold and lowered in HEADING_HINTS:
        return True
    return False


def description_sections(description_html: str) -> list[dict]:
    """Split an employer description into its own headings; never invent text."""
    soup = BeautifulSoup(f'<div data-mindtree-body="1">{description_html or ""}</div>', "lxml")
    root = soup.select_one("[data-mindtree-body]")
    sections, current = [], None
    blocks = [node for node in (root.contents if root else []) if isinstance(node, Tag)]
    plain = not blocks and root is not None
    if plain:
        lines = [line.strip() for line in (root.get_text("\n") if root else "").splitlines() if line.strip()]
        for line in lines:
            if _is_heading(line, False):
                if current is not None:
                    sections.append(current)
                current = {"heading": line, "html": "", "text": ""}
                continue
            if current is None:
                current = {"heading": None, "html": "", "text": ""}
            current["text"] += ("\n" if current["text"] else "") + line
        if current is not None:
            sections.append(current)
        return sections or [{"heading": None, "html": "", "text": ""}]
    for block in blocks:
        heading = ""
        if block.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            heading = normalized_title(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and _is_heading(full_text, True):
                heading = full_text
            elif _is_heading(full_text, False):
                heading = full_text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections or [{"heading": None, "html": "", "text": ""}]


def _salary(compensation_range: str | None, compensation_info: str | None) -> dict | None:
    raw = normalized_title(compensation_range) or normalized_title(compensation_info)
    if not raw:
        return None
    salary = {"source_field": "compensationRange" if normalized_title(compensation_range) else "compensationInfo",
             "source_text": raw}
    match = SALARY_RANGE.search(raw)
    currency = CURRENCY.search(raw)
    if match:
        salary.update({
            "min": float(match.group("min").replace(",", "")),
            "max": float(match.group("max").replace(",", "")),
            "period": match.group("period"),
        })
        if currency:
            salary["currency"] = currency.group("symbol") or currency.group("code")
    return salary


def _ripple_locations(fields: dict) -> list[str]:
    values = []
    for value in (fields.get("jobLocation"), fields.get("jobLocationList")):
        if not value:
            continue
        for part in value.split(","):
            part = normalized_title(part)
            if part and part not in values:
                values.append(part)
    if not values and fields.get("locations"):
        values.append(fields["locations"])
    return values


def _sf_salary(minimum: str, maximum: str) -> dict | None:
    if not minimum and not maximum:
        return None
    return {"source_field": "customfield3/customfield4", "min_text": minimum or None,
            "max_text": maximum or None}


class Mindtree(Adapter):
    """Read the exact LTM/Mindtree RippleHire board or the published SuccessFactors listing."""

    VERIFICATION_REVISION = "mindtree.ltm-ripplehire-successfactors.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.journey = self._journey_for(source.url)

    @staticmethod
    def _journey_for(url: str) -> str:
        host = (urlsplit(url).netloc or "").lower()
        if host == "ltm.ripplehire.com":
            return "ripplehire"
        if host == "careers.ltm.com":
            return "successfactors"
        raise ValueError(f"Mindtree source is not an inspected LTM listing: {url}")

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY, headers=HTML_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _ripple_listing(self, label: str) -> dict:
        started = time.perf_counter()
        pages, rows, issues = [], [], []
        total = expected_pages = None
        page = 0
        while True:
            if not self.page_guard():
                issues.append("ripplehire_page_budget_exhausted")
                break
            params = {"careerSiteUrlParams": _ripple_search_params(page)}
            request_started = time.perf_counter()
            response = await self.http.request("POST", RIPPLE_SEARCH_API, data=params, headers=RIPPLE_HEADERS)
            request_seconds = max(0.0, time.perf_counter() - request_started)
            parsed_url = urlsplit(str(response.url))
            if parsed_url.scheme != "https" or parsed_url.netloc != "ltm.ripplehire.com" or parsed_url.path != "/candidate/candidatejobsearch":
                raise ValueError("RippleHire listing redirected outside the company-scoped route")
            page_value = parse_ripple_list_page(response.json(), page)
            if total is None:
                total = page_value["total"]
                expected_pages = max(1, -(-total // RIPPLE_ITEMS_PER_PAGE))
            elif page_value["total"] != total:
                issues.append("ripplehire_listing_total_changed_during_pass")
            issues.extend(page_value["issues"])
            for row in page_value["rows"]:
                if any(existing["jobSeq"] == row["jobSeq"] for existing in rows):
                    issues.append(f"ripplehire_list_duplicate_job_seq_across_pages:{row['jobSeq']}")
                    continue
                rows.append(row)
            pages.append({
                "page": page,
                "start": page_value["start"],
                "job_ids": [row["jobSeq"] for row in page_value["rows"]],
                "total": page_value["total"],
                "request_seconds": request_seconds,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
            })
            if len(rows) >= total or not page_value["rows"]:
                break
            page += 1
            if expected_pages is not None and page >= expected_pages:
                break
        if total is not None and len(rows) != total:
            issues.append(f"ripplehire_listing_rows_do_not_match_total:{len(rows)}!={total}")
        return {"label": label, "rows": rows, "job_ids": [row["jobSeq"] for row in rows],
                "total": total, "expected_pages": expected_pages, "pages": pages,
                "issues": list(dict.fromkeys(issues)),
                "seconds": max(0.0, time.perf_counter() - started)}

    async def _sf_listing(self, label: str) -> dict:
        started = time.perf_counter()
        pages, rows, issues = [], [], []
        total = page_count = None
        start = 0
        while True:
            if not self.page_guard():
                issues.append("successfactors_page_budget_exhausted")
                break
            request_started = time.perf_counter()
            response = await self.http.get(
                SF_LISTING_URL,
                params={"q": "", "sortColumn": "referencedate", "sortDirection": "desc",
                        **({"startrow": start} if start else {})},
                headers=HTML_HEADERS,
            )
            request_seconds = max(0.0, time.perf_counter() - request_started)
            page_value = parse_sf_list_page(response.text, str(response.url), start)
            if total is None:
                total, page_count = page_value["total"], page_value["page_count"]
            elif (page_value["total"], page_value["page_count"]) != (total, page_count):
                issues.append("successfactors_listing_total_changed_during_pass")
            issues.extend(page_value["issues"])
            for row in page_value["rows"]:
                if any(existing["job_id"] == row["job_id"] for existing in rows):
                    issues.append(f"successfactors_list_duplicate_job_id_across_pages:{row['job_id']}")
                    continue
                rows.append(row)
            pages.append({
                "start": start,
                "page": page_value["page"],
                "job_ids": [row["job_id"] for row in page_value["rows"]],
                "total": page_value["total"],
                "request_seconds": request_seconds,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
            })
            if len(rows) >= total or not page_value["rows"]:
                break
            start += SF_PAGE_SIZE
            if page_count is not None and page_value["page"] >= page_count:
                break
        if total is not None and len(rows) != total:
            issues.append(f"successfactors_listing_rows_do_not_match_total:{len(rows)}!={total}")
        return {"label": label, "rows": rows, "job_ids": [row["job_id"] for row in rows],
                "total": total, "pages": pages, "issues": list(dict.fromkeys(issues)),
                "seconds": max(0.0, time.perf_counter() - started)}

    def _ripple_stub(self, row: dict):
        return self.job(
            {"listing": dict(row)},
            title=row["jobTitle"],
            url=f"{RIPPLE_CANDIDATE_URL}#detail/job/{row['jobSeq']}",
            job_id=row["jobSeq"],
            requisition_id=row.get("jobCode"),
            apply_url=f"{RIPPLE_CANDIDATE_URL}#apply/job/{row['jobSeq']}",
            locations=[row["locations"]] if row.get("locations") else [],
            date_posted=None,
        )

    async def _ripple_detail(self, job):
        request_started = time.perf_counter()
        response = await self.http.request(
            "GET", RIPPLE_DETAIL_API,
            params={"token": RIPPLE_TOKEN, "jobSeq": job.job_id, "source": RIPPLE_SOURCE, "lang": "en"},
            headers=RIPPLE_HEADERS,
        )
        request_seconds = max(0.0, time.perf_counter() - request_started)
        parsed = parse_ripple_detail(response.json(), job.job_id)
        fields = parsed["fields"]
        sections = description_sections(parsed["description_html"])
        responsibilities = [s for s in sections if s["heading"] and "responsibilit" in s["heading"].lower()]
        qualifications = [s for s in sections if s["heading"] and any(
            token in s["heading"].lower() for token in ("qualif", "requirement", "skills"))]
        skills_html = fields.get("jobSkills") or ""
        links = [{"text": a.get_text(" ", strip=True), "url": a.get("href")}
                 for a in BeautifulSoup(parsed["description_html"], "lxml").select("a[href]")]
        attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
        company = parsed["company"]
        job.title = parsed["title"]
        job.description_html = parsed["description_html"]
        job.description_method = "mindtree.ripplehire.candidatejobdetail.jobDesc"
        job.description_source_url = str(response.request.url)
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.locations = _ripple_locations(fields)
        job.department = fields.get("bussinessUnit")
        job.employment_type = fields.get("jobTypeCustom3")
        job.salary = _salary(fields.get("compensationRange"), fields.get("compensationInfo"))
        job.employer = {
            "name": company.get("companyName"),
            "legal_employer_code": company.get("companyCd"),
            "region": company.get("multiCompanyName"),
            "parent_code": company.get("parentCode"),
            "currency_code": company.get("currencyCode"),
        }
        job.responsibilities = responsibilities or None
        job.qualifications = {
            "sections": qualifications or None,
            "source_field": "jobSkills",
            "html": skills_html or None,
            "text": text_html(skills_html) if skills_html else None,
        }
        job.raw.update({
            "detail": {
                "source_fields": dict(fields),
                "company_vo": dict(company),
                "job_vo": dict(parsed["job_vo"]),
                "publish_details": parsed["publish_details"],
                "board_job_count": parsed["job_count"],
                "description_sections": sections,
                "description_links": links,
                "role_attachments": attachments,
                "request_seconds": request_seconds,
                "response_sha256": sha256(response.content).hexdigest(),
                "response_bytes": len(response.content),
                "accept_header": RIPPLE_HEADERS["Accept"],
            },
        })
        if normalized_title(job.title) != normalized_title(job.raw["listing"]["jobTitle"]):
            job.warnings.append("listing_and_detail_title_differ")
        if fields.get("jobStatus") and fields["jobStatus"] != "ACTIVE":
            job.warnings.append("detail_job_status_not_active")
        if not normalized_body_text(parsed["description_html"]):
            job.description_unavailable = True
            job.warnings.append("publisher_empty_description")
        if attachments:
            job.warnings.append("role_attachment_requires_review")
        return job

    def _sf_stub(self, row: dict):
        return self.job(
            {"listing": dict(row)},
            title=row["title"],
            url=row["url"],
            job_id=row["job_id"],
            apply_url=f"{SF_ORIGIN}/talentcommunity/apply/{row['job_id']}/?locale=en_US",
            locations=[row["location"]] if row.get("location") else [],
            date_posted=None,
        )

    async def _sf_detail(self, job):
        request_started = time.perf_counter()
        response = await self.http.get(job.url, headers=HTML_HEADERS)
        request_seconds = max(0.0, time.perf_counter() - request_started)
        parsed = parse_sf_detail(response.text, str(response.url))
        job.title = parsed["title"]
        job.requisition_id = parsed["requisition_id"]
        job.description_html = parsed["description_html"]
        job.description_method = "mindtree.successfactors.jobdescription"
        job.description_source_url = parsed["url"]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        locations = [value for value in (job.raw["listing"].get("location"), parsed["location"]) if value]
        job.locations = list(dict.fromkeys(locations))
        job.employer = {"name": parsed["organization"] or "LTM",
                        "source_field": "meta[itemprop=hiringOrganization]"}
        job.salary = _sf_salary(parsed["min_salary"], parsed["max_salary"])
        job.apply_url = parsed["apply_url"] or job.apply_url
        job.raw.update({
            "detail": {
                "job_req_id": parsed["requisition_id"],
                "job_segment": parsed["job_segment"],
                "min_salary_text": parsed["min_salary"],
                "max_salary_text": parsed["max_salary"],
                "description_sections": description_sections(parsed["description_html"]),
                "request_seconds": request_seconds,
                "response_sha256": sha256(response.content).hexdigest(),
                "response_bytes": len(response.content),
            },
        })
        if normalized_title(job.title) != normalized_title(job.raw["listing"]["title"]):
            job.warnings.append("listing_and_detail_title_differ")
        if not normalized_body_text(parsed["description_html"]):
            job.description_unavailable = True
            job.warnings.append("publisher_empty_description")
        return job

    @staticmethod
    def _membership(initial: dict, final: dict) -> dict:
        changed = {}
        if final is not None:
            initial_rows = {row.get("jobSeq") or row.get("job_id"): row for row in initial["rows"]}
            final_rows = {row.get("jobSeq") or row.get("job_id"): row for row in final["rows"]}
            for identity in initial_rows:
                if identity in final_rows and initial_rows[identity] != final_rows[identity]:
                    changed[identity] = sorted(
                        key for key in set(initial_rows[identity]) | set(final_rows[identity])
                        if initial_rows[identity].get(key) != final_rows[identity].get(key)
                    )
        same = final is not None and initial["job_ids"] == final["job_ids"]
        return {
            "initial_ids": initial["job_ids"],
            "final_ids": final["job_ids"] if final else None,
            "initial_total": initial["total"],
            "final_total": final["total"] if final else None,
            "same_ids_and_order": same,
            "changed_listing_fields": changed,
            "new_ids": [i for i in (final["job_ids"] if final else []) if i not in initial["job_ids"]],
            "removed_ids": [i for i in initial["job_ids"] if final is None or i not in final["job_ids"]],
            "complete": bool(same and final and not changed and not initial["issues"] and not final["issues"]),
        }

    async def _run_ripplehire(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_final_url": OFFICIAL_FINAL,
            "journey": "RippleHire (India / United States / Canada / Europe region cards)",
            "listing_url": RIPPLE_CANDIDATE_URL,
            "listing_authority": "The company board behind the RippleHire region cards published by the LTM careers page.",
            "listing_membership_request": {
                "method": "POST", "url": RIPPLE_SEARCH_API,
                "form": {"careerSiteUrlParams": {"page": "0-based page", "search": "*:*",
                                                 "token": RIPPLE_TOKEN, "source": RIPPLE_SOURCE}},
                "page_size": RIPPLE_ITEMS_PER_PAGE,
                "filters_applied": False,
                "scope": "Only the RippleHire tokens linked by the LTM careers page; no other RippleHire account.",
            },
            "detail_method": "GET candidatejobdetail for every jobSeq exposed by the company board.",
            "request_scope": "Only the company-published RippleHire board and its exact jobSeq details.",
        })
        try:
            initial_entry = await self._official_entry()
            initial = await self._ripple_listing("initial")
        except Exception as exc:
            self.result.errors.append(f"Mindtree RippleHire source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result
        self.result.expected_total = initial["total"]
        self.result.metadata["initial_official_entry"] = initial_entry
        self.result.metadata["initial_listing"] = {k: v for k, v in initial.items() if k != "rows"}
        self.result.errors.extend(initial["issues"])
        jobs = [self._ripple_stub(row) for row in initial["rows"]]
        await self.hydrate(jobs, self._ripple_detail)
        final = None
        try:
            final_entry = await self._official_entry()
            final = await self._ripple_listing("final")
        except Exception as exc:
            self.result.errors.append(f"Mindtree RippleHire final recheck failed: {type(exc).__name__}: {exc}")
            final_entry = None
        membership = self._membership(initial, final)
        if final is not None:
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_listing"] = {k: v for k, v in final.items() if k != "rows"}
            self.result.errors.extend(final["issues"])
            if not membership["same_ids_and_order"] or membership["changed_listing_fields"]:
                self.result.errors.append("mindtree_ripplehire_membership_changed_during_collection")
            if initial_entry["region_links"] != final_entry["region_links"]:
                self.result.errors.append("mindtree_official_region_links_changed_during_collection")
        self.result.metadata["membership_reconciliation"] = membership
        self.result.jobs = jobs
        self.result.listing_complete = bool(membership["complete"] and len(jobs) == initial["total"])
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result

    async def _run_successfactors(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_final_url": OFFICIAL_FINAL,
            "journey": "SuccessFactors Career Site Builder ('Everywhere else' region card)",
            "listing_url": SF_LISTING_URL,
            "listing_authority": "The SuccessFactors LTM Jobs site linked by the LTM careers page as 'Everywhere else'.",
            "listing_membership_request": {
                "method": "GET", "url": SF_LISTING_URL,
                "query": {"q": "", "sortColumn": "referencedate", "sortDirection": "desc",
                          "startrow": "0, 25, ..."},
                "page_size": SF_PAGE_SIZE, "filters_applied": False,
                "scope": "The unfiltered public LTM Jobs site only.",
            },
            "detail_method": "GET the exact /job/<slug>/<id>/ page linked from the listing.",
            "request_scope": "Only the company-published LTM Jobs listing and its exact job pages.",
        })
        try:
            initial_entry = await self._official_entry()
            initial = await self._sf_listing("initial")
        except Exception as exc:
            self.result.errors.append(f"Mindtree SuccessFactors source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result
        self.result.expected_total = initial["total"]
        self.result.metadata["initial_official_entry"] = initial_entry
        self.result.metadata["initial_listing"] = {k: v for k, v in initial.items() if k != "rows"}
        self.result.errors.extend(initial["issues"])
        jobs = [self._sf_stub(row) for row in initial["rows"]]
        await self.hydrate(jobs, self._sf_detail)
        final = None
        try:
            final_entry = await self._official_entry()
            final = await self._sf_listing("final")
        except Exception as exc:
            self.result.errors.append(f"Mindtree SuccessFactors final recheck failed: {type(exc).__name__}: {exc}")
            final_entry = None
        membership = self._membership(initial, final)
        if final is not None:
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_listing"] = {k: v for k, v in final.items() if k != "rows"}
            self.result.errors.extend(final["issues"])
            if not membership["same_ids_and_order"] or membership["changed_listing_fields"]:
                self.result.errors.append("mindtree_successfactors_membership_changed_during_collection")
        self.result.metadata["membership_reconciliation"] = membership
        self.result.jobs = jobs
        self.result.listing_complete = bool(membership["complete"] and len(jobs) == initial["total"])
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result

    async def run(self):
        if self.journey == "ripplehire":
            return await self._run_ripplehire()
        return await self._run_successfactors()


def _ripple_search_params(page: int) -> str:
    import json

    return json.dumps({"page": page, "search": "*:*", "token": RIPPLE_TOKEN, "source": RIPPLE_SOURCE},
                      separators=(",", ":"))
