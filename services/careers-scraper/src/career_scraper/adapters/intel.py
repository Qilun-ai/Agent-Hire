"""Intel's official careers journey into its company-scoped Workday site.

Intel's ledger entry (``jobs.intel.com/en/search-jobs``) redirects through an
Intel redirector to the Intel Careers landing page on Workday tenant ``intel``,
site ``External``. The published **See all jobs** / **Search jobs** action opens
``https://intel.wd1.myworkdayjobs.com/External``. This adapter reads that exact
unfiltered listing and only the detail records for the job paths it exposes.
"""
from __future__ import annotations

from datetime import date
from hashlib import sha256
from html import unescape
from pathlib import PurePosixPath
import re
import time
import unicodedata
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..attachments import extract_attachment
from ..models import text_html


OFFICIAL_ENTRY = "https://jobs.intel.com/en/search-jobs"
WORKDAY_ORIGIN = "https://intel.wd1.myworkdayjobs.com"
WORKDAY_TENANT = "intel"
WORKDAY_SITE = "External"
WORKDAY_LANDING_URL = (
    WORKDAY_ORIGIN + "/External/page/6042070b79e01001f04fa9b468070000"
)
WORKDAY_LISTING_URL = WORKDAY_ORIGIN + "/External"
WORKDAY_API_BASE = f"{WORKDAY_ORIGIN}/wday/cxs/{WORKDAY_TENANT}/{WORKDAY_SITE}"
WORKDAY_JOBS_API = WORKDAY_API_BASE + "/jobs"
WORKDAY_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": WORKDAY_ORIGIN,
    "Referer": WORKDAY_LISTING_URL,
}
PAGE_SIZE = 20
WORKDAY_HOST = "intel.wd1.myworkdayjobs.com"
OFFICIAL_REDIRECT_HOST = "corpredirect.intel.com"
OFFICIAL_REDIRECT_PATH = "/Redirector/404Redirector.aspx"
REQUISITION_RE = re.compile(r"[A-Z]{1,5}\d{4,}")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
VERIFICATION_REVISION = "intel.official-workday-External-unfiltered-listing.1"


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def canonical_job_url(external_path: str) -> str:
    return f"{WORKDAY_ORIGIN}/en-US/{WORKDAY_SITE}{external_path}"


def canonical_apply_url(external_path: str) -> str:
    return canonical_job_url(external_path) + "/apply"


def _requisition_id(bullet_fields) -> str | None:
    if not isinstance(bullet_fields, list):
        return None
    for value in bullet_fields:
        if isinstance(value, str):
            match = REQUISITION_RE.fullmatch(value.strip())
            if match:
                return match.group(0)
    return None


def parse_official_entry_redirect(status: int, location: str | None) -> dict:
    """Validate the observed Intel-owned 301 into its redirector."""
    if status != 301 or not location:
        raise ValueError("Intel official entry no longer returns its inspected 301")
    raw = unescape(location)
    parsed = urlsplit(raw)
    if (parsed.scheme != "https" or parsed.hostname != OFFICIAL_REDIRECT_HOST
            or parsed.path != OFFICIAL_REDIRECT_PATH
            or f"404;{OFFICIAL_ENTRY}" not in raw):
        raise ValueError("Intel official entry redirect changed or left its observed redirector")
    return {
        "status": status,
        "location": location,
        "host": parsed.hostname,
        "path": parsed.path,
        "query": parsed.query,
    }


