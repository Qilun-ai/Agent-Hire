"""Navi Technologies' own careers journey and its linked TurboHire detail pages.

The official page <https://navi.com/careers> publishes one jobs route
(<https://navi.com/careers/jobs>) and one TurboHire careers CTA for the Navi
organisation.  The jobs page calls the same-origin ``/api/careers/jobs`` feed;
that feed is the membership authority.  Each advertised card opens the exact
TurboHire public job page for the same job id.  This module does not discover,
guess or enumerate any other TurboHire tenant or board.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import io
import json
import re
import time
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from xml.etree import ElementTree
import zipfile

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://navi.com/careers"
JOBS_PAGE_URL = "https://navi.com/careers/jobs"
JOBS_API_URL = "https://navi.com/api/careers/jobs"
TURBOHIRE_ORIGIN = "https://navi.turbohire.co"
TURBOHIRE_ORG_ID = "3e818601-0baa-429c-b6f8-4b21903ae0e6"
TURBOHIRE_DASHBOARD_URL = f"{TURBOHIRE_ORIGIN}/dashboardv2?orgId={TURBOHIRE_ORG_ID}&type=0"
API_ORIGIN = "https://thapi-stage2.azurewebsites.net"
TOKEN_URL = API_ORIGIN + "/api/token/noauth"
PUBLIC_JOB_API_URL = API_ORIGIN + "/api/publicjobs/{job_id}"
PUBLIC_JOB_PAGE_URL = TURBOHIRE_ORIGIN + "/job/publicjobs/{job_id}?utm_source=CareerPage"
JOBS_PAGE_SIZE = 10
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
DOCX_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
PDF_CONTENT_TYPE = "application/pdf"
DOCX_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}
ATTACHMENT_METHODS = {
    "pdf": "navi_turbohire_job_description_pdf",
    "docx": "navi_turbohire_job_description_docx",
}


def normalized_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_text(value: str | None) -> str:
    """Canonical rendered text: collapse whitespace and inline-tag punctuation artefacts."""
    text = normalized_space(value)
    text = re.sub(r"\s+([,.;:!?%\)\]])", r"\1", text)
    text = re.sub(r"([\(\[\{])\s+", r"\1", text)
    return text.strip()


def normalized_body_text(html: str | None) -> str:
    """Rendered description text, canonicalised for page comparisons."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return normalized_text(soup.get_text(" "))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def api_headers(token: str | None = None) -> dict:
    """Headers used by the official page for the linked TurboHire API."""
    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": TURBOHIRE_ORIGIN,
        "referer": TURBOHIRE_ORIGIN + "/",
    }
    if token:
        headers["authorization"] = "Bearer " + token
    return headers


def public_job_api_url(job_id: str) -> str:
    return PUBLIC_JOB_API_URL.format(job_id=job_id)


def public_job_page_url(job_id: str) -> str:
    return PUBLIC_JOB_PAGE_URL.format(job_id=job_id)


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "navi.com"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Navi official careers entry redirected away from https://navi.com/careers")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ")) if soup.title else ""
    if "careers at navi" not in title.lower():
        raise ValueError("Navi official careers page no longer identifies itself as Careers At Navi")

    jobs_links, turbohire_links = {}, {}
    for anchor in soup.select("a[href]"):
        href = urljoin(response_url, (anchor.get("href") or "").strip())
        label = normalized_space(anchor.get_text(" "))
        target = urlsplit(href)
        if target.netloc == "navi.com" and target.path.rstrip("/") == "/careers/jobs":
            jobs_links.setdefault(href, []).append(label)
        elif href == TURBOHIRE_DASHBOARD_URL:
            turbohire_links.setdefault(href, []).append(label)
    if not jobs_links:
        raise ValueError("Navi official careers page no longer links to its /careers/jobs route")
    if len(turbohire_links) != 1:
        raise ValueError("Navi official careers page no longer publishes exactly one linked TurboHire jobs CTA")
    turbohire_url, labels = next(iter(turbohire_links.items()))
    query = parse_qs(urlsplit(turbohire_url).query)
    if query.get("orgId") != [TURBOHIRE_ORG_ID] or query.get("type") != ["0"]:
        raise ValueError("Navi TurboHire CTA no longer points at the inspected Navi organisation")
    description = soup.select_one('meta[name="description"]')
    return {
        "url": response_url,
        "title": title,
        "jobs_page_links": jobs_links,
        "jobs_page_url": next(iter(jobs_links)),
        "turbohire_url": turbohire_url,
        "turbohire_org_id": TURBOHIRE_ORG_ID,
        "turbohire_cta_labels": labels,
        "meta_description": normalized_space(description.get("content")) if description else None,
    }


