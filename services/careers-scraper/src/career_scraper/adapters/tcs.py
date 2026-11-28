"""TCS's own global iBegin careers portal, linked from the official TCS careers page.

The marketing entry (https://www.tcs.com/careers) is Akamai-protected and returns
an explicit 403 from this network. Its published "Apply now" CTA points at TCS's
own candidate portal (https://ibegin.tcsapps.com/candidate/), whose live global
country selector and public search API expose the company's current vacancies.
This adapter reads only that TCS-owned portal: the public job search endpoint,
the portal's own country/locale list, and the exact job-detail endpoint for each
listed job ID. No ATS board, aggregator or guessed slug is used.
"""
from __future__ import annotations

from hashlib import sha256
import asyncio
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://www.tcs.com/careers"
PORTAL_ORIGIN = "https://ibegin.tcsapps.com"
PORTAL_BASE = PORTAL_ORIGIN + "/candidate/next"
LANDING_URL = PORTAL_BASE + "/en-IN/landing"
SESSION_URL = PORTAL_BASE + "/api/candidate"
GEOGRAPHIES_URL = PORTAL_BASE + "/api/geographies"
SEARCH_URL = PORTAL_BASE + "/api/{locale}/search/jobs"
DETAIL_URL = PORTAL_BASE + "/api/{locale}/job/desc/{job_id}"
DOMAINS_URL = PORTAL_BASE + "/api/{locale}/domains?openings=true"
EXPERIENCE_URL = PORTAL_BASE + "/api/{locale}/experience-bands"
JOB_PAGE_URL = PORTAL_BASE + "/{locale}/jobs/{job_id}"
SEARCH_PAGE_URL = PORTAL_BASE + "/{locale}/jobs/search"
LISTING_URL = SEARCH_PAGE_URL.format(locale="en-IN")
PAGE_SIZE = 50
MAX_PAGE_SIZE = 50
EMPLOYER = "Tata Consultancy Services Limited"
PORTAL_RELEASE = "4.5.1-3-8.1.1.GA-redhat-00048"
LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
JOB_ID_RE = re.compile(r"^(\d+)([A-Za-z]*)$")
HEADING_RE = re.compile(
    r"(?i)(must have|responsibilit|roles?\b|duties|qualification|desired candidate|"
    r"skills|experience|about|job description|key responsibilities|technical|"
    r"functional|requirements|preferred|education|benefits|location|apply)"
)
RESPONSIBILITY_RE = re.compile(r"(?i)(responsibilit|duties|what you will do|key responsibilities)")
LANGUAGE_NAMES = {
    "EN": "English", "HI": "Hindi", "JA": "Japanese", "ZH": "Chinese",
    "ES": "Spanish", "PT": "Portuguese",
}


class PortalResponseError(ValueError):
    """A TCS iBegin response no longer matches the inspected contract."""


def _portal_payload(value: dict, context: str):
    if not isinstance(value, dict):
        raise PortalResponseError(f"{context} response is not a JSON object")
    if value.get("result") is not True or value.get("status") != 200:
        raise PortalResponseError(
            f"{context} response is not a successful iBegin payload: "
            f"status={value.get('status')!r} message={value.get('message')!r}"
        )
    if "payload" not in value:
        raise PortalResponseError(f"{context} response is missing payload")
    return value["payload"]


def parse_geographies(value: dict) -> list[dict]:
    payload = _portal_payload(value, "geographies")
    if not isinstance(payload, list) or not payload:
        raise PortalResponseError("geographies payload is empty or not a list")
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            raise PortalResponseError("geographies row is not an object")
        required = ("geoCode", "name", "id", "languageCode", "language", "defaultLangFlag")
        missing = [key for key in required if key not in item]
        if missing:
            raise PortalResponseError(f"geographies row missing {missing}")
        if not re.fullmatch(r"[A-Z]{2}", str(item["geoCode"])):
            raise PortalResponseError(f"geographies row has invalid geoCode: {item['geoCode']!r}")
        rows.append(dict(item))
    return rows


def default_country_locales(geographies: list[dict]) -> list[dict]:
    """One default English (or first available) locale per published country."""
    by_country: dict[str, dict] = {}
    for row in geographies:
        code = row["geoCode"]
        current = by_country.get(code)
        preferred = row.get("languageCode") == "EN" and row.get("defaultLangFlag") == "Y"
        if current is None or (preferred and current.get("languageCode") != "EN"):
            by_country[code] = row
    countries = []
    for code, row in by_country.items():
        locale = f"{str(row['languageCode']).lower()}-{code}"
        if not LOCALE_RE.fullmatch(locale):
            raise PortalResponseError(f"derived locale is not a portal locale: {locale}")
        countries.append({
            "country": row["name"],
            "geo_code": code,
            "locale": locale,
            "language_code": row["languageCode"],
            "language": LANGUAGE_NAMES.get(row["languageCode"], row["language"]),
            "geography": row.get("geography"),
        })
    return sorted(countries, key=lambda item: item["country"])


