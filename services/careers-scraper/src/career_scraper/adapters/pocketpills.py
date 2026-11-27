"""Pocketpills' official careers page and its exact company-published BambooHR tenant.

Observed 2026-11-08 (see ``companies/pocketpills/journey-research.md``):

- ``https://www.pocketpills.com/careers`` is a client-rendered Next.js page.  Its
  rendered (and only) jobs CTA is "View current openings", pointing at
  ``https://pocketpills.bamboohr.com/careers``.  The raw page has no job cards,
  no other ATS link and no JSON-LD JobPosting.
- The BambooHR tenant is Pocketpills, account ``419073``.  Its observed listing
  request is ``GET /careers/list`` and its company identity request is
  ``GET /careers/company-info``.  The listing response carries
  ``meta.totalCount`` and all public rows; there is no pagination parameter.
- At the reviewed time the tenant returned ``{"meta":{"totalCount":0},"result":[]}``
  twice and the rendered "Current Openings" surface showed zero job cards.
- The BambooHR careers UI bundle (archived) and a separate public BambooHR
  tenant used only for vendor-schema research show the detail request
  ``GET /careers/{id}/detail`` returning ``result.jobOpening`` with the full
  employer-authored ``description`` HTML.  That route is implemented for future
  non-empty snapshots; the live Pocketpills snapshot has no posting to request.

Only the exact Pocketpills BambooHR tenant reached from the company's own
careers page is queried.  No guessed ATS slug, other BambooHR tenant, parent
board or aggregator is used.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://www.pocketpills.com/careers"
BAMBOOHR_ORIGIN = "https://pocketpills.bamboohr.com"
BAMBOOHR_CAREERS = BAMBOOHR_ORIGIN + "/careers"
BAMBOOHR_LIST_API = BAMBOOHR_ORIGIN + "/careers/list"
BAMBOOHR_COMPANY_INFO_API = BAMBOOHR_ORIGIN + "/careers/company-info"
ACCOUNT_ID = "419073"
EMPLOYER_NAME = "Pocketpills"

CAREERS_PATH = re.compile(r"^/careers/?$")
LIST_PATH = re.compile(r"^/careers/list/?$")
COMPANY_INFO_PATH = re.compile(r"^/careers/company-info/?$")
DETAIL_PATH = re.compile(r"^/careers/(\d+)/detail/?$")
JOB_PATH = re.compile(r"^/careers/(\d+)/?$")
EXACT_BAMBOO_LINK = re.compile(re.escape(BAMBOOHR_CAREERS) + r"(?:[?#][^\"'<>\\\s]*)?")

LOCATION_TYPE_LABEL = {"0": "On-Site", "1": "Remote", "2": "Hybrid"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)


def normalized_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\u00a0", " ")).strip()


def normalized_title(value: str | None) -> str:
    return normalized_space(value)


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


def _same_url(actual: str, expected: str) -> bool:
    actual_parts, expected_parts = urlsplit(actual), urlsplit(expected)
    return (
        actual_parts.scheme == expected_parts.scheme
        and actual_parts.netloc == expected_parts.netloc
        and actual_parts.path.rstrip("/") == expected_parts.path.rstrip("/")
        and actual_parts.query == expected_parts.query
    )


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Validate the current Pocketpills careers page and its only jobs CTA."""
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"pocketpills.com", "www.pocketpills.com"}
        or not CAREERS_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("Pocketpills official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "Pocketpills" not in title:
        raise ValueError(f"Pocketpills official careers page title changed: {title!r}")
    h1_values = [normalized_space(h.get_text(" ", strip=True)) for h in soup.select("h1")]
    if "CAREERS" not in h1_values:
        raise ValueError(f"Pocketpills official careers page h1 changed: {h1_values!r}")

    raw_links = []
    for match in EXACT_BAMBOO_LINK.finditer(html):
        value = match.group(0)
        if value not in raw_links:
            raw_links.append(value)
    if len(raw_links) != 1:
        raise ValueError(
            "Pocketpills official careers page no longer contains exactly one "
            "company-published BambooHR careers link"
        )
    link = raw_links[0]
    if not _same_url(link, BAMBOOHR_CAREERS):
        raise ValueError(f"Pocketpills BambooHR link changed: {link!r}")
    if "View current openings" not in html:
        raise ValueError("Pocketpills official careers page no longer labels its BambooHR CTA")

    anchors = [
        {"href": urljoin(response_url, anchor.get("href") or ""),
         "text": normalized_space(anchor.get_text(" ", strip=True))}
        for anchor in soup.select("a[href]")
        if "bamboohr" in (anchor.get("href") or "").lower()
    ]
    return {
        "url": response_url,
        "title": title,
        "h1": h1_values,
        "careers_link": link,
        "careers_link_count": len(raw_links),
        "careers_link_label": "View current openings",
        "render_time_bamboohr_anchors": anchors,
        "raw_html_has_no_hydrated_anchor": not bool(anchors),
    }


def parse_bamboohr_shell(html: str, response_url: str = BAMBOOHR_CAREERS) -> dict:
    """Validate the exact Pocketpills BambooHR shell and its company identity."""
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "pocketpills.bamboohr.com"
        or not CAREERS_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("Pocketpills BambooHR careers shell is not the inspected tenant route")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_space(soup.title.get_text(" ", strip=True)) if soup.title else ""
    site_name = soup.select_one('meta[property="og:site_name"]')
    og_title = soup.select_one('meta[property="og:title"]')
    og_url = soup.select_one('meta[property="og:url"]')
    site_name_value = normalized_space(site_name.get("content")) if site_name else ""
    og_title_value = normalized_space(og_title.get("content")) if og_title else ""
    og_url_value = normalized_space(og_url.get("content")) if og_url else ""
    if site_name_value != EMPLOYER_NAME:
        raise ValueError(f"Pocketpills BambooHR site name changed: {site_name_value!r}")
    if og_title_value != "Current Openings":
        raise ValueError(f"Pocketpills BambooHR listing heading changed: {og_title_value!r}")
    if not _same_url(og_url_value, BAMBOOHR_CAREERS):
        raise ValueError(f"Pocketpills BambooHR og:url changed: {og_url_value!r}")

    po_data_node = soup.select_one("#poData")
    if po_data_node is None:
        raise ValueError("Pocketpills BambooHR shell no longer publishes its poData app state")
    try:
        po_data = json.loads(po_data_node.get_text())
    except (TypeError, ValueError) as exc:
        raise ValueError("Pocketpills BambooHR poData is not valid JSON") from exc
    logo = ((po_data.get("site") or {}).get("logo") or {})
    logo_alt = normalized_space(logo.get("alt"))
    logo_url = normalized_space(logo.get("url"))
    if logo_alt != EMPLOYER_NAME or f"/{ACCOUNT_ID}/" not in logo_url:
        raise ValueError("Pocketpills BambooHR shell logo/account identity changed")
    return {
        "url": response_url,
        "title": title,
        "og_title": og_title_value,
        "og_site_name": site_name_value,
        "og_url": og_url_value,
        "account_id": ACCOUNT_ID,
        "logo_alt": logo_alt,
        "logo_url": logo_url,
    }


def parse_company_info(payload, response_url: str = BAMBOOHR_COMPANY_INFO_API) -> dict:
    """Validate BambooHR's account-scoped company identity response."""
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "pocketpills.bamboohr.com"
        or not COMPANY_INFO_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("Pocketpills BambooHR company-info request left its exact tenant route")
    if not isinstance(payload, dict):
        raise ValueError("Pocketpills BambooHR company-info response is not an object")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("Pocketpills BambooHR company-info response has no result object")
    name = normalized_space(result.get("name"))
    identity = str(result.get("id") or "")
    share_url = normalized_space(result.get("careerShareUrl"))
    if name != EMPLOYER_NAME or identity != ACCOUNT_ID:
        raise ValueError("Pocketpills BambooHR company-info identity changed")
    if not _same_url(share_url, BAMBOOHR_CAREERS):
        raise ValueError(f"Pocketpills BambooHR careerShareUrl changed: {share_url!r}")
    return {
        "name": name,
        "id": identity,
        "career_share_url": share_url,
        "logo_url": normalized_space(result.get("logoUrl")) or None,
        "share_image_url": normalized_space(result.get("shareImageUrl")) or None,
        "in_trial": result.get("inTrial"),
    }


def _listing_rows(payload) -> tuple[int, list[dict]]:
    if not isinstance(payload, dict):
        raise ValueError("Pocketpills BambooHR listing response is not an object")
    meta = payload.get("meta")
    result = payload.get("result")
    if not isinstance(meta, dict) or not isinstance(meta.get("totalCount"), int) or meta["totalCount"] < 0:
        raise ValueError("Pocketpills BambooHR listing response has no non-negative integer meta.totalCount")
    if isinstance(result, list):
        rows = list(result)
    elif isinstance(result, dict):
        rows = list(result.values())
    else:
        raise ValueError("Pocketpills BambooHR listing response result is not a list or object")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Pocketpills BambooHR listing contains a non-object row")
    return meta["totalCount"], rows


def parse_listing(payload, response_url: str = BAMBOOHR_LIST_API) -> dict:
    """Validate the exact unfiltered company-scoped BambooHR listing payload."""
    parsed = urlsplit(response_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "pocketpills.bamboohr.com"
        or not LIST_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("Pocketpills BambooHR listing request left its exact tenant route")
    total, rows = _listing_rows(payload)
    if total == 0 and rows:
        raise ValueError("Pocketpills BambooHR listing reports zero total but returns rows")
    if total > 0 and len(rows) == 0:
        raise ValueError("Pocketpills BambooHR listing reports a positive total but returns no rows")
    if len(rows) > total:
        raise ValueError("Pocketpills BambooHR listing returns more rows than meta.totalCount")
    seen: set[str] = set()
    normalized = []
    for row in rows:
        identity = str(row.get("id") or "").strip()
        title = normalized_title(row.get("jobOpeningName"))
        if not re.fullmatch(r"\d+", identity):
            raise ValueError("Pocketpills BambooHR listing row has no numeric id")
        if identity in seen:
            raise ValueError(f"Pocketpills BambooHR listing repeats job id {identity}")
        if not title:
            raise ValueError(f"Pocketpills BambooHR listing row {identity} has no jobOpeningName")
        seen.add(identity)
        normalized.append(dict(row))
    if len(rows) != total:
        # BambooHR's listing endpoint returns all public rows in one response;
        # a mismatch means pagination/visibility changed and must be reviewed.
        raise ValueError(
            f"Pocketpills BambooHR listing row count {len(rows)} does not equal meta.totalCount {total}"
        )
    return {
        "total": total,
        "rows": normalized,
        "job_ids": [str(row["id"]) for row in normalized],
        "zero_vacancy": total == 0 and not normalized,
    }


def job_page_url(identity: str) -> str:
    if not re.fullmatch(r"\d+", str(identity or "")):
        raise ValueError("Pocketpills BambooHR job id must be numeric")
    return f"{BAMBOOHR_CAREERS}/{identity}"


def detail_api_url(identity: str) -> str:
    if not re.fullmatch(r"\d+", str(identity or "")):
        raise ValueError("Pocketpills BambooHR job id must be numeric")
    return f"{BAMBOOHR_CAREERS}/{identity}/detail"


def _location_values(job_opening: dict) -> list[dict]:
    found = []
    for key in ("location", "atsLocation"):
        value = job_opening.get(key)
        if not isinstance(value, dict):
            continue
        record = {
            "city": normalized_space(value.get("city")) or None,
            "state": normalized_space(value.get("state")) or None,
            "postal_code": normalized_space(value.get("postalCode")) or None,
            "country": normalized_space(value.get("addressCountry")) or None,
        }
        if any(record.values()) and record not in found:
            found.append(record)
    return found


def _format_location(value: dict) -> str:
    parts = [value.get("city"), value.get("state"), value.get("country")]
    return ", ".join(part for part in parts if part)


def _detail_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(f'<div data-pocketpills-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-pocketpills-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections: list[dict] = []
    current = None
    for block in blocks:
        heading = ""
        if block.name in HEADING_TAGS:
            heading = normalized_space(block.get_text(" ", strip=True))
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
    return sections


def _attachment_links(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    return [
        {"url": anchor.get("href"), "label": normalized_space(anchor.get_text(" ", strip=True)) or None}
        for anchor in soup.select("a[href]")
        if ATTACHMENT_SUFFIX.search(anchor.get("href") or "")
    ]


def parse_detail(payload, row: dict, response_url: str) -> dict:
    """Validate a BambooHR ``/careers/{id}/detail`` response against its listing row."""
    parsed_url = urlsplit(response_url)
    match = DETAIL_PATH.fullmatch(parsed_url.path)
    if parsed_url.scheme != "https" or parsed_url.netloc != "pocketpills.bamboohr.com" or not match:
        raise ValueError("Pocketpills BambooHR detail request left its exact tenant route")
    identity = str(row["id"])
    if match.group(1) != identity:
        raise ValueError("Pocketpills BambooHR detail id does not match its listing row")
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise ValueError("Pocketpills BambooHR detail response has no result object")
    job_opening = payload["result"].get("jobOpening")
    if not isinstance(job_opening, dict):
        raise ValueError("Pocketpills BambooHR detail response has no result.jobOpening")
    title = normalized_title(job_opening.get("jobOpeningName"))
    if title != normalized_title(row.get("jobOpeningName")):
        raise ValueError("Pocketpills BambooHR detail title does not match its listing row")
    share_url = normalized_space(job_opening.get("jobOpeningShareUrl"))
    if share_url and not _same_url(share_url.split("?", 1)[0], job_page_url(identity)):
        raise ValueError("Pocketpills BambooHR detail share URL changed")
    description = job_opening.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Pocketpills BambooHR detail has no employer-authored description")
    body_text = text_html(description)
    if len(body_text.strip()) < 100:
        raise ValueError("Pocketpills BambooHR detail description is implausibly short")
    return {
        "job_opening": job_opening,
        "title": title,
        "description_html": description,
        "description_text": body_text,
        "normalized_body": normalized_body_text(description),
        "job_page_url": share_url or job_page_url(identity),
        "location_values": _location_values(job_opening),
        "sections": _detail_sections(description),
        "attachments": _attachment_links(description),
        "raw_status": job_opening.get("jobOpeningStatus"),
        "date_posted_raw": job_opening.get("datePosted"),
    }


def _date_value(value):
    """Keep only an exact employer-published date/timestamp; never synthesize one."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    try:
        from datetime import datetime
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def build_listing_job(adapter: Adapter, row: dict):
    identity = str(row["id"])
    url = job_page_url(identity)
    job = adapter.job(
        {"listing_row": dict(row)},
        title=normalized_title(row.get("jobOpeningName")),
        url=url,
        job_id=identity,
        requisition_id=None,
        apply_url=url,
        description="",
        method=None,
        description_url=None,
        locations=_location_values(row),
        department=normalized_space(row.get("departmentLabel")) or None,
        employment_type=normalized_space(row.get("employmentStatusLabel")) or None,
        workplace_type=LOCATION_TYPE_LABEL.get(str(row.get("locationType"))) if row.get("locationType") is not None else None,
        employer=EMPLOYER_NAME,
        language="en",
    )
    job.raw["detail_status"] = "pending"
    return job


def build_detail_job(adapter: Adapter, listing_job, detail: dict):
    opening = detail["job_opening"]
    location_values = detail["location_values"]
    locations = [_format_location(value) for value in location_values if _format_location(value)]
    sections = detail["sections"]
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
    warnings = []
    if str(opening.get("jobOpeningStatus") or "Open").lower() != "open":
        warnings.append("pocketpills_detail_status_not_open")
    if detail["attachments"]:
        warnings.append("pocketpills_role_attachment_requires_review")
    job = adapter.job(
        {
            "listing_row": listing_job.raw.get("listing_row", {}),
            "detail": opening,
            "detail_response_url": detail_api_url(listing_job.job_id),
            "description_sections": sections,
            "description_links": _attachment_links(detail["description_html"]),
            "role_attachments": detail["attachments"],
            "location_values": location_values,
            "location_type": opening.get("locationType"),
            "job_opening_status": opening.get("jobOpeningStatus"),
            "date_posted_raw": detail["date_posted_raw"],
            "compensation_raw": opening.get("compensation"),
            "minimum_experience_raw": opening.get("minimumExperience"),
        },
        title=detail["title"],
        url=detail["job_page_url"],
        job_id=listing_job.job_id,
        requisition_id=None,
        apply_url=detail["job_page_url"],
        description=detail["description_html"],
        method="pocketpills.bamboohr.careers_detail.jobOpening.description",
        description_url=detail_api_url(listing_job.job_id),
        locations=locations,
        department=normalized_space(opening.get("departmentLabel")) or listing_job.department,
        employment_type=normalized_space(opening.get("employmentStatusLabel")) or listing_job.employment_type,
        workplace_type=LOCATION_TYPE_LABEL.get(str(opening.get("locationType")))
        if opening.get("locationType") is not None else listing_job.workplace_type,
        date_posted=_date_value(opening.get("datePosted")),
        date_posted_raw=opening.get("datePosted"),
        date_updated=None,
        valid_through=None,
        salary=opening.get("compensation"),
        experience=opening.get("minimumExperience"),
        employer=EMPLOYER_NAME,
        language="en",
        responsibilities=responsibilities or None,
        qualifications=qualifications or None,
        warnings=warnings,
    )
    job.description_text = detail["description_text"]
    job.description_sha256 = sha256(detail["description_text"].encode()).hexdigest()
    return job


class Pocketpills(Adapter):
    """Follow Pocketpills' official careers page into its exact BambooHR tenant."""

    VERIFICATION_REVISION = "pocketpills.official-careers-bamboohr.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.result.metadata["journey"] = []
        self.result.metadata["failed_sources"] = []

    def _failed_source(self, url: str, reason: str):
        self.result.metadata.setdefault("failed_sources", []).append({"url": url, "reason": reason})

    def _record(self, *, url: str, method: str, response, seconds: float, label: str, body=None):
        record = {
            "label": label,
            "url": url,
            "method": method,
            "final_url": str(response.url),
            "status": response.status_code,
            "seconds": round(max(0.0, seconds), 6),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "redirects": [
                {"status": item.status_code, "url": str(item.url), "location": item.headers.get("location")}
                for item in response.history
            ],
            "request_body": body,
        }
        self.result.metadata["journey"].append(record)
        return record

    async def _fetch(self, url: str, *, method: str = "GET", label: str, body=None):
        started = time.perf_counter()
        try:
            response = await self.http.request(method, url, json=body)
            seconds = max(0.0, time.perf_counter() - started)
            self._record(url=url, method=method, response=response, seconds=seconds, label=label, body=body)
            return response
        except FetchError as exc:
            seconds = max(0.0, time.perf_counter() - started)
            self.result.metadata["journey"].append({
                "label": label,
                "url": url,
                "method": method,
                "status": None,
                "error": str(exc),
                "seconds": round(seconds, 6),
                "request_body": body,
            })
            self._failed_source(url, str(exc))
            self.result.errors.append(f"pocketpills_{label}_request_failed: {exc}")
            return None

    async def _official_entry(self, label: str):
        response = await self._fetch(OFFICIAL_ENTRY, label=label)
        if response is None:
            return None, None
        try:
            parsed = parse_official_entry(response.text, str(response.url))
        except ValueError as exc:
            self.result.errors.append(f"pocketpills_official_entry_changed: {exc}")
            return None, response
        return parsed, response

    async def _shell(self, label: str):
        response = await self._fetch(BAMBOOHR_CAREERS, label=label)
        if response is None:
            return None, None
        try:
            parsed = parse_bamboohr_shell(response.text, str(response.url))
        except ValueError as exc:
            self.result.errors.append(f"pocketpills_bamboohr_shell_changed: {exc}")
            return None, response
        return parsed, response

    async def _company_info(self, label: str):
        response = await self._fetch(BAMBOOHR_COMPANY_INFO_API, label=label)
        if response is None:
            return None, None
        try:
            parsed = parse_company_info(response.json(), str(response.url))
        except (ValueError, json.JSONDecodeError) as exc:
            self.result.errors.append(f"pocketpills_bamboohr_company_info_changed: {exc}")
            return None, response
        return parsed, response

    async def _listing(self, label: str):
        response = await self._fetch(BAMBOOHR_LIST_API, label=label)
        if response is None:
            return None
        try:
            parsed = parse_listing(response.json(), str(response.url))
        except (ValueError, json.JSONDecodeError) as exc:
            self.result.errors.append(f"pocketpills_bamboohr_listing_changed: {exc}")
            return None
        self.result.metadata[f"{label}_listing"] = {
            "url": BAMBOOHR_LIST_API,
            "total": parsed["total"],
            "job_ids": parsed["job_ids"],
            "rows": len(parsed["rows"]),
            "response_sha256": sha256(response.content).hexdigest(),
        }
        return parsed

    async def _detail(self, job):
        url = detail_api_url(job.job_id)
        response = await self._fetch(url, label=f"detail_{job.job_id}")
        if response is None:
            raise ValueError(f"Pocketpills detail request failed for {job.job_id}")
        try:
            parsed = parse_detail(response.json(), job.raw["listing_row"], str(response.url))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"Pocketpills detail changed for {job.job_id}: {exc}") from exc
        return build_detail_job(self, job, parsed)

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": BAMBOOHR_CAREERS,
            "listing_api_url": BAMBOOHR_LIST_API,
            "company_info_api_url": BAMBOOHR_COMPANY_INFO_API,
            "legal_employer": EMPLOYER_NAME,
            "bamboohr_account_id": ACCOUNT_ID,
            "listing_authority": (
                "Pocketpills' official https://www.pocketpills.com/careers page publishes exactly one "
                "jobs CTA ('View current openings') to its exact company BambooHR tenant "
                "https://pocketpills.bamboohr.com/careers. The tenant's own unfiltered /careers/list "
                "request is the membership authority."
            ),
            "listing_request": {
                "method": "GET",
                "url": BAMBOOHR_LIST_API,
                "query_shape": {},
                "filters_applied": False,
                "pagination": "One response returns meta.totalCount and every public row; no page parameter.",
                "scope": "Only pocketpills.bamboohr.com account 419073; no other BambooHR tenant.",
            },
            "detail_method": (
                "GET /careers/{id}/detail for actual listing rows only; its result.jobOpening.description "
                "is the employer-authored full HTML. The live snapshot has zero rows, so no detail request "
                "is made and no description is invented."
            ),
            "request_scope": (
                "Only https://www.pocketpills.com/careers and the exact pocketpills.bamboohr.com tenant "
                "requests exposed by that published journey."
            ),
        })
        if self.company.careers_url != OFFICIAL_ENTRY:
            raise ValueError("Pocketpills dedicated flow must start from its official HTTPS careers entry")

        initial_entry, _ = await self._official_entry("initial_official_entry")
        initial_shell, _ = await self._shell("initial_bamboohr_shell")
        initial_info, _ = await self._company_info("initial_company_info")
        initial = await self._listing("initial")
        if initial_entry is None or initial_shell is None or initial_info is None or initial is None:
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.metadata["initial_official_entry"] = initial_entry
        self.result.metadata["initial_bamboohr_shell"] = initial_shell
        self.result.metadata["initial_company_info"] = initial_info
        self.result.expected_total = initial["total"]
        self.result.pages = 1
        self.result.metadata["membership_authority_total"] = initial["total"]

        jobs = []
        if initial["rows"]:
            listing_jobs = [build_listing_job(self, row) for row in initial["rows"]]
            for job in listing_jobs:
                self.add(job)
            jobs = await self.hydrate(listing_jobs, self._detail)
            self.result.jobs = jobs
        else:
            self.result.jobs = []
            self.result.timings["detail_phase_seconds"] = 0.0
            self.result.metadata["details"] = {
                "total": 0,
                "checked": 0,
                "fetched_jobs": 0,
                "verified_cache_hits": 0,
                "unavailable_cache_hits": 0,
                "failed": 0,
                "duration_seconds": 0.0,
            }

        final_entry, _ = await self._official_entry("final_official_entry")
        final_shell, _ = await self._shell("final_bamboohr_shell")
        final_info, _ = await self._company_info("final_company_info")
        final = await self._listing("final")
        final_available = all(item is not None for item in (final_entry, final_shell, final_info, final))
        final_matches = False
        changed_fields_by_job_id = {}
        new_ids: list[str] = []
        removed_ids: list[str] = []
        if final_available:
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_bamboohr_shell"] = final_shell
            self.result.metadata["final_company_info"] = final_info
            initial_by_id = {str(row["id"]): row for row in initial["rows"]}
            final_by_id = {str(row["id"]): row for row in final["rows"]}
            for identity in initial_by_id:
                if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]:
                    changed_fields_by_job_id[identity] = sorted(
                        key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                    )
            new_ids = [identity for identity in final["job_ids"] if identity not in initial_by_id]
            removed_ids = [identity for identity in initial["job_ids"] if identity not in final_by_id]
            entry_same = _same_url(initial_entry["careers_link"], final_entry["careers_link"])
            shell_same = (
                initial_shell["account_id"] == final_shell["account_id"]
                and initial_shell["logo_url"] == final_shell["logo_url"]
                and initial_shell["og_url"] == final_shell["og_url"]
            )
            info_same = (
                initial_info["id"] == final_info["id"]
                and initial_info["career_share_url"] == final_info["career_share_url"]
            )
            counts_same = initial["total"] == final["total"]
            ids_same = initial["job_ids"] == final["job_ids"]
            final_matches = (
                entry_same
                and shell_same
                and info_same
                and counts_same
                and ids_same
                and not changed_fields_by_job_id
                and not new_ids
                and not removed_ids
                and final["total"] == initial["total"]
            )
            if not entry_same:
                self.result.errors.append("pocketpills_official_entry_changed_during_collection")
            if not shell_same:
                self.result.errors.append("pocketpills_bamboohr_shell_changed_during_collection")
            if not info_same:
                self.result.errors.append("pocketpills_bamboohr_company_info_changed_during_collection")
            if not counts_same or not ids_same:
                self.result.errors.append("pocketpills_listing_membership_changed_during_collection")
            if changed_fields_by_job_id:
                self.result.errors.append("pocketpills_listing_fields_changed_during_collection")
            if new_ids or removed_ids:
                self.result.errors.append("pocketpills_listing_rows_changed_during_collection")
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["job_ids"],
                "final_ids": final["job_ids"],
                "collected_ids": [str(job.job_id) for job in jobs],
                "initial_total": initial["total"],
                "final_total": final["total"],
                "initial_final_id_order_matches": ids_same,
                "initial_final_total_matches": counts_same,
                "official_entry_still_exact": entry_same,
                "bamboohr_shell_still_exact": shell_same,
                "company_info_still_exact": info_same,
                "changed_fields_by_job_id": changed_fields_by_job_id,
                "new_job_ids": new_ids,
                "removed_job_ids": removed_ids,
                "zero_vacancy": initial["total"] == 0 and final["total"] == 0,
                "complete": final_matches,
            }
        else:
            self.result.errors.append("pocketpills_final_membership_recheck_failed")
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["job_ids"],
                "final_ids": None,
                "collected_ids": [str(job.job_id) for job in jobs],
                "initial_total": initial["total"],
                "final_total": None,
                "complete": False,
            }

        zero_vacancy = bool(
            initial["total"] == 0
            and final is not None
            and final["total"] == 0
            and final_matches
        )
        self.result.metadata["zero_vacancy"] = {
            "confirmed": zero_vacancy,
            "initial_total": initial["total"],
            "final_total": final["total"] if final is not None else None,
            "initial_rows": len(initial["rows"]),
            "final_rows": len(final["rows"]) if final is not None else None,
            "listing_calls": sum(
                record.get("label") in {"initial", "final"}
                and record.get("url") == BAMBOOHR_LIST_API
                for record in self.result.metadata["journey"]
            ),
            "note": (
                "The zero result is an authoritative HTTP 200 response from the exact "
                "company-published BambooHR listing, not an inaccessible or error response."
                if zero_vacancy else
                "A zero or non-zero membership was not certified by two matching snapshots."
            ),
        }
        if initial["rows"]:
            verified = sum(job.description_verified for job in jobs)
            self.result.listing_complete = bool(
                final_matches
                and len(jobs) == initial["total"]
                and verified == len(jobs)
            )
        else:
            self.result.listing_complete = bool(zero_vacancy)
        self.result.metadata["detail_hydration_mode"] = (
            "GET /careers/{id}/detail for actual listing rows; no detail request exists in the "
            "current zero-vacancy snapshot."
            if not initial["rows"] else
            "GET /careers/{id}/detail per exact listing row; result.jobOpening.description is retained."
        )
        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
