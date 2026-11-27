"""Visa's own careers journey to its two company-scoped Workday sites.

Visa's official careers page publishes ``Job Search`` and ``Find open job
roles`` to the global ``Visa`` Workday site.  Its official Students + Early
Careers page publishes a second ``Visa_Early_Careers`` Workday site.  Both
sites are company-scoped (the official Visa host, the exact Workday tenant
``visa``, and a Visa-only posting site id); this adapter follows both, uses
the native unfiltered CXS listing as membership authority, and reads every
full description from the exact listed job's CXS detail response.

The two boards expose six identical requisitions under different paths
(``REF...`` with and without a ``-1``/``-2`` suffix).  Cross-board
reconciliation in the runner collapses those duplicate exposures by
requisition id and retains the second listing as evidence; this adapter itself
collects exactly the membership exposed by the source it is given.
"""

from __future__ import annotations

from hashlib import sha256
from html import unescape
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .workday import Workday, coordinates
from ..models import text_html


OFFICIAL_ENTRY = "https://corporate.visa.com/en/careers.html"
JOBS_REDIRECT = "https://corporate.visa.com/en/jobs/"
EARLY_CAREERS_PAGE = "https://corporate.visa.com/en/careers/early-careers.html"
WORKDAY_HOST = "visa.wd5.myworkdayjobs.com"
WORKDAY_TENANT = "visa"
MAIN_SITE = "Visa"
EARLY_SITE = "Visa_Early_Careers"
MAIN_LISTING_URL = f"https://{WORKDAY_HOST}/{MAIN_SITE}"
EARLY_LISTING_URL = f"https://{WORKDAY_HOST}/{EARLY_SITE}"
PAGE_SIZE = 20
VERIFICATION_REVISION = "visa.official-careers-two-company-workday-sites.1"
DESCRIPTION_METHOD = "visa.workday.jobPostingInfo.jobDescription"

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REQUISITION = re.compile(r"(?:^|_)(REF[0-9A-Z]+)(?:-\d+)?$")
ATTACHMENT = re.compile(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", re.I)
CURRENCY_CODES = (
    "USD", "EUR", "GBP", "CAD", "AUD", "SGD", "INR", "JPY", "BRL", "MXN",
    "HKD", "NZD", "CHF", "SEK", "PLN", "ZAR", "AED", "CNY",
)
CURRENCY_SYMBOLS = {"$": "$", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}
COUNTRY_CURRENCY = {
    "US": "USD", "CA": "CAD", "AU": "AUD", "SG": "SGD", "IN": "INR",
    "JP": "JPY", "BR": "BRL", "MX": "MXN", "GB": "GBP", "HK": "HKD",
    "NZ": "NZD", "CH": "CHF", "DE": "EUR", "FR": "EUR", "ES": "EUR",
    "IT": "EUR", "NL": "EUR", "IE": "EUR", "PT": "EUR", "AT": "EUR",
    "BE": "EUR", "FI": "EUR", "GR": "EUR", "LU": "EUR", "SK": "EUR",
    "SI": "EUR", "EE": "EUR", "LV": "EUR", "LT": "EUR", "CY": "EUR",
    "MT": "EUR", "PL": "PLN", "SE": "SEK", "ZA": "ZAR", "AE": "AED",
    "CN": "CNY",
}

QUALIFICATION_STARTS = (
    "qualifications",
    "basic qualifications",
    "preferred qualifications",
    "required qualifications",
    "minimum qualifications",
    "what you will need",
    "what qualifications and skills you will need",
    "what we are looking for",
    "what we're looking for",
    "what we'd like from you",
    "requirements",
)
RESPONSIBILITY_STARTS = (
    "key responsibilities",
    "responsibilities",
    "responsibilities include",
    "specific responsibilities include",
    "what you'll do",
    "what you will do",
    "essential functions",
    "what we expect of you",
    "what success looks like",
)
SECTION_STOPS = (
    "information for us applicants",
    "work hours",
    "travel requirements",
    "mental/physical requirements",
    "visa is an eeo employer",
    "pay transparency",
    "about us",
    "job description",
    "additional information",
    "u.s. applicants only",
    "equal opportunity",
    "benefits",
    "compensation",
    "salary range",
    "why this is important to visa",
    "projects you will be a part of",
)
RESPONSIBILITY_STOPS = SECTION_STOPS + (
    "qualifications",
    "basic qualifications",
    "preferred qualifications",
    "what you will need",
    "what qualifications and skills you will need",
    "requirements",
)


def normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_description_text(html: str | None) -> str:
    """Render description text, decoding numeric entities left double-escaped.

    Some Workday job bodies publish ``&amp;#xa;``; one HTML decode leaves the
    literal ``&#xa;`` while the rendered page shows a line break.  Decoding
    numeric entities here makes the stored text match the visible body without
    altering the exact source HTML.
    """
    text = text_html(html)
    return re.sub(
        r"&#(?:x[0-9a-fA-F]+|\d+);",
        lambda match: unescape(match.group(0)),
        text,
    )


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def listing_job_url(site: str, external_path: str) -> str:
    return f"https://{WORKDAY_HOST}/en-US/{site}{external_path}"


def canonical_job_url(site: str, external_path: str) -> str:
    return f"https://{WORKDAY_HOST}/{site}{external_path}"


def api_job_url(api_base: str, external_path: str) -> str:
    return api_base + external_path


def requisition_from_posting_id(value: str | None) -> str | None:
    match = REQUISITION.search(value or "")
    return match.group(1) if match else None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname != "corporate.visa.com"
            or parsed.path not in ("/en/careers.html", "/en/careers")):
        raise ValueError("Visa official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    tenant = soup.select_one('meta[name="tenant"]')
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get("href", "") if canonical else ""
    job_search = [
        urljoin(response_url, anchor.get("href", ""))
        for anchor in soup.select("a[href]")
        if normalized_text(anchor.get_text(" ", strip=True)) == "Job Search"
    ]
    find_roles = [
        urljoin(response_url, anchor.get("href", ""))
        for anchor in soup.select("a[href]")
        if normalized_text(anchor.get_text(" ", strip=True)) == "Find open job roles"
    ]
    early = [
        urljoin(response_url, anchor.get("href", ""))
        for anchor in soup.select("a[href]")
        if normalized_text(anchor.get_text(" ", strip=True)) == "Students + Early Careers"
    ]
    if "Visa careers" not in title or "Progress starts with you" not in title:
        raise ValueError("Visa official careers entry title changed")
    if (tenant is None or tenant.get("content") != "VISA"
            or canonical_url != OFFICIAL_ENTRY):
        raise ValueError("Visa official careers entry no longer identifies the Visa corporate site")
    if job_search != [MAIN_LISTING_URL]:
        raise ValueError("Visa official Job Search destination changed")
    if set(find_roles) != {JOBS_REDIRECT}:
        raise ValueError("Visa official Find open job roles destination changed")
    if early != [EARLY_CAREERS_PAGE]:
        raise ValueError("Visa official Students + Early Careers destination changed")
    return {
        "url": response_url,
        "title": title,
        "canonical_url": canonical_url,
        "tenant": tenant.get("content"),
        "job_search_url": job_search[0],
        "find_open_job_roles_url": JOBS_REDIRECT,
        "early_careers_url": EARLY_CAREERS_PAGE,
    }


def parse_early_careers_page(html: str, response_url: str = EARLY_CAREERS_PAGE) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname != "corporate.visa.com"
            or parsed.path not in ("/en/careers/early-careers.html", "/en/careers/early-careers")):
        raise ValueError("Visa early-careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    tenant = soup.select_one('meta[name="tenant"]')
    find_roles = [
        urljoin(response_url, anchor.get("href", ""))
        for anchor in soup.select("a[href]")
        if normalized_text(anchor.get_text(" ", strip=True)) == "Find open student and early career roles"
    ]
    talent = [
        urljoin(response_url, anchor.get("href", ""))
        for anchor in soup.select("a[href]")
        if normalized_text(anchor.get_text(" ", strip=True)) == "Join our Talent Community"
    ]
    job_search = [
        urljoin(response_url, anchor.get("href", ""))
        for anchor in soup.select("a[href]")
        if normalized_text(anchor.get_text(" ", strip=True)) == "Job Search"
    ]
    if "Students and Early Careers" not in title or "Ignite a career" not in title:
        raise ValueError("Visa early-careers page title changed")
    if tenant is None or tenant.get("content") != "VISA":
        raise ValueError("Visa early-careers page no longer identifies the Visa corporate site")
    if len(find_roles) != 1 or not find_roles[0].startswith(EARLY_LISTING_URL + "/page/"):
        raise ValueError("Visa early-careers published listing destination changed")
    if not any(urlsplit(url).path.rstrip("/") in {
        "/Visa_Early_Careers/introduceYourself", "/en-US/Visa_Early_Careers/introduceYourself",
    } for url in talent):
        raise ValueError("Visa early-careers Talent Community destination changed")
    if job_search != [MAIN_LISTING_URL]:
        raise ValueError("Visa early-careers Job Search destination changed")
    return {
        "url": response_url,
        "title": title,
        "tenant": tenant.get("content"),
        "published_listing_url": find_roles[0],
        "listing_site_url": EARLY_LISTING_URL,
        "talent_community_urls": talent,
        "job_search_url": job_search[0],
    }


def parse_listing_page(value: dict, requested_offset: int, *,
                       page_size: int = PAGE_SIZE, expected_total: int | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Visa Workday listing response is not an object")
    rows = value.get("jobPostings")
    reported_total = value.get("total")
    if not isinstance(rows, list):
        raise ValueError("Visa Workday listing response is missing jobPostings")
    if isinstance(reported_total, bool) or not isinstance(reported_total, int) or reported_total < 0:
        raise ValueError("Visa Workday listing response has an invalid total")
    total = reported_total if expected_total is None else expected_total
    issues = []
    if expected_total is not None and reported_total not in (0, expected_total):
        issues.append(f"reported_total_changed:{expected_total}->{reported_total}")
    if len(rows) > page_size:
        issues.append(f"page_over_limit:{len(rows)}>{page_size}")
    clean = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            issues.append(f"row_{index}_not_object")
            continue
        path = row.get("externalPath")
        title = row.get("title")
        if not isinstance(path, str) or not path.startswith("/job/"):
            issues.append(f"row_{index}_bad_external_path")
            continue
        if not isinstance(title, str) or not title.strip():
            issues.append(f"row_{index}_missing_title")
            continue
        if not isinstance(row.get("locationsText"), str):
            issues.append(f"row_{index}_missing_locations_text")
        if not isinstance(row.get("postedOn"), str):
            issues.append(f"row_{index}_missing_posted_on")
        if row.get("bulletFields") is not None and not isinstance(row["bulletFields"], list):
            issues.append(f"row_{index}_bad_bullet_fields")
        clean.append(row)
    return {
        "total": total,
        "reported_total": reported_total,
        "requested_offset": requested_offset,
        "page_size": page_size,
        "rows": clean,
        "returned": len(clean),
        "issues": issues,
        "total_changed": expected_total is not None and reported_total not in (0, expected_total),
    }


def parse_detail(value: dict, listing_row: dict, *, site: str, api_base: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Visa Workday detail response is not an object")
    info = value.get("jobPostingInfo")
    if not isinstance(info, dict):
        raise ValueError("Visa Workday detail response is missing jobPostingInfo")
    path = listing_row.get("externalPath")
    if not isinstance(path, str) or not path.startswith("/job/"):
        raise ValueError("Visa listing row has no exact externalPath")
    path_tail = path.rsplit("/", 1)[-1]
    title = info.get("title")
    posting_id = info.get("jobPostingId")
    site_id = info.get("jobPostingSiteId")
    external_url = info.get("externalUrl")
    description = info.get("jobDescription")
    bullets = listing_row.get("bulletFields") or []
    req = info.get("jobReqId") or requisition_from_posting_id(posting_id) or requisition_from_posting_id(path_tail)
    checks = {
        "detail_title_matches_listing": normalized_text(title) == normalized_text(listing_row.get("title")),
        "detail_posting_id_matches_path": posting_id == path_tail,
        "detail_site_matches": site_id == site,
        "detail_external_url_matches": external_url == canonical_job_url(site, path),
        "detail_requisition_matches_listing_bullet": not bullets or req in bullets,
        "detail_requisition_present": bool(req),
        "detail_description_present": isinstance(description, str) and bool(description.strip()),
    }
    description = description if isinstance(description, str) else ""
    text = clean_description_text(description)
    location = info.get("location")
    additional = info.get("additionalLocations") or []
    locations = []
    for item in ([location] if isinstance(location, str) else []) + (
            additional if isinstance(additional, list) else []):
        if isinstance(item, str) and item and item not in locations:
            locations.append(item)
    if not locations and isinstance(listing_row.get("locationsText"), str):
        locations = [listing_row["locationsText"]]
    country = info.get("country") if isinstance(info.get("country"), dict) else {}
    country_code = country.get("alpha2Code")
    if not country_code and isinstance(info.get("jobRequisitionLocation"), dict):
        country_code = (info["jobRequisitionLocation"].get("country") or {}).get("alpha2Code")
    employer = value.get("hiringOrganization") if isinstance(value.get("hiringOrganization"), dict) else {}
    employer_name = employer.get("name") if isinstance(employer.get("name"), str) else None
    employer_name = employer_name.strip() or None
    start = info.get("startDate")
    end = info.get("endDate")
    salary = parse_salary(text, country_code)
    qualifications, responsibilities = parse_sections(text)
    attachments = attachment_links(description)
    job_url = listing_job_url(site, path)
    return {
        "info": info,
        "checks": checks,
        "requisition_id": req,
        "title": title if isinstance(title, str) and title.strip() else listing_row.get("title"),
        "description_html": description,
        "description_text": text,
        "description_normalized_chars": len(normalized_text(text)),
        "description_fnv1a64": fnv1a64(normalized_text(text)),
        "date_posted": start if isinstance(start, str) and ISO_DATE.fullmatch(start) else None,
        "date_posted_raw": {
            "postedOn": info.get("postedOn"),
            "startDate": start,
            "jobPostingEndDateAsText": info.get("jobPostingEndDateAsText"),
            "timeLeftToApply": info.get("timeLeftToApply"),
        },
        "valid_through": end if isinstance(end, str) and ISO_DATE.fullmatch(end) else None,
        "locations": locations,
        "employment_type": info.get("timeType"),
        "workplace_type": info.get("remoteType") or listing_row.get("remoteType"),
        "employer": employer_name,
        "salary": salary,
        "qualifications": qualifications,
        "responsibilities": responsibilities,
        "attachments": attachments,
        "job_url": job_url,
        "apply_url": job_url + "/apply",
        "api_url": api_job_url(api_base, path),
        "language": "en-US",
        "source_ui": {
            "listing_url": job_url,
            "detail_page_url": job_url,
            "apply_url": job_url + "/apply",
            "api_detail_url": api_job_url(api_base, path),
            "listing_title": listing_row.get("title"),
            "listing_locations_text": listing_row.get("locationsText"),
            "listing_posted_on": listing_row.get("postedOn"),
            "listing_remote_type": listing_row.get("remoteType"),
            "listing_bullet_fields": bullets,
            "job_posting_site_id": site_id,
            "logo_image": info.get("logoImage"),
            "country": country,
            "job_requisition_location": info.get("jobRequisitionLocation"),
            "can_apply": info.get("canApply"),
            "posted": info.get("posted"),
            "time_type": info.get("timeType"),
            "remote_type": info.get("remoteType"),
            "additional_locations": additional,
            "end_date_text": info.get("jobPostingEndDateAsText"),
            "time_left_to_apply": info.get("timeLeftToApply"),
            "employer_name": employer_name,
            "salary": salary,
            "qualifications_excerpt": qualifications,
            "responsibilities_excerpt": responsibilities,
            "attachment_links": attachments,
        },
    }


def attachment_links(description_html: str) -> list[dict]:
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not ATTACHMENT.search(href):
            continue
        values.append({
            "url": href,
            "label": anchor.get_text(" ", strip=True) or None,
            "source_field": "jobPostingInfo.jobDescription",
        })
    return values


def parse_sections(description_text: str) -> tuple[str | None, str | None]:
    lines = [line.strip() for line in (description_text or "").splitlines() if line.strip()]

    def heading(line):
        return re.sub(r"\s+", " ", line).strip().rstrip(":").lower()

    def stop(lines, start, markers):
        for index in range(start + 1, len(lines)):
            if heading(lines[index]) in markers:
                return index
        return len(lines)

    qualifications = None
    for index, line in enumerate(lines):
        if heading(line) not in QUALIFICATION_STARTS:
            continue
        end = stop(lines, index, SECTION_STOPS)
        excerpt = "\n".join(lines[index:end]).strip()
        content = "\n".join(lines[index + 1:end]).strip()
        if len(excerpt) >= 40:
            qualifications = excerpt
            break
        if content.lower() in {"n/a", "none", "not applicable"}:
            qualifications = content
            break

    responsibilities = None
    for index, line in enumerate(lines):
        key = heading(line)
        is_responsibility = key in RESPONSIBILITY_STARTS or (
            key.startswith("what a ") and key.endswith(" does at visa")
        )
        if not is_responsibility:
            continue
        end = stop(lines, index, RESPONSIBILITY_STOPS)
        excerpt = "\n".join(lines[index:end]).strip()
        if len(excerpt) >= 40:
            responsibilities = excerpt
            break
    return qualifications, responsibilities


def parse_amount(token: str) -> float | None:
    value = re.sub(r"[^\d.,]", "", token.replace("\xa0", " "))
    if not value:
        return None
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        parts = value.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            value = parts[0] + "." + parts[1]
        else:
            value = value.replace(",", "")
    elif "." in value:
        parts = value.split(".")
        if len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3:
            value = parts[0] + parts[1]
    try:
        return float(value)
    except ValueError:
        return None


def salary_currency(text: str, country_code: str | None) -> str | None:
    for code in CURRENCY_CODES:
        if re.search(r"\b" + code + r"\b", text):
            return code
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return COUNTRY_CURRENCY.get(country_code) if code == "$" else code
    return None


def salary_period(text: str) -> str | None:
    lowered = text.lower()
    for pattern, value in (
        (r"\bper\s+year\b|\bper\s+annum\b|\bannually\b|\bannual\b", "year"),
        (r"\bper\s+hour\b|\bhourly\b", "hour"),
        (r"\bper\s+month\b|\bmonthly\b", "month"),
        (r"\bper\s+week\b|\bweekly\b", "week"),
        (r"\bper\s+day\b|\bdaily\b", "day"),
    ):
        if re.search(pattern, lowered):
            return value
    return None


AMOUNT = re.compile(
    r"(?:(?:" + "|".join(CURRENCY_CODES) + r")\s*[$€£¥₹]?\s*(?:\d[\d.,\s]*\d|\d))"
    r"|(?:[$€£¥₹]\s*(?:\d[\d.,\s]*\d|\d))"
    r"|(?:(?:\d[\d.,\s]*\d|\d)\s*(?:" + "|".join(CURRENCY_CODES) + r"))"
)


def amounts_in(text: str) -> list[tuple[str, float]]:
    values = []
    for match in AMOUNT.finditer(text):
        raw = match.group(0)
        value = parse_amount(raw)
        if value is not None:
            values.append((raw, value))
    return values


def parse_salary(description_text: str, country_code: str | None = None) -> dict | None:
    candidates = []
    for line in (description_text or "").splitlines():
        line = line.strip()
        lowered = line.lower()
        if not line or not re.search(r"salary|pay\s+range|compensation", lowered):
            continue
        if not re.search(r"[$€£¥₹]|\b(?:" + "|".join(CURRENCY_CODES) + r")\b", line):
            continue
        amounts = amounts_in(line)
        score = 0
        if re.search(r"salary\s+range|pay\s+range|compensation\s+range", lowered):
            score += 10
        if "estimated" in lowered:
            score += 2
        if "annual salary" in lowered:
            score += 5
        if amounts:
            score += 1
        candidates.append((score, line, amounts))
    if not candidates:
        return None
    candidates.sort(key=lambda item: -item[0])
    _, source_text, amounts = candidates[0]
    if len(amounts) >= 2:
        minimum, maximum = amounts[0][1], amounts[1][1]
        incomplete = False
    elif amounts:
        minimum = amounts[0][1]
        maximum = None if re.search(r"\bto\b|\s-\s", source_text) else minimum
        incomplete = maximum is None
    else:
        minimum = maximum = None
        incomplete = True
    return {
        "source_field": "jobPostingInfo.jobDescription",
        "source_text": source_text,
        "currency": salary_currency(source_text, country_code),
        "min": minimum,
        "max": maximum,
        "period": salary_period(source_text),
        "incomplete": incomplete,
        "amounts": [raw for raw, _ in amounts[:4]],
    }


def reconcile_cross_board_duplicates(main_jobs: list, early_jobs: list) -> dict:
    """Collapse identical requisitions exposed by both Visa Workday sites.

    Returns the kept early-career jobs, the duplicate evidence, and conflict
    evidence.  A duplicate requires the same requisition id, title, location
    set, description sha256, start date and end date.  Anything else is kept
    as a distinct posting and reported as a conflict.
    """
    main_by_requisition = {
        job.requisition_id: job for job in main_jobs if job.requisition_id
    }
    kept, duplicates, conflicts = [], [], []
    for job in early_jobs:
        main = main_by_requisition.get(job.requisition_id)
        if main is None:
            kept.append(job)
            continue
        same = (
            normalized_text(main.title) == normalized_text(job.title)
            and sorted(main.locations) == sorted(job.locations)
            and main.description_sha256 == job.description_sha256
            and main.date_posted == job.date_posted
            and main.valid_through == job.valid_through
        )
        evidence = {
            "requisition_id": job.requisition_id,
            "main_job_url": main.url,
            "early_job_url": job.url,
            "main_external_path": main.raw.get("externalPath"),
            "early_external_path": job.raw.get("externalPath"),
            "main_source": main.source,
            "early_source": job.source,
            "main_title": main.title,
            "early_title": job.title,
            "main_locations": main.locations,
            "early_locations": job.locations,
            "main_date_posted": main.date_posted,
            "early_date_posted": job.date_posted,
            "main_valid_through": main.valid_through,
            "early_valid_through": job.valid_through,
            "main_description_sha256": main.description_sha256,
            "early_description_sha256": job.description_sha256,
            "identical_posting": same,
        }
        if same:
            duplicates.append(evidence)
            main.raw.setdefault("duplicate_listings", []).append(evidence)
        else:
            conflicts.append(evidence)
            kept.append(job)
    return {
        "kept_early_jobs": kept,
        "duplicates": duplicates,
        "conflicts": conflicts,
    }


def audit_jobs(jobs: list) -> dict:
    """Audit the fields actually retained on a set of hydrated jobs."""
    started = time.perf_counter()
    audit = {
        "jobs": len(jobs),
        "identity_verified": 0,
        "identity_failed": [],
        "missing_description": [],
        "missing_requisition": [],
        "missing_employer": 0,
        "salary_published": 0,
        "salary_incomplete": 0,
        "qualifications_published": 0,
        "responsibilities_published": 0,
        "attachments": 0,
        "date_posted_present": 0,
        "valid_through_present": 0,
        "workplace_type_present": 0,
        "description_min_chars": None,
        "description_max_chars": None,
    }
    lengths = []
    for job in jobs:
        checks = job.raw.get("detail_audit") or {}
        if checks and all(checks.values()):
            audit["identity_verified"] += 1
        elif checks:
            audit["identity_failed"].append({
                "id": job.requisition_id or job.job_id,
                "url": job.url,
                "checks": checks,
            })
        if not job.description_text.strip():
            audit["missing_description"].append(job.requisition_id or job.job_id)
        if not job.requisition_id:
            audit["missing_requisition"].append(job.url)
        if not job.employer:
            audit["missing_employer"] += 1
        if job.salary:
            audit["salary_published"] += 1
            if job.salary.get("incomplete"):
                audit["salary_incomplete"] += 1
        if job.qualifications:
            audit["qualifications_published"] += 1
        if job.responsibilities:
            audit["responsibilities_published"] += 1
        if job.raw.get("attachments"):
            audit["attachments"] += len(job.raw["attachments"])
        if job.date_posted:
            audit["date_posted_present"] += 1
        if job.valid_through:
            audit["valid_through_present"] += 1
        if job.workplace_type:
            audit["workplace_type_present"] += 1
        lengths.append(len(job.description_text))
    if lengths:
        audit["description_min_chars"] = min(lengths)
        audit["description_max_chars"] = max(lengths)
    audit["duration_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
    return audit


class Visa(Workday):
    """Follow the exact Visa-published Workday site and hydrate every posting."""

    async def _crawl_listing(self, api_base: str, site: str, *, phase: str) -> dict:
        started = time.perf_counter()
        offset = 0
        pages = 0
        total = None
        rows, paths, offsets, errors = [], [], [], []
        seen = set()
        while True:
            if pages >= self.options.max_pages:
                errors.append(f"{phase}_page_budget_exhausted")
                break
            payload = {
                "appliedFacets": {},
                "limit": PAGE_SIZE,
                "offset": offset,
                "searchText": "",
            }
            data = await self.http.json(api_base + "/jobs", method="POST", json=payload)
            pages += 1
            try:
                page = parse_listing_page(data, offset, expected_total=total)
            except ValueError as exc:
                errors.append(f"{phase}_listing_invalid:{exc}")
                break
            if total is None:
                total = page["total"]
                if total == 0:
                    errors.append(f"{phase}_listing_empty_total")
                    break
            if page["issues"]:
                errors.extend(f"{phase}_{issue}" for issue in page["issues"])
            page_rows = page["rows"]
            if not page_rows:
                errors.append(f"{phase}_empty_page_at_offset_{offset}")
                break
            offsets.append(offset)
            for row in page_rows:
                path = row["externalPath"]
                if path in seen:
                    errors.append(f"{phase}_duplicate_external_path:{path}")
                    continue
                seen.add(path)
                paths.append(path)
                rows.append(row)
            offset += len(page_rows)
            if offset >= total:
                break
            if offset > total + PAGE_SIZE:
                errors.append(f"{phase}_listing_exceeded_total:{offset}/{total}")
                break
        if total is None:
            total = 0
        if len(paths) != len(set(paths)):
            errors.append(f"{phase}_duplicate_paths")
        if total and len(paths) != total:
            errors.append(f"{phase}_membership_count:{len(paths)}/{total}")
        return {
            "phase": phase,
            "site": site,
            "api_base": api_base,
            "total": total,
            "pages": pages,
            "page_offsets": offsets,
            "rows": rows,
            "paths": paths,
            "returned_rows": len(rows),
            "unique_paths": len(set(paths)),
            "errors": errors,
            "duration_seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    def _add_listing_rows(self, rows: list[dict], *, site: str, start_index: int = 0) -> list:
        added = []
        for index, row in enumerate(rows, start=start_index):
            path = row["externalPath"]
            raw = dict(row)
            raw.update({
                "listing_site": site,
                "listing_index": index,
                "listing_url": listing_job_url(site, path),
                "canonical_listing_url": canonical_job_url(site, path),
            })
            job = self.job(
                raw,
                title=row.get("title"),
                job_id=path,
                url=listing_job_url(site, path),
                locations=[row.get("locationsText")] if row.get("locationsText") else [],
                date_posted_raw=row.get("postedOn"),
            )
            self.add(job)
            added.append(job)
            if self.limit():
                break
        return added

    async def _detail(self, job):
        site = job.raw["listing_site"]
        api_base = self.result.metadata["workday_api_base"]
        path = job.raw["externalPath"]
        value = await self.http.json(api_job_url(api_base, path))
        parsed = parse_detail(value, job.raw, site=site, api_base=api_base)
        job.raw["detail"] = value
        job.raw["detail_audit"] = parsed["checks"]
        job.raw["source_ui"] = parsed["source_ui"]
        job.raw["attachments"] = parsed["attachments"]
        job.title = parsed["title"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = DESCRIPTION_METHOD
        job.description_source_url = parsed["api_url"]
        job.requisition_id = parsed["requisition_id"]
        job.job_id = parsed["requisition_id"] or path.rsplit("/", 1)[-1]
        job.apply_url = parsed["apply_url"]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.valid_through = parsed["valid_through"]
        job.locations = parsed["locations"]
        job.employment_type = parsed["employment_type"]
        job.workplace_type = parsed["workplace_type"]
        job.employer = parsed["employer"]
        job.salary = parsed["salary"]
        job.qualifications = parsed["qualifications"]
        job.responsibilities = parsed["responsibilities"]
        job.language = parsed["language"]
        if not all(parsed["checks"].values()):
            job.warnings.append("visa_detail_identity_unverified")
            job.description_unavailable = True
            identity = parsed["requisition_id"] or path
            failed = [name for name, passed in parsed["checks"].items() if not passed]
            self.result.errors.append(f"visa_detail_identity_failed:{identity}:{','.join(failed)}")
        return job

    def _audit_details(self) -> dict:
        return audit_jobs(self.result.jobs)

    async def _reconcile_membership(self, api_base: str, site: str, initial: dict) -> dict:
        started = time.perf_counter()
        final = await self._crawl_listing(api_base, site, phase="final")
        initial_paths = [job.raw.get("externalPath") for job in self.result.jobs]
        final_paths = final["paths"]
        new_paths = [path for path in final_paths if path not in set(initial_paths)]
        removed_paths = [path for path in initial_paths if path not in set(final_paths)]
        repairs = []
        if new_paths or removed_paths or final["total"] != self.result.expected_total:
            rows_by_path = {row["externalPath"]: row for row in final["rows"]}
            new_rows = [rows_by_path[path] for path in new_paths if path in rows_by_path]
            if new_rows:
                new_jobs = self._add_listing_rows(new_rows, site=site, start_index=len(initial_paths))
                await self.hydrate(new_jobs, self._detail)
                repairs.append({"added": [job.raw.get("externalPath") for job in new_jobs]})
            if removed_paths:
                keep = set(final_paths)
                self.result.jobs[:] = [
                    job for job in self.result.jobs if job.raw.get("externalPath") in keep
                ]
                self.seen = {job.key for job in self.result.jobs}
                repairs.append({"removed": removed_paths})
            self.result.expected_total = final["total"] or None
            if final["total"]:
                recheck = await self._crawl_listing(api_base, site, phase="final_recheck")
                final = recheck
                final_paths = recheck["paths"]
                repairs.append({"recheck_paths": len(final_paths)})
        current_paths = [job.raw.get("externalPath") for job in self.result.jobs]
        same_set = set(current_paths) == set(final_paths)
        unresolved_new = [path for path in final_paths if path not in set(current_paths)]
        unresolved_removed = [path for path in current_paths if path not in set(final_paths)]
        complete = (
            not final["errors"]
            and same_set
            and final["total"] == len(current_paths) == len(self.result.jobs)
            and not unresolved_new
            and not unresolved_removed
        )
        result = {
            "initial_total": initial["total"],
            "initial_pages": initial["pages"],
            "initial_returned_rows": initial["returned_rows"],
            "initial_unique_paths": initial["unique_paths"],
            "initial_paths_sha256": sha256(
                "\n".join(sorted(initial["paths"])).encode()
            ).hexdigest(),
            "final_total": final["total"],
            "final_pages": final["pages"],
            "final_returned_rows": final["returned_rows"],
            "final_unique_paths": final["unique_paths"],
            "final_paths_sha256": sha256(
                "\n".join(sorted(final_paths)).encode()
            ).hexdigest(),
            "same_id_set": same_set,
            "new_paths": new_paths,
            "removed_paths": removed_paths,
            "unresolved_new_paths": unresolved_new,
            "unresolved_removed_paths": unresolved_removed,
            "repairs": repairs,
            "errors": final["errors"],
            "duration_seconds": round(max(0.0, time.perf_counter() - started), 6),
        }
        if not complete:
            self.result.errors.extend(
                f"visa_membership_unreconciled:{error}" for error in final["errors"]
            )
            if not same_set:
                self.result.errors.append("visa_membership_id_set_changed")
            if final["total"] != len(self.result.jobs):
                self.result.errors.append(
                    f"visa_membership_count:{final['total']}/{len(self.result.jobs)}"
                )
        self.result.listing_complete = complete
        return result

    async def run(self):
        started = time.perf_counter()
        if self.company.careers_url != OFFICIAL_ENTRY:
            raise ValueError("Visa adapter must start at its official careers entry")
        origin, tenant, site = coordinates(self.source.url)
        if origin != f"https://{WORKDAY_HOST}" or tenant != WORKDAY_TENANT:
            raise ValueError("Visa adapter source is not the official Visa Workday tenant")
        if site not in {MAIN_SITE, EARLY_SITE}:
            raise ValueError("Visa adapter source is not one of Visa's published Workday sites")

        entry_started = time.perf_counter()
        if site == MAIN_SITE:
            entry = await self.http.get(OFFICIAL_ENTRY)
            entry_identity = parse_official_entry(entry.text, str(entry.url))
            redirect = await self.http.get(JOBS_REDIRECT)
            if str(redirect.url) != MAIN_LISTING_URL:
                raise ValueError(
                    f"Visa Find open job roles no longer redirects to the global Workday site: {redirect.url}"
                )
            entry_identity["jobs_redirect"] = {
                "request_url": JOBS_REDIRECT,
                "final_url": str(redirect.url),
                "status": redirect.status_code,
                "history": [str(item.url) for item in redirect.history],
                "content_type": redirect.headers.get("content-type"),
            }
            published_label = "Job Search / Find open job roles"
            published_listing_url = MAIN_LISTING_URL
            source_kind = "main_global"
        else:
            entry = await self.http.get(EARLY_CAREERS_PAGE)
            entry_identity = parse_early_careers_page(entry.text, str(entry.url))
            published_label = "Find open student and early career roles"
            published_listing_url = EARLY_LISTING_URL
            source_kind = "early_careers"
        self.result.timings["official_entry_seconds"] = round(
            max(0.0, time.perf_counter() - entry_started), 6)

        api_base = f"{origin}/wday/cxs/{tenant}/{site}"
        self.result.metadata.update({
            "verification_revision": VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_identity": entry_identity,
            "published_link_label": published_label,
            "published_listing_url": published_listing_url,
            "workday_site": site,
            "workday_tenant": tenant,
            "workday_api_base": api_base,
            "source_kind": source_kind,
            "listing_authority": (
                "Visa's official careers journey to its company-scoped Workday site; "
                "unfiltered native CXS listing is membership authority."
            ),
            "request_source": "Published Workday CXS unfiltered jobs listing and exact listed-job detail requests",
            "native_page_size": PAGE_SIZE,
            "independent_rendered_reference": "companies/visa/browser-reference-worker.json",
            "talent_community_excluded": (
                "Visa Workday introduceYourself talent-community intakes are not job postings and are not enumerated"
            ),
        })

        collection_started = time.perf_counter()
        initial = await self._crawl_listing(api_base, site, phase="initial")
        self.result.timings["source_collection_seconds"] = round(
            max(0.0, time.perf_counter() - collection_started), 6)
        self.result.pages = initial["pages"]
        self.result.expected_total = initial["total"] or None
        self.result.metadata["initial_membership"] = initial
        if initial["errors"]:
            self.result.errors.extend(initial["errors"])
        if not initial["total"]:
            self.result.errors.append("visa_listing_has_no_positive_total")
            self.result.listing_complete = False
            self.result.metadata["initial_page_count"] = initial["pages"]
            return self.result

        self._add_listing_rows(initial["rows"], site=site)
        self.result.metadata["listing_rows"] = len(initial["rows"])
        self.result.metadata["initial_page_count"] = initial["pages"]
        self.result.metadata["initial_expected_total"] = initial["total"]

        await self.hydrate(self.result.jobs, self._detail)
        self.result.timings["detail_phase_seconds"] = self.result.timings.get(
            "detail_phase_seconds", 0.0)

        audit_started = time.perf_counter()
        self.result.metadata["detail_audit"] = self._audit_details()
        self.result.timings["detail_audit_seconds"] = round(
            max(0.0, time.perf_counter() - audit_started), 6)

        reconciliation = await self._reconcile_membership(api_base, site, initial)
        self.result.timings["final_membership_seconds"] = reconciliation["duration_seconds"]
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.metadata["final_expected_total"] = reconciliation["final_total"]

        self.result.timings["total_seconds"] = round(
            max(0.0, time.perf_counter() - started), 6)
        return self.result
