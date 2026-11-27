"""Flipkart's official careers site and its exact company-published TurboHire listing.

The official site (www.flipkartcareers.com) publishes one TurboHire career page
for ``Flipkart Internet Private Limited``.  That page's own career-page API is
the membership authority; this module does not discover or enumerate any other
TurboHire tenant or board.
"""
from __future__ import annotations

from hashlib import sha256
import io
import json
import re
import time
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree
import zipfile

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import Job, now, text_html


OFFICIAL_ENTRY = "https://www.flipkartcareers.com/"
LEGACY_JOBSLIST_URL = "https://www.flipkartcareers.com/jobslist"
TURBOHIRE_ORIGIN = "https://flipkart.turbohire.co"
API_ORIGIN = "https://thapi.azurewebsites.net"
TOKEN_URL = API_ORIGIN + "/api/token/noauth"
DEPARTMENTS_URL = API_ORIGIN + "/api/justdropyourresume/departments"
LISTING_URL = API_ORIGIN + "/api/careerpagev2/filteredjobs"
DETAIL_URL = API_ORIGIN + "/api/referraljobs"
LISTING_PAGE_TYPE = "0"
JOBS_PAGE_URL = TURBOHIRE_ORIGIN + "/dashboardv2?orgId={org_id}&type=0"
CAREERPAGE_PATH = re.compile(
    r"^/careerpage/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$",
    re.I,
)
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
PDF_CONTENT_TYPE = "application/pdf"
DOCX_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    # Flipkart's public blob store serves real .docx files as application/msword.
    "application/msword",
}
DOCX_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
ATTACHMENT_METHODS = {
    "pdf": "careerpage_attachment_pdf",
    "docx": "careerpage_attachment_docx",
}
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


def api_headers(token: str | None = None) -> dict:
    """Headers required by the TurboHire public API (Origin AND Referer)."""
    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": TURBOHIRE_ORIGIN,
        "referer": TURBOHIRE_ORIGIN + "/",
    }
    if token:
        headers["authorization"] = "Bearer " + token
    return headers


