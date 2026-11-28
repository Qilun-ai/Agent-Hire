"""Ola's official careers entry and the exact TurboHire career page it publishes.

Observed on 2026-11-07 (see ``companies/ola/journey-research.md``):

- ``https://www.olacabs.com/careers`` is the ledger's official entry. It is a
  Magento marketing page whose only jobs destination (twice, both labelled
  "See our Openings") is the TurboHire page
  ``https://olacareers.turbohire.co/careerpage/e0c1eb37-...``.
- That TurboHire tenant is ``ANI Technologies Pvt Ltd`` (account name
  ``olacareers``; verified domains @olacabs.com and @olaelectric.com).
- Its public career-page listing request is
  ``POST /api/careerpagev2/filteredjobs?orgId=...&pageType=0``. On the
  observation date it returned ``{"Total":0,"Result":[]}`` for the career
  page and for every public page type/client filter. The rendered page says
  "Sorry, Currently there are no vacancies." and the dashboard says
  "No Jobs listed".

This adapter follows only the official entry and the exact company-published
TurboHire page. It does not guess an ATS slug, enumerate another TurboHire
tenant, or treat a parent/group client as Ola membership. The current snapshot
has zero postings, so no detail page exists; the detail route is implemented
only for actual listing rows and is exercised by tests with synthetic data.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://www.olacabs.com/careers"
TURBOHIRE_ORIGIN = "https://olacareers.turbohire.co"
CAREERPAGE_ORG_ID = "e0c1eb37-eb7a-4ca4-bcc5-d59ce4ce9212"
CAREERPAGE_URL = f"{TURBOHIRE_ORIGIN}/careerpage/{CAREERPAGE_ORG_ID}"
DASHBOARD_URL = f"{TURBOHIRE_ORIGIN}/dashboardv2?orgId={CAREERPAGE_ORG_ID}&type=0"

API_ORIGIN = "https://thapi.azurewebsites.net"
TOKEN_URL = API_ORIGIN + "/api/token/noauth"
PUBLIC_ORG_URL = API_ORIGIN + "/api/publicorganizations/" + CAREERPAGE_ORG_ID
SSO_URL = API_ORIGIN + "/api/org/sso?accountName=olacareers"
DEPARTMENTS_URL = API_ORIGIN + "/api/justdropyourresume/departments"
LISTING_URL = API_ORIGIN + "/api/careerpagev2/filteredjobs"
CONFIG_DEPARTMENTS_URL = API_ORIGIN + "/api/careerpage/departments/org"
CLIENTELE_URL = API_ORIGIN + "/api/organizations/clientele/org"
DETAIL_URL = API_ORIGIN + "/api/referraljobs"

ACCOUNT_NAME = "olacareers"
ORG_NAME = "ANI Technologies Pvt Ltd"
CAREER_PAGE_TYPE = "CAREER_PAGE"
LISTING_PAGE_TYPE = "0"
CAREERPAGE_PATH = re.compile(
    r"^/careerpage/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$",
    re.I,
)
ATTACHMENT_KEYS = ("JobDescriptionBlobUrl", "JobDescriptionURL", "JobDescriptionUrl")

# Exact public request captured from the rendered Ola TurboHire dashboard on
# 2026-11-07. No filters/search are applied; the endpoint returns Total + every
# Result row in one POST.
LISTING_REQUEST_BODY = {
    "SortByV2": {"Key": "PostedDate", "Order": 2},
    "BunitIds": {"Value": None, "FilterType": 0},
    "Experience": {"Value": None, "FilterType": 0},
    "JobTypes": {"Value": None, "FilterType": 0},
    "JobTypeV2": {"Value": None, "FilterType": 0},
    "Locations": {"Value": None, "FilterType": 0},
    "CreatedDate": {"Value": None, "FilterType": 0},
    "Compensation": {"Value": None, "FilterType": 0},
    "Skills": {"Value": None, "FilterType": 0},
    "Keyword": "",
    "ClientIds": {"Value": None, "FilterType": 0},
    "Department": "",
    "CustomFields": {},
}


def normalized_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return normalized_space(soup.get_text(" "))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def api_headers(token: str | None = None) -> dict:
    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": TURBOHIRE_ORIGIN,
        "referer": TURBOHIRE_ORIGIN + "/",
    }
    if token:
        headers["authorization"] = "Bearer " + token
    return headers


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.olacabs.com"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Ola official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ")) if soup.title else ""
    labels: dict[str, list[str]] = {}
    for anchor in soup.select("a[href]"):
        target = urljoin(response_url, (anchor.get("href") or "").strip())
        if not target.startswith(TURBOHIRE_ORIGIN + "/careerpage/"):
            continue
        labels.setdefault(target, []).append(normalized_space(anchor.get_text(" ")))
    if len(labels) != 1:
        raise ValueError(
            "Ola official careers page no longer publishes exactly one TurboHire career-page link"
        )
    careerpage_url, link_labels = next(iter(labels.items()))
    parts = urlsplit(careerpage_url)
    match = CAREERPAGE_PATH.match(parts.path)
    if parts.scheme != "https" or parts.netloc != "olacareers.turbohire.co" or not match:
        raise ValueError("Ola TurboHire link is not the inspected single-tenant career-page route")
    if match.group(1).lower() != CAREERPAGE_ORG_ID:
        raise ValueError("Ola TurboHire career-page organization id changed")
    if not any("opening" in label.lower() for label in link_labels):
        raise ValueError("Ola TurboHire link is no longer labelled as the openings journey")
    return {
        "url": response_url,
        "title": title,
        "careerpage_url": careerpage_url,
        "careerpage_org_id": match.group(1).lower(),
        "link_labels": sorted(set(link_labels)),
    }


def parse_careerpage_shell(html: str, response_url: str = CAREERPAGE_URL,
                           org_id: str = CAREERPAGE_ORG_ID) -> dict:
    parsed = urlsplit(response_url)
    match = CAREERPAGE_PATH.match(parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "olacareers.turbohire.co" or not match:
        raise ValueError("Ola hosted career page is not the inspected TurboHire org route")
    if match.group(1).lower() != org_id.lower():
        raise ValueError("Ola hosted career page organization id changed")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ")) if soup.title else ""
    bundles = [
        script.get("src") for script in soup.select("script[src]")
        if re.search(r"/main\.[0-9a-f]+\.chunk\.js$", script.get("src") or "")
    ]
    if soup.select_one("#root") is None or not bundles:
        raise ValueError("Ola TurboHire career page no longer serves the inspected SPA shell")
    return {
        "url": response_url,
        "org_id": org_id.lower(),
        "title": title,
        "root_present": True,
        "main_bundles": bundles,
    }


def parse_token(payload) -> str:
    if not isinstance(payload, dict):
        raise ValueError("Ola TurboHire token response is not an object")
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("Ola TurboHire token response has no access_token")
    return token


def parse_public_org(payload, org_id: str = CAREERPAGE_ORG_ID) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Ola public organization response is not an object")
    if payload.get("OrgID") != org_id:
        raise ValueError("Ola public organization id changed")
    if normalized_space(payload.get("OrgName")) != ORG_NAME:
        raise ValueError("Ola public organization no longer identifies ANI Technologies Pvt Ltd")
    if payload.get("IsCareerPagePublished") is not True:
        raise ValueError("Ola public career page is no longer published")
    if normalized_space(payload.get("CareerPageSubdomain")) != ACCOUNT_NAME:
        raise ValueError("Ola public career-page subdomain changed")
    pages = payload.get("CareerPages")
    if not isinstance(pages, list):
        raise ValueError("Ola public organization has no CareerPages list")
    career_pages = [page for page in pages
                    if isinstance(page, dict) and page.get("Type") == CAREER_PAGE_TYPE]
    if len(career_pages) != 1:
        raise ValueError("Ola public organization no longer exposes exactly one CAREER_PAGE")
    page = career_pages[0]
    domains = payload.get("OrgVerifiedDomains") or []
    if "@olacabs.com" not in domains:
        raise ValueError("Ola public organization no longer verifies the @olacabs.com domain")
    return {
        "org_id": payload.get("OrgID"),
        "org_name": normalized_space(payload.get("OrgName")),
        "career_page_subdomain": normalized_space(payload.get("CareerPageSubdomain")),
        "career_page_types": [item.get("Type") for item in pages if isinstance(item, dict)],
        "career_page_banner_text": normalized_space(page.get("BannerText")),
        "career_page_banner_subtext": normalized_space(page.get("BannerSubText")),
        "verified_domains": domains,
        "org_url": payload.get("OrgURL"),
        "is_career_page_published": True,
        "created_date": payload.get("CreatedDate"),
        "updated_date": payload.get("UpdatedDate"),
    }


def parse_sso(payload, org_id: str = CAREERPAGE_ORG_ID) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Ola TurboHire SSO response is not an object")
    if payload.get("OrgId") != org_id or normalized_space(payload.get("OrgName")) != ORG_NAME:
        raise ValueError("Ola TurboHire account name no longer resolves to ANI Technologies Pvt Ltd")
    return {
        "org_id": payload.get("OrgId"),
        "org_name": normalized_space(payload.get("OrgName")),
        "sso_provider": payload.get("SSOProvider"),
        "is_sso_enabled": payload.get("IsSSOEnabled"),
    }


def parse_departments(payload) -> dict:
    """The career page's departments-with-active-jobs feed."""
    if not isinstance(payload, list):
        raise ValueError("Ola active departments response is not a list")
    rows = []
    for row in payload:
        if not isinstance(row, dict) or not normalized_space(row.get("Name")):
            raise ValueError("Ola active departments response row is missing Name")
        if not isinstance(row.get("JobsCount"), int) or row["JobsCount"] < 0:
            raise ValueError("Ola active departments response row has no integer JobsCount")
        rows.append({
            "id": row.get("Id"),
            "name": normalized_space(row.get("Name")),
            "jobs_count": row["JobsCount"],
        })
    return {"departments": rows, "reported_total": sum(row["jobs_count"] for row in rows)}


