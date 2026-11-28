"""Sandvine's official careers journey: sandvine.com -> AppLogic Networks -> UKG/UltiPro.

The ledger URL ``https://www.sandvine.com/company/careers`` permanently redirects
to the company's current AppLogic Networks careers page.  That page publishes one
"Open Roles" link to the company's own UKG/UltiPro board
(``PRO1053PROC`` / board ``d6eed263-4950-420d-b9f8-5b1a441c931e``).  This module
reads exactly that board: its own listing request and the complete opportunity
object embedded in each company detail page.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.sandvine.com/company/careers"
OFFICIAL_FINAL = "https://www.applogicnetworks.com/company/careers"
TENANT = "PRO1053PROC"
BOARD_ID = "d6eed263-4950-420d-b9f8-5b1a441c931e"
BOARD_ORIGIN = "https://recruiting2.ultipro.com"
BOARD_ROOT = f"{BOARD_ORIGIN}/{TENANT}/JobBoard/{BOARD_ID}"
LISTING_URL = f"{BOARD_ROOT}/?q=&o=postedDateDesc"
LISTING_API_URL = f"{BOARD_ROOT}/JobBoardView/LoadSearchResults"
DETAIL_URL = BOARD_ROOT + "/OpportunityDetail?opportunityId={opportunity_id}"
APPLY_URL = BOARD_ROOT + "/OpportunityApply?opportunityId={opportunity_id}"
APPLICANT_PRIVACY_URL = "https://www.sandvine.com/global-applicant-privacy-policy-sandvine"
BOARD_NAME = "Job Opportunities"
PAGE_SIZE = 50
EMPLOYER = "AppLogic Networks"
EMPLOYER_NOTE = "Sandvine careers URL redirects to the company's current AppLogic Networks brand."
DETAIL_MARKER = r"new US\.Opportunity\.CandidateOpportunityDetail\("
BOARD_MARKER = r"jobBoard:\s*"
WORKPLACE_TYPES = {0: "Hybrid", 2: "Remote"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "div", "dl", "dt", "dd",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}


def normalized_body_text(html: str | None) -> str:
    """Approximate rendered innerText: inline tags concatenate, blocks separate.

    ``BeautifulSoup.get_text(" ")`` inserts a separator between inline tags and
    can invent a space that no browser shows (for example
    ``<strong>Locations</strong>- DACH``).  This walk keeps inline flow intact so
    the normalized hash can be compared with an independently rendered page.
    """
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    parts: list[str] = []

    def walk(node):
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        if node.name == "br":
            parts.append(" ")
            return
        block = node.name in BLOCK_TAGS
        if block:
            parts.append(" ")
        for child in node.children:
            walk(child)
        if block:
            parts.append(" ")

    walk(soup.body or soup)
    value = "".join(parts).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return value


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Expected an ISO-8601 timestamp, got {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_balanced_object(html: str, marker: str) -> tuple[dict, int, int]:
    """Return the JSON object starting at ``marker`` plus its source span."""
    match = re.search(marker, html)
    if match is None:
        raise ValueError(f"Expected inline object marker was not found: {marker}")
    start = match.end()
    while start < len(html) and html[start].isspace():
        start += 1
    if start >= len(html) or html[start] != "{":
        raise ValueError("Inline object does not start with an opening brace")
    depth = 0
    index = start
    while index < len(html):
        char = html[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                raw = html[start:index + 1]
                return json.loads(raw), start, index + 1
        elif char == '"':
            index += 1
            while index < len(html) and html[index] != '"':
                if html[index] == "\\":
                    index += 1
                index += 1
        index += 1
    raise ValueError("Inline JSON object is not balanced")


def _board_path(path: str) -> bool:
    return path.rstrip("/") == f"/{TENANT}/JobBoard/{BOARD_ID}"


def parse_official_entry(html: str, response_url: str = OFFICIAL_FINAL) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.applogicnetworks.com"
            or parsed.path.rstrip("/") != "/company/careers"):
        raise ValueError("Sandvine careers entry did not redirect to the current AppLogic Networks careers page")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "AppLogic Networks" not in title or "Careers" not in title:
        raise ValueError("Sandvine official careers page no longer identifies AppLogic Networks careers")

    open_roles = []
    other_board_links = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        text = normalized_title(anchor.get_text(" ", strip=True))
        target = urlsplit(href)
        if "ultipro.com" in target.netloc.lower() and "jobboard" in target.path.lower():
            other_board_links.append({"text": text, "href": href})
            if text.lower().startswith("open roles"):
                open_roles.append({"text": text, "href": href})
    if len(open_roles) != 1:
        raise ValueError(f"Expected exactly one company 'Open Roles' link, found {len(open_roles)}")
    if len(other_board_links) != 1:
        raise ValueError(f"Expected exactly one company job-board link, found {len(other_board_links)}")

    observed = open_roles[0]
    target = urlsplit(observed["href"])
    query = parse_qs(target.query)
    if (target.scheme != "https" or target.netloc != "recruiting2.ultipro.com"
            or not _board_path(target.path) or query.get("o") != ["postedDateDesc"]):
        raise ValueError("Sandvine 'Open Roles' link no longer points at the inspected UKG board")
    privacy = sorted({
        anchor.get("href")
        for anchor in soup.select('a[href]')
        if anchor.get("href") and "privacy" in anchor.get("href", "").lower()
    })
    return {
        "url": response_url,
        "title": title,
        "brand": "AppLogic Networks",
        "ledger_company": "Sandvine",
        "observed_open_roles_href": observed["href"],
        "open_roles_text": observed["text"],
        "listing_url": LISTING_URL,
        "board_id": BOARD_ID,
        "tenant": TENANT,
        "page_privacy_policy_urls": privacy,
        "company_job_board_links": other_board_links,
    }


def parse_board_page(html: str, response_url: str = LISTING_URL) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "recruiting2.ultipro.com"
            or not _board_path(parsed.path)):
        raise ValueError("UKG board response came from an unexpected company board route")
    tokens = {
        value
        for value in re.findall(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html)
        if value
    }
    if len(tokens) != 1:
        raise ValueError(f"Expected one UKG request-verification token, found {len(tokens)}")
    board_match = re.search(BOARD_MARKER + r"(\{.*?\}),\s*isAuthenticated", html, re.S)
    if board_match is None:
        raise ValueError("UKG board configuration is missing")
    board = json.loads(board_match.group(1))
    if (board.get("Id") != BOARD_ID or board.get("Name") != BOARD_NAME
            or board.get("AllowInternal") is not False or board.get("AllowExternal") is not True
            or board.get("HasThirdPartyPostings") is not False):
        raise ValueError("UKG board configuration no longer matches the inspected company board")
    if not re.search(r"jobBoardChoices:\s*0\b", html):
        raise ValueError("UKG board now advertises additional job-board choices")
    return {
        "url": response_url,
        "token": next(iter(tokens)),
        "board": board,
        "job_board_choices": 0,
        "is_internal": bool(re.search(r"isInternal:\s*true\b", html)),
    }


def build_listing_payload(top: int = PAGE_SIZE, skip: int = 0) -> dict:
    """Exact observed UKG LoadSearchResults body, with every UI filter unset."""
    return {
        "opportunitySearch": {
            "Top": top,
            "Skip": skip,
            "QueryString": "",
            "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
            "Filters": [
                {"t": "TermsSearchFilterDto", "fieldName": field, "extra": None, "values": []}
                for field in (4, 5, 6, 37)
            ],
        },
        "matchCriteria": {
            "PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [],
            "Skills": [], "hasNoLicenses": False, "SkippedSkills": [],
        },
    }


def _validate_listing_row(row: dict, index: int) -> None:
    if not isinstance(row, dict):
        raise ValueError(f"UKG listing row {index} is not an object")
    identity = str(row.get("Id", ""))
    if not UUID_RE.fullmatch(identity):
        raise ValueError(f"UKG listing row {index} has an invalid opportunity id: {identity!r}")
    if not normalized_title(row.get("Title")):
        raise ValueError(f"UKG listing row {identity} has no title")
    if not normalized_title(row.get("RequisitionNumber")):
        raise ValueError(f"UKG listing row {identity} has no requisition number")
    if parse_iso_datetime(row.get("PostedDate")) is None:
        raise ValueError(f"UKG listing row {identity} has no posted date")
    if not isinstance(row.get("Locations"), list):
        raise ValueError(f"UKG listing row {identity} has no location list")
    if not isinstance(row.get("FullTime"), bool):
        raise ValueError(f"UKG listing row {identity} has an invalid FullTime flag")


def parse_listing(payload: dict, *, requested_skip: int = 0, top: int = PAGE_SIZE) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("UKG listing response is not an object")
    rows = payload.get("opportunities")
    total = payload.get("totalCount")
    initial_total = payload.get("initialTotalOpportunitiesCount")
    if not isinstance(rows, list) or not isinstance(total, int) or total < 0:
        raise ValueError("UKG listing response is missing opportunities/totalCount")
    if initial_total is not None and initial_total != total:
        raise ValueError("UKG listing totalCount and initialTotalOpportunitiesCount disagree")
    if len(rows) > total or len(rows) > top:
        raise ValueError("UKG listing returned more rows than its total or requested page size")
    expected = min(top, max(0, total - requested_skip))
    if len(rows) != expected:
        raise ValueError(
            f"UKG listing page shape changed: expected {expected} rows at skip {requested_skip}, got {len(rows)}"
        )
    seen = set()
    for index, row in enumerate(rows):
        _validate_listing_row(row, index)
        identity = str(row["Id"])
        if identity in seen:
            raise ValueError(f"UKG listing returned duplicate opportunity id: {identity}")
        seen.add(identity)
    return {
        "rows": rows,
        "total_count": total,
        "ids": [str(row["Id"]) for row in rows],
        "skip": requested_skip,
        "top": top,
    }


def job_board_membership(detail: dict, board_id: str = BOARD_ID) -> dict:
    memberships = detail.get("JobBoardMemberships")
    if not isinstance(memberships, list):
        raise ValueError("Opportunity detail has no JobBoardMemberships list")
    matches = [row for row in memberships if isinstance(row, dict) and row.get("JobBoardId") == board_id]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one membership for company board {board_id}, found {len(matches)}")
    membership = matches[0]
    if membership.get("PublishedExternal") is not True:
        raise ValueError("Opportunity is no longer externally published on the company board")
    if parse_iso_datetime(membership.get("ExternalPostedDate")) is None:
        raise ValueError("Opportunity has no external posted date on the company board")
    return membership


def format_location(location: dict) -> list[str]:
    address = location.get("Address") if isinstance(location.get("Address"), dict) else {}
    state = address.get("State") if isinstance(address.get("State"), dict) else {}
    country = address.get("Country") if isinstance(address.get("Country"), dict) else {}
    label = normalized_title(location.get("LocalizedName") or location.get("LocalizedDescription"))
    state_part = " ".join(part for part in (
        normalized_title(state.get("Code") or state.get("Name")),
        normalized_title(address.get("PostalCode")),
    ) if part)
    parts = [
        normalized_title(address.get("City")),
        state_part,
        normalized_title(country.get("Name")),
    ]
    formatted = ", ".join(part for part in parts if part)
    values = [value for value in (label, formatted) if value]
    return list(dict.fromkeys(values))


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-sandvine-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-sandvine-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = normalized_title(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
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
    else:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def parse_detail(html: str, expected_id: str, detail_url: str, board_id: str = BOARD_ID) -> dict:
    parts = urlsplit(detail_url)
    expected_path = f"/{TENANT}/JobBoard/{board_id}/OpportunityDetail"
    if (parts.scheme != "https" or parts.netloc != "recruiting2.ultipro.com"
            or parts.path != expected_path
            or parse_qs(parts.query) != {"opportunityId": [expected_id]}):
        raise ValueError("Opportunity detail response came from an unexpected route")
    detail, start, end = _extract_balanced_object(html, DETAIL_MARKER)
    if str(detail.get("Id", "")) != expected_id:
        raise ValueError("Opportunity detail id does not match the requested company job")
    if not normalized_title(detail.get("Title")):
        raise ValueError("Opportunity detail has no title")
    if not normalized_title(detail.get("RequisitionNumber")):
        raise ValueError("Opportunity detail has no requisition number")
    if detail.get("OpportunityIsClosed") is not False:
        raise ValueError("Opportunity detail reports the company posting is closed")
    description_html = detail.get("Description")
    if not isinstance(description_html, str) or len(normalized_body_text(description_html)) < 100:
        raise ValueError("Opportunity detail has no usable full Description")
    membership = job_board_membership(detail, board_id)

    apply_redirect = None
    redirect_match = re.search(r'opportunityApplyRedirectUrl:\s*"([^"]+)"', html)
    if redirect_match:
        apply_redirect = redirect_match.group(1).replace("\\u0026", "&")

    locations = []
    for location in detail.get("Locations") or []:
        if isinstance(location, dict):
            locations.extend(format_location(location))

    salary = None
    pay_range = detail.get("PayRange")
    if isinstance(pay_range, dict) and (pay_range.get("PayRangeMinimum") or pay_range.get("PayRangeMaximum")):
        salary = {
            "min": pay_range.get("PayRangeMinimum"),
            "max": pay_range.get("PayRangeMaximum"),
            "currency": detail.get("PayRangeCurrencyCode"),
            "source": "PayRange",
            "pay_range_visible": bool(detail.get("PayRangeVisible")),
        }

    sections = description_sections(description_html)
    links = [
        {"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
        for anchor in BeautifulSoup(description_html, "lxml").select("a[href]")
    ]
    text = text_html(description_html)
    return {
        "source": detail,
        "id": expected_id,
        "title": normalized_title(detail.get("Title")),
        "requisition_id": normalized_title(detail.get("RequisitionNumber")),
        "description_html": description_html,
        "description_text": text,
        "description_sections": sections,
        "description_links": links,
        "role_attachments": [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")],
        "locations": locations,
        "department": detail.get("JobCategoryName"),
        "employment_type": "Full Time" if detail.get("FullTime") is True else None,
        "workplace_type": WORKPLACE_TYPES.get(detail.get("JobLocationType")),
        "posted_date_opportunity": detail.get("PostedDate"),
        "updated_date": detail.get("UpdatedDate"),
        "external_posted_date": membership.get("ExternalPostedDate"),
        "job_board_membership": membership,
        "salary": salary,
        "eeo": detail.get("EqualOpportunityEmployerDescription"),
        "pay_transparency": detail.get("PayTransparencyPolicyStatement"),
        "travel_required": detail.get("TravelRequired"),
        "travel_description": detail.get("TravelDescription"),
        "apply_redirect_url": apply_redirect,
        "detail_url": detail_url,
        "inline_object_span": [start, end],
        "description_sha256": sha256(description_html.encode()).hexdigest(),
        "description_normalized_chars": len(normalized_body_text(description_html)),
        "description_fnv1a64": fnv1a64(normalized_body_text(description_html)),
    }


def detail_url(opportunity_id: str) -> str:
    return DETAIL_URL.format(opportunity_id=opportunity_id)


def apply_url(opportunity_id: str) -> str:
    return APPLY_URL.format(opportunity_id=opportunity_id)


class Sandvine(Adapter):
    """Read only the company's own UKG board and its full inline opportunity details."""

    VERIFICATION_REVISION = "sandvine.applogic-networks-ultipro.1"

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            request_url=OFFICIAL_ENTRY,
            redirect_chain=[str(item.url) for item in response.history],
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
        )
        return parsed

    async def _board_page(self):
        started = time.perf_counter()
        response = await self.http.get(LISTING_URL)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_board_page(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            request_url=LISTING_URL,
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
        )
        return parsed

    async def _listing(self, token: str, *, expected_count: int | None = None):
        started = time.perf_counter()
        rows, seen, pages = [], set(), []
        total_count = None
        skip = 0
        while True:
            payload = build_listing_payload(PAGE_SIZE, skip)
            request_started = time.perf_counter()
            response = await self.http.request(
                "POST",
                LISTING_API_URL,
                json=payload,
                headers={
                    "X-RequestVerificationToken": token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Content-Type": "application/json; charset=UTF-8",
                    "Referer": LISTING_URL,
                },
            )
            request_seconds = max(0.0, time.perf_counter() - request_started)
            parsed = parse_listing(response.json(), requested_skip=skip, top=PAGE_SIZE)
            if total_count is None:
                total_count = parsed["total_count"]
            elif parsed["total_count"] != total_count:
                raise ValueError("UKG listing total changed between pages")
            if expected_count is not None and total_count != expected_count:
                raise ValueError(
                    f"UKG listing membership changed: expected {expected_count}, now {total_count}"
                )
            new_ids = [identity for identity in parsed["ids"] if identity not in seen]
            if not new_ids and parsed["rows"]:
                raise ValueError("UKG listing pagination did not advance")
            for row in parsed["rows"]:
                if str(row["Id"]) not in seen:
                    seen.add(str(row["Id"]))
                    rows.append(row)
            pages.append({
                "skip": skip,
                "top": PAGE_SIZE,
                "total_count": total_count,
                "ids": parsed["ids"],
                "request_url": str(response.request.url),
                "final_url": str(response.url),
                "status": response.status_code,
                "request_seconds": request_seconds,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
                "request_payload": payload,
            })
            if len(rows) >= total_count or not parsed["rows"]:
                break
            skip += len(parsed["rows"])
        if total_count is None:
            raise ValueError("UKG listing did not report a total")
        if len(rows) != total_count:
            raise ValueError(f"UKG listing retained {len(rows)} rows but reports {total_count}")
        return {
            "rows": rows,
            "ids": [str(row["Id"]) for row in rows],
            "total_count": total_count,
            "pages": pages,
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def _detail(self, listing_row: dict):
        started = time.perf_counter()
        target = detail_url(str(listing_row["Id"]))
        response = await self.http.get(target)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_detail(response.text, str(listing_row["Id"]), str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=seconds,
            request_url=target,
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
        )
        return parsed

    def _make_job(self, listing_row: dict, detail: dict):
        identity = str(detail["id"])
        job = self.job(
            {"listing": listing_row, "detail": detail},
            title=detail["title"],
            url=detail["detail_url"],
            job_id=identity,
            requisition_id=detail["requisition_id"],
            apply_url=apply_url(identity),
            description=detail["description_html"],
            method="sandvine.ultipro.opportunity_detail.Description",
            description_url=detail["detail_url"],
            locations=detail["locations"],
            department=detail["department"],
            employment_type=detail["employment_type"],
            workplace_type=detail["workplace_type"],
            date_posted=detail["external_posted_date"],
            date_posted_raw={
                "external_posted_date": detail["external_posted_date"],
                "listing_posted_date": listing_row.get("PostedDate"),
                "opportunity_posted_date": detail["posted_date_opportunity"],
                "source_field": "JobBoardMemberships[board].ExternalPostedDate",
            },
            date_updated=detail["updated_date"],
            valid_through=None,
            salary=detail["salary"],
            employer=EMPLOYER,
            language="en",
            responsibilities=[
                section for section in detail["description_sections"]
                if section["heading"] and any(
                    token in section["heading"].lower()
                    for token in ("responsibilit", "what you'll do", "what you will do")
                )
            ] or None,
            qualifications=[
                section for section in detail["description_sections"]
                if section["heading"] and any(
                    token in section["heading"].lower()
                    for token in ("what makes you successful", "what you'll bring", "qualif",
                                  "requirements", "required experience", "preferred qualifications")
                )
            ] or None,
        )
        job.description_text = detail["description_text"]
        job.raw["detail"]["apply_redirect_url"] = detail["apply_redirect_url"]
        job.raw["detail"]["eeo"] = detail["eeo"]
        job.raw["detail"]["pay_transparency"] = detail["pay_transparency"]
        job.raw["detail"]["travel"] = {
            "required": detail["travel_required"],
            "description": detail["travel_description"],
        }
        job.raw["detail"]["description_sha256"] = detail["description_sha256"]
        job.raw["detail"]["description_normalized_chars"] = detail["description_normalized_chars"]
        job.raw["detail"]["description_fnv1a64"] = detail["description_fnv1a64"]
        job.raw["detail"]["description_sections"] = detail["description_sections"]
        job.raw["detail"]["description_links"] = detail["description_links"]
        job.raw["detail"]["role_attachments"] = detail["role_attachments"]
        job.raw["detail"]["dates"] = {
            "date_posted": detail["external_posted_date"],
            "listing_posted_date": listing_row.get("PostedDate"),
            "opportunity_posted_date": detail["posted_date_opportunity"],
            "date_updated": detail["updated_date"],
            "date_posted_source": "JobBoardMemberships[board].ExternalPostedDate",
        }
        job.raw["detail"]["request"] = {
            "status": detail.get("status"),
            "seconds": detail.get("seconds"),
            "response_bytes": detail.get("response_bytes"),
            "response_sha256": detail.get("response_sha256"),
            "request_url": detail.get("request_url"),
        }
        job.raw["employer_note"] = EMPLOYER_NOTE
        job.raw["official_entry_url"] = OFFICIAL_ENTRY
        job.raw["official_final_url"] = OFFICIAL_FINAL
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_final_url": OFFICIAL_FINAL,
            "listing_url": LISTING_URL,
            "listing_authority": "The exact UKG/UltiPro job board linked by the company's current AppLogic Networks careers page.",
            "employer": EMPLOYER,
            "employer_note": EMPLOYER_NOTE,
            "request_scope": "Only the company board PRO1053PROC/d6eed263-... and the exact opportunity IDs it publishes; no tenant-wide or third-party discovery.",
            "detail_method": "Each company detail page embeds the complete opportunity object, including the full Description field.",
            "filters_applied": False,
            "pagination": {
                "page_size": PAGE_SIZE,
                "sort": "postedDateDesc",
                "skip_parameter": "opportunitySearch.Skip",
                "observed_total": None,
            },
            "request_shapes": {
                "listing": {
                    "method": "POST",
                    "url": LISTING_API_URL,
                    "headers": ["X-RequestVerificationToken", "X-Requested-With", "Referer"],
                    "body": build_listing_payload(),
                },
                "detail": {"method": "GET", "url": DETAIL_URL.format(opportunity_id="<uuid>")},
                "apply": {"method": "GET", "url": APPLY_URL.format(opportunity_id="<uuid>"),
                          "observed_behavior": "302 to Account/Login for anonymous visitors"},
            },
        })
        try:
            official_entry = await self._official_entry()
            board_page = await self._board_page()
            initial = await self._listing(board_page["token"])
        except Exception as exc:
            self.result.errors.append(f"Sandvine source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.expected_total = initial["total_count"]
        self.result.metadata["official_entry"] = official_entry
        self.result.metadata["board_page"] = {k: v for k, v in board_page.items() if k != "token"}
        self.result.metadata["initial_listing"] = {
            "total_count": initial["total_count"],
            "ids": initial["ids"],
            "pages": initial["pages"],
        }
        self.result.metadata["pagination"]["observed_total"] = initial["total_count"]
        self.result.metadata["pagination"]["page_count"] = len(initial["pages"])

        semaphore = asyncio.Semaphore(max(1, self.options.detail_concurrency or 2))
        detail_started = time.perf_counter()

        async def one(row):
            async with semaphore:
                try:
                    return row, await self._detail(row), None
                except Exception as exc:
                    return row, None, exc

        outcomes = await asyncio.gather(*(one(row) for row in initial["rows"]))
        detail_seconds = max(0.0, time.perf_counter() - detail_started)

        jobs, detail_audits, failed_jobs = [], {}, []
        for row, detail, error in outcomes:
            identity = str(row["Id"])
            if error is not None:
                message = f"{type(error).__name__}: {error}"
                self.result.errors.append(f"sandvine_detail_fetch_failed:{identity}:{message}")
                failed_jobs.append({"id": identity, "url": detail_url(identity), "reason": message})
                unavailable = self.job(
                    {"listing": row, "detail_error": message},
                    title=normalized_title(row.get("Title")),
                    url=detail_url(identity),
                    job_id=identity,
                    requisition_id=row.get("RequisitionNumber"),
                    apply_url=apply_url(identity),
                    description="",
                    description_unavailable=True,
                    locations=[value for loc in row.get("Locations") or [] if isinstance(loc, dict)
                               for value in format_location(loc)],
                    department=row.get("JobCategoryName"),
                    employment_type="Full Time" if row.get("FullTime") is True else None,
                    workplace_type=WORKPLACE_TYPES.get(row.get("JobLocationType")),
                    date_posted=row.get("PostedDate"),
                    date_posted_raw=row.get("PostedDate"),
                    employer=EMPLOYER,
                    warnings=["detail_fetch_failed"],
                )
                jobs.append(unavailable)
                continue
            if normalized_title(detail["title"]) != normalized_title(row.get("Title")):
                self.result.errors.append(f"sandvine_listing_detail_title_mismatch:{identity}")
            if normalized_title(detail["requisition_id"]) != normalized_title(row.get("RequisitionNumber")):
                self.result.errors.append(f"sandvine_listing_detail_requisition_mismatch:{identity}")
            if detail["external_posted_date"] != row.get("PostedDate"):
                self.result.errors.append(f"sandvine_listing_detail_posted_date_mismatch:{identity}")
            job = self._make_job(row, detail)
            jobs.append(job)
            detail_audits[identity] = {
                "url": detail["detail_url"],
                "status": detail["status"],
                "seconds": detail["seconds"],
                "requisition_id": detail["requisition_id"],
                "external_posted_date": detail["external_posted_date"],
                "listing_posted_date": row.get("PostedDate"),
                "description_sha256": detail["description_sha256"],
                "description_normalized_chars": detail["description_normalized_chars"],
                "description_fnv1a64": detail["description_fnv1a64"],
                "role_attachments": detail["role_attachments"],
                "response_sha256": detail["response_sha256"],
            }
            if detail["role_attachments"]:
                self.result.errors.append(f"sandvine_role_attachments_require_exact_review:{identity}")

        # Fresh final pass: the company entry, board and complete membership must
        # still be the same after all detail requests.
        final = None
        final_entry = final_board = None
        try:
            final_entry = await self._official_entry()
            final_board = await self._board_page()
            final = await self._listing(final_board["token"], expected_count=initial["total_count"])
        except Exception as exc:
            self.result.errors.append(f"Sandvine final source recheck failed: {type(exc).__name__}: {exc}")

        changed_fields = {}
        if final is not None:
            final_by_id = {str(row["Id"]): row for row in final["rows"]}
            initial_by_id = {str(row["Id"]): row for row in initial["rows"]}
            for identity in initial["ids"]:
                if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]:
                    changed_fields[identity] = sorted(
                        key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                    )
            same_ids = initial["ids"] == final["ids"]
            counts_match = initial["total_count"] == final["total_count"]
            entry_match = (
                final_entry is not None
                and final_entry["observed_open_roles_href"] == official_entry["observed_open_roles_href"]
                and final_entry["listing_url"] == LISTING_URL
            )
            board_match = (
                final_board is not None
                and final_board["board"] == board_page["board"]
                and final_board["job_board_choices"] == 0
            )
            final_matches = same_ids and counts_match and not changed_fields and entry_match and board_match
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_board_page"] = {k: v for k, v in (final_board or {}).items() if k != "token"}
            self.result.metadata["final_listing"] = {
                "total_count": final["total_count"],
                "ids": final["ids"],
                "pages": final["pages"],
            }
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["ids"],
                "final_ids": final["ids"],
                "initial_count": initial["total_count"],
                "final_count": final["total_count"],
                "same_ids_and_order": same_ids,
                "counts_match": counts_match,
                "all_listing_fields_match": not changed_fields,
                "changed_fields_by_job_id": changed_fields,
                "new_job_ids": [identity for identity in final["ids"] if identity not in initial["ids"]],
                "removed_job_ids": [identity for identity in initial["ids"] if identity not in final["ids"]],
                "official_entry_unchanged": entry_match,
                "board_configuration_unchanged": board_match,
                "complete": final_matches,
            }
            if not entry_match:
                self.result.errors.append("sandvine_official_entry_changed_during_collection")
            if not board_match:
                self.result.errors.append("sandvine_board_configuration_changed_during_collection")
            if not same_ids or not counts_match:
                self.result.errors.append("sandvine_listing_membership_changed_during_collection")
            if changed_fields:
                self.result.errors.append("sandvine_listing_fields_changed_during_collection")
        else:
            final_matches = False
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["ids"],
                "final_ids": None,
                "complete": False,
                "reason": "final source recheck failed",
            }

        self.result.jobs = jobs
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.timings["listing_phase_seconds"] = initial["seconds"]
        self.result.timings["detail_phase_seconds"] = detail_seconds
        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.metadata["detail_audits"] = detail_audits
        self.result.metadata["failed_jobs"] = failed_jobs
        self.result.metadata["failed_jobs_count"] = len(failed_jobs)
        self.result.metadata["current_native_membership_reconciled"] = final_matches
        self.result.listing_complete = bool(
            final_matches and len(jobs) == initial["total_count"] and not failed_jobs and not self.result.errors
        )
        return self.result