def parse_workday_shell(html: str, final_url: str) -> dict:
    """Read the Intel Careers identity from a Workday page shell."""
    parsed = urlsplit(final_url)
    if (parsed.scheme != "https" or parsed.hostname != WORKDAY_HOST
            or not (parsed.path == "/External"
                    or parsed.path.startswith("/External/page/"))):
        raise ValueError("Intel Workday shell response came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    meta = {}
    for node in soup.select("meta[property],meta[name]"):
        key = node.get("property") or node.get("name")
        if key:
            meta[key] = node.get("content", "")
    tenant = re.search(r'\btenant\s*:\s*["\']([^"\']+)["\']', html)
    site = re.search(r'\bsiteId\s*:\s*["\']([^"\']+)["\']', html)
    locale = re.search(r'\brequestLocale\s*:\s*["\']([^"\']+)["\']', html)
    identity = {
        "url": final_url,
        "host": parsed.hostname,
        "path": parsed.path,
        "og_title": meta.get("og:title"),
        "og_description": meta.get("og:description"),
        "og_url": meta.get("og:url"),
        "tenant": tenant.group(1) if tenant else None,
        "site_id": site.group(1) if site else None,
        "request_locale": locale.group(1) if locale else None,
    }
    if (identity["og_title"] != "Intel Careers"
            or "official careers website" not in (identity["og_description"] or "").lower()
            or identity["tenant"] != WORKDAY_TENANT
            or identity["site_id"] != WORKDAY_SITE):
        raise ValueError("Intel Workday shell identity changed")
    if identity["og_url"] and urlsplit(identity["og_url"]).hostname != WORKDAY_HOST:
        raise ValueError("Intel Workday shell og:url left the company host")
    return identity


def parse_listing_page(value: dict, requested_offset: int) -> dict:
    """Validate one unfiltered Workday CXS listing page."""
    if not isinstance(value, dict):
        raise ValueError("Intel Workday jobs response is not an object")
    rows = value.get("jobPostings")
    if not isinstance(rows, list):
        raise ValueError("Intel Workday jobs response is missing jobPostings")
    if len(rows) > PAGE_SIZE:
        raise ValueError("Intel Workday jobs response exceeds its observed page size")
    total = value.get("total")
    if requested_offset == 0:
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ValueError("Intel Workday first listing page is missing a valid total")
    elif total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
        raise ValueError("Intel Workday listing page has an invalid total")

    issues: list[str] = []
    parsed_rows = []
    seen_paths: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("intel_listing_row_not_object")
            continue
        title = normalized_title(row.get("title"))
        path = row.get("externalPath")
        if not title:
            issues.append("intel_listing_row_missing_title")
            continue
        if not isinstance(path, str) or not path.startswith("/job/") or "?" in path or "#" in path:
            issues.append(f"intel_listing_row_missing_external_path:{title}")
            continue
        if path in seen_paths:
            issues.append(f"intel_listing_duplicate_external_path:{path}")
            continue
        seen_paths.add(path)
        locations_text = row.get("locationsText")
        posted_on = row.get("postedOn")
        bullet_fields = row.get("bulletFields")
        if not isinstance(bullet_fields, list):
            issues.append(f"intel_listing_row_missing_bullet_fields:{path}")
            bullet_fields = []
        req_id = _requisition_id(bullet_fields)
        if req_id is None:
            issues.append(f"intel_listing_row_missing_requisition:{path}")
        path_posting_id = unquote(PurePosixPath(path).name)
        if req_id and req_id not in path_posting_id:
            issues.append(f"intel_listing_requisition_path_mismatch:{req_id}:{path}")
        parsed_rows.append({
            "title": title,
            "externalPath": path,
            "locationsText": locations_text,
            "postedOn": posted_on,
            "bulletFields": bullet_fields,
            "requisition_id": req_id,
            "path_posting_id": path_posting_id,
            "url": canonical_job_url(path),
            "apply_url": canonical_apply_url(path),
        })
    return {
        "total": total,
        "rows": parsed_rows,
        "facets": value.get("facets") if isinstance(value.get("facets"), list) else [],
        "userAuthenticated": value.get("userAuthenticated"),
        "issues": issues,
    }


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-intel-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-intel-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections: list[dict] = []
    current: dict | None = None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = normalized_title(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong", recursive=False) or block.find("b", recursive=False)
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 160:
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
    else:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"]:
            section["text"] = text_html(section["html"])
    return sections


def parse_detail(value: dict, expected_row: dict, requested_url: str) -> dict:
    """Validate a full Workday detail payload against its exact listing row."""
    if not isinstance(value, dict):
        raise ValueError("Intel Workday detail response is not an object")
    info = value.get("jobPostingInfo")
    if not isinstance(info, dict):
        raise ValueError("Intel Workday detail response is missing jobPostingInfo")
    issues: list[str] = []
    path = expected_row["externalPath"]
    if urlsplit(requested_url).hostname != WORKDAY_HOST:
        raise ValueError("Intel Workday detail request left the company host")
    if not requested_url.endswith(WORKDAY_API_BASE + path):
        raise ValueError("Intel Workday detail URL is not the exact exposed job path")

    title = normalized_title(info.get("title"))
    expected_title = normalized_title(expected_row.get("title"))
    if not title:
        issues.append("intel_detail_missing_title")
    if expected_title and title != expected_title:
        issues.append(f"intel_detail_title_mismatch:{title}:{expected_title}")
    req_id = info.get("jobReqId")
    expected_req = expected_row.get("requisition_id")
    if expected_req and req_id != expected_req:
        issues.append(f"intel_detail_requisition_mismatch:{req_id}:{expected_req}")
    if not isinstance(req_id, str) or not REQUISITION_RE.fullmatch(req_id):
        issues.append("intel_detail_missing_public_requisition")
    posting_id = info.get("jobPostingId")
    if posting_id != expected_row.get("path_posting_id"):
        issues.append(f"intel_detail_posting_id_mismatch:{posting_id}:{expected_row.get('path_posting_id')}")
    if not info.get("id"):
        issues.append("intel_detail_missing_workday_posting_id")
    if info.get("jobPostingSiteId") != WORKDAY_SITE:
        issues.append(f"intel_detail_wrong_site:{info.get('jobPostingSiteId')}")
    if info.get("posted") is not True:
        issues.append("intel_detail_not_posted")
    canonical = WORKDAY_ORIGIN + f"/{WORKDAY_SITE}" + path
    if info.get("externalUrl") != canonical:
        issues.append(f"intel_detail_external_url_mismatch:{info.get('externalUrl')}")
    description = info.get("jobDescription")
    if not isinstance(description, str) or not description.strip():
        issues.append("intel_detail_description_missing")
        description = description if isinstance(description, str) else ""
    description_text = normalized_body_text(description)
    if len(description_text) < 100:
        issues.append("intel_detail_description_short")

    start_date = info.get("startDate")
    date_posted = None
    if start_date:
        try:
            date.fromisoformat(start_date)
            date_posted = start_date
        except (TypeError, ValueError):
            issues.append(f"intel_detail_start_date_invalid:{start_date}")

    location = info.get("location")
    if not isinstance(location, str) or not location.strip():
        issues.append("intel_detail_missing_location")
        location = None
    additional = info.get("additionalLocations")
    if additional is None:
        additional = []
    if not isinstance(additional, list) or any(not isinstance(item, str) for item in additional):
        issues.append("intel_detail_bad_additional_locations")
        additional = []
    locations = ([location] if location else []) + additional
    if len(set(locations)) != len(locations):
        issues.append("intel_detail_duplicate_locations")

    description_soup = BeautifulSoup(description, "lxml")
    links = [
        {"text": anchor.get_text(" ", strip=True), "url": urljoin(requested_url, anchor.get("href", ""))}
        for anchor in description_soup.select("a[href]")
    ]
    attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
    employer = value.get("hiringOrganization")
    if isinstance(employer, dict):
        employer = employer.get("name")
    return {
        "url": requested_url,
        "title": title,
        "requisition_id": req_id,
        "posting_id": posting_id,
        "workday_posting_id": info.get("id"),
        "job_posting_site_id": info.get("jobPostingSiteId"),
        "posted": info.get("posted"),
        "can_apply": info.get("canApply"),
        "external_url": info.get("externalUrl"),
        "job_description": description,
        "description_text": description_text,
        "description_normalized_chars": len(description_text),
        "description_fnv1a64": fnv1a64(description_text),
        "description_sections": description_sections(description),
        "description_links": links,
        "role_attachments": attachments,
        "date_posted": date_posted,
        "posted_on": info.get("postedOn"),
        "location": location,
        "additional_locations": additional,
        "locations": locations,
        "time_type": info.get("timeType"),
        "country": info.get("country"),
        "job_requisition_location": info.get("jobRequisitionLocation"),
        "employer": employer,
        "language": "en-US",
        "issues": issues,
    }


class Intel(Adapter):
    """Read Intel's exact Workday listing and exact listed-job detail records."""

    VERIFICATION_REVISION = VERIFICATION_REVISION

    def __init__(self, http, company, source, options, *, journey_reference: dict | None = None):
        super().__init__(http, company, source, options)
        self.journey_reference = journey_reference or {}
        self._detail_request_seconds = 0.0
        self._detail_parse_seconds = 0.0
        self._attachment_seconds = 0.0

    def _browser_chain_verified(self) -> bool:
        chain = self.journey_reference.get("entry_redirect_chain")
        if not isinstance(chain, list) or not chain:
            return False
        first = chain[0] if isinstance(chain[0], dict) else {}
        last = chain[-1] if isinstance(chain[-1], dict) else {}
        redirector_seen = any(
            isinstance(item, dict)
            and str(item.get("url", "")).startswith("https://corpredirect.intel.com/")
            for item in chain
        )
        return (
            first.get("url") == OFFICIAL_ENTRY
            and int(first.get("status") or 0) in (301, 302, 307, 308)
            and redirector_seen
            and last.get("url") == WORKDAY_LANDING_URL
            and int(last.get("status") or 0) == 200
        )

    async def _official_entry(self) -> dict:
        started = time.perf_counter()
        observation = {
            "url": OFFICIAL_ENTRY,
            "method": "GET",
            "followed_redirects": True,
            "browser_redirect_chain_verified": self._browser_chain_verified(),
        }
        try:
            response = await self.http.get(
                OFFICIAL_ENTRY,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            )
            history = [
                {
                    "status": item.status_code,
                    "url": str(item.url),
                    "location": item.headers.get("location"),
                }
                for item in response.history
            ]
            history.append({
                "status": response.status_code,
                "url": str(response.url),
                "location": None,
            })
            observation.update({
                "status": response.status_code,
                "final_url": str(response.url),
                "redirect_chain": history,
                "live_redirect_verified": (
                    str(response.url) == WORKDAY_LANDING_URL
                    and any(
                        "corpredirect.intel.com" in (item.get("url") or "")
                        or "corpredirect.intel.com" in (item.get("location") or "")
                        for item in history
                    )
                ),
                "response_sha256": sha256(response.content).hexdigest(),
                "response_bytes": len(response.content),
                "blocked_by_waf": False,
            })
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            observation.update({
                "status": 403 if "403" in message else None,
                "error": message,
                "blocked_by_waf": "403" in message or "Access Denied" in message,
            })
            self.result.warnings.append("intel_official_entry_http_blocked_by_waf")
        observation["seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        saved = self.journey_reference.get("plain_http_redirector")
        if isinstance(saved, dict):
            observation["plain_http_redirector_reused_evidence"] = saved
        return observation

    async def _shell(self, url: str) -> dict:
        started = time.perf_counter()
        response = await self.http.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        identity = parse_workday_shell(response.text, str(response.url))
        identity.update({
            "status": response.status_code,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        })
        return identity

    async def _listing_snapshot(self, *, reconcile: bool) -> dict:
        started = time.perf_counter()
        offset = 0
        rows: list[dict] = []
        pages: list[dict] = []
        issues: list[str] = []
        seen_paths: set[str] = set()
        total: int | None = None
        while True:
            if len(pages) >= self.options.max_pages:
                issues.append("intel_listing_page_budget_exhausted")
                break
            if total is not None and len(rows) >= total:
                break
            request_url = WORKDAY_JOBS_API
            payload = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
            request_started = time.perf_counter()
            response = await self.http.request(
                "POST", request_url, json=payload, headers=WORKDAY_HEADERS,
            )
            request_seconds = max(0.0, time.perf_counter() - request_started)
            parsed_url = urlsplit(str(response.url))
            if (parsed_url.scheme != "https" or parsed_url.hostname != WORKDAY_HOST
                    or parsed_url.path != f"/wday/cxs/{WORKDAY_TENANT}/{WORKDAY_SITE}/jobs"):
                raise ValueError("Intel Workday listing redirected outside its company-scoped route")
            page = parse_listing_page(response.json(), offset)
            issues.extend(page["issues"])
            if total is None:
                total = page["total"]
                if total is None or total <= 0:
                    issues.append("intel_listing_reported_zero_or_missing_jobs")
            elif page["total"] not in (0, total):
                issues.append(f"intel_listing_total_changed:{total}->{page['total']}")
            if not page["rows"]:
                issues.append(f"intel_listing_empty_page_at_offset:{offset}")
                break
            new_count = 0
            for row in page["rows"]:
                path = row["externalPath"]
                if path in seen_paths:
                    issues.append(f"intel_listing_duplicate_across_pages:{path}")
                    continue
                seen_paths.add(path)
                new_count += 1
                rows.append(row)
                if not reconcile:
                    job = self.job(
                        {"listing": row, "listing_page_offset": offset},
                        title=row["title"],
                        url=row["url"],
                        job_id=row["requisition_id"] or path,
                        apply_url=row["apply_url"],
                        locations=[row["locationsText"]] if row["locationsText"] else [],
                        date_posted_raw=row["postedOn"],
                    )
                    self.add(job)
                    if self.limit():
                        issues.append("intel_sample_limit")
                        break
            pages.append({
                "offset": offset,
                "request_url": request_url,
                "request_body": payload,
                "final_url": str(response.url),
                "status": response.status_code,
                "request_seconds": round(request_seconds, 6),
                "response_sha256": sha256(response.content).hexdigest(),
                "response_bytes": len(response.content),
                "total": page["total"],
                "rows": len(page["rows"]),
                "new_rows": new_count,
            })
            if "intel_sample_limit" in issues:
                break
            if total is not None and len(rows) > total:
                issues.append(f"intel_listing_exceeded_total:{len(rows)}/{total}")
                break
            if not page["rows"]:
                break
            offset += len(page["rows"])
            if total is not None and offset >= total:
                break
        if total is not None and len(rows) != total:
            issues.append(f"intel_listing_membership_count:{len(rows)}/{total}")
        if not reconcile:
            self.result.expected_total = total
            self.result.pages = len(pages)
            self.result.listing_complete = bool(
                not issues and total is not None and total > 0 and len(rows) == total
            )
        return {
            "total": total,
            "rows": rows,
            "paths": [row["externalPath"] for row in rows],
            "requisitions": [row["requisition_id"] for row in rows if row["requisition_id"]],
            "pages": pages,
            "issues": list(dict.fromkeys(issues)),
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    async def _detail(self, job):
        row = job.raw["listing"]
        path = row["externalPath"]
        detail_url = WORKDAY_API_BASE + path
        request_started = time.perf_counter()
        response = await self.http.get(detail_url, headers=WORKDAY_HEADERS)
        self._detail_request_seconds += max(0.0, time.perf_counter() - request_started)
        parsed_url = urlsplit(str(response.url))
        if (parsed_url.scheme != "https" or parsed_url.hostname != WORKDAY_HOST
                or parsed_url.path != f"/wday/cxs/{WORKDAY_TENANT}/{WORKDAY_SITE}{path}"):
            raise ValueError("Intel Workday detail redirected outside its exact company job route")
        parse_started = time.perf_counter()
        parsed = parse_detail(response.json(), row, str(response.url))
        self._detail_parse_seconds += max(0.0, time.perf_counter() - parse_started)
        if parsed["issues"]:
            for issue in parsed["issues"]:
                self.result.errors.append(f"{issue}:{path}")
            job.warnings.append("intel_exact_detail_identity_unverified")
            job.description_unavailable = True
            job.description_verified = False
        job.raw["detail"] = {
            **{key: value for key, value in parsed.items() if key not in {
                "job_description", "description_text", "description_sections",
                "description_links", "role_attachments", "issues",
            }},
            "detail_api_url": detail_url,
            "job_description": parsed["job_description"],
            "description_sections": parsed["description_sections"],
            "description_links": parsed["description_links"],
            "role_attachments": parsed["role_attachments"],
            "detail_issues": parsed["issues"],
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
        }
        job.title = parsed["title"] or job.title
        job.job_id = parsed["requisition_id"] or job.job_id
        job.requisition_id = parsed["requisition_id"]
        job.description_html = parsed["job_description"]
        job.description_method = "workday.jobPostingInfo.jobDescription"
        job.description_source_url = detail_url
        job.apply_url = canonical_apply_url(path)
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = {"postedOn": parsed["posted_on"], "startDate": parsed["date_posted"]}
        job.date_updated = None
        job.valid_through = None
        job.locations = parsed["locations"]
        job.employment_type = parsed["time_type"]
        job.workplace_type = None
        job.department = None
        job.employer = parsed["employer"]
        job.language = parsed["language"]
        job.salary = None
        sections = parsed["description_sections"]
        job.responsibilities = [
            section for section in sections
            if section.get("heading") and any(
                token in section["heading"].lower()
                for token in ("responsibilit", "what you will do", "what you'll do",
                              "the role", "role and impact", "key responsibilities")
            )
        ] or None
        job.qualifications = [
            section for section in sections
            if section.get("heading") and any(
                token in section["heading"].lower()
                for token in ("qualif", "requirement", "minimum qualifications",
                              "preferred qualifications", "what you will need",
                              "what you'll need")
            )
        ] or None
        return job

    async def _fetch_role_attachments(self) -> None:
        started = time.perf_counter()
        supported = {".pdf", ".doc", ".docx", ".rtf", ".xls", ".xlsx"}
        for job in self.result.jobs:
            detail = job.raw.get("detail", {}) if isinstance(job.raw, dict) else {}
            links = detail.get("role_attachments", []) if isinstance(detail, dict) else []
            attachments = []
            seen_urls: set[str] = set()
            for link in links:
                url = link.get("url") if isinstance(link, dict) else None
                if not isinstance(url, str) or url in seen_urls:
                    continue
                seen_urls.add(url)
                suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
                if suffix not in supported:
                    continue
                try:
                    response = await self.http.get(url)
                    extracted = extract_attachment(
                        response.content, PurePosixPath(urlsplit(url).path).name,
                    )
                    attachment = {
                        "url": url,
                        "label": link.get("text"),
                        "source_field": "jobPostingInfo.jobDescription",
                        "content_type": response.headers.get("content-type"),
                        **extracted,
                    }
                    attachments.append(attachment)
                    job.description_html += "\n<pre>" + extracted.get("text", "") + "</pre>"
                except Exception as exc:
                    identity = job.requisition_id or job.job_id or job.url
                    self.result.errors.append(
                        f"intel_attachment_fetch_failed:{identity}:{url}:{type(exc).__name__}:{exc}"
                    )
                    job.warnings.append("intel_role_attachment_unverified")
                    job.description_unavailable = True
                    job.description_verified = False
            if attachments:
                job.raw["detail"]["role_attachments"] = attachments
                job.description_text = text_html(job.description_html)
        self._attachment_seconds += max(0.0, time.perf_counter() - started)

    async def run(self):
        started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": WORKDAY_LISTING_URL,
            "workday_landing_url": WORKDAY_LANDING_URL,
            "workday_tenant": WORKDAY_TENANT,
            "workday_site": WORKDAY_SITE,
            "listing_authority": (
                "Intel's official careers entry redirects through Intel's redirector to "
                "the Intel Careers Workday landing page; its See all jobs/Search jobs "
                "action opens the company's unfiltered External Workday listing."
            ),
            "listing_membership_request": {
                "method": "POST",
                "url": WORKDAY_JOBS_API,
                "body_shape": {"appliedFacets": {}, "limit": PAGE_SIZE,
                               "offset": "UI page offset", "searchText": ""},
                "filters_applied": False,
                "scope": "Intel tenant intel, site External only; no other employer board is queried.",
            },
            "detail_method": (
                "GET /wday/cxs/intel/External/<exact externalPath>; full "
                "jobPostingInfo.jobDescription, checked against the rendered "
                "jobPostingDescription body for representative first/last jobs."
            ),
            "independent_rendered_reference": "companies/intel/browser-reference-worker.json",
            "browser_observed_entry_redirect_chain": self.journey_reference.get("entry_redirect_chain"),
        })

        entry = await self._official_entry()
        self.result.timings["official_entry_seconds"] = entry["seconds"]
        self.result.metadata["official_entry_http_observation"] = entry
        landing_started = time.perf_counter()
        landing = await self._shell(WORKDAY_LANDING_URL)
        listing_shell = await self._shell(WORKDAY_LISTING_URL)
        self.result.timings["workday_shell_seconds"] = round(
            max(0.0, time.perf_counter() - landing_started), 6)
        self.result.metadata["workday_landing_shell"] = landing
        self.result.metadata["workday_listing_shell"] = listing_shell

        initial = await self._listing_snapshot(reconcile=False)
        self.result.timings["initial_listing_seconds"] = initial["seconds"]
        self.result.metadata["initial_listing_pages"] = initial["pages"]
        self.result.metadata["initial_listing_issues"] = initial["issues"]
        if initial["issues"]:
            self.result.errors.extend(initial["issues"])
            self.result.listing_complete = False
        self.result.metadata["filters_observed"] = ["Location", "Time Type", "Job Category", "More"]

        jobs_before = len(self.result.jobs)
        hydrated = await self.hydrate(list(self.result.jobs), self._detail)
        self.result.jobs = hydrated
        self.result.timings["detail_request_seconds_sum"] = round(self._detail_request_seconds, 6)
        self.result.timings["detail_parse_seconds_sum"] = round(self._detail_parse_seconds, 6)
        await self._fetch_role_attachments()
        self.result.timings["role_attachment_seconds"] = round(self._attachment_seconds, 6)

        final_started = time.perf_counter()
        final = await self._listing_snapshot(reconcile=True)
        self.result.timings["final_listing_seconds"] = round(
            max(0.0, time.perf_counter() - final_started), 6)
        initial_paths = [job.raw["listing"]["externalPath"] for job in self.result.jobs]
        final_paths = final["paths"]
        initial_reqs = [job.requisition_id for job in self.result.jobs if job.requisition_id]
        final_reqs = final["requisitions"]
        same_paths = initial_paths == final_paths
        same_reqs = initial_reqs == final_reqs
        new_paths = sorted(set(final_paths) - set(initial_paths))
        removed_paths = sorted(set(initial_paths) - set(final_paths))
        reconciliation_errors = list(final["issues"])
        if final["total"] != self.result.expected_total:
            reconciliation_errors.append(
                f"intel_final_total_changed:{self.result.expected_total}->{final['total']}")
        if not same_paths:
            reconciliation_errors.append("intel_initial_final_membership_changed")
        if not same_reqs:
            reconciliation_errors.append("intel_initial_final_requisition_order_changed")
        if len(self.result.jobs) != self.result.expected_total:
            reconciliation_errors.append(
                f"intel_initial_listing_membership_count:{len(self.result.jobs)}/{self.result.expected_total}")
        if reconciliation_errors:
            self.result.errors.extend(reconciliation_errors)
            self.result.listing_complete = False
        else:
            self.result.listing_complete = True
        self.result.metadata["membership_reconciliation"] = {
            "initial_total": self.result.expected_total,
            "final_total": final["total"],
            "initial_rows": len(initial_paths),
            "final_rows": len(final_paths),
            "initial_ids": initial_reqs,
            "final_ids": final_reqs,
            "initial_external_paths": initial_paths,
            "final_external_paths": final_paths,
            "initial_external_paths_sha256": sha256("\n".join(initial_paths).encode()).hexdigest(),
            "final_external_paths_sha256": sha256("\n".join(final_paths).encode()).hexdigest(),
            "same_path_order": same_paths,
            "same_requisition_order": same_reqs,
            "new_paths": new_paths,
            "removed_paths": removed_paths,
            "complete": not reconciliation_errors,
            "errors": reconciliation_errors,
            "page_count": len(final["pages"]),
            "page_offsets": [page["offset"] for page in final["pages"]],
        }
        self.result.metadata["company_published_job_ids"] = initial_reqs
        self.result.metadata["initial_jobs_before_hydration"] = jobs_before
        self.result.timings["source_collection_seconds"] = round(
            max(0.0, time.perf_counter() - started), 6)
        return self.result