def parse_domains(value: dict) -> dict:
    payload = _portal_payload(value, "domains")
    if not isinstance(payload, list):
        raise PortalResponseError("domains payload is not a list")
    rows, issues = [], []
    for item in payload:
        if not isinstance(item, dict) or "id" not in item or "name" not in item:
            issues.append("domain_row_missing_id_or_name")
            continue
        openings = item.get("openings")
        if not isinstance(openings, int) or openings < 0:
            issues.append(f"domain_openings_invalid:{item.get('id')}")
            openings = 0
        rows.append({"id": item["id"], "name": item["name"], "openings": openings,
                     "description": item.get("description")})
    return {"domains": rows, "openings_sum": sum(row["openings"] for row in rows), "issues": issues}


def parse_experience_bands(value: dict) -> dict:
    payload = _portal_payload(value, "experience-bands")
    if not isinstance(payload, list):
        raise PortalResponseError("experience-bands payload is not a list")
    bands = []
    for item in payload:
        if not isinstance(item, dict) or "experienceBandId" not in item:
            raise PortalResponseError("experience-bands row is not an identified band")
        bands.append({"id": item["experienceBandId"], "band": item.get("band"),
                      "name": item.get("name"), "openings": item.get("openings")})
    return {"bands": bands}


def search_request_body(page: int) -> dict:
    """The portal's own unfiltered search request, sorted by its Posted Date option.

    The UI's default "Most Relevant" body uses an empty-term relevance sort whose
    page order changed between two identical live requests. The same public
    endpoint accepts the UI's "Posted Date" sort key alone, which is deterministic;
    sorting cannot change membership and every page is reconciled against totals.
    """
    if not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    return {
        "includeRegularJob": True,
        "includeWalkIn": True,
        "searchTerms": [],
        "jobLocationFilters": [],
        "jobDomainFilters": [],
        "experienceLevelFilters": [],
        "skillFilters": [],
        "relevancySortOrder": None,
        "recentSortOrder": "desc",
        "lastDateToApplySortOrder": None,
        "page": page,
        "resultsPerPage": PAGE_SIZE,
    }


def parse_search_page(value: dict, requested_page: int) -> dict:
    payload = _portal_payload(value, "search")
    required = ("content", "totalElements", "totalPages", "size", "number",
                "numberOfElements", "first", "last", "empty")
    missing = [key for key in required if key not in payload]
    if missing:
        raise PortalResponseError(f"search payload missing {missing}")
    content = payload["content"]
    total = payload["totalElements"]
    pages = payload["totalPages"]
    size = payload["size"]
    number = payload["number"]
    if not isinstance(content, list) or not isinstance(total, int) or total < 0:
        raise PortalResponseError("search payload content/totalElements invalid")
    if not isinstance(pages, int) or pages < 0 or not isinstance(size, int) or size != PAGE_SIZE:
        raise PortalResponseError("search payload page size/pages changed")
    if not isinstance(number, int) or number != requested_page - 1:
        raise PortalResponseError("search payload page number does not match the request")
    if total == 0:
        if pages != 0 or content:
            raise PortalResponseError("empty search payload has non-empty page metadata")
    else:
        expected_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if pages != expected_pages:
            raise PortalResponseError("search payload totalPages does not match totalElements")
        expected_rows = PAGE_SIZE if requested_page < pages else total - (pages - 1) * PAGE_SIZE
        if len(content) != expected_rows:
            raise PortalResponseError(
                f"search page {requested_page} returned {len(content)} rows, expected {expected_rows}"
            )
    rows, issues = [], []
    for item in content:
        if not isinstance(item, dict):
            issues.append("search_row_not_object")
            continue
        identity = item.get("id")
        title = item.get("title")
        if not isinstance(identity, str) or not JOB_ID_RE.fullmatch(identity):
            issues.append("search_row_invalid_id")
            continue
        if not isinstance(title, str) or not title.strip():
            issues.append(f"search_row_missing_title:{identity}")
            continue
        rows.append(dict(item))
    if len(rows) != len(content):
        raise PortalResponseError(f"search page {requested_page} has unusable rows: {issues}")
    return {
        "rows": rows,
        "total_elements": total,
        "total_pages": pages,
        "size": size,
        "number": number,
        "number_of_elements": payload["numberOfElements"],
        "first": payload["first"],
        "last": payload["last"],
        "empty": payload["empty"],
        "facets": payload.get("facets") or {},
        "issues": issues,
    }


