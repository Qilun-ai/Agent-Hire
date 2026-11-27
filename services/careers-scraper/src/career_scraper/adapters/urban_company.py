"""Urban Company's own careers app, its company gateway, and exact apply pages.

The current official journey is ``www.urbancompany.com`` footer -> ``Careers`` ->
``https://careers.urbancompany.com/``. The registry's old
``https://www.urbancompany.com/careers`` URL is observed as a 404 and is recorded,
not silently replaced. The React app's ``/jobs`` route renders every row returned
by the company gateway ``getAllJobs`` request (9 at inspection time) and its
``/jobDetail?id=`` route renders the full ``getJobDetails`` description. Each
detail page's Apply control points at the exact TurboHire public job page; only
those exact job payloads are read for additional public fields. The TurboHire
board is never enumerated.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html


LEDGER_ENTRY = "https://www.urbancompany.com/careers"
COMPANY_HOMEPAGE = "https://www.urbancompany.com/"
OFFICIAL_ENTRY = "https://careers.urbancompany.com/"
JOBS_PAGE = "https://careers.urbancompany.com/jobs"
DETAIL_PAGE = "https://careers.urbancompany.com/jobDetail"
CAREERS_ORIGIN = "https://careers.urbancompany.com"
GATEWAY_BASE = "https://www.urbanclap.com/api/v2/platform-gateway"
LISTING_API = GATEWAY_BASE + "/getAllJobs"
DETAIL_API = GATEWAY_BASE + "/getJobDetails"
TURBOHIRE_JOB_ORIGIN = "https://urbancompany.turbohire.co"
TURBOHIRE_API_ORIGIN = "https://thapi-stage2.azurewebsites.net"
TOKEN_API = TURBOHIRE_API_ORIGIN + "/api/token/noauth"
PUBLIC_JOB_API = TURBOHIRE_API_ORIGIN + "/api/publicjobs"
EMPLOYER_NAME = "Urban Company Limited"
EMPLOYER_URL = "https://www.urbancompany.com"
CAREER_PAGE_SUBDOMAIN = "urbancompany"
JOB_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
JOB_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{2,7}-\d{3,8}$")
TURBOHIRE_JOB_PATH = re.compile(
    r"^/job/publicjobs/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I
)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?|pptx?)(?:[?#]|$)", re.I)
RESPONSIBILITY_TERMS = (
    "what you'll do", "what you’ll do", "what you will do", "responsibilit",
    "about the role", "role & team", "the role",
)
QUALIFICATION_TERMS = (
    "what we need", "what you'll need", "what you’ll need", "qualif",
    "requirement", "who you are", "skills",
)
CAREER_PAGE_DEPARTMENTS = [
    "Engineering & Data", "Marketing", "Product & Design", "Business",
    "Operations", "International", "Central teams",
]


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(value: str | None) -> str:
    """Normalise either a plain string or an HTML fragment for exact comparisons."""
    if value and "<" in value and ">" in value:
        soup = BeautifulSoup(value, "lxml")
        for node in soup(["script", "style", "noscript"]):
            node.decompose()
        text = soup.get_text(" ", strip=True)
    else:
        text = value or ""
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+([,;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def sha256_text(value: str | None) -> str:
    return sha256((value or "").encode()).hexdigest()


def description_sections(html: str | None) -> list[dict]:
    """Keep each employer-authored heading and the exact blocks below it.

    The source descriptions mix standalone bold headings with inline bold
    intro labels. Only a paragraph whose entire visible text is one short bold
    label becomes a section heading; everything else stays in the body.
    """
    soup = BeautifulSoup(f'<div data-urban-company-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-urban-company-body]")
    blocks = [node for node in (root.contents if root else []) if isinstance(node, Tag)]
    sections: list[dict] = []
    current: dict | None = None
    for block in blocks:
        heading = ""
        if block.name in HEADING_TAGS:
            heading = normalized_title(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong")
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
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


def public_salary(ctc: dict | None) -> dict | None:
    """Return salary only when the exact apply page exposes it to job seekers."""
    if not isinstance(ctc, dict):
        return None
    if ctc.get("IsHidden") is not False:
        return None
    if "JobSeekers" in (ctc.get("HiddenFrom") or []):
        return None
    minimum, maximum = ctc.get("Min"), ctc.get("Max")
    if minimum in (None, "") or maximum in (None, ""):
        return None
    return {
        "min": str(minimum),
        "max": str(maximum),
        "currency": ctc.get("CurrencyCode"),
        "period": ctc.get("Type"),
        "negotiable": ctc.get("Negotiable"),
        "source": "TurboHire public job payload CTCInfo; rendered as Compensation on the exact apply page",
    }


def _date_part(value) -> str | None:
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value[:10]


def parse_ledger_entry(html: str, status: int, response_url: str = LEDGER_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "www.urbancompany.com" or parsed.path.rstrip("/") != "/careers":
        raise ValueError("Urban Company ledger careers URL redirected away from the checked URL")
    soup = BeautifulSoup(html, "lxml")
    body = normalized_body_text(soup.get_text(" ", strip=True))
    not_found = "couldn't find what you were looking for" in body.lower()
    if status == 404 and not not_found:
        raise ValueError("Urban Company ledger careers URL returned 404 without its not-found page")
    return {
        "url": LEDGER_ENTRY,
        "final_url": response_url,
        "status": status,
        "not_found_page": not_found,
        "serves_careers_content": status == 200 and not not_found,
        "title": normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else None,
    }


def parse_homepage_careers_link(html: str, response_url: str = COMPANY_HOMEPAGE) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc not in {"www.urbancompany.com", "urbancompany.com"}:
        raise ValueError("Urban Company homepage redirected to an unexpected host")
    soup = BeautifulSoup(html, "lxml")
    targets = []
    for anchor in soup.select("a[href]"):
        text = normalized_title(anchor.get_text(" ", strip=True))
        href = urljoin(response_url, anchor.get("href", ""))
        target = urlsplit(href)
        if (target.scheme == "https" and target.netloc == "careers.urbancompany.com"
                and "career" in text.lower()):
            targets.append({"href": href, "anchor_text": text})
    unique = list({row["href"]: row for row in targets}.values())
    if len(unique) != 1:
        raise ValueError(
            f"Urban Company homepage no longer publishes exactly one careers link (found {len(unique)})"
        )
    if urlsplit(unique[0]["href"]).path not in ("", "/"):
        raise ValueError("Urban Company homepage careers link is no longer the site root")
    return {
        "homepage_url": COMPANY_HOMEPAGE,
        "final_url": response_url,
        "careers_link": unique[0]["href"],
        "anchor_text": unique[0]["anchor_text"],
        "all_matching_links": targets,
    }


def parse_careers_shell(html: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "careers.urbancompany.com":
        raise ValueError("Urban Company careers shell redirected outside careers.urbancompany.com")
    if parsed.path not in ("/", "/jobs"):
        raise ValueError(f"Urban Company careers shell returned unexpected path: {parsed.path}")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != "Urban Company":
        raise ValueError("Urban Company careers shell page title changed")
    if soup.select_one("#root") is None:
        raise ValueError("Urban Company careers shell no longer exposes its React root")
    if "webpackJsonpcareer-page" not in html:
        raise ValueError("Urban Company careers shell is no longer the reviewed React app")
    chunks = [
        urljoin(response_url, script.get("src", ""))
        for script in soup.select("script[src]")
        if re.search(r"/static/js/main\.[0-9a-f]+\.chunk\.js$", script.get("src", ""))
    ]
    if len(chunks) != 1:
        raise ValueError("Urban Company careers shell no longer exposes exactly one main bundle")
    return {
        "url": response_url,
        "path": parsed.path,
        "title": title,
        "main_chunk_url": chunks[0],
        "react_root": True,
        "bundle_marker": "webpackJsonpcareer-page",
        "server_rendered_job_rows": bool(soup.select(".jobs-list .list-body a")),
    }


def parse_ui_bundle(js: str) -> dict:
    required = {
        "production_gateway_config": '"gatewayBaseUrl":"https://www.urbanclap.com"',
        "gateway_base_path": "/api/v2/platform-gateway",
        "listing_call": "/getAllJobs",
        "detail_call": "/getJobDetails",
        "jobs_route": '"/jobs"',
        "detail_route": '"/jobDetail"',
        "detail_id_route": '"/jobDetail?id="',
        "detail_request_body_key": "jobId",
    }
    missing = [name for name, marker in required.items() if marker not in js]
    if missing:
        raise ValueError(f"Urban Company UI bundle no longer documents: {missing}")
    return {
        "production_gateway_base": "https://www.urbanclap.com",
        "listing_endpoint": LISTING_API,
        "detail_endpoint": DETAIL_API,
        "jobs_route": JOBS_PAGE,
        "detail_route": DETAIL_PAGE,
        "detail_request_body": {"jobId": "<job_id>"},
        "bundle_markers_present": sorted(required),
    }


def parse_listing_api(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), list):
        raise ValueError("Urban Company getAllJobs response is missing its jobs list")
    rows: list[dict] = []
    issues: list[str] = []
    seen: set[str] = set()
    for raw in value["jobs"]:
        if not isinstance(raw, dict):
            issues.append("listing_row_not_object")
            continue
        identity = raw.get("job_id")
        if not isinstance(identity, str) or not JOB_ID_PATTERN.fullmatch(identity):
            issues.append("listing_row_missing_uuid_job_id")
            continue
        if identity in seen:
            issues.append(f"listing_duplicate_job_id:{identity}")
            continue
        code = raw.get("job_code")
        if not isinstance(code, str) or not JOB_CODE_PATTERN.fullmatch(code):
            issues.append(f"listing_row_missing_job_code:{identity}")
        title = normalized_title(raw.get("job_title"))
        if not title:
            issues.append(f"listing_row_missing_title:{identity}")
        department = raw.get("parent_department")
        if not isinstance(department, str) or not department.strip():
            issues.append(f"listing_row_missing_department:{identity}")
            department = ""
        description = raw.get("job_description")
        if not isinstance(description, str) or len(normalized_body_text(description)) < 100:
            issues.append(f"listing_row_missing_full_description:{identity}")
            description = description if isinstance(description, str) else ""
        locations = raw.get("location")
        if (not isinstance(locations, list) or not locations
                or any(not isinstance(item, str) or not item.strip() for item in locations)):
            issues.append(f"listing_row_missing_locations:{identity}")
            locations = []
        cities = raw.get("location_city")
        if (not isinstance(cities, list)
                or any(not isinstance(item, str) or not item.strip() for item in cities)):
            issues.append(f"listing_row_missing_location_cities:{identity}")
            cities = []
        apply_url = raw.get("apply_url")
        if not isinstance(apply_url, str):
            issues.append(f"listing_row_missing_apply_url:{identity}")
            apply_url = ""
        else:
            target = urlsplit(apply_url)
            match = TURBOHIRE_JOB_PATH.fullmatch(target.path)
            if (target.scheme != "https" or target.netloc != "urbancompany.turbohire.co"
                    or match is None or match.group(1).lower() != identity.lower()):
                issues.append(f"listing_row_apply_url_not_exact_turbohire_job:{identity}")
        seen.add(identity)
        rows.append(dict(raw))
    departments: dict[str, int] = {}
    for row in rows:
        key = row.get("parent_department") or ""
        departments[key] = departments.get(key, 0) + 1
    return {
        "rows": rows,
        "count": len(rows),
        "job_ids": [row["job_id"] for row in rows],
        "job_codes": [row["job_code"] for row in rows],
        "departments": departments,
        "issues": issues,
    }


def parse_detail_api(value: dict, expected_job_id: str) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("jobDetails"), dict):
        raise ValueError(f"Urban Company getJobDetails response is missing jobDetails for {expected_job_id}")
    detail = value["jobDetails"]
    if str(detail.get("job_id")) != expected_job_id:
        raise ValueError("Urban Company getJobDetails returned a different job id")
    for field in ("job_code", "job_title", "parent_department", "job_description", "location",
                  "location_city", "apply_url"):
        if field not in detail:
            raise ValueError(f"Urban Company getJobDetails row is missing {field}")
    return detail


def parse_turbohire_job(value: dict, expected_row: dict) -> dict:
    """Validate the exact apply-page payload against the company careers row."""
    identity = expected_row["job_id"]
    if not isinstance(value, dict):
        raise ValueError(f"TurboHire public job payload for {identity} is not an object")
    for field in ("JobId", "JobCode", "JobTitle", "Department", "JobDescription",
                  "PublishedJobBoards", "OrgDetails", "IsPublic", "IsDraft", "Location"):
        if field not in value:
            raise ValueError(f"TurboHire public job payload for {identity} is missing {field}")
    if str(value["JobId"]) != identity:
        raise ValueError("TurboHire public job id does not match the company-listed job id")
    if value["JobCode"] != expected_row["job_code"]:
        raise ValueError("TurboHire job code does not match the company listing")
    if normalized_title(value["JobTitle"]) != normalized_title(expected_row["job_title"]):
        raise ValueError("TurboHire job title does not match the company listing")
    if normalized_title(value["Department"]) != normalized_title(expected_row["parent_department"]):
        raise ValueError("TurboHire department does not match the company listing")
    if value["IsPublic"] is not True or value["IsDraft"] is not False:
        raise ValueError("TurboHire public job payload is not a public non-draft posting")
    boards = value["PublishedJobBoards"]
    if not isinstance(boards, list) or "CAREERPAGE" not in boards:
        raise ValueError("TurboHire job is not published on the company career page")
    org = value["OrgDetails"]
    if not isinstance(org, dict):
        raise ValueError("TurboHire public job payload is missing organization details")
    org_name = normalized_title(org.get("OrgName"))
    if org_name not in {EMPLOYER_NAME, "Urban Company"}:
        raise ValueError("TurboHire employer name changed")
    if org.get("CareerPageSubdomain") != CAREER_PAGE_SUBDOMAIN:
        raise ValueError("TurboHire career page subdomain changed")
    if org.get("IsCareerPagePublished") is not True:
        raise ValueError("TurboHire career page is no longer published")
    if (org.get("OrgURL") or "").rstrip("/") != EMPLOYER_URL:
        raise ValueError("TurboHire organization URL changed")
    company_text = normalized_body_text(expected_row.get("job_description"))
    apply_text = normalized_body_text(value["JobDescription"])
    if company_text != apply_text:
        raise ValueError("Urban Company listing description and exact apply page description differ")

    raw_location = value["Location"]
    try:
        location_rows = json.loads(raw_location) if isinstance(raw_location, str) else raw_location
    except json.JSONDecodeError as exc:
        raise ValueError("TurboHire job location is not valid JSON") from exc
    if not isinstance(location_rows, list):
        raise ValueError("TurboHire job location is not a list")
    addresses = [
        normalized_title(row.get("Address"))
        for row in location_rows
        if isinstance(row, dict) and row.get("Address")
    ]
    if addresses != [normalized_title(item) for item in expected_row["location"]]:
        raise ValueError("TurboHire job locations do not match the company listing")

    published = value.get("PublishedDate")
    published_dates = value.get("PublishedDates") if isinstance(value.get("PublishedDates"), dict) else {}
    expiry_dates = value.get("ExpiryDates") if isinstance(value.get("ExpiryDates"), dict) else {}
    ctc = value.get("CTCInfo") if isinstance(value.get("CTCInfo"), dict) else None
    experience = value.get("Experience") if isinstance(value.get("Experience"), dict) else {}
    min_exp, max_exp = experience.get("MinExp"), experience.get("MaxExp")
    has_experience = isinstance(min_exp, int) and isinstance(max_exp, int) and (min_exp or max_exp)
    employment_type = value.get("Type")
    if employment_type in ("", "UNSPECIFIED"):
        employment_type = None
    date_posted = _date_part(published) or _date_part(published_dates.get("CAREERPAGE"))
    valid_through = _date_part(expiry_dates.get("CAREERPAGE"))
    public_fields = {
        "employer": EMPLOYER_NAME,
        "org_name_raw": org_name,
        "job_code": value["JobCode"],
        "department": value["Department"],
        "employment_type": employment_type,
        "employment_type_raw": value.get("Type"),
        "number_of_vacancies": value.get("NumberOfVacancy"),
        "experience": ({"min_years": min_exp, "max_years": max_exp} if has_experience else None),
        "experience_raw": experience or None,
        "skills": value.get("Skills") if isinstance(value.get("Skills"), list) else [],
        "date_posted": date_posted,
        "date_posted_raw": published,
        "date_updated": value.get("UpdatedDate"),
        "valid_through": valid_through,
        "valid_through_raw": expiry_dates.get("CAREERPAGE"),
        "salary": public_salary(ctc),
        "salary_is_public": public_salary(ctc) is not None,
        "job_status": value.get("JobStatus"),
        "career_page_published_at": published_dates.get("CAREERPAGE"),
        "published_job_boards": boards,
        "locations": addresses,
    }
    kept = {key: value.get(key) for key in (
        "JobId", "JobCode", "JobTitle", "Department", "Type", "NumberOfVacancy",
        "Experience", "Skills", "CTCInfo", "PublishedDate", "UpdatedDate",
        "PublishedDates", "ExpiryDates", "PublishedJobBoards", "JobStatus",
        "IsPublic", "IsDraft", "HideVacancy", "RequisitionId", "AdditionalFields",
        "ClientName", "RolesAndResponsibilities", "Eligibility", "JobDescription",
    )}
    kept["Location"] = addresses
    kept["OrgDetails"] = {
        key: org.get(key) for key in (
            "OrgID", "OrgName", "OrgURL", "CareerPageSubdomain",
            "IsCareerPagePublished", "OrgVerifiedDomains",
        )
    }
    return {"public_fields": public_fields, "raw": kept, "issues": []}


def parse_apply_page_jsonld(html: str, expected_row: dict) -> dict:
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one('script[type="application/ld+json"]')
    if node is None:
        raise ValueError("Urban Company exact apply page is missing its JobPosting JSON-LD")
    try:
        value = json.loads(node.get_text())
    except json.JSONDecodeError as exc:
        raise ValueError("Urban Company exact apply page JobPosting JSON-LD is invalid") from exc
    if not isinstance(value, dict) or value.get("@type") != "JobPosting":
        raise ValueError("Urban Company exact apply page JSON-LD is not a JobPosting")
    title = normalized_title(value.get("title"))
    if title != normalized_title(expected_row["job_title"]):
        raise ValueError("Apply page JSON-LD title does not match the company listing")
    employer = normalized_title((value.get("hiringOrganization") or {}).get("name"))
    if employer != EMPLOYER_NAME:
        raise ValueError("Apply page JSON-LD employer name changed")
    location = value.get("jobLocation") or {}
    address = location.get("address") if isinstance(location, dict) else None
    locality = normalized_title(address.get("addressLocality")) if isinstance(address, dict) else ""
    if locality not in [normalized_title(item) for item in expected_row["location"]]:
        raise ValueError("Apply page JSON-LD location does not match the company listing")
    return {
        "title": title,
        "date_posted": value.get("datePosted"),
        "employer": employer,
        "address": address,
        "skills": value.get("skills") if isinstance(value.get("skills"), list) else [],
    }


def _api_headers(referer: str, origin: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": referer,
    }


class UrbanCompany(Adapter):
    """Collect only the roles rendered by Urban Company's own careers app."""

    VERIFICATION_REVISION = "urbancompany.careers-react-app.gateway-api.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._turbohire_token: str | None = None
        self._turbohire_token_error: str | None = None

    async def _get_html(self, url: str) -> tuple:
        started = time.perf_counter()
        response = await self.http.get(url)
        return response, max(0.0, time.perf_counter() - started)

    async def _ledger(self):
        started = time.perf_counter()
        try:
            response = await self.http.get(LEDGER_ENTRY)
        except FetchError as exc:
            seconds = max(0.0, time.perf_counter() - started)
            if "HTTP 404" not in str(exc):
                raise
            # The shared HTTP helper treats every non-2xx as an error, but the
            # registry's old /careers URL is observed as a 404 by design. Its
            # saved body is kept in companies/urban-company/source for review.
            return {
                "url": LEDGER_ENTRY,
                "final_url": LEDGER_ENTRY,
                "status": 404,
                "not_found_page": None,
                "serves_careers_content": False,
                "live_body_parsed": False,
                "response_seconds": seconds,
                "note": "Expected 404 for the stale registry URL; the current official entry is the site footer link.",
            }
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_ledger_entry(response.text, response.status_code, str(response.url))
        parsed.update(response_seconds=seconds, response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content), live_body_parsed=True)
        return parsed

    async def _homepage(self):
        response, seconds = await self._get_html(COMPANY_HOMEPAGE)
        if response.status_code != 200:
            raise ValueError(f"Urban Company homepage returned HTTP {response.status_code}")
        parsed = parse_homepage_careers_link(response.text, str(response.url))
        parsed.update(status=response.status_code, response_seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _shell(self, url: str):
        response, seconds = await self._get_html(url)
        if response.status_code != 200:
            raise ValueError(f"Urban Company careers page {url} returned HTTP {response.status_code}")
        parsed = parse_careers_shell(response.text, str(response.url))
        parsed.update(status=response.status_code, response_seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _bundle(self, url: str):
        response, seconds = await self._get_html(url)
        if response.status_code != 200:
            raise ValueError(f"Urban Company careers bundle returned HTTP {response.status_code}")
        parsed = parse_ui_bundle(response.text)
        parsed.update(url=url, status=response.status_code, response_seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _listing(self):
        started = time.perf_counter()
        response = await self.http.request(
            "POST", LISTING_API, json={},
            headers=_api_headers(JOBS_PAGE, CAREERS_ORIGIN),
        )
        seconds = max(0.0, time.perf_counter() - started)
        target = urlsplit(str(response.url))
        if (target.scheme != "https" or target.netloc != "www.urbanclap.com"
                or target.path != "/api/v2/platform-gateway/getAllJobs"):
            raise ValueError("Urban Company getAllJobs redirected away from its company gateway route")
        parsed = parse_listing_api(response.json())
        parsed.update(url=LISTING_API, final_url=str(response.url), status=response.status_code,
                      request_body=None, request_seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _turbohire(self):
        started = time.perf_counter()
        response = await self.http.request(
            "GET", TOKEN_API,
            headers=_api_headers(TURBOHIRE_JOB_ORIGIN + "/", TURBOHIRE_JOB_ORIGIN),
        )
        seconds = max(0.0, time.perf_counter() - started)
        value = response.json()
        token = value.get("access_token") if isinstance(value, dict) else None
        if not isinstance(token, str) or token.count(".") != 2:
            raise ValueError("Urban Company apply-page token endpoint did not return a JWT")
        self._turbohire_token = token
        return {"url": TOKEN_API, "status": response.status_code, "seconds": seconds,
                "response_sha256": sha256(response.content).hexdigest(),
                "anonymous_public_page_token": True}

    async def _detail(self, job):
        row = job.raw["listing"]
        started = time.perf_counter()
        response = await self.http.request(
            "POST", DETAIL_API, json={"jobId": job.job_id},
            headers=_api_headers(job.url, CAREERS_ORIGIN),
        )
        detail_seconds = max(0.0, time.perf_counter() - started)
        detail = parse_detail_api(response.json(), job.job_id)
        if detail != row:
            raise ValueError("Urban Company detail response no longer equals its listing row")
        if normalized_body_text(detail["job_description"]) != normalized_body_text(job.description_html):
            raise ValueError("Urban Company detail description changed during collection")
        job.raw["company_detail"] = {
            "request": {"method": "POST", "url": DETAIL_API, "body": {"jobId": job.job_id}},
            "response_url": str(response.url),
            "status": response.status_code,
            "seconds": detail_seconds,
            "response_sha256": sha256(response.content).hexdigest(),
            "row_equals_listing": True,
        }
        if self._turbohire_token is None:
            job.warnings.append("apply_page_fields_unverified")
            return job
        ats_started = time.perf_counter()
        try:
            ats_response = await self.http.request(
                "GET", f"{PUBLIC_JOB_API}/{job.job_id}",
                params={"fieldVisibility": "CareerPage"},
                headers={**_api_headers(job.url, TURBOHIRE_JOB_ORIGIN),
                         "Authorization": f"Bearer {self._turbohire_token}"},
            )
            ats = parse_turbohire_job(ats_response.json(), row)
        except Exception as exc:
            # The company careers description above is complete and verified.
            # An unavailable supplementary apply-page field set is recorded as a
            # partial-field failure, not used to replace that description.
            job.warnings.append("apply_page_fetch_failed")
            self.result.errors.append(f"Apply page {job.job_id}: {type(exc).__name__}: {exc}")
            return job
        ats_seconds = max(0.0, time.perf_counter() - ats_started)
        fields = ats["public_fields"]
        job.raw["turbohire"] = ats["raw"]
        job.raw["apply_page"] = {
            "request": {"method": "GET", "url": f"{PUBLIC_JOB_API}/{job.job_id}",
                        "query": {"fieldVisibility": "CareerPage"}},
            "response_url": str(ats_response.url),
            "status": ats_response.status_code,
            "seconds": ats_seconds,
            "response_sha256": sha256(ats_response.content).hexdigest(),
            "exact_apply_url": row["apply_url"],
            "board_enumeration": False,
            "fields": fields,
        }
        job.employer = fields["employer"]
        job.employment_type = fields["employment_type"]
        job.date_posted = fields["date_posted"]
        job.date_posted_raw = fields["date_posted_raw"]
        job.date_updated = fields["date_updated"]
        job.valid_through = fields["valid_through"]
        job.salary = fields["salary"]
        job.experience = fields["experience"]
        job.raw["number_of_vacancies"] = fields["number_of_vacancies"]
        job.raw["skills"] = fields["skills"]
        self.result.timings["apply_page_fields_seconds"] = round(
            self.result.timings.get("apply_page_fields_seconds", 0.0) + ats_seconds, 6
        )
        return job

    def _make_job(self, row):
        description_html = row["job_description"]
        sections = description_sections(description_html)
        responsibility_sections = [
            section for section in sections
            if section["heading"] and any(term in section["heading"].lower() for term in RESPONSIBILITY_TERMS)
        ]
        qualification_sections = [
            section for section in sections
            if section["heading"] and any(term in section["heading"].lower() for term in QUALIFICATION_TERMS)
        ]
        soup = BeautifulSoup(description_html, "lxml")
        links = [{"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
                 for anchor in soup.select("a[href]")]
        attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
        job = self.job(
            {"listing": dict(row), "job_code": row["job_code"]},
            title=normalized_title(row["job_title"]),
            url=f"{DETAIL_PAGE}?id={row['job_id']}",
            job_id=row["job_id"],
            requisition_id=row["job_code"],
            apply_url=row["apply_url"],
            description=description_html,
            method="urbancompany.platform_gateway.getJobDetails.job_description",
            description_url=DETAIL_API,
            locations=list(row["location"]),
            department=row["parent_department"],
            employment_type=None,
            workplace_type=None,
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            valid_through=None,
            salary=None,
            employer=EMPLOYER_NAME,
            language="en",
            responsibilities=responsibility_sections or None,
            qualifications=qualification_sections or None,
        )
        job.raw["description_sections"] = sections
        job.raw["description_links"] = links
        job.raw["role_attachments"] = attachments
        job.raw["location_city"] = list(row["location_city"])
        job.raw["detail"] = {
            "source_field": "jobDetails.job_description",
            "description_sha256": sha256(description_html.encode()).hexdigest(),
            "description_normalized_chars": len(normalized_body_text(description_html)),
            "description_normalized_sha256": sha256_text(normalized_body_text(description_html)),
            "description_sections": sections,
            "description_links": links,
            "role_attachments": attachments,
        }
        return job

    async def _apply_page_crosscheck(self, jobs):
        started = time.perf_counter()
        checks = []
        selected = [job for job in (jobs[0], jobs[-1]) if "apply_page" in job.raw]
        for job in selected:
            try:
                response = await self.http.get(job.apply_url)
                parsed = parse_apply_page_jsonld(response.text, job.raw["listing"])
            except Exception as exc:
                self.result.errors.append(
                    f"Apply page JSON-LD {job.job_id}: {type(exc).__name__}: {exc}"
                )
                continue
            expected_date = job.date_posted
            expected_skills = job.raw.get("skills")
            match = (
                parsed["date_posted"] == expected_date
                and parsed["employer"] == EMPLOYER_NAME
                and parsed["skills"] == expected_skills
            )
            checks.append({
                "job_id": job.job_id,
                "job_code": job.raw["job_code"],
                "apply_url": job.apply_url,
                "status": response.status_code,
                "response_sha256": sha256(response.content).hexdigest(),
                "date_posted": parsed["date_posted"],
                "employer": parsed["employer"],
                "address": parsed["address"],
                "skills_match_apply_payload": parsed["skills"] == expected_skills,
                "date_matches_apply_payload": parsed["date_posted"] == expected_date,
                "matches_apply_payload": match,
            })
            if not match:
                self.result.errors.append(f"Apply page JSON-LD mismatch for {job.job_id}")
        self.result.timings["apply_page_jsonld_crosscheck_seconds"] = round(
            max(0.0, time.perf_counter() - started), 6
        )
        return checks

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "ledger_entry_url": LEDGER_ENTRY,
            "official_entry_url": OFFICIAL_ENTRY,
            "homepage_url": COMPANY_HOMEPAGE,
            "listing_page_url": JOBS_PAGE,
            "listing_url": LISTING_API,
            "listing_authority": (
                "Careers link published in www.urbancompany.com's own footer, rendered by "
                "careers.urbancompany.com's /jobs route."
            ),
            "listing_membership_request": {
                "method": "POST",
                "url": LISTING_API,
                "body": None,
                "server_side_filters": False,
                "pagination": None,
                "scope": "All rows rendered by the company's own /jobs page; no department filter is required to see them.",
                "ui_bundle_evidence": "The inspected main bundle posts to this exact gateway route and renders every returned row.",
            },
            "detail_method": (
                "POST getJobDetails {jobId} renders the exact /jobDetail?id= page; its full "
                "job_description is the stored description."
            ),
            "apply_page_method": (
                "Each rendered Apply control points at the exact TurboHire public job URL; only "
                "those nine job payloads are read. The TurboHire board is not enumerated."
            ),
            "employer": EMPLOYER_NAME,
            "date_fields": {
                "date_posted": "TurboHire PublishedDate date, equal to the exact apply page's JSON-LD datePosted",
                "date_updated": "TurboHire UpdatedDate",
                "valid_through": "TurboHire ExpiryDates.CAREERPAGE",
                "created_date_used_for_posting_date": False,
            },
            "field_mapping": {
                "job_id": "getAllJobs.job_id (UUID; equal to the exact apply page JobId)",
                "requisition_id": "getAllJobs.job_code (UCL-/UTIPL-); TurboHire RequisitionId is a zero GUID and is not used",
                "employment_type": "TurboHire Type; UNSPECIFIED is stored as null with the raw value retained",
                "salary": "TurboHire CTCInfo only when IsHidden is false and JobSeekers is not in HiddenFrom; otherwise null with raw retained",
                "locations": "getAllJobs.location full addresses; getAllJobs.location_city retained in raw",
                "employer": "TurboHire OrgDetails/JSON-LD legal employer 'Urban Company Limited'",
            },
            "excluded_ats_fields": [
                "CreatedByEmail", "CreatedByName", "OrgDetails billing/GST/SSO fields",
                "ResumeStates and candidate pipeline internals",
            ],
            "request_scope": (
                "www.urbancompany.com footer, careers.urbancompany.com shell+bundle, the company "
                "gateway listing/detail calls, and the exact nine apply-page job payloads."
            ),
        })
        issues: list[str] = []
        try:
            ledger = await self._ledger()
        except Exception as exc:
            ledger = {"url": LEDGER_ENTRY, "status": None,
                      "error": f"{type(exc).__name__}: {exc}"}
            issues.append(f"ledger_entry_check_failed: {type(exc).__name__}: {exc}")
        try:
            homepage = await self._homepage()
        except Exception as exc:
            homepage = {"homepage_url": COMPANY_HOMEPAGE, "status": None,
                        "error": f"{type(exc).__name__}: {exc}"}
            issues.append(f"homepage_link_check_failed: {type(exc).__name__}: {exc}")
        try:
            entry = await self._shell(OFFICIAL_ENTRY)
            jobs_shell = await self._shell(JOBS_PAGE)
            bundle = await self._bundle(entry["main_chunk_url"])
            listing = await self._listing()
        except Exception as exc:
            self.result.errors.append(f"Urban Company source collection failed: {type(exc).__name__}: {exc}")
            self.result.errors.extend(issues)
            self.result.listing_complete = False
            self.result.timings["source_collection_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6
            )
            return self.result

        self.result.timings["source_collection_seconds"] = round(
            max(0.0, time.perf_counter() - source_started), 6
        )
        self.result.expected_total = listing["count"]
        self.result.pages = 1
        self.result.metadata["ledger_entry"] = ledger
        self.result.metadata["homepage_careers_link"] = homepage
        self.result.metadata["careers_entry"] = entry
        self.result.metadata["careers_jobs_shell"] = jobs_shell
        self.result.metadata["ui_bundle"] = bundle
        self.result.metadata["listing_snapshot"] = {
            key: value for key, value in listing.items() if key != "rows"
        }
        self.result.metadata["departments"] = listing["departments"]
        self.result.metadata["filters"] = {
            "type": "client-side",
            "department_options": list(listing["departments"]),
            "location_options": sorted({city for row in listing["rows"] for city in row["location_city"]}),
            "keyword_search": True,
        }
        self.result.errors.extend(issues)
        self.result.errors.extend(listing["issues"])
        if listing["issues"]:
            self.result.listing_complete = False

        jobs = [self._make_job(row) for row in listing["rows"]]
        for job in jobs:
            self.add(job)
        if len(jobs) != listing["count"]:
            self.result.errors.append("urban_company_listing_rows_not_retained")

        token_error = None
        try:
            self.result.metadata["apply_page_token"] = await self._turbohire()
        except Exception as exc:
            token_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(f"Urban Company apply-page token failed: {token_error}")
            self.result.metadata["apply_page_token"] = {"error": token_error}
        await self.hydrate(jobs, self._detail)
        self.result.metadata["apply_page_jsonld_crosscheck"] = await self._apply_page_crosscheck(jobs)

        recheck_started = time.perf_counter()
        try:
            final_listing = await self._listing()
        except Exception as exc:
            final_listing = {"rows": [], "count": None, "job_ids": [], "issues": [
                f"final_listing_recheck_failed: {type(exc).__name__}: {exc}"]}
            self.result.errors.append(final_listing["issues"][0])
        self.result.timings["listing_recheck_seconds"] = round(
            max(0.0, time.perf_counter() - recheck_started), 6
        )
        final_rows = {row["job_id"]: row for row in final_listing["rows"]}
        initial_ids = listing["job_ids"]
        final_ids = final_listing["job_ids"]
        order_preserved = initial_ids == final_ids
        same_rows = all(final_rows.get(job.job_id) == job.raw["listing"] for job in jobs)
        reconciliation = {
            "initial_ids": initial_ids,
            "final_ids": final_ids,
            "initial_count": listing["count"],
            "final_count": final_listing["count"],
            "order_preserved": order_preserved,
            "rows_unchanged": same_rows,
            "complete": (
                order_preserved
                and same_rows
                and final_listing["count"] == listing["count"] == len(jobs)
                and not listing["issues"]
                and not final_listing["issues"]
            ),
        }
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.metadata["final_listing_issues"] = final_listing["issues"]
        if not order_preserved:
            self.result.errors.append("urban_company_listing_order_or_membership_changed")
        if not same_rows:
            self.result.errors.append("urban_company_listing_row_changed_during_collection")
        self.result.errors.extend(final_listing["issues"])
        self.result.listing_complete = reconciliation["complete"] and not issues
        verified = sum(job.description_verified for job in self.result.jobs)
        if verified != len(self.result.jobs):
            self.result.errors.append("urban_company_unverified_description")
        self.result.metadata["source_collection_wall_seconds"] = round(
            max(0.0, time.perf_counter() - source_started), 6
        )
        return self.result
