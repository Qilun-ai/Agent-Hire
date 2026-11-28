"""VMware's current official careers journey (reviewed 2026-11-08, Round 3).

The ledger entry ``https://careers.vmware.com/`` no longer serves a careers
site: the host resolves to 208.91.0.132 and TCP 80/443 both time out.  VMware's
current corporate site publishes a ``Careers`` link to Broadcom's parent careers
page, which publishes a ``Search Jobs`` CTA to Broadcom's parent-wide Workday
site::

    https://www.vmware.com/                       (official VMware site)
      -> Careers -> https://www.broadcom.com/jobs
      -> 301 -> https://www.broadcom.com/company/careers
      -> Search Jobs -> https://broadcom.wd1.myworkdayjobs.com/External_Career

The Workday site is Broadcom's global ``External_Career`` board (390 postings at
capture, 20 pages).  Its only facets are location, full/part-time, job type and
job category; there is no VMware facet, VMware-only site or VMware-scoped
count.  The free-text search is a fuzzy keyword search: ``vmware`` returns 39
rows, 22 of which have a VMware legal employer and 17 of which are Broadcom /
CA / Avago / Brocade roles, while 6 VMware-employer rows are missed entirely.

The Round 3 user decision (``reports/pi-orchestrator/USER_DECISIONS.md`` item 4,
2026-11-08) makes a parent board acceptable scope for an absorbed company when
membership is explicitly attributed by a structured field.  This adapter uses
Workday's structured ``hiringOrganization.name``: a parent-board detail is a
VMware posting exactly when that legal-employer field names a VMware entity.
The keyword search is not used for membership, and descriptions/titles that
merely mention VMware products are excluded.  The adapter enumerates the parent
board only as far as needed to apply that rule (all 390 rows, because no VMware
facet exists) and retains every included posting's full detail body.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from hashlib import sha256
import re
import socket
import time

from bs4 import BeautifulSoup

from ..models import text_html
from .base import Adapter


LEDGER_ENTRY = "https://careers.vmware.com/"
OFFICIAL_ENTRY = "https://www.vmware.com/"
VMWARE_NAVIGATION = "https://www.vmware.com/api/navigation?locale=en-us&url=&site=vm"
VMWARE_CAREERS_PAGE = "https://www.vmware.com/careers"
BROADCOM_JOBS = "https://www.broadcom.com/jobs"
BROADCOM_CAREERS = "https://www.broadcom.com/company/careers"
BROADCOM_CAREERS_DATA = (
    "https://www.broadcom.com/api/getjson?url=company/careers&locale=en-us"
    "&type=general_page&id=blt95df35e41ed40401_en-us"
)
WORKDAY_SITE = "https://broadcom.wd1.myworkdayjobs.com/External_Career"
WORKDAY_HOST = "broadcom.wd1.myworkdayjobs.com"
WORKDAY_CXS = "https://broadcom.wd1.myworkdayjobs.com/wday/cxs/broadcom/External_Career"
TENANT = "broadcom"
SITE = "External_Career"
PAGE_SIZE = 20
VERIFICATION_REVISION = "vmware.round3.parent-board-structured-legal-employer.1"
MEMBERSHIP_FIELD = "hiringOrganization.name"
MEMBERSHIP_RULE = (
    "Parent-board posting is VMware membership iff the structured Workday "
    "detail field hiringOrganization.name names a VMware legal employer "
    "(case-insensitive word-boundary match for 'vmware')."
)

CAREERS_URL = "https://www.broadcom.com/jobs"
WORKDAY_URL = "https://broadcom.wd1.myworkdayjobs.com/External_Career"
VMWARE_TOKEN = re.compile(r"\bvmware\b", re.I)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SALARY_LABEL = re.compile(
    r"(?:annual base salary range|base salary range|salary range|pay range|"
    r"compensation range|annual salary|base pay range)[^.]{0,160}?",
    re.I,
)
MONEY = re.compile(
    r"(?:USD|EUR|GBP|CAD|AUD|SGD|INR|JPY|CHF|HKD|NZD|\$|€|£)\s?"
    r"[\d,]+(?:\.\d+)?(?:\s*(?:to|-|–)\s*"
    r"(?:USD|EUR|GBP|CAD|AUD|SGD|INR|JPY|CHF|HKD|NZD|\$|€|£)?\s?"
    r"[\d,]+(?:\.\d+)?)?",
    re.I,
)
PREAMBLE = re.compile(
    r"^Please Note:\s*1\.\s*If you are a first time user.*?before you apply\.\s*",
    re.I,
)
JOB_HEADING = re.compile(r"^Job Description:\s*", re.I)
JOB_DUPLICATE = re.compile(r"^Job Description\s+", re.I)


def normalize(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def clean_role_text(value: str) -> str:
    """Drop the standard Workday application-instruction preamble.

    The publisher's ``jobDescription`` starts with a ``Please Note`` block about
    candidate accounts, followed by a ``Job Description:`` heading and sometimes
    a duplicate ``Job Description`` paragraph.  Those are application
    instructions, not role content.  Everything after the preamble is retained.
    """
    text = normalize(value)
    text = PREAMBLE.sub("", text)
    if text.lower().startswith("please note:"):
        # Fallback for a differently worded account-instruction block.
        marker = text.lower().find("job description:")
        if marker >= 0:
            text = text[marker + len("job description:"):].strip()
    text = JOB_HEADING.sub("", text)
    text = JOB_DUPLICATE.sub("", text)
    return text.strip()


def _top_level_text(node) -> str:
    try:
        return normalize(node.get_text(" ", strip=True))
    except AttributeError:
        return ""


def clean_role_html(value: str) -> tuple[str, dict]:
    """Remove the leading application-instruction block from the description HTML."""
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    container = soup.body if soup.body is not None else soup
    children = list(container.contents)
    marker = None
    for index, child in enumerate(children):
        text = _top_level_text(child)
        if text.lower().startswith("job description"):
            marker = index
            break
    if marker is not None:
        for child in children[: marker + 1]:
            child.extract()
        first = container.find()
        if first is not None and _top_level_text(first).lower() == "job description":
            first.extract()
        method = "job_description_heading"
    else:
        removed = 0
        for child in list(container.contents):
            if _top_level_text(child).lower().startswith("please note"):
                child.extract()
                removed += 1
            else:
                break
        method = "please_note_paragraphs" if removed else "none"
    cleaned = "".join(str(child) for child in container.contents)
    return cleaned, {
        "method": method,
        "full_html_chars": len(value or ""),
        "cleaned_html_chars": len(cleaned),
        "full_text_chars": len(normalize(text_html(value or ""))),
        "cleaned_text_chars": len(clean_role_text(text_html(cleaned))),
    }


def parse_salary(role_text: str) -> dict | None:
    label = SALARY_LABEL.search(role_text or "")
    if not label:
        return None
    money = MONEY.search(role_text, label.end())
    if not money:
        return None
    start = label.start()
    end = money.end()
    return {
        "text": normalize(role_text[start:end]),
        "range": normalize(money.group(0)),
        "source": "jobPostingInfo.jobDescription",
    }


def parse_sections(html: str) -> list[dict]:
    """Keep each published heading and the text below it.

    Workday publishers use real heading tags for some descriptions and a fully
    bold paragraph (``<p><b>Requirements</b></p>``) for others.  Both are role
    headings; a bold sentence inside a normal paragraph is not.
    """
    soup = BeautifulSoup(html or "", "lxml")
    headings: list[tuple[object, str]] = []
    for node in soup.find_all(["h1", "h2", "h3", "h4", "p"]):
        label = normalize(node.get_text(" ", strip=True))
        if not label:
            continue
        if node.name == "p":
            bold = node.find(["b", "strong"])
            if bold is None:
                continue
            bold_label = normalize(bold.get_text(" ", strip=True))
            if (bold_label != label or len(label) > 120
                    or len(label.split()) > 16):
                continue
        headings.append((node, label))
    heading_ids = {id(node) for node, _ in headings}
    sections = []
    for heading, label in headings:
        chunks = []
        for sibling in heading.next_siblings:
            if (getattr(sibling, "name", None) in ("h1", "h2", "h3", "h4")
                    or id(sibling) in heading_ids):
                break
            if isinstance(sibling, str):
                chunks.append(sibling)
            else:
                chunks.append(sibling.get_text(" ", strip=True))
        sections.append({"heading": label, "text": normalize(" ".join(chunks))})
    return sections


def _find_dicts(value, predicate):
    found = []
    if isinstance(value, dict):
        if predicate(value):
            found.append(value)
        for item in value.values():
            found.extend(_find_dicts(item, predicate))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_dicts(item, predicate))
    return found


def parse_vmware_navigation(payload: dict) -> dict:
    """Read VMware's own navigation payload for the published Careers link."""
    links = _find_dicts(
        payload,
        lambda item: isinstance(item.get("title"), str)
        and item.get("title", "").strip().lower() == "careers"
        and isinstance(item.get("url"), str),
    )
    urls = [item["url"].strip() for item in links]
    if CAREERS_URL not in urls:
        raise ValueError(
            "VMware's navigation payload no longer publishes the Broadcom Careers link"
        )
    return {
        "careers_url": CAREERS_URL,
        "careers_links": [{"title": item["title"].strip(), "url": item["url"].strip()}
                          for item in links],
        "footer_text": normalize(str(payload.get("footer_text") or "")),
        "template": payload.get("template"),
        "request_url": VMWARE_NAVIGATION,
    }