def parse_detail(value: dict, expected_job_id: str) -> dict:
    payload = _portal_payload(value, "job detail")
    if not isinstance(payload, dict):
        raise PortalResponseError("job detail payload is not an object")
    match = JOB_ID_RE.fullmatch(str(expected_job_id))
    if match is None:
        raise PortalResponseError(f"expected job id is not a portal id: {expected_job_id!r}")
    numeric = int(match.group(1))
    if payload.get("jobId") != numeric:
        raise PortalResponseError(
            f"job detail id {payload.get('jobId')!r} does not match listed id {expected_job_id!r}"
        )
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise PortalResponseError(f"job detail {expected_job_id} has no title")
    description = payload.get("description")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise PortalResponseError(f"job detail {expected_job_id} description is not a string")
    sections = description_sections(description)
    text = normalized_body_text(description)
    return {
        "job_id": payload["jobId"],
        "title": title.strip(),
        "location": payload.get("location"),
        "qualifications": payload.get("qualifications"),
        "skilldetail": payload.get("skilldetail"),
        "experience": payload.get("experience"),
        "role": payload.get("role"),
        "description_html": description,
        "description_text": text,
        "description_sections": sections,
        "country": payload.get("country"),
        "function_name": payload.get("functionName"),
        "apply_by": payload.get("applyby"),
        "min_salary": payload.get("minSalary"),
        "max_salary": payload.get("maxSalary"),
        "additional_info": payload.get("additionalInfo"),
        "quiz_flag": payload.get("quizFlag"),
        "quiz_mandatory": payload.get("quizMandatory"),
        "location_id": payload.get("locationId"),
        "country_match": payload.get("countryMatch"),
        "applied": payload.get("applied"),
        "language_id": payload.get("languageId"),
        "payload": payload,
    }


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ")
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def description_sections(html: str | None) -> list[dict]:
    """Keep employer-authored headings and the exact body below each one."""
    soup = BeautifulSoup(f'<div data-tcs-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-tcs-body]")
    if root is None:
        return []
    sections: list[dict] = []
    current = {"heading": None, "html": "", "text": ""}

    def heading_of(node: Tag) -> str | None:
        if node.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = node.get_text(" ", strip=True)
            return text or None
        if node.name in {"p", "div"}:
            full = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            if not full or len(full) > 140:
                return None
            if full.startswith(("•", "-", "*", "–")) or re.match(r"^\d+[.)]\s", full):
                return None
            strong = node.find(["strong", "b"])
            if strong is not None:
                strong_text = re.sub(r"\s+", " ", strong.get_text(" ", strip=True)).strip()
                if strong_text and strong_text.rstrip(":") == full.rstrip(":") and HEADING_RE.search(full):
                    return full
            # Employer-authored standalone labels such as "Roles & Responsibilities"
            # are plain divs in many TCS descriptions; keep only leaf, label-like lines.
            if HEADING_RE.search(full) and not node.find(["ul", "ol", "p", "div"]) and not full.endswith("."):
                return full
        return None

    for node in root.children:
        if not isinstance(node, Tag):
            continue
        heading = heading_of(node)
        if heading:
            if current["heading"] is not None or current["html"]:
                sections.append(current)
            current = {"heading": heading.rstrip(":").strip(), "html": "", "text": ""}
            continue
        current["html"] += str(node)
    if current["heading"] is not None or current["html"]:
        sections.append(current)
    for section in sections:
        section["text"] = normalized_body_text(section["html"])
    return sections


def responsibilities_text(sections: list[dict]) -> str | None:
    parts = [section["text"] for section in sections
             if section.get("heading") and RESPONSIBILITY_RE.search(section["heading"]) and section.get("text")]
    return "\n\n".join(parts) if parts else None


def salary_object(detail: dict) -> dict | None:
    minimum, maximum = detail.get("min_salary"), detail.get("max_salary")
    if not minimum and not maximum:
        return None
    currency = None
    for value in (minimum, maximum):
        if isinstance(value, str):
            if "$" in value:
                currency = "USD"
            elif "£" in value:
                currency = "GBP"
            elif "€" in value:
                currency = "EUR"
            elif "₹" in value:
                currency = "INR"
    return {"min": minimum, "max": maximum, "currency": currency,
            "source_fields": ["minSalary", "maxSalary"]}


def apply_by_outlier(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    match = re.match(r"^(\d{4})-", value)
    if not match:
        return False
    year = int(match.group(1))
    current = time.gmtime().tm_year
    return year < 2000 or year > current + 10


def rendered_detail_reference(html: str, expected_title: str, api_text: str,
                              expected_url: str) -> dict:
    """Compare a rendered TCS detail page with the exact stored API description."""
    parts = urlsplit(expected_url)
    match = re.fullmatch(r"/candidate/next/([a-z]{2}-[A-Z]{2})/jobs/([^/]+)", parts.path)
    if parts.scheme != "https" or parts.netloc != "ibegin.tcsapps.com" or match is None:
        raise ValueError("representative detail URL is not an exact TCS portal job link")
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.select_one('h1[data-testid="job-description-title"]')
    if title_node is None:
        raise ValueError("rendered detail page has no job-description-title")
    title = re.sub(r"\s+", " ", title_node.get_text(" ", strip=True)).strip()
    if title != expected_title.strip():
        raise ValueError(f"rendered detail title {title!r} does not match {expected_title!r}")
    region = soup.select_one('[data-testid="card-description"]')
    if region is None:
        raise ValueError("rendered detail page has no card-description region")
    region_text = normalized_body_text(str(region))
    body_text = normalized_body_text(str(soup.body or soup))
    api_norm = re.sub(r"\s+", " ", api_text or "").strip()
    return {
        "url": expected_url,
        "title": title,
        "visible_body_chars": len(body_text),
        "visible_body_sha256": sha256(body_text.encode()).hexdigest(),
        "description_region_chars": len(region_text),
        "description_region_sha256": sha256(region_text.encode()).hexdigest(),
        "api_description_chars": len(api_norm),
        "api_description_sha256": sha256(api_norm.encode()).hexdigest(),
        "api_description_is_substring_of_rendered_region": api_norm in region_text,
        "rendered_region_contains_application_questions": bool(
            re.search(r"(?i)captcha|enter the code|application form", region_text)
        ),
    }


class TCS(Adapter):
    """Read TCS's global iBegin portal search and exact job-detail API."""

    VERIFICATION_REVISION = "tcs.ibegin-global-portal.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._xsrf: str | None = None
        self._countries: list[dict] = []
        self._session_lock = asyncio.Lock()
        self._session_generation = 0
        self.session_refreshes = 0
        # The iBegin portal serves its JSON API to browser User-Agents; the
        # scraper's default UA is rejected with HTTP 403. Use a normal browser UA
        # for portal requests only; this does not bypass the Akamai-protected
        # official marketing entry, which remains recorded as a failed source.
        if self.http is not None and getattr(self.http, "client", None) is not None:
            self.http.client.headers.update({
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
            })

    # -- transport -------------------------------------------------------
    def _token(self) -> str | None:
        try:
            return self.http.client.cookies.get("XSRF-TOKEN")
        except Exception:
            for cookie in self.http.client.cookies.jar:
                if cookie.name == "XSRF-TOKEN":
                    return cookie.value
        return None

    async def _bootstrap(self):
        started = time.perf_counter()
        landing = await self.http.get(LANDING_URL)
        final = urlsplit(str(landing.url))
        if (final.scheme != "https" or final.netloc != "ibegin.tcsapps.com"
                or not final.path.startswith("/candidate/next/")):
            raise PortalResponseError("TCS portal landing redirected outside its candidate app")
        if "ib-root" not in landing.text or "<title>iBegin</title>" not in landing.text:
            raise PortalResponseError("TCS portal landing is not the iBegin Angular app shell")
        session = await self.http.get(
            SESSION_URL,
            headers={"Origin": PORTAL_ORIGIN, "Referer": str(landing.url)},
        )
        session_value = session.json()
        if not isinstance(session_value, dict) or session_value.get("result") is not True \
                or session_value.get("status") != 200:
            raise PortalResponseError("TCS portal session bootstrap did not succeed")
        token = self._token()
        if not token:
            raise PortalResponseError("TCS portal session did not issue an XSRF-TOKEN")
        self._xsrf = token
        return {
            "landing_url": str(landing.url),
            "landing_status": landing.status_code,
            "landing_bytes": len(landing.content),
            "landing_sha256": sha256(landing.content).hexdigest(),
            "session_url": str(session.url),
            "session_status": session.status_code,
            "xsrf_token_issued": True,
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def _refresh_session(self):
        self.session_refreshes += 1
        await self._bootstrap()

    async def _request_with_session(self, method: str, url: str, **kwargs):
        generation = self._session_generation
        try:
            return await self.http.request(method, url, **kwargs)
        except Exception as exc:
            message = str(exc)
            if "HTTP 401" not in message and "HTTP 403" not in message:
                raise
            async with self._session_lock:
                if self._session_generation == generation:
                    await self._refresh_session()
                    self._session_generation += 1
            return await self.http.request(method, url, **kwargs)

    async def _search(self, locale: str, page: int) -> dict:
        started = time.perf_counter()
        body = search_request_body(page)
        response = await self._request_with_session(
            "POST", SEARCH_URL.format(locale=locale), json=body,
            headers={
                "next-xsrf-token": self._xsrf or "",
                "Origin": PORTAL_ORIGIN,
                "Referer": SEARCH_PAGE_URL.format(locale=locale),
            },
        )
        final = urlsplit(str(response.url))
        expected_path = f"/candidate/next/api/{locale}/search/jobs"
        if final.scheme != "https" or final.netloc != "ibegin.tcsapps.com" or final.path != expected_path:
            raise PortalResponseError("TCS search request redirected outside its published endpoint")
        parsed = parse_search_page(response.json(), page)
        parsed.update({
            "request_url": str(response.request.url),
            "final_url": str(response.url),
            "request_seconds": max(0.0, time.perf_counter() - started),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "request_body": body,
        })
        return parsed

    async def _get_json(self, url: str, context: str) -> dict:
        started = time.perf_counter()
        response = await self._request_with_session(
            "GET", url,
            headers={"Origin": PORTAL_ORIGIN, "Referer": PORTAL_BASE + "/en-IN/jobs/search"},
        )
        final = urlsplit(str(response.url))
        expected = urlsplit(url)
        if (final.scheme != "https" or final.netloc != expected.netloc or final.path != expected.path):
            raise PortalResponseError(f"TCS {context} request redirected outside its published endpoint")
        value = response.json()
        return {
            "value": value,
            "request_url": str(response.request.url),
            "final_url": str(response.url),
            "status": response.status_code,
            "seconds": max(0.0, time.perf_counter() - started),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        }

    async def _detail_request(self, locale: str, job_id: str) -> dict:
        started = time.perf_counter()
        numeric = JOB_ID_RE.fullmatch(job_id).group(1)
        url = DETAIL_URL.format(locale=locale, job_id=numeric)
        response = await self._request_with_session(
            "GET", url, headers={"Referer": JOB_PAGE_URL.format(locale=locale, job_id=job_id)}
        )
        final = urlsplit(str(response.url))
        expected_path = f"/candidate/next/api/{locale}/job/desc/{numeric}"
        if final.scheme != "https" or final.netloc != "ibegin.tcsapps.com" or final.path != expected_path:
            raise PortalResponseError("TCS job detail redirected outside its published endpoint")
        parsed = parse_detail(response.json(), job_id)
        parsed.update({
            "request_url": str(response.request.url),
            "final_url": str(response.url),
            "request_seconds": max(0.0, time.perf_counter() - started),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    # -- collection ------------------------------------------------------
    async def _official_entry_probe(self):
        started = time.perf_counter()
        try:
            response = await self.http.get(OFFICIAL_ENTRY)
            return {
                "url": OFFICIAL_ENTRY,
                "status": response.status_code,
                "final_url": str(response.url),
                "bytes": len(response.content),
                "sha256": sha256(response.content).hexdigest(),
                "blocked": False,
                "seconds": max(0.0, time.perf_counter() - started),
            }
        except Exception as exc:
            return {
                "url": OFFICIAL_ENTRY,
                "status": 403 if "HTTP 403" in str(exc) else None,
                "error": f"{type(exc).__name__}: {exc}",
                "blocked": True,
                "seconds": max(0.0, time.perf_counter() - started),
            }

    async def _probe_country(self, country: dict) -> dict:
        locale = country["locale"]
        domains = await self._get_json(DOMAINS_URL.format(locale=locale), "domains")
        domains_parsed = parse_domains(domains["value"])
        bands = await self._get_json(EXPERIENCE_URL.format(locale=locale), "experience-bands")
        bands_parsed = parse_experience_bands(bands["value"])
        search = await self._search(locale, 1)
        row = {
            **country,
            "domains": domains_parsed["domains"],
            "domains_openings_sum": domains_parsed["openings_sum"],
            "domains_issues": domains_parsed["issues"],
            "experience_bands": bands_parsed["bands"],
            "total_elements": search["total_elements"],
            "total_pages": search["total_pages"],
            "size": search["size"],
            "first_page_ids": [item["id"] for item in search["rows"]],
            "first_page_walk_ins": sum(1 for item in search["rows"] if item.get("walkIn")),
            "domains_match_total": domains_parsed["openings_sum"] == search["total_elements"],
            "probe_requests": {
                "domains": {key: domains[key] for key in ("request_url", "final_url", "status", "seconds", "response_bytes", "response_sha256")},
                "experience_bands": {key: bands[key] for key in ("request_url", "final_url", "status", "seconds", "response_bytes", "response_sha256")},
                "search_page_1": {key: search[key] for key in ("request_url", "final_url", "request_seconds", "response_bytes", "response_sha256")},
            },
        }
        return row

    async def _collect_rows(self, probes: list[dict], label: str) -> dict:
        started = time.perf_counter()
        rows: dict[str, dict] = {}
        per_country: dict[str, dict] = {}
        totals: dict[str, int] = {}
        issues: list[str] = []
        for probe in probes:
            locale = probe["locale"]
            if not self.page_guard():
                issues.append(f"page_budget_exhausted:{locale}:1")
                break
            first = await self._search(locale, 1)
            total, pages = first["total_elements"], first["total_pages"]
            totals[locale] = total
            if total == 0:
                per_country[locale] = {"total": 0, "pages": 0, "swept": 0, "unique": 0,
                                       "page_rows": []}
                continue
            ids: list[str] = []
            page_rows: list[dict] = []

            def consume(value):
                for item in value["rows"]:
                    identity = item["id"]
                    ids.append(identity)
                    if identity in rows:
                        issues.append(f"duplicate_id_across_countries:{identity}")
                        continue
                    rows[identity] = {
                        "listing": item,
                        "listing_locale": locale,
                        "listing_country": probe["country"],
                        "listing_page": value["number"] + 1,
                    }
                page_rows.append({
                    "page": value["number"] + 1,
                    "number": value["number"],
                    "rows": len(value["rows"]),
                    "first_id": value["rows"][0]["id"] if value["rows"] else None,
                    "last_id": value["rows"][-1]["id"] if value["rows"] else None,
                    "total_elements": value["total_elements"],
                    "total_pages": value["total_pages"],
                    "request_seconds": value["request_seconds"],
                    "response_bytes": value["response_bytes"],
                    "response_sha256": value["response_sha256"],
                })

            consume(first)
            for page in range(2, pages + 1):
                if not self.page_guard():
                    issues.append(f"page_budget_exhausted:{locale}:{page}")
                    break
                value = await self._search(locale, page)
                if (value["total_elements"], value["total_pages"]) != (total, pages):
                    issues.append(f"pagination_metadata_changed:{locale}:{page}")
                consume(value)
            per_country[locale] = {
                "total": total,
                "pages": pages,
                "swept": len(ids),
                "unique": len(set(ids)),
                "page_rows": page_rows,
            }
        return {
            "label": label,
            "rows": rows,
            "ids": set(rows),
            "totals": totals,
            "per_country": per_country,
            "issues": issues,
            "seconds": max(0.0, time.perf_counter() - started),
        }

    def _job_from_row(self, row: dict):
        listing = row["listing"]
        locale = row["listing_locale"]
        job_id = listing["id"]
        minimum = listing.get("minimumExperienceInYears")
        maximum = listing.get("maximumExperienceInYears")
        experience = (f"{minimum} - {maximum} Years"
                      if isinstance(minimum, int) and isinstance(maximum, int) else None)
        raw = {
            "listing": listing,
            "listing_locale": locale,
            "listing_country": row["listing_country"],
            "listing_page": row["listing_page"],
            "listing_url": SEARCH_PAGE_URL.format(locale=locale),
            "apply_action": "apply_button_requires_login",
            "apply_login_url": f"{PORTAL_BASE}/{locale}/login",
        }
        job = self.job(
            raw,
            title=listing["title"],
            url=JOB_PAGE_URL.format(locale=locale, job_id=job_id),
            job_id=job_id,
            locations=[listing["location"]] if listing.get("location") else [],
            department=listing.get("domain"),
            experience=experience,
            valid_through=listing.get("applyBy"),
            employer=EMPLOYER,
            language=next((country["language"] for country in self._countries if country["locale"] == locale), None),
        )
        job.requisition_id = None
        job.date_posted = None
        job.date_posted_raw = None
        job.apply_url = None
        job.employment_type = None
        job.workplace_type = None
        job.team = None
        return job

    async def _detail(self, job):
        started = time.perf_counter()
        locale = job.raw["listing_locale"]
        parsed = await self._detail_request(locale, job.job_id)
        job.title = parsed["title"] or job.title
        if parsed.get("location"):
            job.locations = [parsed["location"]]
        job.department = parsed.get("function_name") or job.department
        job.qualifications = parsed.get("qualifications")
        job.education = parsed.get("qualifications")
        job.experience = parsed.get("experience") or job.experience
        job.salary = salary_object(parsed)
        job.valid_through = job.raw["listing"].get("applyBy") or parsed.get("apply_by")
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "official_ibegin_job_detail_api"
        job.description_source_url = parsed["request_url"]
        job.requisition_id = str(parsed["job_id"])
        job.raw["detail"] = parsed["payload"]
        job.raw["detail_url"] = parsed["request_url"]
        job.raw["detail_response_sha256"] = parsed["response_sha256"]
        job.raw["detail_response_bytes"] = parsed["response_bytes"]
        job.raw["detail_request_seconds"] = parsed["request_seconds"]
        job.raw["description_sections"] = parsed["description_sections"]
        job.raw["detail_fields"] = {
            key: parsed[key] for key in (
                "job_id", "role", "experience", "qualifications", "skilldetail", "country",
                "function_name", "apply_by", "min_salary", "max_salary", "additional_info",
                "quiz_flag", "quiz_mandatory", "location_id", "country_match", "language_id",
            )
        }
        job.responsibilities = responsibilities_text(parsed["description_sections"])
        if apply_by_outlier(job.valid_through):
            job.warnings.append("apply_by_year_outlier")
        if parsed.get("additional_info"):
            job.raw["additional_info"] = parsed["additional_info"]
        job.description_unavailable = not job.description_text.strip()
        if job.description_unavailable:
            job.warnings.append("publisher_empty_description")
        job.raw["detail_fetch_seconds"] = max(0.0, time.perf_counter() - started)
        return job

    async def _hydrate_jobs(self, jobs):
        before = dict(self.result.metadata.get("details") or {})
        await self.hydrate(jobs, self._detail)
        # Propagate cached/reused Job objects (which may differ from the
        # originally added objects) back into the live result list. Without
        # this, verified cache hits are dropped on resume runs.
        updated = {job.key: job for job in jobs}
        self.result.jobs[:] = [updated.get(job.key, job) for job in self.result.jobs]
        after = dict(self.result.metadata.get("details") or {})
        merged = {}
        for key in set(before) | set(after):
            left, right = before.get(key, 0), after.get(key, 0)
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                merged[key] = left + right
            else:
                merged[key] = right or left
        self.result.metadata["details"] = merged

    async def _reconcile_membership(self, final: dict, *, confirmation: bool = False):
        collected = {job.job_id for job in self.result.jobs}
        added = sorted(final["ids"] - collected)
        removed = sorted(collected - final["ids"])
        added_jobs = []
        for identity in added:
            row = final["rows"][identity]
            job = self._job_from_row(row)
            self.add(job)
            added_jobs.append(job)
        if removed:
            self.result.jobs[:] = [job for job in self.result.jobs if job.job_id not in removed]
            self.seen = {job.key for job in self.result.jobs}
        if added_jobs:
            await self._hydrate_jobs(added_jobs)
        return {
            "confirmation_pass": confirmation,
            "final_unique_ids": len(final["ids"]),
            "collected_before": len(collected),
            "added_after_detail": added,
            "removed_after_detail": removed,
            "added_details_fetched": len(added_jobs),
        }

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "portal_origin": PORTAL_ORIGIN,
            "portal_release": PORTAL_RELEASE,
            "employer": EMPLOYER,
            "listing_authority": (
                "TCS's own global iBegin candidate portal, linked from the official TCS careers "
                "page's Apply now CTA; the portal publishes the country selector and job search."
            ),
            "listing_membership_request": {
                "method": "POST",
                "path": "/candidate/next/api/{locale}/search/jobs",
                "body_shape": {
                    "includeRegularJob": True, "includeWalkIn": True, "searchTerms": [],
                    "jobLocationFilters": [], "jobDomainFilters": [], "experienceLevelFilters": [],
                    "skillFilters": [], "relevancySortOrder": None, "recentSortOrder": "desc",
                    "lastDateToApplySortOrder": None, "page": "1-based", "resultsPerPage": PAGE_SIZE,
                },
                "filters_applied": False,
                "scope": "Every country/locale published by TCS's own iBegin country selector; no external board.",
                "sort_note": (
                    "The UI's default Most Relevant order changed between identical empty-term requests. "
                    "The UI's own Posted Date sort key is used alone so pagination is deterministic; "
                    "sorting does not change membership and every page is reconciled against totals."
                ),
            },
            "detail_method": "GET /candidate/next/api/{locale}/job/desc/{numericId} for each listed job.",
            "date_policy": "The portal exposes no posted date; date_posted/date_updated stay null. applyBy is kept only as valid_through.",
        })
        try:
            entry = await self._official_entry_probe()
            self.result.metadata["official_entry"] = entry
            if entry.get("blocked"):
                self.result.metadata["official_entry_note"] = (
                    "The marketing entry is Akamai-blocked from this network (HTTP 403). "
                    "The official page's archived 2026-02-01 capture links its Apply now CTA to "
                    "https://ibegin.tcsapps.com/candidate/, which is the live TCS portal used here; "
                    "the portal footer links back to https://www.tcs.com/careers."
                )
            bootstrap = await self._bootstrap()
            self.result.metadata["portal"] = bootstrap
            geographies = await self._get_json(GEOGRAPHIES_URL, "geographies")
            geographies_rows = parse_geographies(geographies["value"])
            countries = default_country_locales(geographies_rows)
            self._countries = countries
            self.result.metadata["geographies"] = {
                "rows": len(geographies_rows),
                "countries": len(countries),
                "request": {key: geographies[key] for key in
                            ("request_url", "final_url", "status", "seconds", "response_bytes", "response_sha256")},
                "published_countries": countries,
            }
            probes = []
            for country in countries:
                probes.append(await self._probe_country(country))
            self.result.metadata["country_probes"] = probes
            initial = await self._collect_rows(probes, "initial")
            self.result.metadata["initial_sweep"] = {
                "seconds": initial["seconds"],
                "unique_ids": len(initial["ids"]),
                "totals": initial["totals"],
                "issues": initial["issues"],
                "per_country": initial["per_country"],
            }
            for identity, row in initial["rows"].items():
                self.add(self._job_from_row(row))
            self.result.metadata["initial_listing_jobs"] = len(self.result.jobs)
            await self._hydrate_jobs(list(self.result.jobs))
            final = await self._collect_rows(probes, "post_detail")
            reconciliation = await self._reconcile_membership(final)
            last_reconciliation = reconciliation
            if reconciliation["added_after_detail"] or reconciliation["removed_after_detail"]:
                confirmation = await self._collect_rows(probes, "confirmation")
                second = await self._reconcile_membership(confirmation, confirmation=True)
                reconciliation["confirmation"] = second
                last_reconciliation = second
                final = confirmation
            self.result.metadata["membership_reconciliation"] = reconciliation
            # Final independent country/domain count recheck after details and any drift.
            final_domains = {}
            for probe in probes:
                value = await self._get_json(DOMAINS_URL.format(locale=probe["locale"]), "domains")
                parsed = parse_domains(value["value"])
                final_domains[probe["locale"]] = {
                    "country": probe["country"],
                    "openings_sum": parsed["openings_sum"],
                    "issues": parsed["issues"],
                    "matches_final_total": parsed["openings_sum"] == final["totals"].get(probe["locale"]),
                    "request": {key: value[key] for key in
                                ("request_url", "final_url", "status", "seconds", "response_bytes", "response_sha256")},
                }
            self.result.metadata["final_domain_recheck"] = final_domains
            self.result.metadata["final_sweep"] = {
                "seconds": final["seconds"],
                "unique_ids": len(final["ids"]),
                "totals": final["totals"],
                "issues": final["issues"],
                "per_country": final["per_country"],
            }
            expected_total = sum(final["totals"].values())
            self.result.expected_total = expected_total
            self.result.metadata["country_totals"] = {
                probe["locale"]: {
                    "country": probe["country"],
                    "probe_total_elements": probe["total_elements"],
                    "final_total_elements": final["totals"].get(probe["locale"]),
                    "final_total_pages": final["per_country"].get(probe["locale"], {}).get("pages"),
                    "probe_domains_openings_sum": probe["domains_openings_sum"],
                    "final_domains_openings_sum": final_domains[probe["locale"]]["openings_sum"],
                    "probe_domains_match_probe_total": probe["domains_match_total"],
                    "final_domains_match_final_total": final_domains[probe["locale"]]["matches_final_total"],
                } for probe in probes
            }
            self.result.metadata["domains_all_match_totals"] = all(
                row["matches_final_total"] for row in final_domains.values()
            )
            self.result.metadata["membership_checks"] = {
                "advertised_total": expected_total,
                "collected_unique_ids": len(final["ids"]),
                "collected_jobs": len(self.result.jobs),
                "initial_sweep_ids": len(initial["ids"]),
                "initial_equals_final": initial["ids"] == final["ids"],
                "domains_all_match_totals": self.result.metadata["domains_all_match_totals"],
                "walk_in_rows": sum(1 for job in self.result.jobs if job.raw["listing"].get("walkIn")),
                "countries_with_openings": sum(1 for probe in probes if probe["total_elements"]),
                "session_refreshes": self.session_refreshes,
            }
            self.result.listing_complete = (
                len(final["ids"]) == expected_total
                and len(self.result.jobs) == expected_total
                and not final["issues"]
                and not initial["issues"]
                and self.result.metadata["domains_all_match_totals"]
                and not last_reconciliation["added_after_detail"]
                and not last_reconciliation["removed_after_detail"]
            )
            if not self.result.listing_complete:
                self.result.errors.append("listing_reconciliation_incomplete")
        except Exception as exc:
            self.result.errors.append(f"TCS source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
        finally:
            self.result.timings["source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            self.result.finish()
        return self.result
