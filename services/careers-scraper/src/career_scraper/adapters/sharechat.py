"""ShareChat's own careers page, its requisition API, and the exact MyNextHire postings it links to."""
from __future__ import annotations

from base64 import b64decode, b64encode
from hashlib import sha256
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://sharechat.com/careers"
CAREERS_LIST_API = "https://sharechat.com/api/careersList"
MNH_BOARD = "https://sharechat.mynexthire.com"
MNH_JOB_PAGE = MNH_BOARD + "/employer/jobs"
MNH_LIST_API = MNH_BOARD + "/employer/careers/reqlist/get"
MNH_DETAIL_API = "https://sharechat.prod.us1.mynexthire.io/d17/careers/requisition/object"
ITEMS_PER_PAGE = 100
MAX_LISTING_PAGES = 50
CAREERS_CHUNK = re.compile(r"^/careers\.[0-9a-f]+\.lazy\.js$", re.I)
BASE64_DISPLAY = re.compile(r"^([0-9a-zA-Z+/]{4})*(([0-9a-zA-Z+/]{2}==)|([0-9a-zA-Z+/]{3}=))?$")

# Public job fields only: recruiter/hiring-manager identities, applicant pipeline
# counts, application questions and tenant form configuration are not job data.
CAREERS_FIELDS = frozenset("""requisitionId requisitionTitle orgUnitName officeLocationNames yrsOfExpMin
yrsOfExpMax roleName designation totalPositions employmentType jobLevel jobDescription createdDate approvedDate
customFields orgUnitId""".split())
TENANT_FIELDS = frozenset("""reqId reqTitle buName location locationAddress locationList jdDisplay approvedOn
employmentType expMin expMax totalPositions mandatorySkillList nonMandatorySkillList educationList fresher
careerStream jobLevel roleName designation statusId customFields buDescription buURL buHierarchy countryCode
complianceNotices requisitionLocationGroup locationGroup reqCurrency ctcBandLowEnd ctcBandHighEnd priority
positionType skillFamily roleId screeningMatchPercent""".split())
DETAIL_FIELDS = frozenset("""reqId reqTitle jdDisplay locationList educationList mandatorySkillList
nonMandatorySkillList expMin expMax fresher requisitionLocationGroup statusId requisitionPublished""".split())


def project(row: dict, fields: frozenset) -> dict:
    return {key: value for key, value in row.items() if key in fields}


def job_context(req_id: int | str, page_type: str = "jd") -> dict:
    """The exact query context ShareChat's careers page builds for each role."""
    return {
        "pageType": page_type,
        "cvSource": "careers",
        "reqId": int(req_id),
        "requester": {"id": "", "code": "", "name": ""},
        "page": "careers",
        "bufilter": -1,
        "customFields": {},
    }


def native_job_url(req_id: int | str, page_type: str = "jd") -> str:
    payload = json.dumps(job_context(req_id, page_type), separators=(",", ":")).encode()
    return MNH_JOB_PAGE + "?src=careers&p=" + b64encode(payload).decode()


