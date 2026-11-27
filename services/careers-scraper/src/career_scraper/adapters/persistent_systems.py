"""Persistent Systems' own careers portal (careers.persistent.com, Zwayam-powered).

The company careers portal is an Angular SPA served from Persistent's own
sub-domain. Its listing is the company-scoped Zwayam search for
``domain=careers.persistent.com`` / ``companyId=16344`` (680 rows at review time,
nine per page with a ``Show More`` button). The exact job page is
``/jobview/<jobUrl>?id=<id>`` and the full employer description comes from the
same portal's ``jobs-service/v1/jobs/careersite`` detail endpoint.

The ledger URL ``https://www.persistent.com/careers/`` 301-redirects to the
company portal ``https://careers.persistent.com/``. A curl-style probe of the
marketing URL can receive a Radware Bot Manager (Perfdrive) challenge page; it is
never solved or bypassed, and the run records whichever response the official
entry actually returns.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import re
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


LEDGER_ENTRY = "https://www.persistent.com/careers/"
CAREERS_ORIGIN = "https://careers.persistent.com"
LISTING_PAGE = CAREERS_ORIGIN + "/explore-opportunities"
SEARCH_API = "https://public.zwayam.com/jobs/search"
DETAIL_API = "https://public.zwayam.com/jobs-service/v1/jobs/careersite"
CONFIG_API = "https://public.zwayam.com/data-service/v2/company/16344/careersite-configurations"
COMPANY_ID = "16344"
COMPANY_ID_B64 = "MTYzNDQ="
DOMAIN = "careers.persistent.com"
PAGE_SIZE = 9
EMPLOYER = "Persistent Systems"
JOB_URL_PREFIX = CAREERS_ORIGIN + "/jobview/"
DETAIL_BATCH = 40
HTTP_REFRESH_AFTER_DETAILS = 120
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
API_HEADERS = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Origin": CAREERS_ORIGIN,
    "Referer": LISTING_PAGE,
    # Observed: a single keep-alive connection that serves ~360 jobs-service
    # requests starts being tarpitted by the Akamai edge (no bytes, no status).
    # Short-lived connections keep the same request shape without tripping it.
    "Connection": "close",
}
BOT_MARKERS = ("perfdrive", "radware captcha", "botmanager_support@radware.com")
SECTIONS_RESPONSIBILITY = (
    "responsibilit", "what you'll do", "what you will do", "what you’ll do",
    "what you will be doing", "duties", "your role",
)
SECTIONS_QUALIFICATION = (
    "qualif", "requirement", "expertise you'll bring", "expertise you’ll bring",
    "what you'll need", "what you’ll need", "what you will need", "who you are",
    "must have", "skills required", "required skills", "desired skills",
)


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def normalize_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return normalized(soup.get_text(" ", strip=True))


def is_bot_challenge(html: str | None) -> bool:
    lowered = (html or "").lower()
    return any(marker in lowered for marker in BOT_MARKERS)


def classify_ledger_entry(html: str, response_url: str, status: int, redirects=None) -> dict:
    """Record the marketing entry exactly as observed; never solve a challenge."""
    soup = BeautifulSoup(html or "", "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    challenge = is_bot_challenge(html)
    parsed = urlsplit(response_url)
    return {
        "url": LEDGER_ENTRY,
        "final_url": response_url,
        "status": status,
        "title": title,
        "redirects": redirects or [],
        "bot_challenge": challenge,
        "bot_manager": "Radware Bot Manager (Perfdrive)" if challenge else None,
        "challenge_page_followed_or_solved": False,
        "resolved_official_careers_host": None if parsed.netloc == "validate.perfdrive.com" else parsed.netloc,
    }


def job_view_url(row: dict) -> str:
    job_url = row.get("jobUrl")
    identity = row.get("id")
    if not isinstance(job_url, str) or not job_url.strip():
        raise ValueError("Persistent Systems listing row is missing its jobUrl slug")
    if identity is None or not re.fullmatch(r"\d+", str(identity)):
        raise ValueError("Persistent Systems listing row is missing its numeric id")
    return f"{JOB_URL_PREFIX}{job_url}?id={identity}"


def job_page_identity(url: str) -> tuple[str, str]:
    """Parse the exposed job page URL back into (slug, id)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "careers.persistent.com":
        raise ValueError("Persistent Systems job URL is not on the company careers host")
    match = re.fullmatch(r"/jobview/([^/]+)", parts.path)
    if match is None:
        raise ValueError("Persistent Systems job URL is not an observed /jobview/ route")
    query = parse_qs(parts.query)
    if list(query) != ["id"] or not re.fullmatch(r"\d+", query["id"][0]):
        raise ValueError("Persistent Systems job URL lost its exact ?id= query")
    return match.group(1), query["id"][0]