def parse_broadcom_careers_data(payload: dict) -> dict:
    """Read Broadcom's page payload for its published Search Jobs CTA."""
    links = _find_dicts(
        payload,
        lambda item: isinstance(item.get("title"), str)
        and item.get("title", "").strip().lower() == "search jobs"
        and isinstance(item.get("url"), str),
    )
    urls = [item["url"].strip() for item in links]
    if WORKDAY_SITE not in urls:
        raise ValueError(
            "Broadcom's careers payload no longer publishes the Workday Search Jobs link"
        )
    return {
        "search_jobs_url": WORKDAY_SITE,
        "search_jobs_links": [{"title": item["title"].strip(), "url": item["url"].strip()}
                              for item in links],
        "page_title": normalize(str(payload.get("title") or "")),
        "page_url": payload.get("url"),
        "request_url": BROADCOM_CAREERS_DATA,
    }


def parse_workday_shell(html: str, final_url: str) -> dict:
    parsed = BeautifulSoup(html or "", "lxml")
    canonical = parsed.select_one('link[rel="canonical"]')
    canonical_url = (canonical.get("href") or "").strip() if canonical else ""
    tenant = re.search(r'\btenant\s*:\s*["\']([^"\']+)["\']', html or "")
    site = re.search(r'\bsiteId\s*:\s*["\']([^"\']+)["\']', html or "")
    request_locale = re.search(r'\brequestLocale\s*:\s*["\']([^"\']+)["\']', html or "")
    identity = {
        "url": final_url,
        "canonical_url": canonical_url,
        "tenant": tenant.group(1) if tenant else "",
        "site_id": site.group(1) if site else "",
        "request_locale": request_locale.group(1) if request_locale else "",
    }
    if (final_url != WORKDAY_SITE or canonical_url != WORKDAY_SITE
            or identity["tenant"] != TENANT or identity["site_id"] != SITE):
        raise ValueError("Broadcom's published Workday site identity changed")
    return identity