def decode_jd_display(value: str) -> str:
    """Mirror the board's encoderFactory.decodeString: only whole-string base64 is decoded."""
    if not isinstance(value, str) or not value:
        return ""
    if not BASE64_DISPLAY.fullmatch(value):
        return value
    try:
        decoded = b64decode(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return value
    return decoded if decoded.strip() else value


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "sharechat.com"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("ShareChat official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    headings = [re.sub(r"\s+", " ", h.get_text(" ", strip=True)) for h in soup.select("h1")]
    if "ShareChat" not in title or "Careers" not in title:
        raise ValueError("ShareChat official careers page title changed")
    if not headings or headings[0] != "Build Your Career with ShareChat":
        raise ValueError("ShareChat official careers page heading changed")
    chunks = [node.get("src") for node in soup.select("script[src]")]
    careers = sorted({src for src in chunks if src and CAREERS_CHUNK.fullmatch(src)})
    if len(careers) != 1:
        raise ValueError("ShareChat careers page no longer loads exactly one careers bundle")
    footer = soup.get_text(" ", strip=True)
    if "Mohalla Tech Private Limited" not in footer:
        raise ValueError("ShareChat careers page no longer publishes its Mohalla Tech legal footer")
    return {
        "url": response_url,
        "title": title,
        "h1": headings[0],
        "careers_chunk_path": careers[0],
        "careers_chunk_url": "https://sharechat.com" + careers[0],
        "legal_entity_footer": "Mohalla Tech Private Limited",
        "html_sha256": sha256(html.encode()).hexdigest(),
    }


def parse_careers_chunk(script: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "sharechat.com" or not CAREERS_CHUNK.fullmatch(parsed.path):
        raise ValueError("ShareChat careers bundle response came from an unexpected URL")
    required = [
        MNH_JOB_PAGE + "?src=careers&p=",
        '"/api/careersList?limit=100"',
        '"/api/careersList?limit=100&offsetToken="',
        'pageType:"jd"',
    ]
    missing = [item for item in required if item not in script]
    if missing:
        raise ValueError(f"ShareChat careers bundle no longer builds the inspected queries: {missing}")
    return {
        "url": response_url,
        "sha256": sha256(script.encode()).hexdigest(),
        "queries": ["GET /api/careersList?limit=100", "GET /api/careersList?limit=100&offsetToken=<token>"],
        "job_link_template": MNH_JOB_PAGE + "?src=careers&p=<base64 page context>",
    }


def parse_careers_list(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
        raise ValueError("ShareChat careersList response is not the inspected object")
    data = value["data"]
    groups = data.get("careersList")
    count, has_next, token = data.get("count"), data.get("hasNext"), data.get("offsetToken")
    if not isinstance(groups, list) or not isinstance(count, int) or not isinstance(has_next, bool):
        raise ValueError("ShareChat careersList pagination schema changed")
    if has_next and not isinstance(token, str):
        raise ValueError("ShareChat careersList advertises another page without an offset token")
    rows, issues, seen, group_titles = [], [], set(), {}
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("data"), list):
            raise ValueError("ShareChat careersList group schema changed")
        title = group.get("title")
        for row in group["data"]:
            if not isinstance(row, dict):
                issues.append("careers_row_not_object")
                continue
            identity = row.get("requisitionId")
            if not isinstance(identity, int) or identity <= 0:
                issues.append("careers_row_missing_requisition_id")
                continue
            if identity in seen:
                issues.append(f"careers_row_duplicate_id:{identity}")
                continue
            if not isinstance(row.get("requisitionTitle"), str) or not row["requisitionTitle"].strip():
                issues.append(f"careers_row_missing_title:{identity}")
                continue
            if not isinstance(row.get("orgUnitName"), str) or not row["orgUnitName"].strip():
                issues.append(f"careers_row_missing_org_unit:{identity}")
            if not isinstance(row.get("officeLocationNames"), list):
                issues.append(f"careers_row_missing_locations:{identity}")
            seen.add(identity)
            rows.append(project(row, CAREERS_FIELDS))
            if isinstance(title, str):
                group_titles[identity] = title
    return {
        "groups": [{"title": g.get("title"), "ids": [r["requisitionId"] for r in g.get("data") or []
                                                      if isinstance(r, dict) and isinstance(r.get("requisitionId"), int)]}
                   for g in groups if isinstance(g, dict)],
        "rows": rows,
        "group_titles": group_titles,
        "ids": [row["requisitionId"] for row in rows],
        "count": count,
        "has_next": has_next,
        "offset_token": token,
        "issues": issues,
    }


def parse_requisition_list(value: dict) -> dict:
    rows = value.get("reqDetailsBOList") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise ValueError("ShareChat MyNextHire tenant job-list schema changed")
    kept, issues, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("ats_row_not_object")
            continue
        identity = row.get("reqId")
        if not isinstance(identity, int) or identity <= 0:
            issues.append("ats_row_missing_req_id")
            continue
        if identity in seen:
            issues.append(f"ats_row_duplicate_id:{identity}")
            continue
        if not isinstance(row.get("reqTitle"), str) or not row["reqTitle"].strip():
            issues.append(f"ats_row_missing_title:{identity}")
            continue
        seen.add(identity)
        kept.append(project(row, TENANT_FIELDS))
    return {"rows": kept, "ids": [row["reqId"] for row in kept], "issues": issues}


def parse_requisition_detail(value: dict, req_id: int, expected_title: str | None = None) -> dict:
    if not isinstance(value, dict) or value.get("status") != 200 or not isinstance(value.get("data"), dict):
        raise ValueError(f"ShareChat exact detail response for {req_id} is not a published requisition object")
    row = value["data"].get("reqDetailsBO")
    if not isinstance(row, dict) or not isinstance(row.get("reqId"), int):
        raise ValueError(f"ShareChat exact detail response for {req_id} has no requisition")
    if row["reqId"] != req_id:
        raise ValueError(f"ShareChat exact detail identity mismatch for {req_id}: {row.get('reqId')}")
    if row.get("requisitionPublished") is not True:
        raise ValueError(f"ShareChat requisition {req_id} is not published")
    title = row.get("reqTitle")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"ShareChat requisition {req_id} has no title")
    if expected_title and re.sub(r"\s+", " ", title).strip() != re.sub(r"\s+", " ", expected_title).strip():
        raise ValueError(f"ShareChat requisition {req_id} title changed: {title!r} != {expected_title!r}")
    display = row.get("jdDisplay")
    if not isinstance(display, str) or not display.strip():
        raise ValueError(f"ShareChat requisition {req_id} has no published description")
    html = decode_jd_display(display)
    text = text_html(html)
    if len(text.strip()) < 100:
        raise ValueError(f"ShareChat requisition {req_id} description is empty or truncated")
    if not isinstance(row.get("locationList"), list):
        raise ValueError(f"ShareChat requisition {req_id} no longer publishes its location list")
    return {"row": project(row, DETAIL_FIELDS), "description_html": html, "description_text": text}


class Sharechat(Adapter):
    VERIFICATION_REVISION = "sharechat.careers-api+mynexthire-tenant.exact-jd.1"

    async def careers_listing(self) -> dict:
        """Every requisition the company's own careers page publishes, across its offset pages."""
        rows, ids, groups, tokens = [], [], [], set()
        token, pages, issue_list, declared = None, 0, [], 0
        while True:
            if not self.page_guard():
                raise ValueError("ShareChat public requisition listing request budget exhausted")
            params = {"limit": ITEMS_PER_PAGE}
            if token:
                params["offsetToken"] = token
            page = parse_careers_list(await self.http.json(CAREERS_LIST_API, params=params))
            pages += 1
            declared = page["count"]
            issue_list.extend(page["issues"])
            groups.extend(page["groups"])
            rows.extend(page["rows"])
            ids.extend(page["ids"])
            if len(rows) > page["count"]:
                raise ValueError("ShareChat careersList returned more rows than its declared count")
            if not page["has_next"]:
                if len(rows) != page["count"]:
                    raise ValueError(f"ShareChat careersList count {page['count']} != {len(rows)} returned rows")
                break
            if page["offset_token"] in tokens:
                raise ValueError("ShareChat careersList repeated an offset token")
            if pages >= MAX_LISTING_PAGES:
                raise ValueError("ShareChat careersList page budget exhausted")
            tokens.add(page["offset_token"])
            token = page["offset_token"]
        if len(set(ids)) != len(ids):
            raise ValueError("ShareChat careersList repeats a requisition across its pages")
        return {"rows": rows, "ids": ids, "groups": groups,
                "declared_count": declared, "pages": pages, "issues": issue_list}

    async def tenant_listing(self) -> dict:
        if not self.page_guard():
            raise ValueError("ShareChat tenant listing request budget exhausted")
        body = {"source": "careers", "code": "", "filterByBuId": -1}
        parsed = parse_requisition_list(await self.http.json(MNH_LIST_API, method="POST", json=body))
        parsed["request_body"] = body
        return parsed

    async def detail(self, job):
        identity = int(job.job_id)
        if identity not in self.published_ids:
            raise ValueError(f"ShareChat detail {identity} was not exposed by the company's own careers listing")
        body = {"source": "careers", "id": "", "code": "", "reqId": identity}
        parsed = parse_requisition_detail(await self.http.json(MNH_DETAIL_API, method="POST", json=body),
                                          identity, job.title)
        row = parsed["row"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "sharechat.mynexthire.requisition_object.jdDisplay"
        job.description_source_url = MNH_DETAIL_API
        job.locations = row.get("locationList") or job.locations
        job.experience = {"minimum_years": row.get("expMin"), "maximum_years": row.get("expMax")}
        job.education = row.get("educationList")
        job.apply_url = native_job_url(identity, "application")
        job.raw["detail"] = row
        job.raw["detail_request"] = {"url": MNH_DETAIL_API, "body": body}
        job.raw["native_detail_url"] = job.url
        job.raw["skills"] = {"mandatory": row.get("mandatorySkillList"), "optional": row.get("nonMandatorySkillList")}
        job.raw["date_semantics"] = "No posting/deadline date is published; requisition approval metadata is not a posting date."
        return job

    async def run(self):
        collection_started = time.perf_counter()
        entry = await self.http.get(OFFICIAL_ENTRY)
        official = parse_official_entry(entry.text, str(entry.url))
        chunk = await self.http.get(official["careers_chunk_url"])
        bundle = parse_careers_chunk(chunk.text, str(chunk.url))

        careers = await self.careers_listing()
        tenant = await self.tenant_listing()
        careers_ids, tenant_ids = careers["ids"], tenant["ids"]
        membership_equal = set(careers_ids) == set(tenant_ids)
        membership_order_equal = careers_ids == tenant_ids
        if not membership_equal:
            self.result.errors.append("sharechat_careers_and_tenant_membership_differ")
        by_id = {row["reqId"]: row for row in tenant["rows"]}
        if careers["issues"]:
            self.result.errors.extend(f"careers_listing:{issue}" for issue in careers["issues"])
        if tenant["issues"]:
            self.result.errors.extend(f"tenant_listing:{issue}" for issue in tenant["issues"])
        self.published_ids = set(careers_ids)
        self.result.expected_total = len(careers_ids)

        for row in careers["rows"]:
            identity = row["requisitionId"]
            ats = by_id.get(identity, {})
            self.add(self.job(
                {"careers_listing": row, "tenant_listing": ats},
                title=row["requisitionTitle"], job_id=identity, url=native_job_url(identity),
                apply_url=native_job_url(identity, "application"),
                employer="ShareChat",
                locations=ats.get("locationList") or [
                    {"office": office, "address": office} for office in row.get("officeLocationNames") or []
                ],
                department=row.get("orgUnitName") or careers["group_titles"].get(identity),
                employment_type=ats.get("employmentType") or row.get("employmentType"),
                experience={"minimum_years": ats.get("expMin", row.get("yrsOfExpMin")),
                            "maximum_years": ats.get("expMax", row.get("yrsOfExpMax"))},
                education=ats.get("educationList") or row.get("educationList"),
            ))
            if self.limit():
                break
        collection_seconds = max(0.0, time.perf_counter() - collection_started)
        self.result.timings["source_collection_seconds"] = self.result.timings.get(
            "source_collection_seconds", 0.0) + collection_seconds

        self.result.jobs = await self.hydrate(self.result.jobs, self.detail)

        final_started = time.perf_counter()
        final_careers = await self.careers_listing()
        final_tenant = await self.tenant_listing()
        final_membership_equal = (set(final_careers["ids"]) == set(careers_ids)
                                  and set(final_tenant["ids"]) == set(tenant_ids))
        if not final_membership_equal:
            self.result.errors.append("sharechat_membership_changed_during_run")
        self.result.timings["final_listing_recheck_seconds"] = round(
            max(0.0, time.perf_counter() - final_started), 6)

        verified = sum(job.description_verified for job in self.result.jobs)
        self.result.listing_complete = bool(
            "sample_limit" not in self.result.warnings
            and final_membership_equal
            and membership_equal
            and len(self.result.jobs) == len(careers_ids) == (self.result.expected_total or 0)
            and not self.result.errors
        )
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry_url=official["url"],
            official_entry_title=official["title"],
            official_entry_heading=official["h1"],
            legal_entity_footer=official["legal_entity_footer"],
            careers_bundle=bundle,
            listing_url=CAREERS_LIST_API + "?limit=100",
            listing_authority="Only the requisitions rendered by ShareChat's own /careers page and its /api/careersList request",
            tenant_board=MNH_BOARD,
            tenant_listing_url=MNH_LIST_API,
            tenant_listing_request=tenant["request_body"],
            detail_request_url=MNH_DETAIL_API,
            listing_pages=careers["pages"] + final_careers["pages"],
            careers_listing_count=careers["declared_count"],
            careers_listing_groups=careers["groups"],
            tenant_listing_count=len(tenant_ids),
            careers_and_tenant_ids_equal=membership_equal,
            careers_and_tenant_listing_order_equal=membership_order_equal,
            final_listing_ids_equal=final_membership_equal,
            ui_layout="4 department accordions in 2 columns (even-index groups left, odd right), 11 role links, no pagination controls",
            date_semantics="No posting/deadline date is published; approvedOn/createdDate are requisition workflow metadata, so dates stay null",
            salary_semantics="No salary field is published (CTC bands are 0.0); salary stays null",
            application_questions_excluded=True,
            descriptions_verified=verified,
        )
        return self.result