def parse_careersite_config(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("responseStatus") != "SUCCESS":
        raise ValueError("Persistent Systems careersite configuration response changed")
    company = (value.get("reponseObject") or {}).get("company") or {}
    if str(company.get("id")) != COMPANY_ID or company.get("companyName") != EMPLOYER:
        raise ValueError("Persistent Systems careersite configuration is not the company tenant 16344")
    if company.get("careerSiteUrl") != DOMAIN or company.get("domainName") != DOMAIN:
        raise ValueError("Persistent Systems careersite configuration domain changed")
    if company.get("website") != "https://www.persistent.com":
        raise ValueError("Persistent Systems careersite configuration website changed")
    return {
        "company_id": str(company["id"]),
        "company_name": company["companyName"],
        "career_site_url": company["careerSiteUrl"],
        "domain_name": company["domainName"],
        "website": company["website"],
        "folder": company.get("folder"),
        "seo": (value.get("reponseObject") or {}).get("configurationSeo") or {},
        "common_configuration": (value.get("reponseObject") or {}).get("companyCommonConfiguration") or {},
        "company_logo": ((value.get("reponseObject") or {}).get("configurationExternalSystem") or {}).get("companyLogoName"),
    }


def parse_search_page(value: dict, requested_offset: int) -> dict:
    if not isinstance(value, dict) or value.get("code") != 200:
        raise ValueError("Persistent Systems jobs/search response is not a successful object")
    data = value.get("data")
    if not isinstance(data, dict):
        raise ValueError("Persistent Systems jobs/search response is missing data")
    total, has_more, rows = data.get("totalCount"), data.get("hasMoreData"), data.get("data")
    config = data.get("facetedSearchConfig") or {}
    if not isinstance(total, int) or not isinstance(has_more, bool) or not isinstance(rows, list):
        raise ValueError("Persistent Systems jobs/search pagination fields changed")
    page_size = config.get("paginationHowMuch")
    try:
        page_size = int(page_size)
    except (TypeError, ValueError):
        page_size = None
    if page_size != PAGE_SIZE:
        raise ValueError(f"Persistent Systems jobs/search page size changed: {config.get('paginationHowMuch')!r}")
    issues = []
    if len(rows) > page_size:
        issues.append(f"persistent_search_page_rows_exceed_page_size:{len(rows)}")
    kept = []
    for row in rows:
        source = row.get("_source") if isinstance(row, dict) else None
        if not isinstance(source, dict):
            issues.append("persistent_search_row_missing_source")
            continue
        identity = source.get("id")
        if identity is None or not re.fullmatch(r"\d+", str(identity)):
            issues.append("persistent_search_row_missing_numeric_id")
            continue
        if not normalized(source.get("jobTitle")):
            issues.append(f"persistent_search_row_missing_title:{identity}")
            continue
        if not source.get("jobUrl"):
            issues.append(f"persistent_search_row_missing_job_url:{identity}")
            continue
        if str(source.get("companyId")) != COMPANY_ID:
            issues.append(f"persistent_search_row_wrong_company:{identity}")
            continue
        if source.get("careerSiteUrl") != DOMAIN:
            issues.append(f"persistent_search_row_wrong_career_site:{identity}")
            continue
        kept.append(source)
    if len(kept) != len(rows):
        issues.append("persistent_search_rows_dropped")
    if has_more != (requested_offset + len(rows) < total):
        issues.append("persistent_search_has_more_inconsistent")
    if requested_offset >= total and rows:
        issues.append("persistent_search_rows_past_total")
    return {
        "offset": requested_offset,
        "total": total,
        "has_more": has_more,
        "page_size": page_size,
        "rows": kept,
        "row_ids": [str(row["id"]) for row in kept],
        "facets": data.get("facets") or {},
        "facet_config": {
            "pagination_required": config.get("paginationRequired"),
            "visible_number_of_pages": config.get("visibleNumberOfPages"),
            "sort_based_on": config.get("sortBasedOnConfigCriteria"),
            "query_criteria": config.get("queryCriteria"),
        },
        "issues": issues,
    }


def parse_detail(value: dict, row: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Persistent Systems job detail response is not an object")
    if str(value.get("id")) != str(row.get("id")) or value.get("jobUrl") != row.get("jobUrl"):
        raise ValueError("Persistent Systems job detail identity does not match the listed job")
    title = normalized(value.get("jobTitle"))
    if not title:
        raise ValueError("Persistent Systems job detail is missing its jobTitle")
    description = value.get("longDescription") or ""
    if not normalized(normalize_body_text(description)):
        raise ValueError("Persistent Systems job detail has an empty longDescription")
    issues = []
    if str(value.get("companyId")) != COMPANY_ID:
        issues.append("persistent_detail_wrong_company")
    if value.get("status") != 1:
        issues.append(f"persistent_detail_status_not_open:{value.get('status')}")
    if value.get("jobVisibiltyLevel") == "P":
        issues.append("persistent_detail_private_job_visibility")
    config_description = ((value.get("jobConfigurationData") or {}).get("Description")) or ""
    if config_description and normalize_body_text(config_description) != normalize_body_text(description):
        issues.append("persistent_detail_configuration_description_differs")
    if normalized(value.get("careersiteRedirectionUrl") or ""):
        issues.append("persistent_detail_has_external_apply_redirect")
    return {
        "detail": value,
        "title": title,
        "description_html": description,
        "job_configuration_data": value.get("jobConfigurationData") or {},
        "issues": issues,
    }


def split_locations(value: str | None) -> list[str]:
    """The source packs multi-city postings as 'CITY / CITY, Region, Country'.

    The listing field uses `` / ``, while the detail endpoint returns the same
    multi-city value semicolon-separated; both are split into the published
    locations, keeping the raw string in the source fields.
    """
    text = normalized(value)
    if not text:
        return []
    parts = [part.strip(" ,;/") for part in re.split(r"\s*/\s*|;", text)]
    return [part for part in dict.fromkeys(parts) if part]


def description_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(f'<div data-persistent-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-persistent-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    sections, current = [], None
    for block in ([node for node in root.contents if isinstance(node, Tag)] if root else []):
        heading = ""
        if block.name in heading_tags:
            heading = block.get_text(" ", strip=True)
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized(block.get_text(" ", strip=True))
            strong_text = normalized(strong.get_text(" ", strip=True)) if strong else ""
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
        current["text"] = text_html(current["html"])
        sections.append(current)
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def _matching_sections(sections: list[dict], tokens: tuple[str, ...]) -> list[dict]:
    return [section for section in sections if section["heading"]
            and any(token in section["heading"].lower() for token in tokens)]


def salary_from_detail(detail: dict) -> dict | None:
    minimum = normalized(detail.get("minJobSalary"))
    maximum = normalized(detail.get("maxJobSalary"))
    if not minimum and not maximum:
        return None
    return {"min": minimum or None, "max": maximum or None,
            "currency": detail.get("currencyType"), "source": "detail minJobSalary/maxJobSalary"}


class PersistentSystems(Adapter):
    """Company-scoped Zwayam careers site published on careers.persistent.com."""

    VERIFICATION_REVISION = "persistent-systems.careers-portal.zwayam-search.1"

    def __init__(self, http, company, source, options, *, http_factory=None):
        super().__init__(http, company, source, options)
        self.http_factory = http_factory
        self._details_on_client = 0

    async def _rotate_http(self):
        """Drop a client whose edge connections have served too many requests.

        The Akamai edge silently tarpits a client after roughly 330-360
        jobs-service requests (no bytes, no status). Short-lived connections and
        a fresh client restore normal responses; the rotation is recorded.
        """
        old = self.http
        self.http = self.http_factory()
        self.result.metadata.setdefault("http_client_rotations", []).append(
            {"after_detail_requests": self._details_on_client})
        self._details_on_client = 0
        await old.__aexit__(None, None, None)

    async def _post_retry(self, url, **kwargs):
        """Retry once with a fresh connection after an edge tarpit (no bytes, no status)."""
        try:
            return await asyncio.wait_for(self.http.request("POST", url, **kwargs), timeout=75)
        except TimeoutError:
            await asyncio.sleep(10)
            return await asyncio.wait_for(self.http.request("POST", url, **kwargs), timeout=75)

    async def _ledger_entry(self):
        started = time.perf_counter()
        response = await self.http.get(LEDGER_ENTRY, headers=BROWSER_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        redirects = [{"status": item.status_code, "url": str(item.url),
                      "location": item.headers.get("location")} for item in response.history]
        parsed = classify_ledger_entry(response.text, str(response.url), response.status_code, redirects)
        parsed["seconds"] = seconds
        parsed["response_bytes"] = len(response.content)
        parsed["response_sha256"] = sha256(response.content).hexdigest()
        return parsed

    async def _careers_portal(self):
        started = time.perf_counter()
        response = await self.http.get(LISTING_PAGE, headers=BROWSER_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        if urlsplit(str(response.url)).netloc != "careers.persistent.com":
            raise ValueError("Persistent Systems careers portal redirected off the company host")
        soup = BeautifulSoup(response.text, "lxml")
        title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
        canonical = soup.select_one('link[rel="canonical"], link[rel="“canonical”"]')
        return {
            "url": LISTING_PAGE,
            "final_url": str(response.url),
            "status": response.status_code,
            "title": title,
            "canonical": canonical.get("href") if canonical else None,
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        }

    async def _configuration(self):
        started = time.perf_counter()
        response = await self.http.get(CONFIG_API, headers=API_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_careersite_config(response.json())
        parsed.update(url=CONFIG_API, seconds=seconds, status=response.status_code,
                      response_sha256=sha256(response.content).hexdigest())
        return parsed

    async def _listing_pass(self, pass_name: str):
        started = time.perf_counter()
        first_seconds = None
        first_facets = {}
        pages, rows, seen, issues = [], [], set(), []
        expected_total = None
        offset = 0
        while True:
            if not self.page_guard():
                issues.append("persistent_listing_page_budget_exhausted")
                break
            criteria = {
                "paginationStartNo": offset,
                "selectedCall": "sort",
                "sortCriteria": {"name": "modifiedDate", "isAscending": False},
                "anyOfTheseWords": "",
            }
            request_started = time.perf_counter()
            response = await self._post_retry(
                SEARCH_API,
                files={"filterCri": (None, _json(criteria)),
                       "domain": (None, DOMAIN),
                       "companyId": (None, COMPANY_ID_B64)},
                headers=API_HEADERS,
            )
            request_seconds = max(0.0, time.perf_counter() - request_started)
            if first_seconds is None:
                first_seconds = request_seconds
            parsed_url = urlsplit(str(response.url))
            if parsed_url.scheme != "https" or parsed_url.netloc != "public.zwayam.com" or parsed_url.path != "/jobs/search":
                raise ValueError("Persistent Systems jobs/search redirected away from its company-scoped route")
            page = parse_search_page(response.json(), offset)
            issues.extend(page["issues"])
            if expected_total is None:
                expected_total = page["total"]
                first_facets = page["facets"]
            elif page["total"] != expected_total:
                issues.append(f"persistent_listing_total_changed_mid_pass:{page['total']}")
            for row in page["rows"]:
                identity = str(row["id"])
                if identity in seen:
                    issues.append(f"persistent_listing_duplicate_id:{identity}")
                    continue
                seen.add(identity)
                rows.append(row)
            pages.append({
                "offset": offset,
                "row_ids": page["row_ids"],
                "row_count": len(page["rows"]),
                "total": page["total"],
                "has_more": page["has_more"],
                "request_seconds": request_seconds,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
            })
            if not page["rows"] or not page["has_more"]:
                break
            offset += len(page["rows"])
        if expected_total is None:
            raise ValueError("Persistent Systems jobs/search returned no total count")
        if len(rows) != expected_total:
            issues.append(f"persistent_listing_rows_do_not_match_total:{len(rows)}!={expected_total}")
        facet_sums = {name: sum(entry.get("doc_count", 0) for entry in values)
                      for name, values in first_facets.items()}
        return {
            "pass": pass_name,
            "total": expected_total,
            "rows": rows,
            "row_ids": [str(row["id"]) for row in rows],
            "pages": pages,
            "page_count": len(pages),
            "facets": first_facets,
            "facet_sums": facet_sums,
            "issues": issues,
            "request_seconds": max(0.0, time.perf_counter() - started),
            "first_request_seconds": first_seconds,
        }

    async def _detail(self, job):
        row = job.raw["listing"]
        started = time.perf_counter()
        try:
            response = await self._post_retry(
                DETAIL_API,
                json={"jobUrl": row["jobUrl"], "externalSource": "CareerSite",
                      "campusUrl": "empty", "companyId": COMPANY_ID, "jobId": str(row["id"])},
                headers={**API_HEADERS, "Referer": job.url},
            )
        except TimeoutError as exc:
            raise ValueError(f"Persistent Systems detail request timed out for {row['id']}") from exc
        seconds = max(0.0, time.perf_counter() - started)
        self._details_on_client += 1
        if urlsplit(str(response.url)).netloc != "public.zwayam.com":
            raise ValueError("Persistent Systems job detail redirected off the careers API")
        parsed = parse_detail(response.json(), row)
        return self._make_job(row, parsed, job_url=job.url, request_seconds=seconds,
                              response_sha256=sha256(response.content).hexdigest(),
                              response_bytes=len(response.content))

    def _make_job(self, row, parsed, *, job_url, request_seconds, response_sha256, response_bytes):
        detail = parsed["detail"]
        description_html = parsed["description_html"]
        sections = description_sections(description_html)
        links = [{"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
                 for anchor in BeautifulSoup(description_html, "lxml").select("a[href]")]
        title = parsed["title"]
        reference = normalized(detail.get("referenceNumber") or row.get("referenceNumber"))
        job = self.job(
            {
                "listing": dict(row),
                "detail": detail,
                "detail_sections": sections,
                "detail_links": links,
                "job_code": detail.get("jobCode") or row.get("newJobCode"),
                "new_job_code": row.get("newJobCode"),
                "reference_number": reference or None,
                "dates": {
                    "createdDate": row.get("createdDate"),
                    "modifiedDate": row.get("modifiedDate"),
                    "createDate": detail.get("createDate"),
                    "lastUpdatedTime": detail.get("lastUpdatedTime"),
                    "startDate": detail.get("startDate"),
                    "endtDate": detail.get("endtDate"),
                    "curatedDate": detail.get("curatedDate"),
                    "posting_date_mapping": None,
                    "closing_date_mapping": None,
                },
                "application_route": {
                    "exact_job_page_url": job_url,
                    "listing_cta": "Apply",
                    "detail_page_apply_control": "Submit your application now",
                    "application_form_in_page": True,
                    "separate_form_action_url": detail.get("careersiteRedirectionUrl"),
                    "application_questions_source": "jobSpecificQuestions (excluded from description)",
                },
                "detail_request": {"url": DETAIL_API, "method": "POST",
                                   "seconds": request_seconds,
                                   "response_sha256": response_sha256,
                                   "response_bytes": response_bytes},
            },
            title=title,
            url=job_url,
            job_id=str(detail["id"]),
            requisition_id=reference or None,
            apply_url=job_url,
            description=description_html,
            method="persistent.zwayam.jobs-service.careersite.longDescription",
            description_url=DETAIL_API,
            locations=split_locations(detail.get("location") or row.get("locationSeparatedbySlash")),
            department=detail.get("departmentName") or row.get("departmentName"),
            team=(detail.get("department") or {}).get("departmentFullcode"),
            employment_type=None,
            workplace_type=None,
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            valid_through=None,
            salary=salary_from_detail(detail),
            qualifications=_matching_sections(sections, SECTIONS_QUALIFICATION) or None,
            responsibilities=_matching_sections(sections, SECTIONS_RESPONSIBILITY) or None,
            experience={"min_years": detail.get("minYrsOfExperience"),
                        "max_years": detail.get("maxYrsOfExperience"),
                        "label": detail.get("yrsOfExperience")},
            education=detail.get("eduqualification"),
            employer=EMPLOYER,
            language="en",
        )
        job.description_text = text_html(description_html)
        job.company_scope_verified = self.company.scope == "company"
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": LEDGER_ENTRY,
            "official_careers_host": CAREERS_ORIGIN,
            "listing_url": LISTING_PAGE,
            "listing_authority": (
                "Persistent Systems' own careers portal on careers.persistent.com "
                "(Zwayam-powered, company tenant 16344 / domain careers.persistent.com)."
            ),
            "listing_membership_request": {
                "method": "POST",
                "url": SEARCH_API,
                "content_type": "multipart/form-data",
                "fields": {"filterCri": "JSON criteria", "domain": DOMAIN, "companyId": COMPANY_ID_B64},
                "criteria": {"paginationStartNo": "row offset, 0,9,18,...", "selectedCall": "sort",
                             "sortCriteria": {"name": "modifiedDate", "isAscending": False},
                             "anyOfTheseWords": ""},
                "filters_applied": False,
                "scope": "company tenant 16344 and careers.persistent.com only; no other Zwayam board is queried.",
            },
            "detail_method": (
                "Full longDescription from the same portal's jobs-service/v1/jobs/careersite "
                "endpoint, one request per listed posting."
            ),
            "request_scope": "Only the exact Zwayam tenant/domain request shapes observed in the company bundle.",
        })
        try:
            entry = await self._ledger_entry()
        except Exception as exc:
            entry = {"url": LEDGER_ENTRY, "final_url": None, "status": None,
                     "error": f"{type(exc).__name__}: {exc}", "bot_challenge": None,
                     "challenge_page_followed_or_solved": False}
            self.result.warnings.append("persistent_ledger_entry_request_failed")
        try:
            portal = await self._careers_portal()
            configuration = await self._configuration()
        except Exception as exc:
            self.result.errors.append(f"Persistent Systems careers portal request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["ledger_entry"] = entry
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result
        self.result.metadata["ledger_entry"] = entry
        self.result.metadata["careers_portal"] = portal
        self.result.metadata["careersite_configuration"] = configuration

        initial = None
        try:
            initial = await self._listing_pass("initial")
        except Exception as exc:
            self.result.errors.append(f"Persistent Systems initial listing failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result
        self.result.expected_total = initial["total"]
        self.result.metadata["initial_listing"] = {
            "total": initial["total"], "page_count": initial["page_count"],
            "facet_sums": initial["facet_sums"],
            "issues": initial["issues"],
            "pages": initial["pages"],
            "pass_seconds": initial["request_seconds"],
        }
        self.result.errors.extend(initial["issues"])
        facet_sum = initial["facet_sums"].get("Location")
        if facet_sum is not None and facet_sum != initial["total"]:
            self.result.errors.append(f"persistent_listing_location_facet_sum_mismatch:{facet_sum}!={initial['total']}")

        aggregate = {"total": 0, "checked": 0, "fetched_jobs": 0, "verified_cache_hits": 0,
                     "unavailable_cache_hits": 0, "failed": 0, "detail_chunks": 0}
        detail_started = time.perf_counter()
        for start in range(0, len(initial["rows"]), DETAIL_BATCH):
            chunk_rows = initial["rows"][start:start + DETAIL_BATCH]
            placeholders = [self.job(
                {"listing": dict(row)}, title=normalized(row.get("jobTitle")),
                url=job_view_url(row), job_id=str(row["id"]),
                requisition_id=normalized(row.get("referenceNumber")) or None,
                apply_url=job_view_url(row),
            ) for row in chunk_rows]
            hydrated_chunk = await self.hydrate(placeholders, self._detail)
            for job in hydrated_chunk:
                self.add(job)
            aggregate["detail_chunks"] += 1
            for key in ("total", "checked", "fetched_jobs", "verified_cache_hits",
                        "unavailable_cache_hits", "failed"):
                aggregate[key] = aggregate.get(key, 0) + (self.result.metadata.get("details") or {}).get(key, 0)
            if (self.http_factory and self._details_on_client >= HTTP_REFRESH_AFTER_DETAILS):
                await self._rotate_http()
            if self.options.checkpoint:
                self.options.checkpoint(self.result)
        hydrated = list(self.result.jobs)
        self.result.metadata["details"] = aggregate
        self.result.metadata["detail_chunk_seconds"] = max(0.0, time.perf_counter() - detail_started)
        self.result.timings["detail_phase_seconds"] = self.result.metadata["detail_chunk_seconds"]
        for job in hydrated:
            listed_title = normalized(job.raw.get("listing", {}).get("jobTitle"))
            if listed_title and listed_title != job.title:
                self.result.errors.append(f"persistent_detail_title_mismatch:{job.job_id}")
            try:
                job_page_identity(job.url)
            except ValueError as exc:
                self.result.errors.append(f"persistent_job_url_invalid:{job.job_id}:{exc}")
        final = None
        try:
            final = await self._listing_pass("final")
        except Exception as exc:
            self.result.errors.append(f"Persistent Systems final listing recheck failed: {type(exc).__name__}: {exc}")

        reconciliation = {
            "initial_total": initial["total"],
            "initial_page_count": initial["page_count"],
            "initial_ids": initial["row_ids"],
            "final_total": final["total"] if final else None,
            "final_page_count": final["page_count"] if final else None,
            "final_ids": final["row_ids"] if final else None,
            "initial_final_id_order_matches": bool(final and final["row_ids"] == initial["row_ids"]),
            "final_issues": final["issues"] if final else ["final_listing_pass_failed"],
            "retained_rows": len(hydrated),
        }
        if final is None:
            reconciliation["complete"] = False
        elif final["row_ids"] != initial["row_ids"] or final["total"] != initial["total"]:
            self.result.errors.append("persistent_membership_changed_between_initial_and_final_pass")
            reconciliation["complete"] = False
        elif len(hydrated) != initial["total"]:
            self.result.errors.append("persistent_retained_rows_do_not_match_listing_total")
            reconciliation["complete"] = False
        else:
            reconciliation["complete"] = True
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.metadata["final_listing"] = ({
            "total": final["total"], "page_count": final["page_count"], "issues": final["issues"],
            "pages": final["pages"], "pass_seconds": final["request_seconds"],
        } if final else None)
        self.result.metadata["detail_requests"] = {
            "total": len(hydrated),
            "with_full_description": sum(1 for job in hydrated if job.raw.get("detail")),
            "salary_published": sum(1 for job in hydrated if job.salary),
            "with_multiple_locations": sum(1 for job in hydrated if len(job.locations) > 1),
        }
        self.result.listing_complete = bool(
            reconciliation["complete"]
            and initial["total"] == len(hydrated)
            and not self.result.errors
        )
        self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def rendered_description_text(page_html: str) -> str:
    """Extract the exact rendered description container from a jobview page."""
    soup = BeautifulSoup(page_html, "lxml")
    container = soup.select_one("div.job-view__description")
    if container is None:
        raise ValueError("Persistent Systems rendered job page has no description container")
    data = container.select_one("div.attribute-data")
    if data is None:
        raise ValueError("Persistent Systems rendered job page has no description attribute-data")
    return normalized(data.get_text(" ", strip=True))