def parse_listing_page(payload: dict, offset: int, *, source_label: str = "listing") -> dict:
    if not isinstance(payload, dict):
        raise ValueError(f"Workday {source_label} response is not an object")
    total = payload.get("total")
    rows = payload.get("jobPostings")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError(f"Workday {source_label} total is invalid: {total!r}")
    if not isinstance(rows, list):
        raise ValueError(f"Workday {source_label} jobPostings is not a list")
    if len(rows) > PAGE_SIZE:
        raise ValueError(f"Workday {source_label} returned {len(rows)} rows for page size {PAGE_SIZE}")
    parsed_rows = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"Workday {source_label} row is not an object")
        path = row.get("externalPath")
        title = row.get("title")
        if not isinstance(path, str) or not path.startswith("/job/"):
            raise ValueError(f"Workday {source_label} row externalPath is invalid: {path!r}")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"Workday {source_label} row title is invalid for {path}")
        parsed_rows.append({
            "externalPath": path,
            "title": normalize(title),
            "timeType": row.get("timeType"),
            "locationsText": row.get("locationsText"),
            "postedOn": row.get("postedOn"),
            "bulletFields": row.get("bulletFields") if isinstance(row.get("bulletFields"), list) else [],
        })
    facets = []
    for facet in payload.get("facets") or []:
        if not isinstance(facet, dict):
            continue
        facets.append({
            "parameter": facet.get("facetParameter"),
            "descriptor": facet.get("descriptor"),
            "values": [
                {"id": value.get("id"), "descriptor": value.get("descriptor"),
                 "count": value.get("count")}
                for value in (facet.get("values") or []) if isinstance(value, dict)
            ],
        })
    return {
        "offset": offset,
        "total": total,
        "page_size": PAGE_SIZE,
        "rows": parsed_rows,
        "row_count": len(parsed_rows),
        "facets": facets,
        "user_authenticated": bool(payload.get("userAuthenticated")),
        "source_label": source_label,
    }


def facet_has_vmware(facets: list[dict]) -> bool:
    for facet in facets:
        if VMWARE_TOKEN.search(str(facet.get("parameter") or "")):
            return True
        if VMWARE_TOKEN.search(str(facet.get("descriptor") or "")):
            return True
        for value in facet.get("values") or []:
            if VMWARE_TOKEN.search(str(value.get("descriptor") or "")):
                return True
    return False


def is_vmware_employer(name: str | None) -> bool:
    return bool(VMWARE_TOKEN.search(name or ""))


def membership_decision(employer: str | None) -> dict:
    """Apply the Round 3 structured legal-employer attribution rule."""
    value = normalize(employer)
    included = is_vmware_employer(value)
    return {
        "included": included,
        "rule": "structured_legal_employer_attribution",
        "field": MEMBERSHIP_FIELD,
        "value": value or None,
        "fallback_used": False,
        "reason": (
            "structured legal employer names VMware"
            if included
            else "structured legal employer is not a VMware entity"
        ),
    }


def workday_human_url(external_path: str) -> str:
    return f"https://{WORKDAY_HOST}/en-US/{SITE}{external_path}"


def parse_detail(payload: dict, row: dict) -> dict:
    """Parse one exact Workday detail response and its legal employer."""
    if not isinstance(payload, dict):
        raise ValueError("Workday detail response is not an object")
    info = payload.get("jobPostingInfo")
    if not isinstance(info, dict):
        raise ValueError("Workday detail response has no jobPostingInfo")
    external_path = row["externalPath"]
    path_tail = external_path.rsplit("/", 1)[-1]
    title = normalize(info.get("title"))
    job_req_id = info.get("jobReqId")
    posting_id = info.get("jobPostingId")
    site_id = info.get("jobPostingSiteId")
    description = info.get("jobDescription")
    employer = (payload.get("hiringOrganization") or {}).get("name") \
        if isinstance(payload.get("hiringOrganization"), dict) else None
    checks = {
        "title_present": bool(title),
        "listing_title_matches": title == normalize(row.get("title")),
        "job_req_id_present": isinstance(job_req_id, str) and bool(job_req_id),
        "posting_id_matches_path": isinstance(posting_id, str) and posting_id == path_tail,
        "site_matches": site_id == SITE,
        "description_present": isinstance(description, str) and bool(description.strip()),
        "employer_present": isinstance(employer, str) and bool(employer.strip()),
    }
    if not checks["title_present"] or not checks["job_req_id_present"]:
        raise ValueError(f"Workday detail identity is incomplete for {external_path}")
    cleaned_html, cleaning = clean_role_html(description or "")
    role_text = clean_role_text(text_html(cleaned_html))
    full_text = normalize(text_html(description or ""))
    sections = parse_sections(cleaned_html)
    responsibilities = None
    qualifications = None
    responsibility_sections = []
    qualification_sections = []
    for section in sections:
        heading = section["heading"].lower()
        if re.search(
                r"responsibilit|what you.ll do|duties|you will|key tasks", heading):
            responsibility_sections.append(section)
            if responsibilities is None:
                responsibilities = section["text"] or None
        if re.search(
                r"qualif|requirement|what we.re looking|skills|experience|about you", heading):
            qualification_sections.append(section)
            if qualifications is None:
                qualifications = section["text"] or None
    locations = []
    primary = info.get("jobRequisitionLocation")
    if isinstance(primary, dict) and primary.get("descriptor"):
        locations.append(primary["descriptor"])
    for extra in info.get("additionalLocations") or []:
        if isinstance(extra, str) and extra and extra not in locations:
            locations.append(extra)
    start_date = info.get("startDate") if isinstance(info.get("startDate"), str) else None
    end_date = info.get("endDate") if isinstance(info.get("endDate"), str) else None
    if start_date is not None and not ISO_DATE.match(start_date):
        start_date = None
    if end_date is not None and not ISO_DATE.match(end_date):
        end_date = None
    human_url = workday_human_url(external_path)
    record = {
        "job_id": job_req_id,
        "requisition_id": job_req_id,
        "title": title,
        "url": human_url,
        "apply_url": human_url + "/apply",
        "detail_api_url": f"{WORKDAY_CXS}{external_path}",
        "external_path": external_path,
        "posting_id": posting_id,
        "site_id": site_id,
        "employer": employer,
        "is_vmware_employer": is_vmware_employer(employer),
        "membership_decision": membership_decision(employer),
        "locations": locations,
        "location_detail": primary if isinstance(primary, dict) else None,
        "country": info.get("country"),
        "employment_type": info.get("timeType") or row.get("timeType"),
        "workplace_type": None,
        "date_posted": start_date,
        "date_posted_raw": {"postedOn": info.get("postedOn"), "startDate": info.get("startDate")},
        "date_updated": None,
        "valid_through": end_date,
        "salary": parse_salary(role_text),
        "description_html": cleaned_html,
        "description_text": role_text,
        "description_full_text": full_text,
        "description_chars": len(role_text),
        "description_sha256": sha256(role_text.encode()).hexdigest(),
        "description_fnv1a64": fnv1a64(role_text),
        "description_method": "workday_cxs.jobPostingInfo.jobDescription",
        "description_source_url": f"{WORKDAY_CXS}{external_path}",
        "description_verified": bool(role_text) and len(role_text) >= 100,
        "description_unavailable": not role_text,
        "description_sections": sections,
        "responsibilities": responsibilities,
        "qualifications": qualifications,
        "responsibility_sections": responsibility_sections,
        "qualification_sections": qualification_sections,
        "cleaning": cleaning,
        "detail_identity": checks,
        "raw_listing": row,
        "raw_detail": payload,
    }
    return record


