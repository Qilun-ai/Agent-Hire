"""Hella Infra Market's company-published HROne career-portal journey.

Observed on 2026-11-07 (see ``companies/hella-infra-market-private-limited/
journey-research.md``):

- The official entry ``https://infra.market/careers/`` publishes one
  "Explore Opportunities" anchor to ``https://hr1.to//56f9dd``.
- That short link redirects to ``https://career.hrone.cloud/career-portal`` with
  the portal's ``appId``, ``dc=infra``, ``rqt`` and ``cc`` routing values.
- The rendered portal shows "CURRENT VACANCIES" with preferred-location and
  experience controls, then "CURRENTLY NO VACANCIES". Its unfiltered listing
  request is ``POST .../api/external/public/referral/CareerPosition/Details``
  with empty filters, ``jobPosted="0"`` and page size 15.
- On the observation date that exact request returned ``[]`` for every
  freshness value tested (0/1/2/5/10/30/31). There were no postings to detail.

The adapter follows only the published official-entry link and the exact
company-scoped HROne portal it reaches. It never guesses an ATS slug, queries
another tenant, or treats an unknown/error response as an empty board.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..attachments import extract_attachment
from ..http import HTTP
from ..models import now, text_html


OFFICIAL_ENTRY = "https://infra.market/careers/"
SHORTLINK_URL = "https://hr1.to//56f9dd"
SHORTLINK_HOST = "hr1.to"
SHORTLINK_PATH = "//56f9dd"
PORTAL_HOST = "career.hrone.cloud"
PORTAL_PATH = "/career-portal"
API_ORIGIN = "https://gateway.app.hrone.cloud"
API = API_ORIGIN + "/api/external/public"
LISTING_URL = API + "/referral/CareerPosition/Details"
PORTAL_DETAILS_URL = API + "/referral/CareerPortal/{rqt}/{cc}"
DETAIL_URL = API + "/referral/JobOpening/Request/Details/{encrypted_position_id}/{cc}/{source_type}"
FILE_URL = API + "/access/FileVirtualUrl"
PAGE_SIZE = 15
PORTAL_NAME = "Infra Market"
LEGAL_NAME = "Hella Infra Market Private Limited"
PORTAL_COMPANY_CODE = "infra"
REQUEST_QUERY_KEYS = {"appId", "dc", "rqt", "cc"}
TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")

# The exact public job fields returned by HROne's detail endpoint. Internal
# staff/admin objects are deliberately not retained.
JOB_FIELDS = frozenset(
    "employeeType seniorityName functionName currencyName designation company "
    "businessUnit department subDepartment region branch subBranch currentStatus "
    "isClosedForAll canAddCandidate qualificationList preferredLocationList requestId "
    "jobCode jobTitle jobType noOfOpening jobDescriptionBody jobDescriptionBodyWithHtml "
    "documentFileName uploadedFileName skillsName experienceFrom experienceTo "
    "qualificationCode salaryFrom salaryTo currencyCode companyCode businessUnitCode "
    "regionCode branchCode subBranchCode departmentCode subDepartmentCode "
    "designationCode preferredLocations projectName preferredLocationOptionName "
    "lastApplicationDate applicantGuidelines applicantGuidelinesBodyWithHtml "
    "jobFieldOne jobFieldTwo jobFieldThree jobFieldFour jobFieldFive jobFieldSix "
    "jobFieldSeven jobFieldEight jobFieldNine jobFieldTen".split()
)
LISTING_FILTERS = (
    "departmentCode", "companyCode", "careerPortalType", "jobTitle",
    "employmentType", "seniorityName", "jobFunction", "company",
    "businessUnitCode", "department", "subDepartment", "gradeCode",
    "designationCode", "levelCode", "branchCode", "subBranchCode",
    "regionCode", "locationId", "experience", "qualification", "skillsName",
    "urgentOpening", "jobPosted", "isShortUrl", "pagination",
    "nationality", "preferredLocationId",
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _portal_details_filter(payload: dict) -> dict:
    return {
        "companyName": payload.get("companyName"),
        "companyCode": payload.get("companyCode"),
        "isActive": payload.get("isActive"),
        "careerPortalId": payload.get("careerPortalId"),
        "careerPortalDetailId": payload.get("careerPortalDetailId"),
        "headerColor": payload.get("headerColor"),
        "fontStyle": payload.get("fontStyle"),
        "buttonColor": payload.get("buttonColor"),
    }


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "infra.market"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Hella Infra Market official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != "Careers - Infra.Market":
        raise ValueError("Hella Infra Market official careers page title changed")
    links = []
    for anchor in soup.select("a[href]"):
        target = urljoin(response_url, anchor.get("href", ""))
        parts = urlsplit(target)
        if parts.hostname == SHORTLINK_HOST:
            links.append({
                "label": normalized_title(anchor.get_text(" ", strip=True)),
                "url": target,
                "path": parts.path,
            })
    if len(links) != 1:
        raise ValueError("Hella Infra Market official careers page no longer publishes exactly one hr1.to opportunity link")
    link = links[0]
    if link["label"] != "Explore Opportunities" or link["path"] not in {SHORTLINK_PATH, "/56f9dd"}:
        raise ValueError("Hella Infra Market published opportunity link or label changed")
    if soup.select_one("iframe") is not None:
        raise ValueError("Hella Infra Market official careers page gained an unexpected embedded job listing")
    return {"official_entry_url": response_url, "title": title, "link": link}


def parse_portal_redirect(final_url: str) -> dict:
    parsed = urlsplit(final_url)
    if parsed.scheme != "https" or parsed.netloc != PORTAL_HOST or parsed.path != PORTAL_PATH:
        raise ValueError("Hella Infra Market short link redirected outside its inspected HROne career portal")
    query = parse_qs(parsed.query)
    if set(query) != REQUEST_QUERY_KEYS:
        raise ValueError("Hella Infra Market HROne portal query shape changed")
    values = {}
    for key in REQUEST_QUERY_KEYS:
        if len(query[key]) != 1 or not TOKEN.fullmatch(query[key][0]):
            raise ValueError(f"Hella Infra Market HROne portal query value is invalid: {key}")
        values[key] = query[key][0]
    if values["dc"] != "infra":
        raise ValueError("Hella Infra Market HROne portal domainCode is not infra")
    return {
        "portal_url": final_url,
        "appId": values["appId"],
        "domainCode": values["dc"],
        "requestType": values["rqt"],
        "companyCode": values["cc"],
        "query": values,
    }


def parse_portal_details(payload, *, expected_company_code: str = PORTAL_COMPANY_CODE) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Hella Infra Market HROne portal branding response is not an object")
    details = payload.get("portalDetails")
    if not isinstance(details, list) or len(details) != 1 or not isinstance(details[0], dict):
        raise ValueError("Hella Infra Market HROne portal branding is missing or ambiguous")
    company = details[0]
    name = normalized_title(company.get("companyName"))
    if ("infra" not in name.lower() or "market" not in name.lower()
            or company.get("companyCode") != expected_company_code
            or company.get("isActive") is not True):
        raise ValueError("Hella Infra Market HROne portal branding does not identify its inspected company portal")
    return _portal_details_filter(company)


def parse_listing_rows(payload, page: int = 1, page_size: int = PAGE_SIZE) -> dict:
    if not isinstance(payload, list):
        if isinstance(payload, dict) and payload.get("validationType") not in (None, "0", 0):
            raise ValueError(
                "Hella Infra Market listing returned a validation error: "
                + str(payload.get("message") or payload.get("validationType"))
            )
        raise ValueError("Hella Infra Market listing response is not a JSON array")
    if len(payload) > page_size:
        raise ValueError("Hella Infra Market listing ignored its observed page size")
    rows, ids = [], []
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("Hella Infra Market listing contains a non-object row")
        job_id = normalized_title(row.get("jobCode"))
        title = normalized_title(row.get("jobTitle"))
        encrypted = str(row.get("encryptedPositionId") or "")
        source_type = str(row.get("sourceType") or "")
        if not job_id or not title or not encrypted or not source_type:
            raise ValueError("Hella Infra Market listing row is missing its published job identity fields")
        if job_id in ids:
            raise ValueError(f"Hella Infra Market listing repeats a job code on one page: {job_id}")
        ids.append(job_id)
        rows.append(row)
    return {"rows": rows, "job_ids": ids, "count": len(rows), "page": page,
            "page_size": page_size, "has_more": len(rows) == page_size}


def job_url_from_row(row: dict, portal_url: str, style: dict) -> str:
    encrypted = str(row.get("encryptedPositionId") or "")
    if not encrypted:
        raise ValueError("Hella Infra Market listing row has no encrypted position id")
    supplied = row.get("jobApplicationPath")
    if supplied:
        target = urljoin(portal_url, str(supplied))
    else:
        parsed = urlsplit(portal_url)
        extra = {
            "pid": encrypted,
            "dptc": row.get("departmentCode") or "",
            "st": row.get("sourceType") or "",
            "fm": "CR",
            "headerColor": style.get("headerColor") or "",
            "fontSize": str(style.get("fontStyle") or ""),
            "buttonColor": style.get("buttonColor") or "",
        }
        target = urlunsplit((
            parsed.scheme,
            parsed.netloc,
            "/apply-job",
            urlencode(parse_qsl(parsed.query) + list(extra.items())),
            "",
        ))
    parts = urlsplit(target)
    query = parse_qs(parts.query)
    if parts.scheme != "https" or parts.hostname != PORTAL_HOST or query.get("pid") != [encrypted]:
        raise ValueError("Hella Infra Market listing supplied an unexpected exact-job destination")
    return target


def parse_detail_payload(payload, row: dict, title: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Hella Infra Market job detail response is not an object")
    expected_code = normalized_title(row.get("jobCode"))
    detail_code = normalized_title(payload.get("jobCode"))
    detail_title = normalized_title(payload.get("jobTitle"))
    if detail_code != expected_code or detail_title != normalized_title(title):
        raise ValueError("Hella Infra Market detail job code/title does not match its advertised listing row")
    if row.get("positionId") is not None and str(payload.get("requestId")) != str(row.get("positionId")):
        raise ValueError("Hella Infra Market detail request id does not match its listing row")
    company = normalized_title(payload.get("company"))
    if not company or ("infra" not in company.lower() and "hella" not in company.lower()):
        raise ValueError("Hella Infra Market detail no longer identifies an Infra/Hella employer")
    body = payload.get("jobDescriptionBodyWithHtml") or ""
    if not body:
        raise ValueError("Hella Infra Market detail full rich-text description field is missing")
    filtered = {key: value for key, value in payload.items() if key in JOB_FIELDS}
    return {"data": filtered, "description_html": body, "employer": company}


class HellaInfraMarketPrivateLimited(Adapter):
    """Read only the official Infra.Market careers link and its HROne portal."""

    VERIFICATION_REVISION = "hella-infra-market.hrone-official-shortlink-zero-vacancy.1"

    def _evidence(self, url: str, data, *, method: str = "GET", request_body=None, scope: str):
        """Archive only the public job/branding fields used as evidence."""
        if not self.http.output:
            return
        content = json.dumps(data, ensure_ascii=False).encode()
        digest = sha256(content).hexdigest()
        target = self.http.output / "responses" / f"{digest}.gz"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            import gzip
            target.write_bytes(gzip.compress(content))
        with (self.http.output / "requests.jsonl").open("a") as handle:
            handle.write(json.dumps({
                "url": url,
                "final_url": url,
                "method": method,
                "request_body": request_body,
                "status": 200,
                "sha256": digest,
                "evidence_scope": scope,
                "fetched_at": now(),
            }, ensure_ascii=False) + "\n")

    def _headers(self, query: dict) -> dict:
        return {
            "domainCode": query["domainCode"],
            "apiKey": query["appId"],
            "AccessMode": "W",
            "X-Requested-With": "https://career.hrone.cloud",
        }

    async def _settings(self, api, query: dict) -> dict:
        url = PORTAL_DETAILS_URL.format(rqt=query["requestType"], cc=query["companyCode"])
        payload = await api.json(url, headers=self._headers(query))
        parsed = parse_portal_details(payload)
        self._evidence(url, parsed, scope="portal_branding_only")
        return parsed

    async def _listing(self, api, query: dict, page: int) -> dict:
        if not self.page_guard():
            raise ValueError("Hella Infra Market listing request budget exhausted")
        body = {key: "" for key in LISTING_FILTERS}
        body.update(
            companyCode=query["companyCode"],
            careerPortalType=query["requestType"],
            jobPosted="0",
            isShortUrl=False,
            pagination={"pageNumber": page, "pageSize": PAGE_SIZE},
        )
        rows = await api.json(LISTING_URL, method="POST", json=body, headers=self._headers(query))
        parsed = parse_listing_rows(rows, page=page)
        self._evidence(LISTING_URL, rows, method="POST", request_body=body,
                       scope="company_scoped_listing_rows")
        return parsed

    async def _attachment(self, api, query: dict, job) -> dict | None:
        data = job.raw["detail"]
        filename = data.get("documentFileName")
        uploaded = data.get("uploadedFileName")
        if not filename and not uploaded:
            return None
        if not filename or not uploaded:
            raise ValueError("Hella Infra Market published attachment metadata is incomplete")
        info = await api.json(
            FILE_URL,
            params={"fileDbName": uploaded, "requestSource": "null"},
            headers=self._headers(query),
        )
        file_url = info.get("fileVirtualPath") if isinstance(info, dict) else None
        parsed = urlsplit(file_url or "")
        if not file_url or parsed.hostname is None or not parsed.hostname.endswith(".blob.core.windows.net"):
            raise ValueError("Hella Infra Market attachment did not return its observed public document URL")
        response = await api.get(file_url)
        digest = sha256(response.content).hexdigest()
        if self.http.output:
            target = self.http.output / "attachments" / f"{job.job_id}-{digest[:16]}{Path(filename).suffix.lower()}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.content)
        started = time.perf_counter()
        extracted = await asyncio.to_thread(extract_attachment, response.content, filename)
        self.result.timings["attachment_extraction_seconds"] = (
            self.result.timings.get("attachment_extraction_seconds", 0.0)
            + max(0.0, time.perf_counter() - started)
        )
        return {
            "filename": filename,
            "sha256": digest,
            "source_url": FILE_URL,
            "uploaded_file_name": uploaded,
            **extracted,
        }

    async def _detail(self, api, query: dict, job) -> dict:
        row = job.raw["listing"]
        url = DETAIL_URL.format(
            encrypted_position_id=row["encryptedPositionId"],
            cc=query["companyCode"],
            source_type=row["sourceType"],
        )
        payload = await api.json(url, headers=self._headers(query))
        parsed = parse_detail_payload(payload, row, job.title)
        self._evidence(url, parsed["data"], scope="job_fields_only; internal staff objects omitted")
        job.raw["detail"] = parsed["data"]
        job.description_html = parsed["description_html"]
        job.description_text = text_html(job.description_html)
        job.description_method = "hella_infra_market.hrone_full_job_description_body"
        job.description_source_url = url
        job.employment_type = parsed["data"].get("employeeType")
        job.department = parsed["data"].get("department")
        job.qualifications = parsed["data"].get("qualificationList")
        job.valid_through = parsed["data"].get("lastApplicationDate")
        locations = []
        for item in parsed["data"].get("preferredLocationList") or []:
            if isinstance(item, dict) and item.get("text"):
                locations.append(str(item["text"]))
        if not locations and row.get("preferredLocation"):
            locations.append(str(row["preferredLocation"]))
        job.locations = locations
        from_value, to_value = parsed["data"].get("experienceFrom"), parsed["data"].get("experienceTo")
        if from_value is not None or to_value is not None:
            job.experience = {"minimum": from_value, "maximum": to_value, "unit": "years"}
        salary_from, salary_to = parsed["data"].get("salaryFrom"), parsed["data"].get("salaryTo")
        if salary_from is not None or salary_to is not None:
            job.salary = {
                "from": salary_from,
                "to": salary_to,
                "currency": parsed["data"].get("currencyCode") or parsed["data"].get("currencyName"),
            }
        job.employer = parsed["employer"] or LEGAL_NAME
        if parsed["data"].get("applicantGuidelinesBodyWithHtml"):
            guide = parsed["data"]["applicantGuidelinesBodyWithHtml"]
            job.description_html += "<section><h2>Applicant guidelines</h2>" + guide + "</section>"
            job.description_text += "\nApplicant guidelines\n" + text_html(guide)
        attachment = await self._attachment(api, query, job)
        if attachment:
            job.raw["attachment"] = attachment
            job.description_html += (
                "<section><h2>Attached job description</h2><pre>"
                + escape(attachment["text"]) + "</pre></section>"
            )
            job.description_text += "\nAttached job description\n" + attachment["text"]
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_authority": "Official Infra.Market careers page -> its single published hr1.to link -> company-branded HROne career portal.",
            "legal_employer": LEGAL_NAME,
            "portal_company_name": PORTAL_NAME,
            "listing_request": {
                "method": "POST",
                "url": LISTING_URL,
                "query_shape": None,
                "filters": "empty search/filter fields; jobPosted=0 (the live default)",
                "page_size": PAGE_SIZE,
                "scope": "The exact HROne portal reached by the company's published short link; no other tenant or ATS board is queried.",
            },
        })
        if self.company.careers_url != OFFICIAL_ENTRY:
            raise ValueError("Hella Infra Market dedicated flow must start from its official HTTPS careers entry")
        if not self.page_guard():
            return self.result
        entry_started = time.perf_counter()
        entry = await self.http.get(OFFICIAL_ENTRY)
        entry_seconds = max(0.0, time.perf_counter() - entry_started)
        entry_parts = urlsplit(str(entry.url))
        if entry_parts.scheme != "https" or entry_parts.netloc != "infra.market" or entry_parts.path.rstrip("/") != "/careers":
            raise ValueError("Hella Infra Market official careers entry redirected off its inspected URL")
        parsed_entry = parse_official_entry(entry.text, str(entry.url))
        link = parsed_entry["link"]["url"]

        if not self.page_guard():
            return self.result
        redirect_started = time.perf_counter()
        portal = await self.http.get(link)
        redirect_seconds = max(0.0, time.perf_counter() - redirect_started)
        query = parse_portal_redirect(str(portal.url))
        portal_soup = BeautifulSoup(portal.text, "lxml")
        portal_title = normalized_title(portal_soup.title.get_text(" ", strip=True)) if portal_soup.title else ""
        main_bundles = [
            anchor.get("src") for anchor in portal_soup.select("script[src]")
            if str(anchor.get("src") or "").startswith("main.")
        ]
        if portal_soup.select_one('base[href="/"]') is None or not main_bundles:
            raise ValueError("Hella Infra Market short link no longer reaches the inspected HROne portal shell")

        self.result.metadata["journey"] = {
            "official_entry": {
                "url": OFFICIAL_ENTRY,
                "final_url": str(entry.url),
                "status": entry.status_code,
                "seconds": entry_seconds,
                "sha256": sha256(entry.content).hexdigest(),
                "bytes": len(entry.content),
            },
            "published_shortlink": {
                "url": link,
                "final_url": str(portal.url),
                "status": portal.status_code,
                "seconds": redirect_seconds,
                "sha256": sha256(portal.content).hexdigest(),
                "bytes": len(portal.content),
                "redirects": [str(item.url) for item in portal.history],
            },
            "portal_url": query["portal_url"],
            "portal_query": query["query"],
            "portal_html_title": portal_title,
            "portal_main_bundle": main_bundles[0],
        }

        api_started = time.perf_counter()
        async with HTTP(
            None,
            delay=self.http.delay,
            timeout=self.http.client.timeout,
            retries=self.http.retries,
            max_response_bytes=self.http.max_response_bytes,
        ) as api:
            style = await self._settings(api, query)
            first = await self._listing(api, query, 1)
            first_ids = first["job_ids"]
            initial_rows = first["rows"]

            if initial_rows:
                page = 1
                rows_by_page = []
                seen = set()
                while True:
                    page_value = first if page == 1 else await self._listing(api, query, page)
                    rows_by_page.append(page_value)
                    for row in page_value["rows"]:
                        job_id = normalized_title(row.get("jobCode"))
                        if job_id in seen:
                            raise ValueError(f"Hella Infra Market listing repeats a job code across pages: {job_id}")
                        seen.add(job_id)
                        job = self.job(
                            {"listing": row, "portal": query["portal_url"]},
                            title=normalized_title(row.get("jobTitle")),
                            url=job_url_from_row(row, query["portal_url"], style),
                            job_id=job_id,
                            requisition_id=str(row["positionId"]) if row.get("positionId") is not None else None,
                            apply_url=job_url_from_row(row, query["portal_url"], style),
                            description="",
                            method="hella_infra_market.hrone_full_job_description_body",
                            description_url=None,
                            locations=[],
                            department=None,
                            employment_type=None,
                            workplace_type=None,
                            date_posted=None,
                            date_posted_raw=None,
                            date_updated=None,
                            valid_through=None,
                            salary=None,
                            employer=LEGAL_NAME,
                            language="en",
                        )
                        job.record_identity = sha256(
                            f"{self.company.name}|hrone-infra|{row.get('positionId')}|{job_id}".encode()
                        ).hexdigest()
                        self.add(job)
                        if self.limit():
                            break
                    if "sample_limit" in self.result.warnings or not page_value["has_more"]:
                        break
                    page += 1
                    if page > self.options.max_pages:
                        self.result.errors.append("listing_page_budget_exhausted")
                        break
                self.result.expected_total = len(seen)
                self.result.jobs = await self.hydrate(self.result.jobs, lambda job: self._detail(api, query, job))
            else:
                self.result.expected_total = 0
                self.result.jobs = []

            final = await self._listing(api, query, 1)
            final_ids = final["job_ids"]
            current_ids = [job.job_id for job in self.result.jobs]
            if not initial_rows:
                current_ids = []
            membership_matches = current_ids == first_ids == final_ids
            if not membership_matches:
                self.result.listing_complete = False
                self.result.errors.append("hella_infra_market_listing_changed_during_collection")
            else:
                self.result.listing_complete = "sample_limit" not in self.result.warnings
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": first_ids,
                "final_ids": final_ids,
                "retained_ids": current_ids,
                "initial_count": len(first_ids),
                "final_count": len(final_ids),
                "retained_count": len(current_ids),
                "matches": membership_matches,
                "zero_vacancy": not first_ids and not final_ids,
            }
            self.result.metadata["portal_branding"] = style
            self.result.metadata["zero_vacancy"] = {
                "confirmed": not first_ids and not final_ids and membership_matches,
                "listing_api_returned_empty_array": True,
                "listing_calls": 2,
                "observed_rendered_notice": "CURRENTLY NO VACANCIES",
                "note": "The empty array is an authoritative live response from the company-scoped portal, not an inaccessible or error response.",
            }
            if not initial_rows:
                self.result.timings["detail_phase_seconds"] = 0.0
        api_seconds = max(0.0, time.perf_counter() - api_started)
        self.result.timings["source_collection_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        self.result.timings["portal_api_seconds"] = api_seconds
        self.result.timings["official_entry_seconds"] = entry_seconds
        self.result.timings["shortlink_seconds"] = redirect_seconds
        return self.result