def parse_configured_departments(payload) -> dict:
    """The organization's configured department list (not live postings)."""
    if not isinstance(payload, list):
        raise ValueError("Ola configured departments response is not a list")
    names = []
    for row in payload:
        if not isinstance(row, dict) or not normalized_space(row.get("Name")):
            raise ValueError("Ola configured departments response row is missing Name")
        names.append(normalized_space(row.get("Name")))
    return {"count": len(names), "names": names}


def parse_clientele(payload) -> dict:
    if not isinstance(payload, list):
        raise ValueError("Ola clientele response is not a list")
    clients = []
    for row in payload:
        if not isinstance(row, dict) or not normalized_space(row.get("ClientName")):
            raise ValueError("Ola clientele response row is missing ClientName")
        clients.append({
            "client_name": normalized_space(row.get("ClientName")),
            "client_id": row.get("Id"),
            "client_url": row.get("ClientUrl"),
        })
    return {"clients": clients, "count": len(clients)}


def parse_listing(payload) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Ola career-page listing response is not an object")
    total, rows = payload.get("Total"), payload.get("Result")
    if not isinstance(total, int) or total < 0 or not isinstance(rows, list):
        raise ValueError("Ola career-page listing Total/Result have unexpected types")
    if total != len(rows):
        raise ValueError(
            f"Ola career-page listing pagination shape changed: Total {total} != Result rows {len(rows)}"
        )
    codes, ids, tokens = [], [], []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Ola career-page listing row is not an object")
        for key in ("JobCode", "JobId", "JobIdObfuscated", "JobTitle"):
            if not normalized_space(str(row.get(key) or "")):
                raise ValueError(f"Ola career-page listing row is missing {key}")
        code = normalized_space(str(row["JobCode"]))
        job_id = normalized_space(str(row["JobId"]))
        token = str(row["JobIdObfuscated"])
        codes.append(code)
        ids.append(job_id)
        tokens.append(token)
    if len(set(codes)) != len(codes):
        raise ValueError("Ola career-page listing contains duplicate JobCode values")
    if len(set(ids)) != len(ids):
        raise ValueError("Ola career-page listing contains duplicate JobId values")
    if len(set(tokens)) != len(tokens):
        raise ValueError("Ola career-page listing contains duplicate JobIdObfuscated values")
    return {"total": total, "rows": rows, "codes": codes, "job_ids": ids, "tokens": tokens}


