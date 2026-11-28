"""Slack's own careers page and the company-scoped Workday site it links to.

Membership comes from the job rows rendered by slack.com/careers.  Every row
links to salesforce.wd12.myworkdayjobs.com/Slack, whose unfiltered external
listing is used only to reconcile those exact IDs and to retrieve each exact
job's full public detail.  No other Salesforce or Workday site is enumerated.
"""
from __future__ import annotations

from datetime import date
from hashlib import sha256
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://slack.com/careers"
WORKDAY_ORIGIN = "https://salesforce.wd12.myworkdayjobs.com"
WORKDAY_SITE = "Slack"
WORKDAY_BOARD_URL = f"{WORKDAY_ORIGIN}/{WORKDAY_SITE}"
WORKDAY_CXS = f"{WORKDAY_ORIGIN}/wday/cxs/salesforce/{WORKDAY_SITE}"
WORKDAY_LISTING_API = f"{WORKDAY_CXS}/jobs"
WORKDAY_DETAIL_API = WORKDAY_CXS
LISTING_PATH = re.compile(r"^/(?:intl/[a-z]{2}-[a-z]{2}/)?careers/?$", re.I)
JOB_ID_RE = re.compile(r"^JR\d+(?:-\d+)?$")
JOB_API_PATH = re.compile(r"^/Slack/job/[^/]+/[^/]+$")
WORKDAY_HOSTS = {"salesforce.wd12.myworkdayjobs.com"}
SLACK_HOSTS = {"slack.com", "www.slack.com"}
METHOD = "slack.workday.jobPostingInfo.jobDescription"
PAGE_SIZE = 20
BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "br", "table", "tr", "td", "section", "article", "blockquote", "pre",
}
HEADING_TOKEN_RE = re.compile(
    r"(?:responsib|what you|you're our|you’re our|you are our|even better|qualif|requirement|"
    r"minimum|preferred|the experience|about |how you|job category|job details|"
    r"posting statement|accommodations|unleash your potential|what we value|"
    r"what makes this role|our core values)",
    re.I,
)
RESPONSIBILITY_RE = re.compile(
    r"(?:responsib|what you.{0,40}(?:doing|be doing|being doing)|"
    r"core responsibilities|key responsibilities)",
    re.I,
)
QUALIFICATION_RE = re.compile(
    r"(?:our person if|even better|qualif|requirement|minimum|preferred|"
    r"what you should have|what we're looking for|what we are looking for|"
    r"about you|skills|how you think)",
    re.I,
)
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)


def normalize_text(value: str | None) -> str:
    value = re.sub(r"[\u00a0\u2007\u202f]", " ", value or "")
    value = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def normalized_title(value: str | None) -> str:
    return normalize_text(unicodedata.normalize("NFC", value or ""))


def normalize_body_text(value: str | None) -> str:
    """Whitespace-normal form used for independent rendered-body comparisons."""
    return normalize_text(value)


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _render_text(node) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name in {"script", "style", "noscript"}:
        return ""
    if name == "br":
        return "\n"
    inner = "".join(_render_text(child) for child in node.children)
    if name in BLOCK_TAGS:
        return "\n" + inner + "\n"
    return inner


def html_to_text(source: str | None) -> str:
    """Render the publisher's HTML with block boundaries, not inline-word breaks.

    Workday stores some literal line breaks as double-escaped ``&#xa;`` text;
    the public page renders those as newlines, so they are normalised the same
    way here.  No other entity is decoded a second time.
    """
    rendered = _render_text(BeautifulSoup(source or "", "lxml"))
    rendered = rendered.replace("\u00a0", " ")
    rendered = rendered.replace("&#xa;", "\n").replace("&#x9;", "\t")
    rendered = re.sub(r"[ \t\f\v]+", " ", rendered)
    rendered = re.sub(r"\n[ \t]*", "\n", rendered)
    rendered = re.sub(r"\n{2,}", "\n", rendered)
    return rendered.strip()