def normalized_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_body_text(html: str | None) -> str:
    """Rendered description text, whitespace-normalised for page comparisons."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return normalized_space(soup.get_text(" "))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.flipkartcareers.com"
            or parsed.path.rstrip("/") not in ("", "/index")):
        raise ValueError("Flipkart official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    links = {}
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if href.startswith(TURBOHIRE_ORIGIN + "/careerpage/"):
            links.setdefault(href, []).append(normalized_space(anchor.get_text(" ")))
    if len(links) != 1:
        raise ValueError(
            "Flipkart official careers page no longer publishes exactly one TurboHire career-page link"
        )
    careerpage_url, labels = next(iter(links.items()))
    match = CAREERPAGE_PATH.match(urlsplit(careerpage_url).path)
    if not match:
        raise ValueError("Flipkart TurboHire career-page link is not a single tenant/org id")
    if not any("explore jobs" in label.lower() or label.lower() == "jobs" for label in labels):
        raise ValueError("Flipkart TurboHire link is not labelled as the jobs journey")
    return {
        "url": response_url,
        "careerpage_url": careerpage_url,
        "careerpage_org_id": match.group(1).lower(),
        "link_labels": labels,
    }


def parse_careerpage_shell(html: str, response_url: str, org_id: str) -> dict:
    parsed = urlsplit(response_url)
    match = CAREERPAGE_PATH.match(parsed.path)
    if parsed.netloc != "flipkart.turbohire.co" or not match:
        raise ValueError("Flipkart hosted career page is not the inspected TurboHire org route")
    if match.group(1).lower() != org_id.lower():
        raise ValueError("Flipkart hosted career page org id changed")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ")) if soup.title else ""
    site_name = ""
    meta = soup.select_one('meta[property="og:site_name"]')
    if meta:
        site_name = normalized_space(meta.get("content"))
    if title != "Flipkart Internet Private Limited" or site_name != title:
        raise ValueError("Flipkart hosted career page no longer identifies Flipkart Internet Private Limited")
    return {"url": response_url, "org_id": org_id.lower(), "title": title, "site_name": site_name}


def parse_departments(payload) -> dict:
    if not isinstance(payload, list):
        raise ValueError("Flipkart departments response is not a list")
    rows = []
    for row in payload:
        if not isinstance(row, dict) or not row.get("Name") or not isinstance(row.get("JobsCount"), int):
            raise ValueError("Flipkart departments response row is missing Name/JobsCount")
        rows.append({"id": row.get("Id"), "name": row["Name"], "jobs_count": row["JobsCount"]})
    return {"departments": rows, "reported_total": sum(row["jobs_count"] for row in rows)}


def parse_listing(payload) -> dict:
    if not isinstance(payload, dict) or "Total" not in payload or "Result" not in payload:
        raise ValueError("Flipkart career-page listing payload lacks Total/Result")
    total, rows = payload["Total"], payload["Result"]
    if not isinstance(total, int) or total < 0 or not isinstance(rows, list):
        raise ValueError("Flipkart career-page listing Total/Result have unexpected types")
    if total != len(rows):
        raise ValueError(f"Flipkart career-page listing Total {total} != Result rows {len(rows)}")
    codes, ids, tokens = [], [], []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Flipkart listing row is not an object")
        for key in ("JobId", "JobIdObfuscated", "JobCode", "JobTitle"):
            if not row.get(key):
                raise ValueError(f"Flipkart listing row is missing {key}")
        codes.append(row["JobCode"])
        ids.append(row["JobId"])
        tokens.append(row["JobIdObfuscated"])
    if len(set(codes)) != len(codes) or len(set(ids)) != len(ids) or len(set(tokens)) != len(tokens):
        raise ValueError("Flipkart career-page listing contains duplicate JobCode/JobId/token")
    return {"total": total, "rows": rows, "codes": codes, "job_ids": ids}


def parse_detail(payload, listing_row: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Flipkart job detail payload is not an object")
    if payload.get("JobCode") != listing_row["JobCode"]:
        raise ValueError("Flipkart job detail JobCode does not match the listing row")
    if payload.get("JobId") != listing_row["JobId"]:
        raise ValueError("Flipkart job detail JobId does not match the listing row")
    if normalized_space(payload.get("JobTitle")) != normalized_space(listing_row["JobTitle"]):
        raise ValueError("Flipkart job detail title does not match the listing row")
    description = payload.get("JobDescriptionV2") or payload.get("JobDescription")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Flipkart job detail has no public description field")
    return payload


def attachment_filename(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def attachment_kind(url: str, content_type: str | None) -> str:
    """Classify a public JD attachment from its served type, falling back to its filename."""
    name = attachment_filename(url).lower()
    served = (content_type or "").split(";", 1)[0].strip().lower()
    if served == PDF_CONTENT_TYPE or name.endswith(".pdf"):
        return "pdf"
    if name.endswith(".docx") or served in DOCX_CONTENT_TYPES:
        return "docx"
    return "other"


def extract_docx_text(content: bytes) -> str:
    """Paragraph text from word/document.xml (no python-docx dependency)."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            document = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError):
        return ""
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError:
        return ""
    lines = []
    for paragraph in root.iter(DOCX_NAMESPACE + "p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == DOCX_NAMESPACE + "t" and node.text:
                parts.append(node.text)
            elif node.tag == DOCX_NAMESPACE + "tab":
                parts.append("\t")
            elif node.tag in (DOCX_NAMESPACE + "br", DOCX_NAMESPACE + "cr"):
                parts.append("\n")
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def extract_pdf_text(content: bytes) -> str:
    """Text layer of a PDF, if it has one. Many Flipkart JDs are print-to-PDF with none."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception:
        return ""
    return "\n\n".join(page for page in pages if page)


def build_attachment(url: str, content: bytes, content_type: str | None, *,
                     final_url: str | None = None, transcriptions: dict | None = None) -> dict:
    """Public JD attachment evidence: identity, served bytes and its extracted/transcribed text."""
    digest = sha256(content).hexdigest()
    kind = attachment_kind(url, content_type)
    method = ATTACHMENT_METHODS.get(kind)
    text = extract_docx_text(content) if kind == "docx" else extract_pdf_text(content) if kind == "pdf" else ""
    source = "extracted_text_layer" if text else None
    transcription = (transcriptions or {}).get(digest)
    note = transcription if isinstance(transcription, dict) else {}
    if not text and transcription:
        candidate = transcription if isinstance(transcription, str) else transcription.get("text") or ""
        if candidate.strip():
            text = candidate.strip()
            method = note.get("method") or method or "careerpage_attachment_transcription"
            source = "stored_transcription"
    record = {
        "url": url,
        "final_url": final_url or url,
        "filename": attachment_filename(url),
        "content_type": (content_type or "").split(";", 1)[0].strip().lower() or None,
        "bytes": len(content),
        "sha256": digest,
        "kind": kind,
        "fetched_at": now(),
        "extraction_method": method,
        "text_source": source,
        "text_chars": len(text),
        "extracted_text": text,
    }
    for key in ("page_embed_url", "transcription_method", "transcribed_at"):
        if note.get(key):
            record[key] = note[key]
    return record


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


def experience_from(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    experience = {}
    if isinstance(value.get("MinExp"), (int, float)):
        experience["minimum_years"] = value["MinExp"]
    if isinstance(value.get("MaxExp"), (int, float)):
        experience["maximum_years"] = value["MaxExp"]
    return experience or None


def job_url(obfuscated: str) -> str:
    return TURBOHIRE_ORIGIN + "/job/publicjobs/" + quote(obfuscated, safe="%")


def detail_api_url(obfuscated: str) -> str:
    return DETAIL_URL + "?tkn=" + obfuscated + "&fieldVisibility=CareerPage"


def listing_api_url(org_id: str) -> str:
    return f"{LISTING_URL}?orgId={org_id}&pageType={LISTING_PAGE_TYPE}"


def departments_api_url(org_id: str) -> str:
    return f"{DEPARTMENTS_URL}?orgId={org_id}&pageType={LISTING_PAGE_TYPE}"


def build_listing_job(adapter: Adapter, row: dict, org_name: str) -> Job:
    url = job_url(row["JobIdObfuscated"])
    expiry = (row.get("ExpiryDates") or {}).get("CAREERPAGE")
    return adapter.job(
        row,
        title=row["JobTitle"],
        url=url,
        job_id=row["JobCode"],
        apply_url=url,
        locations=parse_locations(row.get("Location")),
        department=row.get("Department"),
        date_posted=row.get("PublishedDate"),
        date_posted_raw={
            "listing_PublishedDate": row.get("PublishedDate"),
            "listing_ExpiryDates": row.get("ExpiryDates"),
        },
        date_updated=row.get("UpdatedDate"),
        valid_through=expiry,
        experience=experience_from(row.get("Experience")),
        employer=org_name,
        description="",
        warnings=[],
    )


def build_detail_job(adapter: Adapter, listing_job: Job, detail: dict, org_name: str,
                     response_sha256: str | None, attachment: dict | None = None) -> Job:
    description_html = detail.get("JobDescriptionV2") or detail.get("JobDescription")
    inline_text = text_html(description_html)
    publisher_empty = len(inline_text) < 100
    attachment_text = (attachment or {}).get("extracted_text") or ""
    attachment_used = publisher_empty and len(attachment_text) >= 100
    description_method = "careerpage_detail_api:JobDescriptionV2"
    description_url = detail_api_url(detail["JobIdObfuscated"])
    description_unavailable = publisher_empty and not attachment_used
    warnings = []
    if attachment_used:
        description_method = attachment.get("extraction_method") or "careerpage_attachment"
        description_url = attachment["url"]
        warnings.append("inline_placeholder_replaced_by_published_attachment")
    elif publisher_empty:
        warnings.append("publisher_empty_or_placeholder_description")
    expiry = (detail.get("ExpiryDates") or {}).get("CAREERPAGE")
    listing_row = listing_job.raw.get("listing_row", listing_job.raw)
    org_details = detail.get("OrgDetails") or {}
    client_company = detail.get("ClientCompany") or {}
    job = adapter.job(
        {
            "listing_row": listing_row,
            "listing_fields": listing_job.raw,
            "detail": detail,
            "detail_response_sha256": response_sha256,
            "job_code": detail["JobCode"],
            "native_job_id": detail["JobId"],
            "job_status": detail.get("JobStatus"),
            "published_job_boards": detail.get("PublishedJobBoards"),
            "description_blob_url": (detail.get("JobDescriptionBlobUrl") or "").strip() or None,
            "inline_description_text": inline_text,
            "attachment": attachment,
            "organisation_name": org_details.get("OrgName"),
            "client_name": detail.get("ClientName"),
            "client_company": client_company,
            "show_client_name": detail.get("ShowClientName"),
            "application": {
                "route": "Sign in to apply on the exact public job page",
                "public_apply_url_published": False,
            },
        },
        title=detail["JobTitle"],
        url=listing_job.url,
        job_id=detail["JobCode"],
        apply_url=listing_job.url,
        locations=parse_locations(detail.get("Location")) or listing_job.locations,
        department=detail.get("Department") or listing_job.department,
        date_posted=listing_job.date_posted,
        date_posted_raw={
            **listing_job.date_posted_raw,
            "detail_PublishedDate": detail.get("PublishedDate"),
            "detail_PublishedDates": detail.get("PublishedDates"),
        },
        date_updated=detail.get("UpdatedDate") or listing_job.date_updated,
        valid_through=expiry if "ExpiryDates" in detail else listing_job.valid_through,
        experience=experience_from(detail.get("Experience")) or listing_job.experience,
        employer=org_details.get("OrgName") or org_name,
        description=description_html,
        description_text=attachment_text if attachment_used else "",
        method=description_method,
        description_url=description_url,
        description_unavailable=description_unavailable,
        warnings=warnings,
    )
    return job


def extract_visible_description(visible_text: str) -> str:
    """Description window on the exact job page: after 'Job Description', before 'Sign in to apply'."""
    marker = "Job Description"
    start = visible_text.find(marker)
    if start < 0:
        raise ValueError("Rendered job page has no Job Description heading")
    rest = visible_text[start + len(marker):]
    end = rest.find("Sign in to apply")
    if end < 0:
        raise ValueError("Rendered job page has no description boundary before the apply block")
    return normalized_space(rest[:end])


def parse_legacy_jobslist(html: str) -> dict:
    """The official /jobslist page is static, truncated and not an enumerable listing."""
    count = re.search(r"(\d+)\s+Openings found", html)
    cards = len(re.findall(r'class="[^"]*\bjob-item\b', html))
    truncated_titles = re.findall(r"<h6>([^<]*\.\.\.)</h6>", html)
    show_more_stub = "alert('Loading more jobs...')" in html or 'alert("Loading more jobs...")' in html
    card_links = re.findall(r'class="[^"]*\bjob-item\b".{0,600}?<a\b[^>]*href="http', html, re.S)
    return {
        "url": LEGACY_JOBSLIST_URL,
        "advertised_openings_text": count.group(0) if count else None,
        "advertised_openings": int(count.group(1)) if count else None,
        "static_job_cards": cards,
        "truncated_titles": len(truncated_titles),
        "card_links": len(card_links),
        "show_more_is_alert_stub": show_more_stub,
        "enumerable": cards == int(count.group(1)) if count else False,
    }


class Flipkart(Adapter):
    def __init__(self, http, company, source, options, transcriptions: dict | None = None):
        super().__init__(http, company, source, options)
        self.transcriptions = transcriptions or {}

    async def fetch_attachment(self, url: str) -> dict:
        response = await self.http.get(url)
        return build_attachment(
            url,
            response.content,
            response.headers.get("content-type"),
            final_url=str(response.url),
            transcriptions=self.transcriptions,
        )

    async def run(self):
        run_started = time.perf_counter()

        started = time.perf_counter()
        entry_response = await self.http.get(OFFICIAL_ENTRY)
        official = parse_official_entry(entry_response.text, str(entry_response.url))
        self.result.timings["official_entry_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.metadata["official_entry"] = official
        org_id = official["careerpage_org_id"]

        started = time.perf_counter()
        legacy_response = await self.http.get(LEGACY_JOBSLIST_URL)
        legacy = parse_legacy_jobslist(legacy_response.text)
        legacy["http_status"] = legacy_response.status_code
        legacy["last_modified"] = legacy_response.headers.get("last-modified")
        self.result.metadata["legacy_jobslist"] = legacy
        self.result.timings["legacy_jobslist_check_seconds"] = max(0.0, time.perf_counter() - started)

        started = time.perf_counter()
        shell_response = await self.http.get(official["careerpage_url"])
        shell = parse_careerpage_shell(shell_response.text, str(shell_response.url), org_id)
        self.result.timings["careerpage_shell_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.metadata["careerpage_shell"] = shell

        started = time.perf_counter()
        token_response = await self.http.get(TOKEN_URL, headers=api_headers())
        token_payload = token_response.json()
        token = token_payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("Flipkart TurboHire token endpoint returned no access_token")
        self.result.timings["token_seconds"] = max(0.0, time.perf_counter() - started)
        auth_headers = api_headers(token)

        started = time.perf_counter()
        departments_response = await self.http.get(departments_api_url(org_id), headers=auth_headers)
        departments = parse_departments(departments_response.json())
        self.result.timings["departments_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.metadata["departments"] = departments

        started = time.perf_counter()
        listing_response = await self.http.request(
            "POST", listing_api_url(org_id), headers=auth_headers, json=LISTING_REQUEST_BODY
        )
        listing = parse_listing(listing_response.json())
        self.result.timings["listing_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.expected_total = listing["total"]
        self.result.pages = 1
        self.result.metadata["listing"] = {
            "request_url": listing_api_url(org_id),
            "request_body": LISTING_REQUEST_BODY,
            "total": listing["total"],
            "rows": len(listing["rows"]),
            "codes": listing["codes"],
            "sort": "PostedDate descending",
            "filters_applied": False,
            "pagination": "One POST returns every row; Total == Result length.",
        }
        if departments["reported_total"] != listing["total"]:
            self.result.warnings.append(
                f"department_jobs_count_sum_{departments['reported_total']}_vs_listing_{listing['total']}"
            )

        org_name = "Flipkart Internet Private Limited"
        listing_jobs = []
        for row in listing["rows"]:
            job = build_listing_job(self, row, org_name)
            job.raw = {"listing_row": row, "job_code": row["JobCode"], "native_job_id": row.get("JobId")}
            self.add(job)
            listing_jobs.append(job)

        self.result.metadata["attachments"] = []

        async def detail(job: Job) -> Job:
            row = job.raw["listing_row"]
            response = await self.http.get(detail_api_url(row["JobIdObfuscated"]), headers=auth_headers)
            payload = parse_detail(response.json(), row)
            attachment = None
            blob_url = (payload.get("JobDescriptionBlobUrl") or "").strip()
            if blob_url:
                attachment_started = time.perf_counter()
                try:
                    attachment = await self.fetch_attachment(blob_url)
                except Exception as exc:
                    attachment = {
                        "url": blob_url,
                        "fetch_error": f"{type(exc).__name__}: {exc}",
                        "fetched_at": now(),
                    }
                    self.result.warnings.append(f"attachment_fetch_failed:{blob_url}")
                self.result.timings["attachment_fetch_seconds"] = (
                    self.result.timings.get("attachment_fetch_seconds", 0.0)
                    + max(0.0, time.perf_counter() - attachment_started)
                )
                summary_keys = (
                    "url", "final_url", "filename", "content_type", "bytes", "sha256", "kind",
                    "fetched_at", "extraction_method", "text_source", "text_chars", "fetch_error",
                    "page_embed_url",
                )
                self.result.metadata["attachments"].append({
                    "job_code": payload["JobCode"],
                    **{key: attachment[key] for key in summary_keys if key in attachment},
                })
            return build_detail_job(
                self, job, payload, org_name, sha256(response.content).hexdigest(), attachment
            )

        self.result.metadata["detail_request_shape"] = {
            "url_template": DETAIL_URL + "?tkn=<JobIdObfuscated>&fieldVisibility=CareerPage",
            "visibility": "CareerPage",
        }
        jobs = await self.hydrate(listing_jobs, detail)
        self.result.jobs = jobs

        started = time.perf_counter()
        final_response = await self.http.request(
            "POST", listing_api_url(org_id), headers=auth_headers, json=LISTING_REQUEST_BODY
        )
        final_listing = parse_listing(final_response.json())
        self.result.timings["final_listing_recheck_seconds"] = max(0.0, time.perf_counter() - started)
        final_codes = final_listing["codes"]
        initial_codes = listing["codes"]
        same_ids = final_codes == initial_codes
        same_total = final_listing["total"] == listing["total"]
        collected_codes = [job.job_id for job in jobs]
        self.result.metadata["final_listing_recheck"] = {
            "total": final_listing["total"],
            "codes": final_codes,
            "same_order_and_ids_as_initial": same_ids,
            "same_total_as_initial": same_total,
        }
        self.result.listing_complete = bool(same_ids and same_total and collected_codes == initial_codes)
        if not self.result.listing_complete:
            self.result.errors.append("flipkart_listing_membership_changed_during_run")
        self.result.timings["adapter_total_seconds"] = max(0.0, time.perf_counter() - run_started)
        self.result.metadata["publisher_empty_descriptions"] = [
            job.job_id for job in jobs if job.description_unavailable
        ]
        return self.result