def parse_detail(payload, listing_row: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Ola job detail response is not an object")
    for key in ("JobCode", "JobId", "JobTitle"):
        if normalized_space(str(payload.get(key) or "")) != normalized_space(str(listing_row.get(key) or "")):
            raise ValueError(f"Ola job detail {key} does not match its advertised listing row")
    description = payload.get("JobDescriptionV2") or payload.get("JobDescription")
    if not isinstance(description, str):
        raise ValueError("Ola job detail has no public JobDescriptionV2/JobDescription field")
    return payload


def description_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(f'<div data-ola-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-ola-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    sections, current = [], None
    for block in root.contents if root else []:
        if not isinstance(block, Tag):
            continue
        heading = ""
        if block.name in heading_tags:
            heading = normalized_space(block.get_text(" "))
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized_space(block.get_text(" "))
            strong_text = normalized_space(strong.get_text(" ")) if strong else ""
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
    elif not sections:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def parse_locations(value) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    locations = []
    for item in value:
        address = item.get("Address") if isinstance(item, dict) else item
        if isinstance(address, str) and address.strip() and address.strip() not in locations:
            locations.append(address.strip())
    return locations


def parse_experience(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    experience = {}
    if isinstance(value.get("MinExp"), (int, float)):
        experience["minimum_years"] = value["MinExp"]
    if isinstance(value.get("MaxExp"), (int, float)):
        experience["maximum_years"] = value["MaxExp"]
    return experience or None


def expiry_value(payload: dict) -> str | None:
    expiry = payload.get("ExpiryDates")
    if isinstance(expiry, dict):
        value = expiry.get("CAREERPAGE")
        return str(value) if value else None
    if isinstance(expiry, str) and expiry.strip():
        return expiry.strip()
    return None


def job_url(obfuscated: str) -> str:
    return TURBOHIRE_ORIGIN + "/job/publicjobs/" + quote(obfuscated, safe="%")


def detail_api_url(obfuscated: str) -> str:
    return DETAIL_URL + "?tkn=" + quote(obfuscated, safe="%") + "&fieldVisibility=CareerPage"


def listing_api_url(org_id: str = CAREERPAGE_ORG_ID) -> str:
    return LISTING_URL + f"?orgId={org_id}&pageType={LISTING_PAGE_TYPE}"


def departments_api_url(org_id: str = CAREERPAGE_ORG_ID) -> str:
    return DEPARTMENTS_URL + f"?orgId={org_id}&pageType={LISTING_PAGE_TYPE}"


def config_departments_api_url(org_id: str = CAREERPAGE_ORG_ID) -> str:
    return CONFIG_DEPARTMENTS_URL + f"?orgId={org_id}"


def clientele_api_url(org_id: str = CAREERPAGE_ORG_ID) -> str:
    return CLIENTELE_URL + f"?orgId={org_id}"


def _response_record(url: str, method: str, response, seconds: float, body=None) -> dict:
    return {
        "url": url,
        "method": method,
        "status": response.status_code,
        "final_url": str(response.url),
        "bytes": len(response.content),
        "sha256": sha256(response.content).hexdigest(),
        "seconds": round(seconds, 6),
        "request_body": body,
        "redirects": [str(item.url) for item in response.history],
    }


def build_listing_job(adapter: Adapter, row: dict, org_name: str):
    url = job_url(row["JobIdObfuscated"])
    job = adapter.job(
        {
            "listing_row": row,
            "job_code": row["JobCode"],
            "native_job_id": row.get("JobId"),
            "job_token": row["JobIdObfuscated"],
        },
        title=normalized_space(row.get("JobTitle")),
        url=url,
        job_id=normalized_space(str(row["JobCode"])),
        requisition_id=None,
        apply_url=url,
        locations=parse_locations(row.get("Location")),
        department=row.get("Department"),
        employment_type=row.get("JobTypeV2") or row.get("Type"),
        date_posted=row.get("PublishedDate"),
        date_posted_raw={
            "listing_PublishedDate": row.get("PublishedDate"),
            "listing_PublishedDates": row.get("PublishedDates"),
            "listing_ExpiryDates": row.get("ExpiryDates"),
        },
        date_updated=row.get("UpdatedDate"),
        valid_through=expiry_value(row),
        experience=parse_experience(row.get("Experience")),
        employer=org_name,
        description="",
        method=None,
        description_url=None,
    )
    job.raw["detail_status"] = "pending"
    return job


def build_detail_job(adapter: Adapter, listing_job, detail: dict, org_name: str,
                     response_sha256: str | None):
    description_html = detail.get("JobDescriptionV2") or detail.get("JobDescription") or ""
    inline_text = text_html(description_html)
    publisher_empty = len(inline_text.strip()) < 100
    sections = description_sections(description_html)
    responsibilities = [
        section for section in sections
        if section["heading"] and any(token in section["heading"].lower()
                                      for token in ("responsibilit", "what you will do", "what you'll do"))
    ]
    qualifications = [
        section for section in sections
        if section["heading"] and any(token in section["heading"].lower()
                                       for token in ("qualif", "requirement", "what you will need",
                                                     "what you'll need", "who you are", "skills"))
    ]
    org_details = detail.get("OrgDetails") or {}
    employer = normalized_space(org_details.get("OrgName")) or org_name
    attachments = [detail[key] for key in ATTACHMENT_KEYS
                   if isinstance(detail.get(key), str) and detail[key].strip()]
    warnings = []
    if publisher_empty:
        warnings.append("publisher_empty_or_placeholder_description")
    if attachments:
        warnings.append("role_attachment_requires_review")
    job = adapter.job(
        {
            "listing_row": listing_job.raw.get("listing_row", {}),
            "listing": listing_job.raw,
            "detail": detail,
            "detail_response_sha256": response_sha256,
            "job_code": detail.get("JobCode"),
            "native_job_id": detail.get("JobId"),
            "requisition_id": detail.get("RequisitionId"),
            "job_status": detail.get("JobStatus"),
            "published_job_boards": detail.get("PublishedJobBoards"),
            "organisation_name": org_details.get("OrgName"),
            "client_name": detail.get("ClientName"),
            "client_company": detail.get("ClientCompany"),
            "show_client_name": detail.get("ShowClientName"),
            "number_of_vacancy": detail.get("NumberOfVacancy"),
            "skills": detail.get("Skills"),
            "additional_fields": detail.get("AdditionalFields"),
            "role_attachment_urls": attachments,
            "description_sections": sections,
            "application": {
                "route": "Exact public TurboHire job page; sign-in required to submit an application.",
                "public_apply_url_published": False,
            },
        },
        title=normalized_space(detail.get("JobTitle")),
        url=listing_job.url,
        job_id=normalized_space(str(detail.get("JobCode") or listing_job.job_id)),
        requisition_id=detail.get("RequisitionId") or None,
        apply_url=listing_job.url,
        locations=parse_locations(detail.get("Location")) or listing_job.locations,
        department=detail.get("Department") or listing_job.department,
        employment_type=detail.get("JobTypeV2") or detail.get("Type") or listing_job.employment_type,
        date_posted=detail.get("PublishedDate") or listing_job.date_posted,
        date_posted_raw={
            **(listing_job.date_posted_raw or {}),
            "detail_PublishedDate": detail.get("PublishedDate"),
            "detail_PublishedDates": detail.get("PublishedDates"),
            "detail_ExpiryDates": detail.get("ExpiryDates"),
        },
        date_updated=detail.get("UpdatedDate") or listing_job.date_updated,
        valid_through=expiry_value(detail) or listing_job.valid_through,
        salary=detail.get("Compensation") or None,
        experience=parse_experience(detail.get("Experience")) or listing_job.experience,
        employer=employer,
        language="en",
        responsibilities=responsibilities or None,
        qualifications=qualifications or None,
        description=description_html,
        description_text=inline_text,
        method="careerpage_detail_api:JobDescriptionV2" if detail.get("JobDescriptionV2")
        else "careerpage_detail_api:JobDescription",
        description_url=detail_api_url(detail["JobIdObfuscated"]),
        description_unavailable=publisher_empty,
        warnings=warnings,
    )
    return job


class Ola(Adapter):
    VERIFICATION_REVISION = "ola.official-turbohire-zero-vacancy.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.result.metadata["journey"] = []
        self.result.metadata["failed_sources"] = []
        self._token = None
        self._org_name = ORG_NAME

    def _failed_source(self, url: str, reason: str):
        self.result.metadata.setdefault("failed_sources", []).append({"url": url, "reason": reason})

    async def _fetch(self, url: str, *, method: str = "GET", headers: dict | None = None,
                     body: dict | None = None):
        started = time.perf_counter()
        try:
            response = await self.http.request(method, url, headers=headers, json=body)
            seconds = max(0.0, time.perf_counter() - started)
            record = _response_record(url, method, response, seconds, body)
            self.result.metadata["journey"].append(record)
            return response
        except FetchError as exc:
            seconds = max(0.0, time.perf_counter() - started)
            self.result.metadata["journey"].append({
                "url": url, "method": method, "status": None, "error": str(exc),
                "seconds": round(seconds, 6), "request_body": body,
            })
            self._failed_source(url, str(exc))
            return None

    def _auth_headers(self) -> dict:
        return api_headers(self._token)

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": listing_api_url(),
            "careerpage_url": CAREERPAGE_URL,
            "dashboard_url": DASHBOARD_URL,
            "legal_employer": ORG_NAME,
            "listing_authority": (
                "Official https://www.olacabs.com/careers -> its single published "
                "'See our Openings' link -> the ANI Technologies TurboHire career page; "
                "the page's own unfiltered careerpagev2/filteredjobs request is the membership authority."
            ),
            "listing_request": {
                "method": "POST",
                "url": listing_api_url(),
                "query_shape": {"orgId": CAREERPAGE_ORG_ID, "pageType": LISTING_PAGE_TYPE},
                "body": LISTING_REQUEST_BODY,
                "filters_applied": False,
                "pagination": "One POST returns Total and every Result row; Total must equal Result length.",
                "scope": "Only the exact TurboHire tenant/org linked by Ola; no other tenant or guessed ATS slug.",
            },
            "detail_method": (
                "GET /api/referraljobs?tkn=<JobIdObfuscated>&fieldVisibility=CareerPage for actual "
                "listing rows only. The current company-scoped listing has no rows, so no detail request "
                "is made and no posting/description is invented."
            ),
            "request_scope": (
                "Only https://www.olacabs.com/careers, https://olacareers.turbohire.co and "
                "https://thapi.azurewebsites.net requests exposed by that published journey."
            ),
        })
        if self.company.careers_url != OFFICIAL_ENTRY:
            raise ValueError("Ola dedicated flow must start from its official HTTPS careers entry")

        entry_response = await self._fetch(OFFICIAL_ENTRY)
        if entry_response is None:
            self.result.errors.append("ola_official_entry_unavailable")
            self.result.listing_complete = False
            return self.result
        try:
            entry = parse_official_entry(entry_response.text, str(entry_response.url))
        except ValueError as exc:
            self.result.errors.append(f"ola_official_entry_changed: {exc}")
            self.result.listing_complete = False
            return self.result
        self.result.metadata["official_entry"] = entry

        shell_response = await self._fetch(entry["careerpage_url"])
        if shell_response is None:
            self.result.errors.append("ola_careerpage_shell_unavailable")
            self.result.listing_complete = False
            return self.result
        try:
            shell = parse_careerpage_shell(shell_response.text, str(shell_response.url), entry["careerpage_org_id"])
        except ValueError as exc:
            self.result.errors.append(f"ola_careerpage_shell_changed: {exc}")
            self.result.listing_complete = False
            return self.result
        self.result.metadata["careerpage_shell"] = shell

        token_response = await self._fetch(TOKEN_URL, headers=api_headers())
        if token_response is None:
            self.result.errors.append("ola_turbohire_token_unavailable")
            self.result.listing_complete = False
            return self.result
        try:
            self._token = parse_token(token_response.json())
        except (ValueError, json.JSONDecodeError) as exc:
            self.result.errors.append(f"ola_turbohire_token_changed: {exc}")
            self.result.listing_complete = False
            return self.result

        public_org_response = await self._fetch(PUBLIC_ORG_URL, headers=self._auth_headers())
        if public_org_response is None:
            self.result.errors.append("ola_public_organization_unavailable")
            self.result.listing_complete = False
            return self.result
        try:
            public_org = parse_public_org(public_org_response.json(), entry["careerpage_org_id"])
        except (ValueError, json.JSONDecodeError) as exc:
            self.result.errors.append(f"ola_public_organization_changed: {exc}")
            self.result.listing_complete = False
            return self.result
        self.result.metadata["public_org"] = public_org
        self._org_name = public_org["org_name"]

        sso_response = await self._fetch(SSO_URL, headers=self._auth_headers())
        if sso_response is None:
            self.result.warnings.append("ola_account_name_check_unavailable")
            self._failed_source(SSO_URL, "SSO/account-name check unavailable")
        else:
            try:
                self.result.metadata["sso"] = parse_sso(sso_response.json(), entry["careerpage_org_id"])
            except (ValueError, json.JSONDecodeError) as exc:
                self.result.errors.append(f"ola_account_name_changed: {exc}")

        departments_response = await self._fetch(departments_api_url(entry["careerpage_org_id"]),
                                                 headers=self._auth_headers())
        active_departments = None
        if departments_response is None:
            self.result.warnings.append("ola_active_departments_unavailable")
        else:
            try:
                active_departments = parse_departments(departments_response.json())
                self.result.metadata["active_departments"] = active_departments
            except (ValueError, json.JSONDecodeError) as exc:
                self.result.errors.append(f"ola_active_departments_changed: {exc}")

        listing_response = await self._fetch(listing_api_url(entry["careerpage_org_id"]),
                                             method="POST", headers=self._auth_headers(),
                                             body=LISTING_REQUEST_BODY)
        if listing_response is None:
            self.result.errors.append("ola_listing_unavailable")
            self.result.listing_complete = False
            return self.result
        try:
            listing = parse_listing(listing_response.json())
        except (ValueError, json.JSONDecodeError) as exc:
            self.result.errors.append(f"ola_listing_changed: {exc}")
            self.result.listing_complete = False
            return self.result
        self.result.expected_total = listing["total"]
        self.result.pages = 1
        self.result.metadata["initial_listing"] = {
            "url": listing_api_url(entry["careerpage_org_id"]),
            "total": listing["total"],
            "codes": listing["codes"],
            "native_job_ids": listing["job_ids"],
            "rows": len(listing["rows"]),
        }
        if active_departments is not None and active_departments["reported_total"] != listing["total"]:
            self.result.warnings.append(
                f"ola_department_jobs_count_sum_{active_departments['reported_total']}"
                f"_vs_listing_{listing['total']}"
            )

        config_response = await self._fetch(config_departments_api_url(entry["careerpage_org_id"]),
                                           headers=self._auth_headers())
        if config_response is None:
            self.result.warnings.append("ola_configured_departments_unavailable")
        else:
            try:
                configured = parse_configured_departments(config_response.json())
                self.result.metadata["configured_departments"] = {
                    "count": configured["count"],
                    "note": "Organization configuration only; not active job postings.",
                }
            except (ValueError, json.JSONDecodeError) as exc:
                self.result.warnings.append(f"ola_configured_departments_changed: {exc}")

        clientele_response = await self._fetch(clientele_api_url(entry["careerpage_org_id"]),
                                               headers=self._auth_headers())
        if clientele_response is None:
            self.result.warnings.append("ola_clientele_unavailable")
        else:
            try:
                self.result.metadata["clientele"] = parse_clientele(clientele_response.json())
            except (ValueError, json.JSONDecodeError) as exc:
                self.result.warnings.append(f"ola_clientele_changed: {exc}")

        if listing["rows"]:
            listing_jobs = [build_listing_job(self, row, self._org_name) for row in listing["rows"]]
            for job in listing_jobs:
                self.add(job)

            async def detail(job):
                row = job.raw["listing_row"]
                url = detail_api_url(row["JobIdObfuscated"])
                response = await self._fetch(url, headers=self._auth_headers())
                if response is None:
                    raise ValueError(f"Ola detail request failed for {job.job_id}")
                payload = parse_detail(response.json(), row)
                return build_detail_job(self, job, payload, self._org_name,
                                        sha256(response.content).hexdigest())

            self.result.jobs = await self.hydrate(self.result.jobs, detail)
        else:
            self.result.jobs = []
            self.result.timings["detail_phase_seconds"] = 0.0

        final_entry_response = await self._fetch(OFFICIAL_ENTRY)
        final_shell_response = await self._fetch(entry["careerpage_url"])
        final_org_response = await self._fetch(PUBLIC_ORG_URL, headers=self._auth_headers())
        final_departments_response = await self._fetch(departments_api_url(entry["careerpage_org_id"]),
                                                       headers=self._auth_headers())
        final_listing_response = await self._fetch(listing_api_url(entry["careerpage_org_id"]),
                                                   method="POST", headers=self._auth_headers(),
                                                   body=LISTING_REQUEST_BODY)
        final_listing = None
        if final_entry_response is not None:
            try:
                final_entry = parse_official_entry(final_entry_response.text, str(final_entry_response.url))
            except ValueError as exc:
                self.result.errors.append(f"ola_final_official_entry_changed: {exc}")
                final_entry = None
        else:
            final_entry = None
        if final_shell_response is not None:
            try:
                final_shell = parse_careerpage_shell(final_shell_response.text,
                                                     str(final_shell_response.url),
                                                     entry["careerpage_org_id"])
            except ValueError as exc:
                self.result.errors.append(f"ola_final_careerpage_shell_changed: {exc}")
                final_shell = None
        else:
            final_shell = None
        final_org = None
        if final_org_response is not None:
            try:
                final_org = parse_public_org(final_org_response.json(), entry["careerpage_org_id"])
            except (ValueError, json.JSONDecodeError) as exc:
                self.result.errors.append(f"ola_final_public_organization_changed: {exc}")
        final_active_departments = None
        if final_departments_response is not None:
            try:
                final_active_departments = parse_departments(final_departments_response.json())
            except (ValueError, json.JSONDecodeError) as exc:
                self.result.errors.append(f"ola_final_active_departments_changed: {exc}")
        if final_listing_response is not None:
            try:
                final_listing = parse_listing(final_listing_response.json())
            except (ValueError, json.JSONDecodeError) as exc:
                self.result.errors.append(f"ola_final_listing_changed: {exc}")

        if final_listing is None:
            self.result.listing_complete = False
        else:
            initial_ids = listing["codes"]
            final_ids = final_listing["codes"]
            initial_total = listing["total"]
            final_total = final_listing["total"]
            same_ids = initial_ids == final_ids
            same_total = initial_total == final_total
            same_entry = (final_entry is not None
                          and final_entry["careerpage_url"] == entry["careerpage_url"]
                          and final_entry["careerpage_org_id"] == entry["careerpage_org_id"])
            same_shell = final_shell is not None and final_shell["org_id"] == shell["org_id"]
            same_org = (final_org is not None
                        and final_org["org_id"] == public_org["org_id"]
                        and final_org["org_name"] == public_org["org_name"]
                        and final_org["career_page_types"] == public_org["career_page_types"])
            if active_departments is not None and final_active_departments is not None:
                same_departments = final_active_departments == active_departments
            elif active_departments is None and final_active_departments is None:
                same_departments = True
                self.result.warnings.append("ola_active_departments_unavailable_both_snapshots")
            else:
                same_departments = False
            collected_ids = [job.job_id for job in self.result.jobs]
            zero_vacancy = (not initial_ids and not final_ids
                            and initial_total == 0 and final_total == 0)
            matches = bool(same_ids and same_total and collected_ids == initial_ids
                           and same_entry and same_shell and same_org)
            if not matches:
                self.result.errors.append("ola_listing_membership_changed_during_collection")
            if not same_departments:
                self.result.errors.append("ola_active_departments_changed_during_collection")
            if not same_org:
                self.result.errors.append("ola_public_organization_changed_during_collection")
            if not same_entry or not same_shell:
                self.result.errors.append("ola_official_journey_changed_during_collection")
            self.result.listing_complete = bool(matches and same_departments and not self.result.errors)
            self.result.metadata["final_listing"] = {
                "total": final_total,
                "codes": final_ids,
            }
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial_ids,
                "final_ids": final_ids,
                "collected_ids": collected_ids,
                "initial_total": initial_total,
                "final_total": final_total,
                "initial_final_id_order_matches": same_ids,
                "initial_final_total_matches": same_total,
                "official_entry_still_exact": same_entry,
                "careerpage_shell_still_exact": same_shell,
                "public_org_still_exact": same_org,
                "active_departments_still_exact": same_departments,
                "zero_vacancy": zero_vacancy,
                "complete": self.result.listing_complete,
            }
            self.result.metadata["zero_vacancy"] = {
                "confirmed": zero_vacancy and self.result.listing_complete,
                "listing_api_returned_zero_total": initial_total == 0 and final_total == 0,
                "listing_calls": 2,
                "rendered_notice": "Sorry, Currently there are no vacancies.",
                "dashboard_notice": "No Jobs listed",
                "active_departments": active_departments,
                "configured_departments": self.result.metadata.get("configured_departments", {}),
                "note": (
                    "The empty result is an authoritative HTTP 200 response from the exact "
                    "company-published TurboHire career page, not an inaccessible or error response. "
                    "No detail page exists because there is no posting."
                ),
            }

        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        self.result.timings["adapter_total_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result