def scope_decision(board: dict, search: dict, classified: list[dict],
                   journey: dict) -> dict:
    """Apply the Round 3 structured legal-employer membership rule.

    ``classified`` contains one parsed detail per current parent-board row.  A
    row is included only when its structured Workday ``hiringOrganization.name``
    names a VMware entity.  Title/description mentions are evidence of a
    borderline exclusion, never a membership fallback while the field exists.
    """
    included = [item for item in classified if item.get("is_vmware_employer")]
    excluded = [item for item in classified if not item.get("is_vmware_employer")]
    included_ids = {item.get("job_id") for item in included}
    search_ids = {
        (row.get("bulletFields") or [None])[0]
        for row in search.get("rows", [])
    }
    search_included = [item for item in included if item.get("job_id") in search_ids]
    search_excluded = [item for item in excluded if item.get("job_id") in search_ids]
    missing_from_search = sorted(included_ids - search_ids)
    employer_counts = Counter(item.get("employer") for item in included)
    excluded_mentions = []
    for item in excluded:
        title_match = bool(VMWARE_TOKEN.search(item.get("title") or ""))
        description_match = bool(VMWARE_TOKEN.search(item.get("description_text") or ""))
        if not title_match and not description_match:
            continue
        excluded_mentions.append({
            "job_id": item.get("job_id"),
            "title": item.get("title"),
            "employer": item.get("employer"),
            "external_path": item.get("external_path"),
            "title_mentions_vmware": title_match,
            "description_mentions_vmware": description_match,
            "decision": "excluded",
            "reason": (
                "title names a VMware product but the structured legal employer "
                "is not a VMware entity; product mention is not membership"
                if title_match else
                "description mentions VMware only as a product/skill; the "
                "structured legal employer is not a VMware entity"
            ),
        })
    reason = (
        "Round 3 user decision (reports/pi-orchestrator/USER_DECISIONS.md item 4, "
        "2026-11-08) makes a parent board acceptable scope for an absorbed company when "
        "postings are explicitly attributed to it by a structured field. VMware's ledger "
        "careers host careers.vmware.com is a TCP black hole (208.91.0.132, ports 80/443 "
        "timeout) and www.vmware.com/careers is Page Not Found; the current official VMware "
        "site's Careers link reaches Broadcom's parent careers page, whose Search Jobs CTA "
        f"opens Broadcom's parent-wide Workday External_Career board ({board.get('total')} "
        f"jobs, {board.get('page_count', '?')} pages) with no VMware facet. The board's "
        f"per-posting structured {MEMBERSHIP_FIELD} is the attribution field: "
        f"{len(included)} of {len(classified)} postings name a VMware legal employer "
        f"({len(employer_counts)} distinct VMware legal entities) and are collected as "
        f"VMware membership. {len(excluded)} postings are excluded because that field is "
        f"not VMware; {len(excluded_mentions)} of them mention VMware only as a product or "
        "skill (including one title that names VMware Cloud Foundation but whose legal "
        "employer is CA Singapore). The keyword search is not used for membership: it "
        f"returns {search.get('total')} fuzzy matches ({len(search_included)} VMware-employer, "
        f"{len(search_excluded)} non-VMware) and misses {len(missing_from_search)} "
        "VMware-employer rows."
    )
    return {
        "company_scoped": True,
        "company_scope_basis": "round3_absorbed_company_structured_parent_attribution",
        "user_decision": "reports/pi-orchestrator/USER_DECISIONS.md item 4 (2026-11-08)",
        "reason": reason,
        "ledger_entry": LEDGER_ENTRY,
        "ledger_entry_status": journey.get("legacy_entry_probe", {}).get("result"),
        "official_journey": {
            "vmware_home": OFFICIAL_ENTRY,
            "careers_link": CAREERS_URL,
            "broadcom_careers": BROADCOM_CAREERS,
            "search_jobs": WORKDAY_SITE,
        },
        "parent_board_jobs": board.get("total"),
        "parent_board_pages": board.get("page_count"),
        "parent_board_facets": board.get("facets"),
        "has_vmware_facet": board.get("has_vmware_facet"),
        "membership_rule": {
            "rule": MEMBERSHIP_RULE,
            "field": MEMBERSHIP_FIELD,
            "field_kind": "structured legal employer",
            "predicate": "case-insensitive word-boundary match for 'vmware'",
            "fallback_used": False,
            "fallback_note": (
                "The structured legal-employer field is present on every parent-board "
                "detail, so no title/description-header fallback was needed."
            ),
            "parent_board_rows_classified": len(classified),
            "included": len(included),
            "excluded": len(excluded),
            "included_ids": sorted(included_ids),
            "included_by_employer": [
                {"employer": employer, "count": count}
                for employer, count in sorted(employer_counts.items())
            ],
            "excluded_vmware_mentions": excluded_mentions,
        },
        "search_total": search.get("total"),
        "search_vmware_employer": len(search_included),
        "search_non_vmware": len(search_excluded),
        "search_missing_vmware_employer": missing_from_search,
        "observed_vmware_employer": len(included),
        "observed_ids": sorted(included_ids),
        "observed_not_collected_membership": False,
    }


