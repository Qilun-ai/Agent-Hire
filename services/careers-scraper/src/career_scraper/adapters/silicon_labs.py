"""Silicon Labs' official careers page and its published Workday tenant.

The company's own careers page publishes a ``Browse Job Openings`` action to
``silabs.wd1.myworkdayjobs.com/SiliconLabsCareers`` plus three category actions
(``Internships``, ``NewCollegeGraduates`` and ``ExperiencedJobs``).  All four
destinations belong to Silicon Labs' own Workday tenant ``silabs``.  The main
site is the complete published membership; the three category sites are
navigation subsets of it (verified by exact requisition ids).

This adapter starts from the Silicon Labs-owned careers page, verifies the
Workday site identities, enumerates the unfiltered main site through the same
CXS request the rendered UI uses, and hydrates only the exact listed job paths.
"""

from __future__ import annotations

from hashlib import sha256
import re
import time
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.silabs.com/about-us/careers"
WORKDAY_HOST = "https://silabs.wd1.myworkdayjobs.com"
WORKDAY_HOSTNAME = "silabs.wd1.myworkdayjobs.com"
TENANT = "silabs"
MAIN_SITE = "SiliconLabsCareers"
CATEGORY_SITES = ("Internships", "NewCollegeGraduates", "ExperiencedJobs")
PUBLISHED_SITES = (MAIN_SITE,) + CATEGORY_SITES
PUBLISHED_MAIN_URL = f"{WORKDAY_HOST}/{MAIN_SITE}"
FEATURED_JOBS_URL = "https://www.silabs.com/sl/workdaydata"
PAGE_SIZE = 20
VERIFICATION_REVISION = "silicon-labs.official-careers-workday-multisite.1"

ENTRY_TITLE = "Careers - Silicon Labs"
ENTRY_H1 = "Careers at Silicon Labs"
ENTRY_DESCRIPTION_PREFIX = "Careers at Silicon Labs."
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SITE_PATH_RE = re.compile(r"^/([A-Za-z0-9_-]+)/?$")
SALARY_PHRASE = re.compile(
    r"(?:annualized\s+base\s+pay\s+range|base\s+pay\s+range|net\s+monthly\s+salary|"
    r"monthly\s+salary|salary\s+range)", re.I)
SALARY_CODE = r"(?:USD|EUR|HUF|SEK|NOK|DKK|INR|SGD|TWD|CNY|RMB)"
SALARY_NUMBER = rf"(?:(?:{SALARY_CODE}|Ft|kr|[$€£])\s?)?\d[\d,.]*"
SALARY_RANGE = re.compile(
    rf"(?:between|from)\s+(?P<min>{SALARY_NUMBER})\s*(?:-|to|–)\s*"
    rf"(?P<max>{SALARY_NUMBER})(?:\s+(?P<code>{SALARY_CODE}))?\s*[.,]?", re.I)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
WORKDAY_HEADERS = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Accept-Language": "en-US",
    "Origin": WORKDAY_HOST,
    "Referer": PUBLISHED_MAIN_URL,
}
FEATURED_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def canonical_body_text(html: str | None) -> str:
    """Canonical visible text used to compare the API body with a rendered page.

    Workday's HTML uses inline spans around links and quotes.  The browser's
    ``innerText`` and BeautifulSoup's text extraction can therefore disagree
    about whitespace immediately around punctuation and quotation marks even
    though the visible words are identical.  This function removes only that
    markup-induced whitespace; it does not remove or rewrite any word.
    """
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    value = re.sub(r"\s+([,.;:!?)\]}'\"])", r"\1", value)
    value = re.sub(r"([(\[{'\"])\s+", r"\1", value)
    return value