def _is_heading_line(line: str) -> bool:
    value = normalized_title(line)
    if not value or len(value) > 140 or len(value.split()) > 16:
        return False
    if value.endswith(".") and not value.endswith("..."):
        return False
    return bool(HEADING_TOKEN_RE.search(value))


def extract_sections(text: str) -> list[dict]:
    sections, current = [], None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if _is_heading_line(line):
            if current is not None:
                sections.append(current)
            current = {"heading": line, "lines": []}
            continue
        if current is None:
            current = {"heading": None, "lines": []}
        current["lines"].append(line)
    if current is not None:
        sections.append(current)
    output = []
    for section in sections:
        section["text"] = "\n".join(section.pop("lines")).strip()
        output.append(section)
    return output


def responsibility_sections(text: str) -> list[dict]:
    return [section for section in extract_sections(text)
            if section.get("heading") and RESPONSIBILITY_RE.search(section["heading"])]


def qualification_sections(text: str) -> list[dict]:
    return [section for section in extract_sections(text)
            if section.get("heading") and QUALIFICATION_RE.search(section["heading"])]


def extract_job_category(text: str) -> str | None:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if line.casefold() == "job category":
            return lines[index + 1] or None
    return None


def extract_salary(text: str) -> dict | None:
    """Return the publisher-stated base salary range, without inventing one."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    candidates = [line for line in lines if re.search(r"(?i)base salary range|salary range|pay range", line)]
    if not candidates:
        return None
    ranges = []
    for line in candidates:
        for match in re.finditer(r"\$([\d,]+)\s*(?:-|–|—|to)\s*\$([\d,]+)", line):
            minimum = int(match.group(1).replace(",", ""))
            maximum = int(match.group(2).replace(",", ""))
            ranges.append({"minimum": minimum, "maximum": maximum, "publisher_text": line})
    if not ranges:
        return None
    period = None
    joined = " ".join(candidates).casefold()
    if "annually" in joined or "per year" in joined or "a year" in joined:
        period = "year"
    elif "hourly" in joined or "per hour" in joined:
        period = "hour"
    elif "monthly" in joined or "per month" in joined:
        period = "month"
    return {
        "currency": "USD",
        "period": period,
        "ranges": ranges,
        "source_text": " ".join(candidates),
        "source_field": "publisher job description",
        "note": "Publisher-stated base salary range; not a structured Workday field.",
    }


def _validate_job_url(value: str) -> tuple[str, str]:
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.netloc not in WORKDAY_HOSTS:
        raise ValueError(f"Slack job URL is not the observed company Workday host: {value}")
    if not JOB_API_PATH.fullmatch(parts.path):
        raise ValueError(f"Slack job URL is not the observed /Slack/job/ route: {value}")
    path_id = parts.path.rsplit("_", 1)[-1]
    if not re.match(r"^JR\d+", path_id):
        raise ValueError(f"Slack job URL has no requisition suffix: {value}")
    return parts.path, path_id


def parse_official_listing(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc not in SLACK_HOSTS or not LISTING_PATH.fullmatch(parts.path):
        raise ValueError("Slack official careers URL redirected away from its observed /careers route")
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if title != "Careers | Slack":
        raise ValueError(f"Slack careers page title changed: {title!r}")
    section = soup.select_one("section.o-section--job-listings")
    if section is None:
        raise ValueError("Slack careers page no longer exposes its job-listings section")
    total_node = section.select_one(".jobs-filter__total-count")
    total_match = re.search(r"(\d+)\s*Open positions", total_node.get_text(" ", strip=True) if total_node else "")
    if total_match is None:
        raise ValueError("Slack careers page no longer states its visible open-positions count")
    advertised_total = int(total_match.group(1))

    rows, issues, seen = [], [], set()
    for block in section.select(".job-listing"):
        category_node = block.select_one(".job-listing__category-title")
        category = category_node.get_text(" ", strip=True) if category_node else None
        for tr in block.select("tr.for-desktop-only--table-row"):
            link = tr.select_one("a[data-job-id]")
            if link is None:
                issues.append("slack_job_row_missing_apply_link")
                continue
            identity = (link.get("data-job-id") or "").strip()
            href = link.get("href") or ""
            cells = tr.select("td")
            row_title = cells[0].get_text(" ", strip=True) if cells else ""
            location_text = next(
                (cell.get_text(" ", strip=True) for cell in cells
                 if any("location-col" in (name or "") for name in (cell.get("class") or []))),
                None,
            )
            if not JOB_ID_RE.fullmatch(identity):
                issues.append(f"slack_job_row_bad_id:{identity}")
                continue
            if identity in seen:
                issues.append(f"slack_job_row_duplicate_id:{identity}")
                continue
            try:
                path, path_id = _validate_job_url(href)
            except ValueError as exc:
                issues.append(f"slack_job_row_bad_url:{identity}:{exc}")
                continue
            if not path_id.startswith(identity):
                issues.append(f"slack_job_row_url_id_mismatch:{identity}:{path_id}")
                continue
            if not row_title:
                issues.append(f"slack_job_row_missing_title:{identity}")
                continue
            seen.add(identity)
            rows.append({
                "id": identity,
                "title": normalized_title(row_title),
                "location_text": normalized_title(location_text),
                "department": normalized_title(category),
                "url": href,
                "path": path,
                "path_id": path_id,
            })
    if not rows:
        issues.append("slack_careers_page_has_no_exposed_job_rows")
    if len(rows) != advertised_total:
        issues.append(f"slack_advertised_count_mismatch:{advertised_total}!={len(rows)}")
    controls = [
        node.get_text(" ", strip=True)
        for node in section.select("button,a")
        if re.search(r"\b(?:next|previous|load more)\b", node.get_text(" ", strip=True), re.I)
    ]
    filters = [node.get_text(" ", strip=True)
               for node in section.select(".jobs-filter__selected, #job-listing-filter-trigger")]
    return {
        "url": response_url,
        "page_title": title,
        "total": advertised_total,
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "pagination_controls": controls,
        "filters": [value for value in filters if value],
        "issues": issues,
    }


def parse_workday_listing(value: dict, requested_offset: int = 0, limit: int = PAGE_SIZE) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Slack Workday listing response is not an object")
    total, postings = value.get("total"), value.get("jobPostings")
    if not isinstance(total, int) or total < 0 or not isinstance(postings, list):
        raise ValueError("Slack Workday listing response is missing total/jobPostings")
    if len(postings) > limit or requested_offset < 0:
        raise ValueError("Slack Workday listing page exceeds the requested window")
    rows, issues, seen = [], [], set()
    for posting in postings:
        if not isinstance(posting, dict):
            issues.append("workday_posting_not_object")
            continue
        external_path = posting.get("externalPath")
        title = normalized_title(posting.get("title"))
        bullets = posting.get("bulletFields")
        if not isinstance(external_path, str) or not external_path.startswith("/job/"):
            issues.append("workday_posting_missing_external_path")
            continue
        if not isinstance(bullets, list):
            issues.append("workday_posting_missing_bullet_fields")
            continue
        identity = next((value for value in bullets if isinstance(value, str) and JOB_ID_RE.fullmatch(value)), None)
        if identity is None:
            issues.append("workday_posting_missing_requisition_id")
            continue
        if identity in seen:
            issues.append(f"workday_posting_duplicate_id:{identity}")
            continue
        if not title:
            issues.append(f"workday_posting_missing_title:{identity}")
            continue
        url = f"{WORKDAY_ORIGIN}/{WORKDAY_SITE}{external_path}"
        try:
            _validate_job_url(url)
        except ValueError as exc:
            issues.append(f"workday_posting_bad_url:{identity}:{exc}")
            continue
        seen.add(identity)
        rows.append({
            "id": identity,
            "title": title,
            "url": url,
            "external_path": external_path,
            "locations_text": normalized_title(posting.get("locationsText")),
            "posted_on": normalized_title(posting.get("postedOn")),
            "bullet_fields": list(bullets),
        })
    return {
        "total": total,
        "requested_offset": requested_offset,
        "limit": limit,
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "issues": issues,
    }


def _parse_iso_date(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError as exc:
        raise ValueError(f"Slack Workday startDate is not an ISO date: {value!r}") from exc


def parse_workday_detail(value: dict, *, expected_id: str, expected_url: str | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Slack Workday detail response is not an object")
    info = value.get("jobPostingInfo")
    if not isinstance(info, dict):
        raise ValueError("Slack Workday detail response is missing jobPostingInfo")
    identity = info.get("jobReqId")
    if identity != expected_id or not isinstance(identity, str):
        raise ValueError(f"Slack Workday detail requisition changed: {identity!r} != {expected_id!r}")
    title = normalized_title(info.get("title"))
    if not title:
        raise ValueError("Slack Workday detail is missing its title")
    description_html = info.get("jobDescription")
    if not isinstance(description_html, str):
        raise ValueError("Slack Workday detail is missing jobPostingInfo.jobDescription")
    external_url = info.get("externalUrl")
    if not isinstance(external_url, str) or not external_url:
        raise ValueError("Slack Workday detail is missing its externalUrl")
    external_path, path_id = _validate_job_url(external_url)
    if not path_id.startswith(identity):
        raise ValueError(f"Slack Workday detail URL does not belong to {identity}")
    if expected_url is not None and external_url != expected_url:
        raise ValueError(f"Slack Workday detail externalUrl changed: {external_url!r} != {expected_url!r}")

    primary_location = normalized_title(info.get("location"))
    additional = info.get("additionalLocations")
    if additional is None:
        additional = []
    if not isinstance(additional, list):
        raise ValueError("Slack Workday additionalLocations is not a list")
    locations = []
    for location in [primary_location, *(normalized_title(item) for item in additional)]:
        if location and location not in locations:
            locations.append(location)

    description_text = html_to_text(description_html)
    job_category = extract_job_category(description_text)
    salary = extract_salary(description_text)
    responsibilities = responsibility_sections(description_text)
    qualifications = qualification_sections(description_text)
    description_normalized = normalize_body_text(description_text)
    description_links = [
        {"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
        for anchor in BeautifulSoup(description_html, "lxml").select("a[href]")
    ]
    attachments = [link for link in description_links
                   if ATTACHMENT_SUFFIX.search(link["url"] or "")]
    employer = (value.get("hiringOrganization") or {}).get("name")
    can_apply = info.get("canApply")
    apply_url = external_url.rstrip("/") + "/apply" if can_apply is True else None
    issues = []
    if not description_text.strip():
        issues.append("publisher_empty_description")
    if not employer:
        issues.append("workday_missing_hiring_organization")
    if not isinstance(can_apply, bool):
        issues.append("workday_missing_can_apply")
    if not isinstance(info.get("posted"), bool):
        issues.append("workday_missing_posted_flag")
    return {
        "job_id": identity,
        "title": title,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(description_normalized),
        "description_fnv1a64": fnv1a64(description_normalized),
        "description_sha256": sha256(description_html.encode()).hexdigest(),
        "locations": locations,
        "primary_location": primary_location or None,
        "additional_locations": [normalized_title(item) for item in additional if normalized_title(item)],
        "job_category": job_category,
        "employment_type": info.get("timeType"),
        "workplace_type": info.get("remoteType"),
        "date_posted": _parse_iso_date(info.get("startDate")),
        "date_posted_raw": info.get("postedOn"),
        "date_updated": None,
        "valid_through": None,
        "employer": employer,
        "salary": salary,
        "responsibilities": responsibilities or None,
        "qualifications": qualifications or None,
        "external_url": external_url,
        "external_path": external_path,
        "apply_url": apply_url,
        "can_apply": can_apply,
        "posted": info.get("posted"),
        "job_posting_id": info.get("jobPostingId"),
        "job_posting_site_id": info.get("jobPostingSiteId"),
        "questionnaire_id": info.get("questionnaireId"),
        "description_links": description_links,
        "attachments": attachments,
        "issues": issues,
    }


class Slack(Adapter):
    async def _workday_csrf(self):
        started = time.perf_counter()
        response = await self.http.get(
            WORKDAY_BOARD_URL,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        token = response.headers.get("x-calypso-csrf-token")
        if not token:
            try:
                token = self.http.client.cookies.get("CALYPSO_CSRF_TOKEN")
            except Exception:
                token = None
        if not token:
            raise ValueError("Workday Slack board did not expose a CSRF token")
        return token, max(0.0, time.perf_counter() - started), response

    async def _workday_listing(self):
        token, bootstrap_seconds, _ = await self._workday_csrf()
        rows, requests, issues = [], [], []
        first_total = None
        offset = 0
        request_seconds = 0.0
        while True:
            payload = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
            started = time.perf_counter()
            value = await self.http.json(
                WORKDAY_LISTING_API,
                method="POST",
                json=payload,
                headers={"Accept": "application/json", "X-Calypso-CSRF-Token": token},
            )
            request_seconds += max(0.0, time.perf_counter() - started)
            page = parse_workday_listing(value, requested_offset=offset, limit=PAGE_SIZE)
            issues.extend(page["issues"])
            requests.append({"request": payload, "total": page["total"], "rows": len(page["rows"])})
            if first_total is None:
                first_total = page["total"]
            elif page["total"] != first_total:
                issues.append(f"workday_total_changed:{first_total}->{page['total']}")
            rows.extend(page["rows"])
            if len(rows) >= first_total:
                break
            if not page["rows"]:
                issues.append(f"workday_listing_stopped_at_offset:{offset}/{first_total}")
                break
            offset += len(page["rows"])
            if offset > first_total + PAGE_SIZE:
                issues.append("workday_listing_offset_overflow")
                break
        return {
            "rows": rows,
            "total": first_total if first_total is not None else 0,
            "requests": requests,
            "issues": issues,
            "bootstrap_seconds": bootstrap_seconds,
            "listing_request_seconds": request_seconds,
        }

    async def _detail(self, job):
        api_url = job.description_source_url
        if not api_url:
            raise ValueError(f"Slack detail API URL is missing for {job.job_id}")
        value = await self.http.json(api_url, headers={"Accept": "application/json"})
        parsed = parse_workday_detail(value, expected_id=job.job_id, expected_url=job.url)
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = METHOD
        job.description_source_url = api_url
        job.locations = parsed["locations"]
        if job.department and parsed["job_category"] and normalized_title(job.department) != normalized_title(parsed["job_category"]):
            job.warnings.append(
                f"slack_category_mismatch:{normalized_title(job.department)}:{normalized_title(parsed['job_category'])}"
            )
        job.department = normalized_title(job.department) or parsed["job_category"]
        job.employment_type = parsed["employment_type"]
        job.workplace_type = parsed["workplace_type"]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.date_updated = None
        job.valid_through = None
        job.employer = parsed["employer"]
        job.salary = parsed["salary"]
        job.responsibilities = parsed["responsibilities"]
        job.qualifications = parsed["qualifications"]
        job.apply_url = parsed["apply_url"] or job.apply_url
        job.language = "en"
        job.warnings.extend(parsed["issues"])
        if not parsed["description_text"].strip():
            job.description_unavailable = True
        job.raw["detail_response"] = value
        job.raw["detail"] = {
            "source_field": "jobPostingInfo.jobDescription",
            "description_sha256": parsed["description_sha256"],
            "description_normalized_chars": parsed["description_normalized_chars"],
            "description_fnv1a64": parsed["description_fnv1a64"],
            "sections": extract_sections(parsed["description_text"]),
            "links": parsed["description_links"],
            "attachments": parsed["attachments"],
            "role_bearing_attachments": [],
            "job_category": parsed["job_category"],
            "dates": {
                "date_posted_field": "jobPostingInfo.startDate",
                "date_posted": parsed["date_posted"],
                "relative_label": parsed["date_posted_raw"],
                "relative_label_used_as_date": False,
                "date_updated": None,
                "valid_through": None,
            },
            "application_route": {
                "job_page_url": parsed["external_url"],
                "apply_url": parsed["apply_url"],
                "apply_control": "Workday Apply" if parsed["can_apply"] is True else None,
                "can_apply": parsed["can_apply"],
                "posted": parsed["posted"],
            },
            "workday_ids": {
                "job_req_id": parsed["job_id"],
                "job_posting_id": parsed["job_posting_id"],
                "job_posting_site_id": parsed["job_posting_site_id"],
                "questionnaire_id": parsed["questionnaire_id"],
            },
        }
        return job

    async def run(self):
        collection_started = time.perf_counter()
        official_seconds = 0.0
        board_seconds = 0.0
        listing_seconds = 0.0
        page = None
        board = None
        try:
            started = time.perf_counter()
            response = await self.http.get(
                OFFICIAL_ENTRY,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            official_seconds = max(0.0, time.perf_counter() - started)
            page = parse_official_listing(response.text, str(response.url))
            self.result.expected_total = page["total"]
            self.result.metadata["official_entry"] = {
                "request_url": OFFICIAL_ENTRY,
                "final_url": str(response.url),
                "status": response.status_code,
                "redirects": [str(item.url) for item in response.history],
                "seconds": round(official_seconds, 6),
            }
            self.result.metadata["slack_listing"] = {
                "url": str(response.url),
                "page_title": page["page_title"],
                "visible_total_text": f"{page['total']} open positions",
                "row_count": len(page["rows"]),
                "pagination_controls": page["pagination_controls"],
                "filters_visible": page["filters"],
                "issues": page["issues"],
            }
            self.result.errors.extend(f"slack_listing:{issue}" for issue in page["issues"])
            if not page["rows"]:
                raise ValueError("Slack careers page exposed no job rows")
        except Exception as exc:
            self.result.errors.append(f"slack_source_collection_failed:{type(exc).__name__}:{exc}")
            self.result.listing_complete = False
            self.result.finish()
            return self.result

        try:
            board = await self._workday_listing()
            board_seconds = board["bootstrap_seconds"]
            listing_seconds = board["listing_request_seconds"]
            self.result.metadata["workday_board"] = {
                "url": WORKDAY_BOARD_URL,
                "listing_api": WORKDAY_LISTING_API,
                "request_method": "POST",
                "request_body": {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": "paged", "searchText": ""},
                "total": board["total"],
                "rows": len(board["rows"]),
                "requests": board["requests"],
                "issues": board["issues"],
            }
            self.result.errors.extend(f"workday_listing:{issue}" for issue in board["issues"])
        except Exception as exc:
            self.result.errors.append(f"workday_listing_failed:{type(exc).__name__}:{exc}")
            self.result.metadata["workday_board"] = {
                "url": WORKDAY_BOARD_URL,
                "listing_api": WORKDAY_LISTING_API,
                "error": f"{type(exc).__name__}: {exc}",
            }
            board = {"rows": [], "total": 0, "issues": [], "bootstrap_seconds": 0.0, "listing_request_seconds": 0.0}

        page_rows = page["rows"]
        page_ids = [row["id"] for row in page_rows]
        board_ids = [row["id"] for row in board["rows"]]
        board_by_id = {row["id"]: row for row in board["rows"]}
        page_by_id = {row["id"]: row for row in page_rows}
        missing_from_board = [identity for identity in page_ids if identity not in board_by_id]
        extra_on_board = [identity for identity in board_ids if identity not in page_by_id]
        ids_equal = set(page_ids) == set(board_ids) and len(page_ids) == len(set(page_ids))
        membership_complete = (
            ids_equal
            and not missing_from_board
            and not extra_on_board
            and board["total"] == page["total"] == len(page_rows)
            and len(board["rows"]) == board["total"]
        )
        for identity in missing_from_board:
            self.result.errors.append(f"workday_board_missing_slack_listing_id:{identity}")
        for identity in extra_on_board:
            self.result.errors.append(f"workday_board_extra_id_not_on_slack_page:{identity}")
        if board["total"] != page["total"]:
            self.result.errors.append(f"slack_workday_total_mismatch:{page['total']}!={board['total']}")

        jobs = []
        for row in page_rows:
            board_row = board_by_id.get(row["id"])
            path = row["path"]
            detail_api_url = f"{WORKDAY_ORIGIN}/wday/cxs/salesforce{path}"
            job = self.job(
                {"slack_listing": row, "workday_listing": board_row},
                title=row["title"],
                url=row["url"],
                job_id=row["id"],
                requisition_id=row["id"],
                apply_url=row["url"].rstrip("/") + "/apply",
                locations=[],
                department=row["department"],
                description="",
                method=None,
                description_url=detail_api_url,
            )
            job.raw["detail_api_url"] = detail_api_url
            if board_row is None:
                job.warnings.append("workday_board_missing_listing_row")
            self.add(job)
            jobs.append(job)
            if self.limit():
                break
        if len(jobs) != len(page_rows):
            self.result.errors.append(f"slack_job_sample_limit:{len(jobs)}/{len(page_rows)}")
            membership_complete = False

        if jobs:
            await self.hydrate(jobs, self._detail)

        final_ids = [job.job_id for job in self.result.jobs]
        self.result.listing_complete = membership_complete
        self.result.metadata["membership_reconciliation"] = {
            "complete": membership_complete,
            "initial_ids": page_ids,
            "final_ids": final_ids,
            "workday_board_ids": board_ids,
            "expected_total": page["total"],
            "slack_page_rows": len(page_rows),
            "workday_total": board["total"],
            "workday_rows": len(board["rows"]),
            "ids_equal": ids_equal,
            "missing_from_workday_board": missing_from_board,
            "extra_on_workday_board": extra_on_board,
            "order": {"slack_page": page_ids, "workday_board": board_ids},
            "href_paths_equal": {row["id"]: row["path"] for row in page_rows} ==
                                {row["id"]: row["external_path"] for row in board["rows"]}
                                if ids_equal else False,
        }
        self.result.metadata["workday_detail"] = {
            "url_pattern": f"{WORKDAY_DETAIL_API}/job/<location>/<title>_<JR...>",
            "method": "GET",
            "source_field": "jobPostingInfo.jobDescription",
            "jobs_requested": len(jobs),
            "jobs_verified": sum(job.description_verified for job in self.result.jobs),
            "failed_ids": [job.job_id for job in self.result.jobs if not job.description_verified],
        }
        self.result.metadata["date_semantics"] = {
            "date_posted": "jobPostingInfo.startDate",
            "relative_label": "jobPostingInfo.postedOn is retained as date_posted_raw and never parsed as a date",
            "date_updated": None,
            "valid_through": None,
            "request_time_used_as_posting_date": False,
        }
        self.result.metadata["salary_semantics"] = (
            "Only publisher-stated base salary range paragraphs in the description are extracted; "
            "there is no structured Workday salary field."
        )
        self.result.metadata["attachments"] = (
            "Description links are retained. Only generic candidate-privacy/accommodations links occur; "
            "none is classified as a role-bearing attachment."
        )
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - collection_started)
        self.result.timings["official_entry_request_seconds"] = official_seconds
        self.result.timings["workday_board_bootstrap_seconds"] = board_seconds
        self.result.timings["workday_listing_request_seconds_sum"] = listing_seconds
        return self.result