class VMware(Adapter):
    """Collect parent-board postings attributed to VMware by structured employer."""

    VERIFICATION_REVISION = VERIFICATION_REVISION

    def __init__(self, http, company, source, options, *, cached_details=None):
        super().__init__(http, company, source, options)
        self.cached_details = cached_details or {}

    def _make_job(self, item: dict):
        """Convert one included structured-attribution detail into a Job."""
        job = self.job(
            {
                "listing": item.get("raw_listing"),
                "detail": item.get("raw_detail"),
                "membership_decision": item.get("membership_decision"),
                "detail_source": item.get("detail_source"),
                "detail_reused_from": item.get("detail_reused_from"),
                "present_in_final_listing": item.get("present_in_final_listing"),
                "parent_board": {
                    "url": WORKDAY_SITE,
                    "total": item.get("parent_board_total"),
                    "listing_index": item.get("listing_index"),
                },
            },
            title=item["title"],
            url=item["url"],
            job_id=item["job_id"],
            requisition_id=item.get("requisition_id"),
            apply_url=item.get("apply_url"),
            description=item.get("description_html") or "",
            method=item.get("description_method"),
            description_url=item.get("description_source_url"),
            locations=item.get("locations") or [],
            employment_type=item.get("employment_type"),
            workplace_type=item.get("workplace_type"),
            date_posted=item.get("date_posted"),
            date_posted_raw=item.get("date_posted_raw"),
            date_updated=item.get("date_updated"),
            valid_through=item.get("valid_through"),
            salary=item.get("salary"),
            qualifications=item.get("qualifications"),
            responsibilities=item.get("responsibilities"),
            employer=item.get("employer"),
            language="en",
        )
        job.description_text = item.get("description_text") or ""
        job.description_verified = bool(item.get("description_verified"))
        job.description_unavailable = bool(item.get("description_unavailable"))
        return job

    def probe_legacy_entry(self) -> dict:
        """Record the exact network failure of the ledger careers host."""
        started = time.perf_counter()
        result = {
            "url": LEDGER_ENTRY,
            "host": "careers.vmware.com",
            "resolved_ips": [],
            "tcp_443": None,
            "tcp_80": None,
            "result": "unreachable",
        }
        try:
            result["resolved_ips"] = sorted({item[4][0] for item in
                                             socket.getaddrinfo("careers.vmware.com", 443)})
        except OSError as exc:
            result["result"] = f"dns_error: {type(exc).__name__}: {exc}"
            result["seconds"] = round(max(0.0, time.perf_counter() - started), 6)
            return result
        for port in (443, 80):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(6)
            probe_started = time.monotonic()
            try:
                sock.connect(("careers.vmware.com", port))
                result[f"tcp_{port}"] = {"connected": True,
                                         "seconds": round(time.monotonic() - probe_started, 3)}
            except OSError as exc:
                result[f"tcp_{port}"] = {
                    "connected": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "seconds": round(time.monotonic() - probe_started, 3),
                }
            finally:
                sock.close()
        if not any([(result.get("tcp_443") or {}).get("connected"),
                    (result.get("tcp_80") or {}).get("connected")]):
            result["result"] = "tcp_timeout"
        result["seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        return result

    async def _fetch_listing(self, label: str) -> dict:
        started = time.perf_counter()
        offset = 0
        rows: list[dict] = []
        offsets: list[int] = []
        first_total = None
        first_facets: list[dict] = []
        pages: list[dict] = []
        while True:
            if not self.page_guard():
                raise ValueError(f"Workday {label} page budget exhausted")
            payload = {"appliedFacets": {}, "limit": PAGE_SIZE,
                       "offset": offset, "searchText": ""}
            data = await self.http.json(WORKDAY_CXS + "/jobs", method="POST", json=payload)
            page = parse_listing_page(data, offset, source_label=label)
            if first_total is None:
                first_total = page["total"]
                first_facets = page["facets"]
            elif page["total"] != first_total:
                raise ValueError(
                    f"Workday {label} total changed {first_total} -> {page['total']} at offset {offset}"
                )
            if not page["rows"]:
                raise ValueError(f"Workday {label} returned an empty page at offset {offset}")
            rows.extend(page["rows"])
            offsets.append(offset)
            pages.append({
                "offset": offset,
                "row_count": page["row_count"],
                "total": page["total"],
            })
            offset += page["row_count"]
            if offset >= page["total"]:
                break
            if offset > page["total"] + PAGE_SIZE:
                raise ValueError(f"Workday {label} exceeded its advertised total")
        paths = [row["externalPath"] for row in rows]
        unique = set(paths)
        if len(unique) != len(paths):
            raise ValueError(f"Workday {label} has duplicate externalPath rows")
        if len(unique) != first_total:
            raise ValueError(
                f"Workday {label} membership {len(unique)} != advertised total {first_total}"
            )
        facets = first_facets
        return {
            "total": first_total,
            "page_size": PAGE_SIZE,
            "page_offsets": offsets,
            "page_count": len(offsets),
            "row_count": len(rows),
            "unique_count": len(unique),
            "rows": rows,
            "facets": facets,
            "has_vmware_facet": facet_has_vmware(facets),
            "external_paths_sha256": sha256("\n".join(sorted(unique)).encode()).hexdigest(),
            "pages": pages,
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    async def _fetch_search(self) -> dict:
        started = time.perf_counter()
        offset = 0
        rows: list[dict] = []
        first_total = None
        while True:
            if not self.page_guard():
                raise ValueError("Workday search page budget exhausted")
            payload = {"appliedFacets": {}, "limit": PAGE_SIZE,
                       "offset": offset, "searchText": "vmware"}
            data = await self.http.json(WORKDAY_CXS + "/jobs", method="POST", json=payload)
            page = parse_listing_page(data, offset, source_label="search")
            if first_total is None:
                first_total = page["total"]
            elif page["total"] != first_total:
                raise ValueError(f"Workday search total changed {first_total} -> {page['total']}")
            if not page["rows"]:
                break
            rows.extend(page["rows"])
            offset += page["row_count"]
            if offset >= page["total"]:
                break
        return {
            "search_text": "vmware",
            "total": first_total,
            "row_count": len(rows),
            "rows": rows,
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    async def _fetch_detail(self, row: dict) -> dict:
        data = await self.http.json(WORKDAY_CXS + row["externalPath"])
        return parse_detail(data, row)

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "ledger_entry_url": LEDGER_ENTRY,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": WORKDAY_SITE,
            "listing_authority": (
                "VMware's current official site publishes a Careers link to Broadcom's "
                "parent careers page, which publishes the Search Jobs CTA to Broadcom's "
                "parent-wide Workday External_Career board. Under the Round 3 user "
                "decision the board is acceptable scope for VMware only through the "
                "structured per-posting legal-employer field."
            ),
            "detail_method": (
                "Every parent-board detail was read from the exact listed Workday CXS "
                "externalPath. The structured top-level hiringOrganization.name is the "
                "membership field; every posting whose legal employer names VMware is "
                "retained with its full jobPostingInfo.jobDescription."
            ),
            "request_scope": (
                "VMware home + its own navigation payload + the legacy www.vmware.com/"
                "careers route probe; the exact Broadcom careers page and page payload; "
                "Broadcom's full External_Career listing; the VMware keyword search probe; "
                "and every listed detail for structured-employer classification. No guessed "
                "ATS slug, aggregator or VMware-only site was used."
            ),
            "collection_policy": (
                "The 390-row Broadcom board is enumerated because no VMware facet exists. "
                "A row is VMware membership only when the structured Workday "
                "hiringOrganization.name is a VMware legal entity. Product mentions in "
                "titles/descriptions do not count. All included postings get full details."
            ),
            "membership_rule": {
                "rule": MEMBERSHIP_RULE,
                "field": MEMBERSHIP_FIELD,
                "fallback_used": False,
            },
        })

        legacy = self.probe_legacy_entry()
        self.result.metadata["legacy_entry_probe"] = legacy
        self.result.warnings.append("legacy_official_entry_unreachable")

        journey = {"legacy_entry_probe": legacy}
        try:
            self.page_guard()
            home = await self.http.get(OFFICIAL_ENTRY)
            home_soup = BeautifulSoup(home.text, "lxml")
            home_title = normalize(home_soup.title.get_text(" ", strip=True)) if home_soup.title else ""
            journey["vmware_home"] = {
                "url": OFFICIAL_ENTRY,
                "status": home.status_code,
                "final_url": str(home.url),
                "title": home_title,
                "response_bytes": len(home.content),
                "response_sha256": sha256(home.content).hexdigest(),
            }
            navigation = await self.http.json(VMWARE_NAVIGATION)
            journey["navigation"] = parse_vmware_navigation(navigation)
        except Exception as exc:
            self.result.errors.append(f"vmware_journey_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["vmware_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        try:
            self.page_guard()
            careers_page = await self.http.get(VMWARE_CAREERS_PAGE)
            careers_soup = BeautifulSoup(careers_page.text, "lxml")
            careers_title = (normalize(careers_soup.title.get_text(" ", strip=True))
                             if careers_soup.title else "")
            careers_text = normalize(careers_soup.get_text(" ", strip=True))
            journey["vmware_careers_page"] = {
                "url": VMWARE_CAREERS_PAGE,
                "status": careers_page.status_code,
                "final_url": str(careers_page.url),
                "title": careers_title,
                "raw_shell_page_not_found": "page not found" in careers_text.lower(),
                "note": (
                    "The raw 200 response is a Broadcom JavaScript shell; the rendered "
                    "page is checked independently by the browser reference."
                ),
                "response_bytes": len(careers_page.content),
                "response_sha256": sha256(careers_page.content).hexdigest(),
            }
            if "page not found" not in careers_text.lower():
                self.result.warnings.append("vmware_careers_raw_shell_requires_rendered_check")
        except Exception as exc:
            journey["vmware_careers_page"] = {
                "url": VMWARE_CAREERS_PAGE,
                "error": f"{type(exc).__name__}: {exc}",
            }
            self.result.warnings.append("vmware_careers_page_probe_failed")

        try:
            self.page_guard()
            broadcom = await self.http.get(BROADCOM_JOBS)
            broadcom_soup = BeautifulSoup(broadcom.text, "lxml")
            journey["broadcom_careers"] = {
                "requested_url": BROADCOM_JOBS,
                "status": broadcom.status_code,
                "final_url": str(broadcom.url),
                "title": normalize(broadcom_soup.title.get_text(" ", strip=True))
                if broadcom_soup.title else "",
                "history": [{"status": item.status_code, "url": str(item.url),
                             "location": item.headers.get("location")}
                            for item in broadcom.history],
                "response_bytes": len(broadcom.content),
                "response_sha256": sha256(broadcom.content).hexdigest(),
            }
            if str(broadcom.url) != BROADCOM_CAREERS:
                raise ValueError(f"Broadcom careers journey ended at {broadcom.url}")
            page_payload = await self.http.json(BROADCOM_CAREERS_DATA)
            journey["broadcom_page_payload"] = parse_broadcom_careers_data(page_payload)
        except Exception as exc:
            self.result.errors.append(f"broadcom_journey_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["vmware_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        try:
            self.page_guard()
            shell = await self.http.get(
                WORKDAY_SITE,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            )
            journey["workday_shell"] = parse_workday_shell(shell.text, str(shell.url))
        except Exception as exc:
            self.result.errors.append(f"workday_shell_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["vmware_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        self.result.metadata["journey"] = journey
        self.result.metadata["official_entry"] = journey["vmware_home"]

        try:
            board = await self._fetch_listing("initial")
        except Exception as exc:
            self.result.errors.append(f"workday_board_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["vmware_source_collection_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result
        board_meta = {key: board[key] for key in (
            "total", "page_size", "page_offsets", "page_count", "row_count",
            "unique_count", "facets", "has_vmware_facet", "external_paths_sha256",
            "seconds",
        )}
        self.result.metadata["workday_board"] = board_meta
        if board["has_vmware_facet"]:
            self.result.warnings.append("vmware_facet_present_review_facet_use")

        try:
            search = await self._fetch_search()
        except Exception as exc:
            self.result.errors.append(f"workday_search_probe_failed: {type(exc).__name__}: {exc}")
            search = {"search_text": "vmware", "total": None, "row_count": 0, "rows": []}
        self.result.timings["vmware_source_collection_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )

        detail_started = time.perf_counter()
        semaphore = asyncio.Semaphore(max(1, self.options.detail_concurrency))
        details: dict[str, dict] = {}
        detail_errors: list[dict] = []
        detail_source_counts: Counter = Counter()
        rate_limited = False

        def cached_record(row):
            cached = self.cached_details.get(row["externalPath"])
            if not isinstance(cached, dict):
                return None, None
            payload = cached.get("payload", cached)
            meta = cached.get("meta") if isinstance(cached.get("meta"), dict) else None
            try:
                return parse_detail(payload, row), meta
            except Exception:
                return None, None

        def fail(row, message, cached=None):
            entry = {
                "external_path": row["externalPath"],
                "title": row["title"],
                "job_id": (row.get("bulletFields") or [None])[0],
                "error": message,
            }
            if cached is not None:
                entry["fallback_used"] = True
                entry["fallback_detail_source"] = cached.get("detail_reused_from")
            detail_errors.append(entry)

        async def one(row):
            nonlocal rate_limited
            async with semaphore:
                path = row["externalPath"]
                cached, cache_meta = cached_record(row)
                if (cached is not None and not cached["is_vmware_employer"]
                        and cached.get("detail_identity", {}).get("listing_title_matches")):
                    cached["detail_source"] = "reused_saved_cxs_response"
                    cached["detail_reused_from"] = cache_meta
                    details[path] = cached
                    detail_source_counts["reused"] += 1
                    return
                if rate_limited:
                    if cached is not None:
                        cached["detail_source"] = "reused_saved_cxs_response_after_429"
                        cached["detail_reused_from"] = cache_meta
                        details[path] = cached
                        detail_source_counts["fallback"] += 1
                    fail(row, "detail_fetch_skipped_after_429", cached)
                    return
                if not self.page_guard():
                    if cached is not None:
                        cached["detail_source"] = "reused_saved_cxs_response_after_budget"
                        cached["detail_reused_from"] = cache_meta
                        details[path] = cached
                        detail_source_counts["fallback"] += 1
                    fail(row, "detail_page_budget_exhausted", cached)
                    return
                try:
                    detail = await self._fetch_detail(row)
                    detail["detail_source"] = "live_cxs_response"
                    details[path] = detail
                    detail_source_counts["live"] += 1
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    if "429" in message:
                        rate_limited = True
                    if cached is not None:
                        cached["detail_source"] = "reused_saved_cxs_response_after_live_error"
                        cached["detail_live_error"] = message
                        cached["detail_reused_from"] = cache_meta
                        details[path] = cached
                        detail_source_counts["fallback"] += 1
                    fail(row, message, cached)

        await asyncio.gather(*(one(row) for row in board["rows"]))
        self.result.timings["vmware_detail_seconds"] = max(
            0.0, time.perf_counter() - detail_started
        )

        classified: list[dict] = []
        missing: list[dict] = []
        for index, row in enumerate(board["rows"]):
            item = details.get(row["externalPath"])
            if item is None:
                missing.append({
                    "external_path": row["externalPath"],
                    "title": row["title"],
                    "job_id": (row.get("bulletFields") or [None])[0],
                })
                continue
            item["listing_index"] = index
            item["parent_board_total"] = board["total"]
            classified.append(item)

        search_row_details = []
        search_by_path = {item["external_path"]: item for item in classified}
        for row in search.get("rows", []):
            item = search_by_path.get(row["externalPath"])
            if item is None:
                search_row_details.append({
                    "job_id": (row.get("bulletFields") or [None])[0],
                    "title": row.get("title"),
                    "employer": None,
                    "is_vmware_employer": None,
                    "external_path": row.get("externalPath"),
                })
            else:
                search_row_details.append({
                    "job_id": item.get("job_id"),
                    "title": item.get("title"),
                    "employer": item.get("employer"),
                    "is_vmware_employer": item.get("is_vmware_employer"),
                    "external_path": item.get("external_path"),
                })
        search["row_details"] = search_row_details
        search_ids = {(row.get("bulletFields") or [None])[0]
                      for row in search.get("rows", [])}
        for item in classified:
            item["search_probe_match"] = item.get("job_id") in search_ids
        search["vmware_employer_count"] = sum(
            1 for item in search_row_details if item.get("is_vmware_employer")
        )
        search["non_vmware_count"] = sum(
            1 for item in search_row_details if item.get("is_vmware_employer") is False
        )
        search["unclassified_count"] = sum(
            1 for item in search_row_details if item.get("is_vmware_employer") is None
        )

        final_started = time.perf_counter()
        try:
            final_board = await self._fetch_listing("final")
            final_paths = {row["externalPath"] for row in final_board["rows"]}
            final_meta = {
                "total": final_board["total"],
                "page_count": final_board["page_count"],
                "unique_count": final_board["unique_count"],
                "external_paths_sha256": final_board["external_paths_sha256"],
                "seconds": final_board["seconds"],
            }
            membership_errors = []
            if final_board["total"] != board["total"]:
                membership_errors.append(
                    f"parent_total_changed:{board['total']}->{final_board['total']}"
                )
            initial_paths = {row["externalPath"] for row in board["rows"]}
            if final_paths != initial_paths:
                membership_errors.append(
                    f"parent_membership_changed:{len(initial_paths)}->{len(final_paths)}"
                )
            final_meta["membership_errors"] = membership_errors
            final_meta["new_external_paths"] = sorted(final_paths - initial_paths)
            final_meta["removed_external_paths"] = sorted(initial_paths - final_paths)
            for item in classified:
                item["present_in_final_listing"] = item["external_path"] in final_paths
        except Exception as exc:
            final_meta = {"error": f"{type(exc).__name__}: {exc}"}
            final_paths = None
            for item in classified:
                item["present_in_final_listing"] = None
        self.result.timings["vmware_final_listing_seconds"] = max(
            0.0, time.perf_counter() - final_started
        )

        membership = [item for item in classified if item["is_vmware_employer"]]
        current_membership = [
            item for item in membership if item.get("present_in_final_listing") is not False
        ]
        for item in current_membership:
            self.add(self._make_job(item))

        scope_started = time.perf_counter()
        decision = scope_decision(
            {**board_meta, "page_count": board["page_count"]},
            search,
            classified,
            journey,
        )
        self.result.timings["vmware_scope_audit_seconds"] = max(
            0.0, time.perf_counter() - scope_started
        )

        membership_stable = bool(
            final_paths is not None
            and final_board["external_paths_sha256"] == board["external_paths_sha256"]
        )
        listing_complete = bool(
            len(classified) == board["row_count"]
            and not missing
            and not detail_errors
            and not rate_limited
            and membership_stable
        )
        self.result.expected_total = len(current_membership) if listing_complete else None
        self.result.listing_complete = listing_complete

        if missing:
            self.result.errors.append(f"parent_detail_missing:{len(missing)}")
        if detail_errors:
            self.result.errors.append(f"parent_detail_failures:{len(detail_errors)}")
        if rate_limited:
            self.result.errors.append("parent_detail_rate_limited_429")
        if final_meta.get("membership_errors"):
            self.result.errors.append("parent_membership_changed_during_run")
        if not membership_stable:
            self.result.warnings.append("parent_board_not_stable_during_run")
        if detail_source_counts["reused"]:
            self.result.warnings.append("reused_saved_parent_board_details")
        if detail_source_counts["fallback"]:
            self.result.warnings.append("detail_fallback_to_saved_parent_board_response")

        membership_summary = [{
            "job_id": item.get("job_id"),
            "requisition_id": item.get("requisition_id"),
            "title": item.get("title"),
            "url": item.get("url"),
            "apply_url": item.get("apply_url"),
            "employer": item.get("employer"),
            "locations": item.get("locations"),
            "employment_type": item.get("employment_type"),
            "date_posted": item.get("date_posted"),
            "valid_through": item.get("valid_through"),
            "salary": item.get("salary"),
            "description_chars": item.get("description_chars"),
            "description_sha256": item.get("description_sha256"),
            "description_verified": item.get("description_verified"),
            "external_path": item.get("external_path"),
            "listing_index": item.get("listing_index"),
            "present_in_final_listing": item.get("present_in_final_listing"),
            "detail_source": item.get("detail_source"),
        } for item in current_membership]

        classification_summary = [{
            "job_id": item.get("job_id"),
            "title": item.get("title"),
            "employer": item.get("employer"),
            "included": bool(item.get("is_vmware_employer")),
            "rule": item.get("membership_decision", {}).get("rule"),
            "field": item.get("membership_decision", {}).get("field"),
            "decision_reason": item.get("membership_decision", {}).get("reason"),
            "external_path": item.get("external_path"),
            "listing_index": item.get("listing_index"),
            "present_in_final_listing": item.get("present_in_final_listing"),
            "search_probe_match": item.get("search_probe_match"),
            "title_mentions_vmware": bool(VMWARE_TOKEN.search(item.get("title") or "")),
            "description_mentions_vmware": bool(
                VMWARE_TOKEN.search(item.get("description_text") or "")
            ),
            "date_posted": item.get("date_posted"),
            "description_chars": item.get("description_chars"),
            "description_sha256": item.get("description_sha256"),
            "detail_source": item.get("detail_source"),
            "detail_reused_from": item.get("detail_reused_from"),
        } for item in classified]

        detail_stats = {
            "listed_rows": board["row_count"],
            "classified_rows": len(classified),
            "details_missing": len(missing),
            "live_details": detail_source_counts.get("live", 0),
            "reused_details": detail_source_counts.get("reused", 0),
            "fallback_details": detail_source_counts.get("fallback", 0),
            "detail_failures": len(detail_errors),
            "rate_limited": rate_limited,
            "membership_rows": len(current_membership),
            "membership_verified_descriptions": sum(
                1 for item in current_membership if item.get("description_verified")
            ),
            "detail_seconds": self.result.timings.get("vmware_detail_seconds"),
        }

        self.result.metadata.update({
            "scope_decision": decision,
            "workday_search_probe": {
                "search_text": search.get("search_text"),
                "total": search.get("total"),
                "row_count": search.get("row_count"),
                "vmware_employer_count": search.get("vmware_employer_count"),
                "non_vmware_count": search.get("non_vmware_count"),
                "unclassified_count": search.get("unclassified_count"),
                "non_vmware_examples": [
                    item for item in search.get("row_details", [])
                    if item.get("is_vmware_employer") is False
                ][:20],
            },
            "membership_postings": membership_summary,
            "membership_count": len(current_membership),
            "membership_by_employer": decision["membership_rule"]["included_by_employer"],
            "membership_ids": [item.get("job_id") for item in current_membership],
            "parent_board_classification": classification_summary,
            "parent_board_classification_counts": {
                "rows": len(classification_summary),
                "included": sum(1 for item in classification_summary if item["included"]),
                "excluded": sum(1 for item in classification_summary if not item["included"]),
                "excluded_title_mentions_vmware": sum(
                    1 for item in classification_summary
                    if not item["included"] and item["title_mentions_vmware"]
                ),
                "excluded_description_mentions_vmware": sum(
                    1 for item in classification_summary
                    if not item["included"] and item["description_mentions_vmware"]
                ),
            },
            "final_parent_listing": final_meta,
            "detail_stats": detail_stats,
            "detail_errors": detail_errors,
            "missing_details": missing,
            "reused_detail_evidence": {
                "reused_rows": detail_source_counts.get("reused", 0),
                "fallback_rows": detail_source_counts.get("fallback", 0),
                "note": (
                    "Non-VMware parent-board details were reused from the prior saved CXS "
                    "responses after the live 390-row listing membership hash matched. "
                    "Every included VMware detail was fetched live in this run."
                ),
            },
        })
        return self.result