def _published_site(path: str | None) -> str | None:
    """Map a Workday site path to the exact company-published site name."""
    match = SITE_PATH_RE.fullmatch(path or "")
    if match is None:
        return None
    value = match.group(1).lower()
    return next((site for site in PUBLISHED_SITES if site.lower() == value), None)


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Confirm the official Silicon Labs careers page and its Workday actions."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname != "www.silabs.com"
            or parsed.path.rstrip("/") != "/about-us/careers"):
        raise ValueError("Silicon Labs official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    heading = soup.select_one("h1")
    heading_text = heading.get_text(" ", strip=True) if heading else ""
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href", "") if canonical else ""
    description = soup.select_one('meta[name="description"]')
    description_text = description.get("content", "") if description else ""
    if (title != ENTRY_TITLE or heading_text != ENTRY_H1
            or canonical_url.rstrip("/") != OFFICIAL_ENTRY
            or not description_text.startswith(ENTRY_DESCRIPTION_PREFIX)):
        raise ValueError("Silicon Labs official careers page identity changed")

    links, issues = [], []
    for anchor in soup.select("a[href]"):
        raw_href = (anchor.get("href") or "").strip()
        if "myworkdayjobs" not in raw_href.lower():
            continue
        target = urlsplit(raw_href)
        if target.hostname != WORKDAY_HOSTNAME:
            raise ValueError(f"Silicon Labs careers page publishes an unexpected Workday host: {raw_href}")
        site = _published_site(target.path)
        if site is None or target.query or target.fragment:
            raise ValueError(f"Silicon Labs careers page publishes an unexpected Workday site: {raw_href}")
        links.append({
            "published_href": raw_href,
            "normalized_url": f"{WORKDAY_HOST}/{site}",
            "site": site,
            "label": normalized_text(anchor.get_text(" ", strip=True)),
            "published_path": target.path,
        })
    grouped: dict[str, list[dict]] = {}
    for link in links:
        grouped.setdefault(link["site"], []).append(link)
    missing = [site for site in PUBLISHED_SITES if site not in grouped]
    if missing:
        raise ValueError(f"Silicon Labs careers page no longer publishes: {missing}")
    labels = {
        site: sorted({link["label"] for link in grouped[site] if link["label"]})
        for site in PUBLISHED_SITES
    }
    if not any("Browse Job Openings" in label for label in labels[MAIN_SITE]):
        raise ValueError("Silicon Labs main Workday action label changed")
    for site in CATEGORY_SITES:
        if not any(label.startswith("Browse ") for label in labels[site]):
            raise ValueError(f"Silicon Labs {site} Workday action label changed")
    if len(grouped) != len(PUBLISHED_SITES):
        issues.append("unexpected_workday_site_count")
    return {
        "url": response_url,
        "title": title,
        "h1": heading_text,
        "canonical_url": canonical_url,
        "meta_description": description_text,
        "workday_links": links,
        "labels": labels,
        "main_published_path": grouped[MAIN_SITE][0]["published_path"],
        "issues": issues,
    }


def parse_workday_shell(html: str, final_url: str, expected_site: str) -> dict:
    """Read and verify the Workday site identity from its public page shell."""
    parsed = urlsplit(final_url)
    if (parsed.scheme != "https" or parsed.hostname != WORKDAY_HOSTNAME
            or (parsed.path.rstrip("/") or "/").lower() != f"/{expected_site}".lower()):
        raise ValueError(f"Silicon Labs {expected_site} Workday shell is not the published site")
    soup = BeautifulSoup(html, "lxml")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href", "") if canonical else ""
    tenant = re.search(r'\btenant\s*:\s*["\']([^"\']+)["\']', html)
    site = re.search(r'\bsiteId\s*:\s*["\']([^"\']+)["\']', html)
    locale = re.search(r'\brequestLocale\s*:\s*["\']([^"\']+)["\']', html)
    identity = {
        "host": parsed.hostname or "",
        "path": parsed.path,
        "canonical_url": canonical_url,
        "tenant": tenant.group(1) if tenant else "",
        "site_id": site.group(1) if site else "",
        "request_locale": locale.group(1) if locale else "",
    }
    if (identity["tenant"] != TENANT or identity["site_id"].lower() != expected_site.lower()
            or identity["request_locale"] != "en-US"
            or canonical_url.rstrip("/").lower() != f"{WORKDAY_HOST}/{expected_site}".lower()):
        raise ValueError(f"Silicon Labs {expected_site} Workday shell identity changed")
    return identity


def parse_listing_page(value: dict, requested_offset: int, *, expected_site: str = MAIN_SITE,
                       expected_total: int | None = None) -> dict:
    """Parse one CXS listing page and check its exact page shape."""
    if not isinstance(value, dict):
        raise ValueError("Silicon Labs Workday listing response is not an object")
    if value.get("errors"):
        raise ValueError(f"Silicon Labs Workday listing returned errors: {value['errors']}")
    total = value.get("total")
    rows = value.get("jobPostings")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError("Silicon Labs Workday listing total is not a non-negative integer")
    if not isinstance(rows, list):
        raise ValueError("Silicon Labs Workday listing jobPostings is not a list")
    if len(rows) > PAGE_SIZE:
        raise ValueError("Silicon Labs Workday listing returned more rows than its page size")
    if requested_offset < 0 or requested_offset % PAGE_SIZE:
        raise ValueError("Silicon Labs Workday listing offset is not a page boundary")

    issues = []
    effective_total = expected_total if expected_total is not None else total
    if effective_total is not None:
        expected_rows = min(PAGE_SIZE, max(0, effective_total - requested_offset))
        if len(rows) != expected_rows:
            issues.append(f"page_row_count:{len(rows)}/{expected_rows}@{requested_offset}")
    parsed_rows, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("listing_row_not_object")
            continue
        path = row.get("externalPath")
        title = row.get("title")
        if not isinstance(path, str) or not path.startswith("/job/"):
            issues.append("listing_row_missing_external_path")
            continue
        if path in seen:
            issues.append(f"duplicate_external_path:{path}")
            continue
        if not isinstance(title, str) or not title.strip():
            issues.append(f"listing_row_missing_title:{path}")
            continue
        if not isinstance(row.get("locationsText"), str) or not row["locationsText"].strip():
            issues.append(f"listing_row_missing_location:{path}")
        if not isinstance(row.get("postedOn"), str) or not row["postedOn"].strip():
            issues.append(f"listing_row_missing_posted_on:{path}")
        bullet_fields = row.get("bulletFields")
        if not isinstance(bullet_fields, list) or not any(
                isinstance(field, str) and field.strip() for field in bullet_fields):
            issues.append(f"listing_row_missing_requisition:{path}")
        parsed_rows.append(dict(row))
        seen.add(path)
    return {
        "site": expected_site,
        "offset": requested_offset,
        "total": total,
        "rows": parsed_rows,
        "facets": value.get("facets") if isinstance(value.get("facets"), list) else [],
        "user_authenticated": value.get("userAuthenticated"),
        "issues": issues,
    }


def _listing_requisition(row: dict) -> str | None:
    for field in row.get("bulletFields") or []:
        if isinstance(field, str) and field.strip():
            return field.strip()
    return None


def parse_detail(value: dict, listing_row: dict, expected_path: str,
                 *, expected_site: str = MAIN_SITE) -> dict:
    """Parse one exact CXS detail response and check its posting identity."""
    if not isinstance(value, dict) or not isinstance(value.get("jobPostingInfo"), dict):
        raise ValueError("Silicon Labs Workday detail response is missing jobPostingInfo")
    info = value["jobPostingInfo"]
    required = ("title", "jobDescription", "jobReqId", "jobPostingId", "jobPostingSiteId",
                "externalUrl", "startDate", "postedOn", "location", "timeType")
    missing = [field for field in required if field not in info]
    if missing:
        raise ValueError(f"Silicon Labs Workday detail is missing fields: {missing}")
    if str(info.get("jobPostingSiteId") or "").lower() != expected_site.lower():
        raise ValueError("Silicon Labs Workday detail belongs to another published site")
    external_url = info.get("externalUrl")
    parsed = urlsplit(external_url if isinstance(external_url, str) else "")
    if (parsed.scheme != "https" or parsed.hostname != WORKDAY_HOSTNAME
            or unquote(parsed.path) != f"/{expected_site}{expected_path}"):
        raise ValueError("Silicon Labs Workday detail externalUrl does not match its listed path")
    posting_id = info.get("jobPostingId")
    expected_posting_id = unquote(expected_path).rsplit("/", 1)[-1]
    if posting_id != expected_posting_id:
        raise ValueError("Silicon Labs Workday detail jobPostingId does not match its listed path")
    listing_requisition = _listing_requisition(listing_row)
    if str(info.get("jobReqId") or "") != listing_requisition:
        raise ValueError("Silicon Labs Workday detail requisition does not match its listing row")
    description = info.get("jobDescription")
    if not isinstance(description, str) or len(description.strip()) < 100:
        raise ValueError("Silicon Labs Workday detail description is empty or truncated")
    issues = []
    start_date = info.get("startDate")
    if not isinstance(start_date, str) or DATE_RE.fullmatch(start_date) is None:
        issues.append("detail_missing_or_invalid_start_date")
    end_date = info.get("endDate")
    if end_date is not None and (not isinstance(end_date, str) or DATE_RE.fullmatch(end_date) is None):
        issues.append("detail_invalid_end_date")
    if normalized_text(info.get("title")) != normalized_text(listing_row.get("title")):
        issues.append("listing_detail_title_changed")
    if info.get("posted") is not True or info.get("canApply") is not True:
        issues.append("detail_posting_or_apply_flag_changed")
    canonical = canonical_body_text(description)
    return {
        "info": info,
        "issues": issues,
        "description_text": text_html(description),
        "description_canonical_chars": len(canonical),
        "description_canonical_fnv1a64": fnv1a64(canonical),
        "description_canonical_sha256": sha256(canonical.encode()).hexdigest(),
        "description_html_sha256": sha256(description.encode()).hexdigest(),
        "listing_requisition": listing_requisition,
    }


def extract_salary(text: str | None) -> dict | None:
    """Keep an exact publisher-stated pay range from the job description.

    Workday exposes no structured salary field for this tenant.  Some postings
    publish an explicit annual base-pay or monthly salary range inside
    ``jobDescription``; this function retains the exact sentence and the
    publisher's own minimum/maximum strings.  It never invents a currency or a
    number: ambiguous ``kr`` ranges keep ``currency`` null with ``currency_text``
    set to the literal ``kr``.
    """
    value = re.sub(r"\s+", " ", text or "").strip()
    match = SALARY_PHRASE.search(value)
    if match is None:
        return None
    start = match.start()
    if value[max(0, start - 4):start].lower() == "the ":
        start -= 4
    tail = value[start:start + 400]
    cut = len(tail)
    for stop in re.finditer(r"\.(?:\s|$)", tail):
        cut = stop.start() + 1
        break
    raw = tail[:cut].strip()
    range_match = SALARY_RANGE.search(raw)
    if range_match is None:
        return None
    min_text = range_match.group("min").strip().rstrip(".,")
    max_text = range_match.group("max").strip().rstrip(".,")
    code = (range_match.group("code") or "").upper()
    currency = None
    currency_text = code or None
    for token in (min_text, max_text):
        lowered = token.lower()
        if token.startswith("$"):
            currency, currency_text = "USD", "$"
        elif token.startswith("€"):
            currency, currency_text = "EUR", "€"
        elif lowered.startswith("ft"):
            currency, currency_text = "HUF", "Ft"
        elif lowered.startswith("huf"):
            currency, currency_text = "HUF", "HUF"
        elif lowered.startswith("kr"):
            currency_text = "kr"
    if code:
        currency, currency_text = code, code
    period = "year" if "annual" in raw.lower() else ("month" if "month" in raw.lower() else None)
    if "base pay" in raw.lower():
        basis = "base pay"
    elif "net monthly" in raw.lower():
        basis = "net monthly salary"
    else:
        basis = "salary range"
    return {
        "raw": raw,
        "min_text": min_text,
        "max_text": max_text,
        "currency": currency,
        "currency_text": currency_text,
        "period": period,
        "basis": basis,
        "source": "jobPostingInfo.jobDescription",
    }


def description_sections(html: str | None) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-silicon-labs-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-silicon-labs-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = normalized_text(block.get_text(" ", strip=True))
        elif block.name in {"p", "div", "section"}:
            strong = block.find(["strong", "b"])
            full_text = normalized_text(block.get_text(" ", strip=True))
            strong_text = normalized_text(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
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
        sections.append(current)
    if not sections:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        section["text"] = text_html(section["html"])
    return sections


def description_links(html: str | None) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    links = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
            continue
        is_attachment = bool(re.search(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", href, re.I))
        links.append({
            "text": normalized_text(anchor.get_text(" ", strip=True)) or None,
            "url": href,
            "is_file_attachment": is_attachment,
        })
    return links


def apply_route(external_url: str, external_path: str, *, expected_site: str = MAIN_SITE) -> dict:
    """Derive the public Apply route observed on the rendered Workday page."""
    parsed = urlsplit(external_url)
    if (parsed.scheme != "https" or parsed.hostname != WORKDAY_HOSTNAME
            or unquote(parsed.path) != f"/{expected_site}{external_path}"):
        raise ValueError("Silicon Labs Apply route requires the exact listed externalUrl")
    return {
        "job_url": external_url,
        "apply_url": external_url.rstrip("/") + "/apply",
        "observed_rendered_apply_path": f"/en-US/{expected_site}{external_path}/apply",
        "observed_apply_anchor": "Apply",
    }


class SiliconLabs(Adapter):
    """Collect the exact public postings reached from Silicon Labs' careers page."""

    VERIFICATION_REVISION = VERIFICATION_REVISION

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._department_by_req: dict[str, str] = {}
        self._worker_subtype_by_req: dict[str, str] = {}

    async def _entry(self) -> dict:
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY, headers=BROWSER_HEADERS)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update({
            "status": response.status_code,
            "seconds": max(0.0, time.perf_counter() - started),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _shell(self, site: str) -> dict:
        started = time.perf_counter()
        response = await self.http.get(f"{WORKDAY_HOST}/{site}", headers=BROWSER_HEADERS)
        parsed = parse_workday_shell(response.text, str(response.url), site)
        parsed.update({
            "status": response.status_code,
            "seconds": max(0.0, time.perf_counter() - started),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _listing_slice(self, site: str, *, facets: dict | None = None,
                             expected_total: int | None = None, purpose: str = "listing") -> dict:
        started = time.perf_counter()
        url = f"{WORKDAY_HOST}/wday/cxs/{TENANT}/{site}/jobs"
        rows, pages, issues = [], [], []
        first_total, first_facets, offset = None, [], 0
        while True:
            if not self.page_guard():
                issues.append("page_budget_exhausted")
                break
            payload = {"appliedFacets": facets or {}, "limit": PAGE_SIZE,
                       "offset": offset, "searchText": ""}
            request_started = time.perf_counter()
            response = await self.http.request("POST", url, json=payload, headers=WORKDAY_HEADERS)
            request_seconds = max(0.0, time.perf_counter() - request_started)
            page = parse_listing_page(
                response.json(), offset, expected_site=site,
                expected_total=first_total if first_total is not None else expected_total,
            )
            if first_total is None:
                first_total = page["total"]
                first_facets = page["facets"]
                if expected_total is not None and first_total != expected_total:
                    page["issues"].append(f"slice_total:{first_total}/{expected_total}")
            rows.extend(page["rows"])
            issues.extend(page["issues"])
            pages.append({
                "offset": offset,
                "status": response.status_code,
                "reported_total": page["total"],
                "rows": len(page["rows"]),
                "request_seconds": request_seconds,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
            })
            if first_total == 0:
                break
            if len(rows) >= first_total:
                break
            if not page["rows"]:
                issues.append("listing_page_empty_before_total")
                break
            offset += len(page["rows"])
        unique, seen = [], set()
        for row in rows:
            path = row["externalPath"]
            if path in seen:
                issues.append(f"duplicate_external_path:{path}")
                continue
            seen.add(path)
            unique.append(row)
        if first_total is not None and len(unique) != first_total:
            issues.append(f"slice_membership_count:{len(unique)}/{first_total}")
        return {
            "purpose": purpose,
            "site": site,
            "url": url,
            "facets": facets or {},
            "total": first_total,
            "rows": unique,
            "external_paths": [row["externalPath"] for row in unique],
            "requisitions": [_listing_requisition(row) for row in unique],
            "page_facets": first_facets,
            "pages": pages,
            "issues": list(dict.fromkeys(issues)),
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def _featured_jobs(self) -> dict:
        started = time.perf_counter()
        response = await self.http.get(FEATURED_JOBS_URL, headers=FEATURED_HEADERS)
        value = response.json()
        entries = value.get("Report_Entry")
        if not isinstance(entries, list):
            raise ValueError("Silicon Labs featured jobs response is missing Report_Entry")
        rows = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            requisition = entry.get("RequisitionID")
            if not isinstance(requisition, str) or not requisition.strip():
                continue
            urls = [
                item.get("External_Posting_URL")
                for item in entry.get("Job_Posting_Details_group") or []
                if isinstance(item, dict) and isinstance(item.get("External_Posting_URL"), str)
            ]
            rows.append({
                "requisition": requisition.strip(),
                "label": entry.get("Job_Requisition"),
                "location": entry.get("Location"),
                "spotlight": entry.get("Spotlight_Job"),
                "posting_urls": urls,
            })
        return {
            "url": FEATURED_JOBS_URL,
            "status": response.status_code,
            "seconds": max(0.0, time.perf_counter() - started),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "rows": rows,
        }

    async def _detail(self, job) -> object:
        listing = job.raw["listing"]
        path = listing["externalPath"]
        listing_site = job.raw.get("listing_site") or MAIN_SITE
        url = f"{WORKDAY_HOST}/wday/cxs/{TENANT}/{listing_site}{path}"
        started = time.perf_counter()
        response = await self.http.get(url, headers=WORKDAY_HEADERS)
        request_seconds = max(0.0, time.perf_counter() - started)
        data = response.json()
        parsed = parse_detail(data, listing, path, expected_site=listing_site)
        info = parsed["info"]
        requisition = str(info["jobReqId"])
        external_url = info["externalUrl"]
        route = apply_route(external_url, path, expected_site=listing_site)
        start_date = info.get("startDate")
        end_date = info.get("endDate")
        locations = [info.get("location")]
        locations.extend(info.get("additionalLocations") or [])
        locations = list(dict.fromkeys(location for location in locations if location))
        sections = description_sections(info.get("jobDescription"))
        links = description_links(info.get("jobDescription"))
        salary = extract_salary(parsed["description_text"])
        responsibilities = [
            section for section in sections
            if section["heading"] and any(token in section["heading"].lower() for token in (
                "responsibilit", "what you will do", "what you'll do", "the role",
                "key responsibilities", "what we are looking for",
            ))
        ]
        qualifications = [
            section for section in sections
            if section["heading"] and any(token in section["heading"].lower() for token in (
                "qualif", "requirement", "what you will need", "what you'll need",
                "skills you will need", "minimum qualifications", "preferred qualifications",
                "ideal applicant profile", "ideal intern profile", "experience",
                "education", "certification", "good to have",
            ))
        ]
        employer = data.get("hiringOrganization")
        if not isinstance(employer, dict):
            employer = {}
        employer_name = normalized_text(employer.get("name")) or None
        job.title = normalized_text(info.get("title")) or job.title
        job.job_id = str(info.get("jobPostingId"))
        job.requisition_id = requisition
        job.url = external_url
        job.apply_url = route["apply_url"]
        job.description_html = info.get("jobDescription") or ""
        job.description_method = "workday.jobPostingInfo.jobDescription"
        job.description_source_url = url
        job.date_posted = start_date if isinstance(start_date, str) and DATE_RE.fullmatch(start_date) else None
        job.date_posted_raw = info.get("postedOn")
        job.date_updated = None
        job.valid_through = end_date if isinstance(end_date, str) and DATE_RE.fullmatch(end_date) else None
        job.locations = locations
        job.department = self._department_by_req.get(requisition)
        job.employment_type = info.get("timeType")
        job.workplace_type = None
        job.salary = salary
        job.employer = employer_name
        job.responsibilities = responsibilities or None
        job.qualifications = qualifications or None
        job.raw.update({
            "detail": data,
            "detail_request": {
                "url": url,
                "final_url": str(response.url),
                "status": response.status_code,
                "request_seconds": request_seconds,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
            },
            "published_detail_fields": sorted(info),
            "description_sections": sections,
            "description_links": links,
            "salary_extraction": salary,
            "description_canonical": {
                "chars": parsed["description_canonical_chars"],
                "fnv1a64": parsed["description_canonical_fnv1a64"],
                "sha256": parsed["description_canonical_sha256"],
                "html_sha256": parsed["description_html_sha256"],
            },
            "dates": {
                "startDate": info.get("startDate"),
                "postedOn": info.get("postedOn"),
                "endDate": info.get("endDate"),
                "jobPostingEndDateAsText": info.get("jobPostingEndDateAsText"),
                "timeLeftToApply": info.get("timeLeftToApply"),
                "posting_date_field": "startDate",
                "closing_date_field": "endDate",
            },
            "facet_membership": {
                "jobFamilyGroup": self._department_by_req.get(requisition),
                "workerSubType": self._worker_subtype_by_req.get(requisition),
            },
            "worker_subtype": self._worker_subtype_by_req.get(requisition),
            "apply_route": {
                **route,
                "canApply": info.get("canApply"),
                "posted": info.get("posted"),
            },
            "listing_detail_identity": {
                "listing_requisition": parsed["listing_requisition"],
                "detail_jobReqId": requisition,
                "listing_title": listing.get("title"),
                "detail_title": info.get("title"),
                "jobPostingSiteId": info.get("jobPostingSiteId"),
            },
        })
        for issue in parsed["issues"]:
            job.warnings.append(issue)
        return job

    async def _facet_membership(self, main: dict) -> dict:
        started = time.perf_counter()
        main_requisitions = [req for req in main["requisitions"] if req]
        main_set = set(main_requisitions)
        result = {"dimensions": {}, "seconds": 0.0}
        for parameter in ("jobFamilyGroup", "workerSubType"):
            facet = next((item for item in main["page_facets"]
                          if item.get("facetParameter") == parameter), None)
            if facet is None or not isinstance(facet.get("values"), list):
                result["dimensions"][parameter] = {"issues": [f"missing_facet:{parameter}"], "values": []}
                continue
            values = [value for value in facet["values"] if isinstance(value, dict) and value.get("id")]
            count_sum = sum(int(value.get("count") or 0) for value in values)
            issues = []
            if count_sum != main["total"]:
                issues.append(f"facet_count_sum:{parameter}:{count_sum}/{main['total']}")
            mapping, per_value = {}, []
            for value in values:
                count = int(value.get("count") or 0)
                if count <= 0:
                    continue
                slice_value = await self._listing_slice(
                    MAIN_SITE, facets={parameter: [value["id"]]},
                    expected_total=count, purpose=f"facet:{parameter}:{value.get('descriptor')}",
                )
                issues.extend(slice_value["issues"])
                for row in slice_value["rows"]:
                    requisition = _listing_requisition(row)
                    if not requisition:
                        continue
                    if requisition in mapping:
                        issues.append(f"facet_duplicate:{parameter}:{requisition}")
                    mapping[requisition] = value.get("descriptor")
                per_value.append({
                    "descriptor": value.get("descriptor"),
                    "id": value["id"],
                    "advertised_count": count,
                    "returned_count": len(slice_value["rows"]),
                    "total": slice_value["total"],
                    "pages": len(slice_value["pages"]),
                    "issues": slice_value["issues"],
                })
            missing = sorted(main_set - set(mapping))
            if missing:
                issues.append(f"facet_membership_missing:{parameter}:{missing[:10]}")
            extra = sorted(set(mapping) - main_set)
            if extra:
                issues.append(f"facet_membership_extra:{parameter}:{extra[:10]}")
            result["dimensions"][parameter] = {
                "values": per_value,
                "mapping": mapping,
                "count_sum": count_sum,
                "issues": list(dict.fromkeys(issues)),
            }
        result["seconds"] = max(0.0, time.perf_counter() - started)
        return result

    async def _final_membership(self, initial_main: dict, initial_categories: dict) -> dict:
        started = time.perf_counter()
        main = await self._listing_slice(MAIN_SITE, purpose="final_main")
        initial_paths = initial_main["external_paths"]
        final_paths = main["external_paths"]
        category_checks = {}
        for site in CATEGORY_SITES:
            current = await self._listing_slice(site, purpose=f"final_category:{site}")
            initial = initial_categories.get(site) or {}
            category_checks[site] = {
                "initial_total": initial.get("total"),
                "final_total": current["total"],
                "same_requisitions": set(initial.get("requisitions") or []) == set(current["requisitions"]),
                "same_external_paths": set(initial.get("external_paths") or []) == set(current["external_paths"]),
                "issues": current["issues"],
            }
        initial_facets = {
            facet.get("facetParameter"): {
                value.get("descriptor"): value.get("count")
                for value in facet.get("values") or [] if isinstance(value, dict)
            }
            for facet in initial_main.get("page_facets") or []
        }
        final_facets = {
            facet.get("facetParameter"): {
                value.get("descriptor"): value.get("count")
                for value in facet.get("values") or [] if isinstance(value, dict)
            }
            for facet in main.get("page_facets") or []
        }
        return {
            "initial_total": initial_main["total"],
            "final_total": main["total"],
            "same_total": initial_main["total"] == main["total"],
            "same_external_paths": set(initial_paths) == set(final_paths),
            "same_order": initial_paths == final_paths,
            "initial_external_paths": initial_paths,
            "final_external_paths": final_paths,
            "categories": category_checks,
            "facet_counts_unchanged": initial_facets == final_facets,
            "initial_facet_counts": initial_facets,
            "final_facet_counts": final_facets,
            "final_pages": main["pages"],
            "issues": list(dict.fromkeys(main["issues"] + [
                issue for check in category_checks.values() for issue in check["issues"]
            ])),
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def run(self):
        if self.company.slug != "silicon-labs":
            raise ValueError("Silicon Labs adapter requires the Silicon Labs company")
        if self.source.url != PUBLISHED_MAIN_URL:
            raise ValueError(f"Silicon Labs source must be its published main Workday site: {PUBLISHED_MAIN_URL}")
        if self.source.evidence_url != OFFICIAL_ENTRY:
            raise ValueError(f"Silicon Labs evidence must be its official careers page: {OFFICIAL_ENTRY}")

        started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": PUBLISHED_MAIN_URL,
            "published_sites": list(PUBLISHED_SITES),
            "listing_authority": (
                "Silicon Labs' own careers page publishes Browse Job Openings to its "
                "Workday tenant site SiliconLabsCareers; the three category actions are "
                "navigation subsets of the same tenant."
            ),
            "listing_membership_request": {
                "method": "POST",
                "path": f"/wday/cxs/{TENANT}/{MAIN_SITE}/jobs",
                "body_shape": {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": "0, then +20", "searchText": ""},
                "filters_applied": False,
                "scope": "Silicon Labs tenant silabs, published site SiliconLabsCareers only",
            },
            "detail_method": "GET /wday/cxs/silabs/SiliconLabsCareers<exact listed externalPath>",
            "request_scope": "Only the exact published sites and exact listed job paths; no ATS-wide discovery.",
            "field_mapping": {
                "job_id": "jobPostingInfo.jobPostingId",
                "requisition_id": "jobPostingInfo.jobReqId",
                "url": "jobPostingInfo.externalUrl",
                "apply_url": "jobPostingInfo.externalUrl + '/apply' (observed Apply route)",
                "date_posted": "jobPostingInfo.startDate",
                "date_posted_raw": "jobPostingInfo.postedOn (relative label, never used as a date)",
                "valid_through": "jobPostingInfo.endDate when published",
                "locations": "jobPostingInfo.location + additionalLocations",
                "employment_type": "jobPostingInfo.timeType",
                "employer": "hiringOrganization.name",
                "department": "jobFamilyGroup facet descriptor, mapped by exact filtered listing",
                "worker_subtype": "workerSubType facet descriptor, mapped by exact filtered listing",
                "salary": "publisher-stated range extracted from jobPostingInfo.jobDescription; null when no range",
                "workplace_type": None,
            },
        })
        try:
            entry = await self._entry()
            shells = {site: await self._shell(site) for site in PUBLISHED_SITES}
            self.result.timings["official_entry_seconds"] = entry["seconds"]
            self.result.timings["workday_shell_seconds"] = sum(item["seconds"] for item in shells.values())
            self.result.metadata["official_entry"] = entry
            self.result.metadata["workday_shells"] = shells

            main = await self._listing_slice(MAIN_SITE, purpose="main")
            self.result.timings["main_listing_seconds"] = main["seconds"]
            self.result.metadata["main_listing"] = {
                key: value for key, value in main.items()
                if key not in ("rows", "page_facets")
            }
            if main["issues"]:
                self.result.errors.extend(f"main_listing:{issue}" for issue in main["issues"])
                self.result.listing_complete = False
            if main["total"] is None or main["total"] == 0 or not main["rows"]:
                self.result.errors.append("silicon_labs_main_listing_empty_or_unavailable")
                self.result.listing_complete = False
                self.result.expected_total = main["total"]
                self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
                return self.result
            self.result.expected_total = main["total"]
            self.result.listing_complete = True
            for row in main["rows"]:
                job = self.job(
                    {"listing": dict(row)},
                    title=row.get("title"), job_id=row["externalPath"],
                    url=f"{WORKDAY_HOST}/{MAIN_SITE}{row['externalPath']}",
                    locations=[row.get("locationsText")],
                    date_posted_raw=row.get("postedOn"),
                )
                job.raw["listing_site"] = MAIN_SITE
                self.add(job)

            categories = {}
            for site in CATEGORY_SITES:
                category = await self._listing_slice(site, purpose=f"category:{site}")
                categories[site] = category
                self.result.timings[f"category_listing_{site}_seconds"] = category["seconds"]
                if category["issues"]:
                    self.result.errors.extend(f"category:{site}:{issue}" for issue in category["issues"])
                    self.result.listing_complete = False
            self.result.metadata["category_listings"] = {
                site: {key: value for key, value in category.items() if key not in ("rows", "page_facets")}
                for site, category in categories.items()
            }
            main_by_req = {
                _listing_requisition(row): row for row in main["rows"] if _listing_requisition(row)
            }
            main_paths = set(main["external_paths"])
            for site, category in categories.items():
                for row in category["rows"]:
                    requisition = _listing_requisition(row)
                    if requisition in main_by_req:
                        parent = main_by_req[requisition]
                        if (normalized_text(parent.get("title")) != normalized_text(row.get("title"))
                                or normalized_text(parent.get("locationsText")) != normalized_text(row.get("locationsText"))):
                            self.result.errors.append(
                                f"category_listing_metadata_mismatch:{site}:{requisition}")
                    elif row["externalPath"] not in main_paths:
                        self.result.errors.append(
                            f"category_listing_new_requisition:{site}:{requisition}")
                        self.result.listing_complete = False
                        job = self.job(
                            {"listing": dict(row)},
                            title=row.get("title"), job_id=row["externalPath"],
                            url=f"{WORKDAY_HOST}/{site}{row['externalPath']}",
                            locations=[row.get("locationsText")],
                            date_posted_raw=row.get("postedOn"),
                        )
                        job.raw["listing_site"] = site
                        self.add(job)

            facets = await self._facet_membership(main)
            self.result.timings["facet_listing_seconds"] = facets["seconds"]
            self.result.metadata["facet_membership"] = facets
            for parameter, dimension in facets["dimensions"].items():
                for issue in dimension["issues"]:
                    self.result.errors.append(f"facet:{parameter}:{issue}")
                if parameter == "jobFamilyGroup":
                    self._department_by_req = dimension.get("mapping") or {}
                elif parameter == "workerSubType":
                    self._worker_subtype_by_req = dimension.get("mapping") or {}

            try:
                featured = await self._featured_jobs()
                featured["missing_from_main"] = sorted(
                    row["requisition"] for row in featured["rows"]
                    if row["requisition"] not in main_by_req
                )
                if featured["missing_from_main"]:
                    self.result.errors.append(
                        f"featured_jobs_missing_from_main:{featured['missing_from_main'][:10]}")
                self.result.timings["featured_jobs_seconds"] = featured["seconds"]
                self.result.metadata["featured_jobs"] = featured
            except Exception as exc:
                self.result.warnings.append("featured_jobs_unavailable")
                self.result.metadata["featured_jobs"] = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "url": FEATURED_JOBS_URL,
                }

            self.result.jobs = await self.hydrate(self.result.jobs, self._detail)
            audit = self._audit_details()
            self.result.metadata["detail_audit"] = audit
            for issue in audit["issues"]:
                self.result.errors.append(issue)
            if not audit["complete"]:
                self.result.listing_complete = False

            final = await self._final_membership(main, categories)
            self.result.timings["final_membership_seconds"] = final["seconds"]
            self.result.metadata["final_membership"] = final
            if final["issues"]:
                self.result.errors.extend(f"final_membership:{issue}" for issue in final["issues"])
            if not (final["same_total"] and final["same_external_paths"] and final["same_order"]):
                self.result.errors.append("silicon_labs_final_membership_changed")
                self.result.listing_complete = False
            if not final["facet_counts_unchanged"]:
                self.result.errors.append("silicon_labs_final_facet_counts_changed")
            if any(not check["same_requisitions"] for check in final["categories"].values()):
                self.result.errors.append("silicon_labs_final_category_membership_changed")
        except Exception as exc:
            self.result.errors.append(f"Silicon Labs source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
            return self.result

        detail_seconds = self.result.timings.get("detail_phase_seconds", 0.0)
        source_seconds = sum(
            value for key, value in self.result.timings.items()
            if key.endswith("_seconds") and key not in ("detail_phase_seconds", "final_membership_seconds",
                                                        "local_validation_seconds")
        )
        self.result.timings["source_collection_seconds"] = round(source_seconds, 6)
        self.result.timings["adapter_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        self.result.metadata["timing_note"] = (
            "source_collection_seconds sums the measured request phases (entry, shells, main/category/facet/"
            "featured listings); detail_phase_seconds is measured separately by the shared hydrator; "
            "final_membership_seconds is a separate post-detail recheck."
        )
        self.result.metadata["detail_seconds"] = detail_seconds
        self.result.metadata["listing_membership_reconciled"] = (
            len(self.result.jobs) == self.result.expected_total
            and not self.result.errors
            and self.result.listing_complete
        )
        return self.result

    def _audit_details(self) -> dict:
        checks, issues = {}, []
        for job in self.result.jobs:
            detail = job.raw.get("detail") if isinstance(job.raw, dict) else None
            info = (detail or {}).get("jobPostingInfo") or {}
            listing = job.raw.get("listing") or {}
            path = listing.get("externalPath")
            requisition = _listing_requisition(listing)
            check = {
                "requisition": requisition,
                "external_path": path,
                "has_detail": bool(info),
                "jobPostingId": info.get("jobPostingId"),
                "jobReqId": info.get("jobReqId"),
                "jobPostingSiteId": info.get("jobPostingSiteId"),
                "startDate": info.get("startDate"),
                "description_html_chars": len(info.get("jobDescription") or ""),
                "description_verified": job.description_verified,
                "date_posted": job.date_posted,
                "apply_url": job.apply_url,
                "department": job.department,
                "worker_subtype": job.raw.get("worker_subtype"),
            }
            if not info:
                issues.append(f"detail_missing:{requisition}")
            if str(info.get("jobReqId") or "") != str(requisition or ""):
                issues.append(f"detail_requisition_mismatch:{requisition}")
            if info.get("jobPostingId") != str(job.job_id):
                issues.append(f"detail_posting_id_mismatch:{requisition}")
            expected_site = (job.raw.get("listing_site") or MAIN_SITE).lower()
            if str(info.get("jobPostingSiteId") or "").lower() != expected_site:
                issues.append(f"detail_wrong_site:{requisition}")
            if not job.description_verified:
                issues.append(f"detail_description_unverified:{requisition}")
            if not job.date_posted:
                issues.append(f"detail_missing_posting_date:{requisition}")
            checks[str(requisition or path)] = check
        return {
            "complete": len(checks) == len(self.result.jobs) and not issues,
            "checked": len(checks),
            "issues": issues,
            "per_job": checks,
        }