def parse_jobs_page(html: str, response_url: str = JOBS_PAGE_URL) -> dict:
    """The server shell is shared with /careers; the rendered list is checked separately."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "navi.com"
            or parsed.path.rstrip("/") != "/careers/jobs"):
        raise ValueError("Navi jobs page is not the inspected /careers/jobs route")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ")) if soup.title else ""
    if "careers at navi" not in title.lower():
        raise ValueError("Navi jobs page no longer returns the Navi careers shell")
    turbohire = [
        urljoin(response_url, anchor.get("href"))
        for anchor in soup.select("a[href]")
        if urljoin(response_url, anchor.get("href")) == TURBOHIRE_DASHBOARD_URL
    ]
    scripts = [
        urljoin(response_url, script.get("src"))
        for script in soup.select("script[src]")
        if urljoin(response_url, script.get("src")).startswith("https://navi.com/static/js/")
    ]
    return {
        "url": response_url,
        "server_title": title,
        "turbohire_link_count": len(turbohire),
        "main_script_count": len(scripts),
        "client_rendered": True,
        "rendered_listing_api": JOBS_API_URL,
    }


def _require_iso(value, field: str):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Navi jobs API row is missing {field}")
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"Navi jobs API row has invalid {field}: {value!r}") from exc
    return value


def parse_locations(value) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [{"Address": value}]
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


def additional_fields_map(row: dict) -> dict:
    fields = {}
    for item in row.get("AdditionalFields") or []:
        if not isinstance(item, dict):
            continue
        custom = item.get("JobCustomField") or {}
        name = custom.get("FieldName")
        if isinstance(name, str) and name.strip():
            fields[name.strip()] = item.get("Value")
    return fields


def _valid_apply_url(value: str, job_id: str) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "navi.turbohire.co"
        and parsed.path == f"/job/publicjobs/{job_id}"
        and parse_qs(parsed.query) == {"utm_source": ["CareerPage"]}
    )


def parse_jobs_api(payload: dict, *, page_size: int = JOBS_PAGE_SIZE) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Navi official jobs API response is not an object")
    for key in ("Total", "FilteredCount", "FiltersData", "Jobs"):
        if key not in payload:
            raise ValueError(f"Navi official jobs API response is missing {key}")
    total, filtered = payload["Total"], payload["FilteredCount"]
    rows = payload["Jobs"]
    if not isinstance(total, int) or total < 0 or not isinstance(filtered, int) or not isinstance(rows, list):
        raise ValueError("Navi official jobs API Total/FilteredCount/Jobs have unexpected types")
    if filtered != total:
        raise ValueError("Navi official jobs API is filtered, but the company page was collected without filters")
    if len(rows) != total:
        raise ValueError(f"Navi official jobs API Total {total} != Jobs rows {len(rows)}")

    filters_data = payload["FiltersData"]
    if not isinstance(filters_data, dict):
        raise ValueError("Navi official jobs API FiltersData is not an object")
    filters = {}
    for key in ("DepartmentData", "LocationData", "JobTypeData"):
        options = filters_data.get(key)
        if not isinstance(options, list):
            raise ValueError(f"Navi official jobs API FiltersData.{key} is not a list")
        parsed = []
        for option in options:
            if not isinstance(option, dict) or not isinstance(option.get("Name"), str) or not isinstance(option.get("TotalJobs"), int):
                raise ValueError(f"Navi official jobs API FiltersData.{key} has an unidentified option")
            parsed.append({"name": option["Name"], "total_jobs": option["TotalJobs"]})
        filters[key] = parsed

    ids, codes, titles, seen_ids, seen_codes = [], [], [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Navi official jobs API row is not an object")
        job_id = row.get("JobId")
        code = row.get("JobCode")
        title = row.get("JobTitle")
        if not isinstance(job_id, str) or not GUID.fullmatch(job_id):
            raise ValueError(f"Navi official jobs API row has invalid JobId: {job_id!r}")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"Navi official jobs API row {job_id} is missing JobCode")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"Navi official jobs API row {code} is missing JobTitle")
        if not isinstance(row.get("JobDescriptionV2"), str) or not isinstance(row.get("JobDescription"), str):
            raise ValueError(f"Navi official jobs API row {code} is missing its description fields")
        if not _valid_apply_url(row.get("ApplyUrl"), job_id):
            raise ValueError(f"Navi official jobs API row {code} has an unexpected ApplyUrl")
        _require_iso(row.get("PublishedDate"), "PublishedDate")
        _require_iso(row.get("CreatedDate"), "CreatedDate")
        _require_iso(row.get("UpdatedDate"), "UpdatedDate")
        if row.get("PromotionExpiryDate"):
            _require_iso(row["PromotionExpiryDate"], "PromotionExpiryDate")
        if not isinstance(row.get("Experience"), dict):
            raise ValueError(f"Navi official jobs API row {code} has invalid Experience")
        if not isinstance(row.get("Location"), str):
            raise ValueError(f"Navi official jobs API row {code} has invalid Location")
        if not parse_locations(row["Location"]):
            raise ValueError(f"Navi official jobs API row {code} has no public location address")
        if not isinstance(row.get("Skills"), list):
            raise ValueError(f"Navi official jobs API row {code} has invalid Skills")
        if not isinstance(row.get("AdditionalFields"), list):
            raise ValueError(f"Navi official jobs API row {code} has invalid AdditionalFields")
        if job_id in seen_ids:
            raise ValueError(f"Navi official jobs API has duplicate JobId {job_id}")
        if code in seen_codes:
            raise ValueError(f"Navi official jobs API has duplicate JobCode {code}")
        seen_ids.add(job_id)
        seen_codes.add(code)
        ids.append(job_id)
        codes.append(code)
        titles.append(normalized_space(title))

    return {
        "total": total,
        "filtered_count": filtered,
        "filters": filters,
        "rows": rows,
        "job_ids": ids,
        "job_codes": codes,
        "titles": titles,
        "ui_page_size": page_size,
        "ui_pages": (total + page_size - 1) // page_size if total else 0,
        "ui_page_counts": [min(page_size, max(0, total - page * page_size)) for page in range((total + page_size - 1) // page_size)],
        "filter_sums": {key: sum(option["total_jobs"] for option in options) for key, options in filters.items()},
    }


def parse_public_job_detail(payload: dict, listing_row: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Navi TurboHire public job detail is not an object")
    if payload.get("JobId") != listing_row.get("JobId"):
        raise ValueError("Navi detail JobId does not match the official listing row")
    if payload.get("JobCode") != listing_row.get("JobCode"):
        raise ValueError("Navi detail JobCode does not match the official listing row")
    if normalized_space(payload.get("JobTitle")) != normalized_space(listing_row.get("JobTitle")):
        raise ValueError("Navi detail title does not match the official listing row")
    org = payload.get("OrgDetails") or {}
    if org.get("OrgID") != TURBOHIRE_ORG_ID:
        raise ValueError("Navi detail response came from a different TurboHire organisation")
    if payload.get("JobStatus") != "Active" or payload.get("IsPublic") is not True:
        raise ValueError("Navi detail response is not an active public posting")

    issues = []
    description_html = payload.get("JobDescriptionV2") or payload.get("JobDescription") or ""
    description_field = "JobDescriptionV2" if payload.get("JobDescriptionV2") else "JobDescription"
    description_text = normalized_body_text(description_html)
    listing_description = listing_row.get("JobDescriptionV2") or listing_row.get("JobDescription") or ""
    if description_text != normalized_body_text(listing_description):
        issues.append("detail_description_differs_from_official_listing_api")
    expiry = (payload.get("ExpiryDates") or {}).get("CAREERPAGE")
    if expiry and listing_row.get("PromotionExpiryDate") and expiry != listing_row["PromotionExpiryDate"]:
        issues.append("detail_expiry_differs_from_official_listing_api")
    detail_locations = parse_locations(payload.get("Location"))
    listing_locations = parse_locations(listing_row.get("Location"))
    if detail_locations != listing_locations:
        issues.append("detail_locations_differ_from_official_listing_api")
    if payload.get("Department") != listing_row.get("Department"):
        issues.append("detail_department_differs_from_official_listing_api")
    publisher_empty = len(description_text) < 100
    return {
        "payload": payload,
        "description_html": description_html,
        "description_field": description_field,
        "description_text": description_text,
        "description_normalized_chars": len(description_text),
        "description_fnv1a64": fnv1a64(description_text),
        "publisher_empty": publisher_empty,
        "expiry": expiry,
        "locations": detail_locations,
        "experience": experience_from(payload.get("Experience")),
        "number_of_vacancies": payload.get("NumberOfVacancy"),
        "description_blob_url": (payload.get("JobDescriptionBlobUrl") or "").strip() or None,
        "issues": issues,
    }


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-navi-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-navi-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = block.get_text(" ", strip=True)
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized_space(block.get_text(" ", strip=True))
            strong_text = normalized_space(strong.get_text(" ", strip=True)) if strong else ""
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
    else:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


RESPONSIBILITY = re.compile(r"responsibilit|what you.?ll own|what you will own|what you.?ll do|what you will do|key duties", re.I)
QUALIFICATION = re.compile(
    r"qualif|must have|what we expect|what we.?re looking for|what makes you a great fit|"
    r"required experience|education|who you are|requirements|skills",
    re.I,
)


def sections_matching(sections: list[dict], pattern: re.Pattern) -> list[dict] | None:
    matches = [
        {"heading": section["heading"], "text": section["text"]}
        for section in sections
        if section["heading"] and pattern.search(section["heading"]) and section["text"].strip()
    ]
    return matches or None


def extract_visible_description(visible_text: str) -> str:
    """Description window on the exact job page: after 'Job Description', before the apply block."""
    marker = "Job Description"
    start = visible_text.find(marker)
    if start < 0:
        raise ValueError("Rendered Navi job page has no Job Description heading")
    rest = visible_text[start + len(marker):]
    boundaries = [index for index in (rest.find("\nSign in to apply"), rest.find("\nSimilar Jobs")) if index >= 0]
    if not boundaries:
        raise ValueError("Rendered Navi job page has no description boundary before the apply/similar block")
    rest = re.sub(r"^\s*View full description\s*", "", rest[:min(boundaries)])
    return normalized_text(rest)


def attachment_filename(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def attachment_kind(url: str, content_type: str | None) -> str:
    name = attachment_filename(url).lower()
    served = (content_type or "").split(";", 1)[0].strip().lower()
    if served == PDF_CONTENT_TYPE or name.endswith(".pdf"):
        return "pdf"
    if name.endswith(".docx") or served in DOCX_CONTENT_TYPES:
        return "docx"
    return "other"


def extract_docx_text(content: bytes) -> str:
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


def build_attachment(url: str, content: bytes, content_type: str | None, *, final_url: str | None = None) -> dict:
    digest = sha256(content).hexdigest()
    kind = attachment_kind(url, content_type)
    method = ATTACHMENT_METHODS.get(kind)
    text = extract_docx_text(content) if kind == "docx" else extract_pdf_text(content) if kind == "pdf" else ""
    return {
        "url": url,
        "final_url": final_url or url,
        "filename": attachment_filename(url),
        "content_type": (content_type or "").split(";", 1)[0].strip().lower() or None,
        "bytes": len(content),
        "sha256": digest,
        "kind": kind,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "extraction_method": method,
        "text_source": "extracted_text_layer" if text else None,
        "text_chars": len(text),
        "extracted_text": text,
    }


class NaviTechnologies(Adapter):
    """Read only Navi's own careers page, its same-origin jobs feed and its linked TurboHire details."""

    VERIFICATION_REVISION = "navi-technologies.official-careers-feed-and-linked-turbohire.1"

    def _job_from_detail(self, listing_row: dict, detail: dict, detail_sha256: str) -> object:
        af = additional_fields_map(listing_row)
        payload = detail["payload"]
        org = payload.get("OrgDetails") or {}
        job_id = payload["JobId"]
        code = payload["JobCode"]
        requisition = af.get("Requisition ID for Mapping")
        if isinstance(requisition, str):
            requisition = requisition.strip() or None
        else:
            requisition = None
        if not requisition and payload.get("RequisitionId") not in (None, "", ZERO_GUID):
            requisition = payload["RequisitionId"]
        employer = af.get("Entity Name") or payload.get("ClientName") or listing_row.get("CompanyName") or org.get("OrgName")
        team = af.get("Team")
        if not isinstance(team, str) or team.strip().lower() in ("", "none"):
            team = None
        employment_type = listing_row.get("JobType")
        if not isinstance(employment_type, str) or employment_type.strip().lower() in ("", "unspecified"):
            employment_type = af.get("Job Type")
        if not isinstance(employment_type, str) or employment_type.strip().lower() in ("", "unspecified"):
            employment_type = None
        description_html = detail["description_html"]
        description_text = text_html(description_html)
        sections = description_sections(description_html)
        responsibilities = sections_matching(sections, RESPONSIBILITY)
        qualifications = sections_matching(sections, QUALIFICATION)
        expiry = detail["expiry"] or listing_row.get("PromotionExpiryDate")
        apply_url = listing_row["ApplyUrl"]
        description_method = f"navi.turbohire.publicjobs.{detail['description_field']}"
        warnings = list(detail["issues"])
        unavailable = detail["publisher_empty"]
        if unavailable:
            warnings.append("publisher_placeholder_or_empty_description")
        job = self.job(
            {
                "listing_row": listing_row,
                "detail": payload,
                "detail_response_sha256": detail_sha256,
                "job_code": code,
                "native_job_id": job_id,
                "job_status": payload.get("JobStatus"),
                "published_job_boards": payload.get("PublishedJobBoards"),
                "published_dates": payload.get("PublishedDates"),
                "expiry_dates": payload.get("ExpiryDates"),
                "description_blob_url": detail["description_blob_url"],
                "description_sections": sections,
                "description_links": [
                    {"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
                    for anchor in BeautifulSoup(description_html, "lxml").select("a[href]")
                ],
                "additional_fields": af,
                "organisation_name": org.get("OrgName"),
                "client_name": payload.get("ClientName"),
                "company_name": listing_row.get("CompanyName"),
                "job_type_v2": payload.get("JobTypeV2"),
                "ctc_info": payload.get("CTCInfo"),
                "number_of_vacancies": detail["number_of_vacancies"],
                "requisition_id_source": "AdditionalFields.Requisition ID for Mapping" if af.get("Requisition ID for Mapping") else None,
                "date_policy": {
                    "date_posted": "official jobs API Jobs[].PublishedDate",
                    "created_date_not_used_as_posting_date": listing_row.get("CreatedDate"),
                    "valid_through": "TurboHire ExpiryDates.CAREERPAGE = official PromotionExpiryDate",
                    "observed_ui_card_date": listing_row.get("CreatedDate"),
                },
                "attachment": None,
            },
            title=normalized_space(payload.get("JobTitle")),
            url=apply_url,
            job_id=code,
            requisition_id=requisition,
            apply_url=apply_url,
            description=description_html,
            method=description_method,
            description_url=public_job_api_url(job_id),
            locations=detail["locations"],
            department=payload.get("Department"),
            team=team,
            employment_type=employment_type,
            workplace_type=None,
            date_posted=listing_row.get("PublishedDate"),
            date_posted_raw={
                "PublishedDate": listing_row.get("PublishedDate"),
                "PublishedDates": payload.get("PublishedDates"),
                "CreatedDate": listing_row.get("CreatedDate"),
                "UpdatedDate": listing_row.get("UpdatedDate"),
                "ExpiryDates": payload.get("ExpiryDates"),
                "PromotionExpiryDate": listing_row.get("PromotionExpiryDate"),
            },
            date_updated=listing_row.get("UpdatedDate"),
            valid_through=expiry,
            salary=None,
            qualifications=qualifications,
            responsibilities=responsibilities,
            experience=detail["experience"],
            employer=employer,
            language="en",
            description_unavailable=unavailable,
            warnings=warnings,
        )
        job.description_text = description_text
        job.description_sha256 = sha256(description_text.encode()).hexdigest() if description_text else None
        return job

    async def _fetch_attachments(self, jobs):
        self.result.metadata["attachments"] = []
        for job in jobs:
            url = job.raw.get("description_blob_url")
            if not url:
                continue
            started = time.perf_counter()
            try:
                response = await self.http.get(url)
                record = build_attachment(
                    url, response.content, response.headers.get("content-type"), final_url=str(response.url)
                )
                job.raw["attachment"] = dict(record)
                job.raw["attachment"]["extracted_text_chars"] = record["text_chars"]
                self.result.metadata["attachments"].append({
                    "job_code": job.job_id,
                    **{key: record[key] for key in (
                        "url", "final_url", "filename", "content_type", "bytes", "sha256",
                        "kind", "extraction_method", "text_source", "text_chars",
                    )},
                })
                attachment_text = record.get("extracted_text") or ""
                if job.description_unavailable and len(attachment_text.strip()) >= 100:
                    job.description_html = f"<div>{escape(attachment_text)}</div>"
                    job.description_text = attachment_text
                    job.description_method = record.get("extraction_method") or "navi_turbohire_job_description_attachment"
                    job.description_source_url = url
                    job.description_unavailable = False
                    job.warnings.append("inline_placeholder_replaced_by_published_attachment")
                    self.result.validate_job(job)
            except Exception as exc:
                job.raw["attachment"] = {
                    "url": url,
                    "fetch_error": f"{type(exc).__name__}: {exc}",
                }
                job.warnings.append("attachment_fetch_failed")
                self.result.warnings.append(f"attachment_fetch_failed:{url}")
            finally:
                self.result.timings["attachment_fetch_seconds"] = (
                    self.result.timings.get("attachment_fetch_seconds", 0.0)
                    + max(0.0, time.perf_counter() - started)
                )

    async def run(self):
        run_started = time.perf_counter()

        started = time.perf_counter()
        entry_response = await self.http.get(OFFICIAL_ENTRY)
        official = parse_official_entry(entry_response.text, str(entry_response.url))
        official.update(
            status=entry_response.status_code,
            response_sha256=sha256(entry_response.content).hexdigest(),
            response_bytes=len(entry_response.content),
        )
        self.result.timings["official_entry_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.metadata["official_entry"] = official

        started = time.perf_counter()
        jobs_page_response = await self.http.get(JOBS_PAGE_URL)
        jobs_page = parse_jobs_page(jobs_page_response.text, str(jobs_page_response.url))
        jobs_page.update(
            status=jobs_page_response.status_code,
            response_sha256=sha256(jobs_page_response.content).hexdigest(),
            response_bytes=len(jobs_page_response.content),
        )
        self.result.timings["jobs_page_shell_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.metadata["jobs_page"] = jobs_page

        started = time.perf_counter()
        listing_response = await self.http.get(JOBS_API_URL)
        listing = parse_jobs_api(listing_response.json())
        self.result.timings["official_listing_seconds"] = max(0.0, time.perf_counter() - started)
        self.result.expected_total = listing["total"]
        self.result.pages = 1
        if listing["total"] == 0:
            self.result.errors.append("navi_official_jobs_api_returned_zero_jobs")
            self.result.listing_complete = False
        self.result.metadata["listing"] = {
            "url": JOBS_API_URL,
            "total": listing["total"],
            "filtered_count": listing["filtered_count"],
            "filters": listing["filters"],
            "filter_sums": listing["filter_sums"],
            "job_ids": listing["job_ids"],
            "job_codes": listing["job_codes"],
            "titles": listing["titles"],
            "ui_page_size": listing["ui_page_size"],
            "ui_pages": listing["ui_pages"],
            "ui_page_counts": listing["ui_page_counts"],
            "request_pages": 1,
            "filters_applied": False,
            "membership_authority": "Navi's own same-origin /api/careers/jobs feed used by https://navi.com/careers/jobs",
        }
        for key, total in listing["filter_sums"].items():
            if total != listing["total"]:
                self.result.warnings.append(f"navi_filter_sum_mismatch:{key}:{total}:{listing['total']}")
        if listing["total"] != len(listing["job_ids"]):
            self.result.errors.append("navi_official_listing_total_mismatch")
            self.result.listing_complete = False

        started = time.perf_counter()
        token_response = await self.http.get(TOKEN_URL, headers=api_headers())
        token_payload = token_response.json()
        token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError("Navi linked TurboHire token endpoint returned no access_token")
        self.result.timings["token_seconds"] = max(0.0, time.perf_counter() - started)
        auth_headers = api_headers(token)

        listing_jobs = []
        for row in listing["rows"]:
            job = self.job(
                {"listing_row": row, "job_code": row["JobCode"], "native_job_id": row["JobId"]},
                title=normalized_space(row["JobTitle"]),
                url=row["ApplyUrl"],
                job_id=row["JobCode"],
                apply_url=row["ApplyUrl"],
                locations=parse_locations(row["Location"]),
                department=row.get("Department"),
                employment_type=row.get("JobType"),
                date_posted=row.get("PublishedDate"),
                date_updated=row.get("UpdatedDate"),
                valid_through=row.get("PromotionExpiryDate"),
                description="",
            )
            job.raw["listing_row"] = row
            job.raw["job_code"] = row["JobCode"]
            job.raw["native_job_id"] = row["JobId"]
            self.add(job)
            listing_jobs.append(job)

        self.result.metadata["detail_request_shape"] = {
            "url_template": PUBLIC_JOB_API_URL,
            "query": {"fieldVisibility": "CareerPage"},
            "headers": "Origin/Referer=https://navi.turbohire.co and Bearer token from /api/token/noauth",
            "public_page_template": PUBLIC_JOB_PAGE_URL,
            "scope": "Only the JobId values returned by Navi's own /api/careers/jobs feed.",
        }

        async def detail(job):
            row = job.raw["listing_row"]
            response = await self.http.get(
                public_job_api_url(row["JobId"]),
                params={"fieldVisibility": "CareerPage"},
                headers=auth_headers,
            )
            parsed = parse_public_job_detail(response.json(), row)
            return self._job_from_detail(row, parsed, sha256(response.content).hexdigest())

        jobs = await self.hydrate(listing_jobs, detail)
        self.result.jobs = jobs
        await self._fetch_attachments(jobs)

        started = time.perf_counter()
        final_response = await self.http.get(JOBS_API_URL)
        final_listing = parse_jobs_api(final_response.json())
        self.result.timings["final_listing_recheck_seconds"] = max(0.0, time.perf_counter() - started)
        same_ids = final_listing["job_ids"] == listing["job_ids"]
        same_total = final_listing["total"] == listing["total"]
        collected_codes = [job.job_id for job in jobs]
        self.result.metadata["final_listing_recheck"] = {
            "total": final_listing["total"],
            "job_ids": final_listing["job_ids"],
            "same_order_and_ids_as_initial": same_ids,
            "same_total_as_initial": same_total,
            "collected_codes_match_initial": collected_codes == listing["job_codes"],
        }
        self.result.listing_complete = bool(
            same_ids and same_total and collected_codes == listing["job_codes"] and not self.result.errors
        )
        if not self.result.listing_complete:
            self.result.errors.append("navi_listing_membership_changed_or_incomplete_during_run")
        self.result.metadata["publisher_empty_descriptions"] = [
            job.job_id for job in jobs if job.description_unavailable
        ]
        self.result.metadata["verification_revision"] = self.VERIFICATION_REVISION
        self.result.metadata["scope_evidence"] = {
            "official_entry": OFFICIAL_ENTRY,
            "jobs_page": JOBS_PAGE_URL,
            "same_origin_listing_api": JOBS_API_URL,
            "linked_turbohire_org_id": TURBOHIRE_ORG_ID,
            "linked_turbohire_dashboard": TURBOHIRE_DASHBOARD_URL,
            "scope_reason": (
                "Navi's own careers page publishes /careers/jobs and exactly one TurboHire CTA for the "
                "Navi organisation; the rendered jobs page and the same-origin feed return the same ordered IDs."
            ),
        }
        self.result.timings["adapter_total_seconds"] = max(0.0, time.perf_counter() - run_started)
        return self.result
