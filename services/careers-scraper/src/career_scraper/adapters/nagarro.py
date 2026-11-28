"""Nagarro's own global job search (Azure table listing) and its exact linked postings.

The company publishes ``/en/careers/job-search``; that page loads the company's
own ``nagcareersite`` Azure Table Storage listing and renders it client-side.
Only the exact posting IDs the company's Main/Split/Experience tables expose are
fetched as details (the SmartRecruiters ``Nagarro1`` board itself is much larger
and is never enumerated). Container rows are expanded through the same Split /
Experience tables the page's own JavaScript uses.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import re
import time
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.nagarro.com/en/careers"
LISTING_URL = "https://www.nagarro.com/en/careers/job-search"
TABLE_ORIGIN = "https://nagcareersite.table.core.windows.net"
POSTING_API = "https://api.smartrecruiters.com/v1/companies/Nagarro1/postings/{posting_id}"
BOARD = "Nagarro1"
POSTING_PATH = re.compile(r"^/Nagarro1/(\d+)(?:-[^/]*)?$")
SECTION_ORDER = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
DESCRIPTION_METHOD = "nagarro.company-listing.linked-smartrecruiters-posting-sections"


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


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


def _js_object(text: str, name: str) -> dict:
    match = re.search(rf"{re.escape(name)}=(\{{[^{{}}]*\}})", text)
    if match is None:
        raise ValueError(f"Nagarro UI bundle no longer defines {name}")
    value = {}
    for key, item in re.findall(r'([A-Za-z_][A-Za-z0-9_]*):"([^"]*)"', match.group(1)):
        value[key] = item
    if not value:
        raise ValueError(f"Nagarro UI bundle {name} is empty")
    return value


def _js_string(text: str, name: str) -> str:
    match = re.search(rf'{re.escape(name)}="([^"]+)"', text)
    if match is None:
        raise ValueError(f"Nagarro UI bundle no longer defines {name}")
    return match.group(1)


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "www.nagarro.com"
            or parts.path.rstrip("/") != "/en/careers"):
        raise ValueError("Nagarro official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Careers | Nagarro" not in title:
        raise ValueError("Nagarro official careers entry title changed")
    iframes = soup.select("iframe")
    if iframes:
        raise ValueError("Nagarro official careers entry unexpectedly embeds a third-party frame")
    buttons = soup.select(f'button.filter-click[data-url="{LISTING_URL}"]')
    if len(buttons) != 1:
        raise ValueError("Nagarro official careers entry no longer exposes one job-search CTA")
    cta = buttons[0]
    return {
        "url": response_url,
        "page_title": title,
        "listing_cta_text": normalized_title(cta.get_text(" ", strip=True)),
        "listing_cta_data_url": cta.get("data-url"),
        "company_search_manual_links": [
            anchor.get("href") for anchor in soup.select('a[href*="/en/careers/job-search"]')
        ],
        "iframe_count": len(iframes),
    }


def parse_job_search_page(html: str, response_url: str = LISTING_URL) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "www.nagarro.com"
            or parts.path.rstrip("/") != "/en/careers/job-search"):
        raise ValueError("Nagarro job-search page redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Job Search" not in title:
        raise ValueError("Nagarro job-search page title changed")
    if soup.select_one("#all-listings") is None:
        raise ValueError("Nagarro job-search page lost its #all-listings container")
    for field_id in ("expertiseField", "countryField", "cityField"):
        if soup.select_one(f"#{field_id}") is None:
            raise ValueError(f"Nagarro job-search page lost its {field_id} filter")
    keyword = soup.select_one("#keywordSearch")
    if keyword is None:
        raise ValueError("Nagarro job-search page lost its keyword search")
    static_rows = len(soup.select(".job-row"))
    scripts = [script.get("src") for script in soup.select("script[src]")
               if "template_JS_NW_Careers_JobListing" in (script.get("src") or "")]
    if len(scripts) != 1:
        raise ValueError("Nagarro job-search page no longer loads exactly one listing bundle")
    return {
        "url": response_url,
        "page_title": title,
        "listing_container_id": "all-listings",
        "filter_fields": ["expertiseField", "countryField", "cityField"],
        "keyword_search_id": keyword.get("id"),
        "keyword_search_placeholder": keyword.get("placeholder"),
        "server_rendered_job_rows": static_rows,
        "listing_bundle_url": scripts[0],
        "pagination_controls": [
            node.get_text(" ", strip=True) for node in soup.select("button,a")
            if re.search(r"\b(?:next|previous|page)\b", node.get_text(" ", strip=True), re.I)
        ],
    }


def parse_ui_bundle(text: str, bundle_url: str) -> dict:
    if "getAllJobsAPIURL" not in text or "fetchDataFromServer" not in text:
        raise ValueError("Nagarro listing bundle no longer contains the observed table loader")
    urls = {
        "main": _js_string(text, "ALL_JOBS_API"),
        "split": _js_string(text, "SJ_URL_API"),
        "experience": _js_string(text, "JD_URL_API"),
    }
    tables = {"main": "Main", "split": "Split", "experience": "Experience"}
    for key, value in urls.items():
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.netloc != "nagcareersite.table.core.windows.net":
            raise ValueError(f"Nagarro listing bundle {key} table left the company table origin")
        if f"tn={tables[key]}" not in parts.query:
            raise ValueError(f"Nagarro listing bundle {key} table name changed")
    fields = _js_object(text, "FIELDS_TO_KEY_MAP")
    split_fields = _js_object(text, "SJ_URL_FIELDS_MAP")
    experience_fields = _js_object(text, "JD_URL_FIELDS_MAP")
    required = {"expertise", "title", "city", "country", "level", "levelId", "descURL",
                "remote", "isMultipleExperience", "issimilarjobs", "istype", "sjnumber",
                "jobTitleKey"}
    if set(fields) != required:
        raise ValueError("Nagarro listing field map changed")
    if set(split_fields) != {"jobTitle", "joburl", "jobRowkey"}:
        raise ValueError("Nagarro split-table field map changed")
    if set(experience_fields) != {"experienceRange", "jdURL", "jobRowKey"}:
        raise ValueError("Nagarro experience-table field map changed")
    continuation = bool(re.search(r"x-ms-continuation-NextPartitionKey", text, re.I)
                        and re.search(r"x-ms-continuation-NextRowKey", text, re.I))
    if not continuation:
        raise ValueError("Nagarro listing bundle no longer pages with Azure continuation headers")
    select = ",".join(fields.values())
    if "dbColumnFields" not in text or "&$select=${dbColumnFields}" not in text:
        raise ValueError("Nagarro listing bundle no longer builds the observed Main $select request")
    return {
        "bundle_url": bundle_url,
        "table_urls": urls,
        "job_fields": fields,
        "split_fields": split_fields,
        "experience_fields": experience_fields,
        "main_select": select,
        "continuation_headers": ["x-ms-continuation-NextPartitionKey", "x-ms-continuation-NextRowKey"],
        "rows_per_call_cap": 1000,
    }


def parse_table_page(value, *, table: str) -> list[dict]:
    if not isinstance(value, dict):
        raise ValueError(f"Nagarro {table} table response is not an object")
    if value.get("odata.error"):
        raise ValueError(f"Nagarro {table} table returned an OData error")
    rows = value.get("value")
    if not isinstance(rows, list):
        raise ValueError(f"Nagarro {table} table response is missing value")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"Nagarro {table} table contains a non-object row")
    return rows


def main_row_identity(row: dict, fields: dict) -> dict:
    index = row.get(fields["jobTitleKey"])
    if not isinstance(index, int):
        raise ValueError("Nagarro Main table row has no integer index")
    country = row.get(fields["country"])
    if not isinstance(country, str) or not country.strip():
        raise ValueError(f"Nagarro Main table row {index} has no country")
    title = normalized_title(row.get(fields["title"]))
    if not title:
        raise ValueError(f"Nagarro Main table row {index} has no title")
    return {"index": index, "title": title, "country": country.strip()}


def posting_from_url(url: str) -> dict:
    if not isinstance(url, str):
        raise ValueError("Nagarro listing row has no job URL")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "jobs.smartrecruiters.com":
        raise ValueError(f"Nagarro listing URL left the company's linked ATS host: {url}")
    match = POSTING_PATH.fullmatch(parts.path)
    if match is None:
        raise ValueError(f"Nagarro listing URL is not an exact Nagarro1 posting: {url}")
    return {"posting_id": match.group(1), "url": url, "path": parts.path}


def build_membership(main_rows: list[dict], split_rows: list[dict],
                     experience_rows: list[dict], bundle: dict) -> dict:
    fields = bundle["job_fields"]
    split_fields = bundle["split_fields"]
    experience_fields = bundle["experience_fields"]
    split_by_main = {}
    for child in split_rows:
        key = child.get(split_fields["jobRowkey"])
        split_by_main.setdefault(key, []).append(child)
    experience_by_main = {}
    for child in experience_rows:
        key = child.get(experience_fields["jobRowKey"])
        experience_by_main.setdefault(key, []).append(child)

    postings, issues, notes, containers = [], [], [], []
    seen_indexes, seen_ids = set(), set()
    for row in main_rows:
        identity = main_row_identity(row, fields)
        index = identity["index"]
        if index in seen_indexes:
            issues.append(f"duplicate_main_index:{index}")
            continue
        seen_indexes.add(index)
        multiple = bool(row.get(fields["isMultipleExperience"]))
        similar = bool(row.get(fields["issimilarjobs"]))
        direct_url = row.get(fields["descURL"])
        if similar or multiple:
            kind = "similar_jobs" if similar else "multiple_experience"
            children = split_by_main.get(index, []) if similar else experience_by_main.get(index, [])
            if not children:
                issues.append(f"unresolved_container:{index}:{kind}")
                containers.append({"index": index, "kind": kind, "title": identity["title"],
                                   "count_opening": row.get(fields["sjnumber"]),
                                   "children": [], "resolved": False})
                continue
            containers.append({"index": index, "kind": kind, "title": identity["title"],
                               "count_opening": row.get(fields["sjnumber"]),
                               "children": [dict(child) for child in children], "resolved": True})
            if row.get(fields["sjnumber"]) is not None and row.get(fields["sjnumber"]) != len(children):
                notes.append(f"container_child_count_differs:{index}")
            for child in children:
                url = child.get(split_fields["joburl"]) if similar else child.get(experience_fields["jdURL"])
                parsed = posting_from_url(url)
                if parsed["posting_id"] in seen_ids:
                    issues.append(f"duplicate_container_posting:{parsed['posting_id']}")
                    continue
                seen_ids.add(parsed["posting_id"])
                postings.append({
                    "posting_id": parsed["posting_id"],
                    "url": url,
                    "source_kind": kind,
                    "main_index": index,
                    "listing_title": identity["title"],
                    "container": {"kind": kind, "index": index, "title": identity["title"]},
                    "child": dict(child),
                    "main_row": row,
                })
        else:
            if not direct_url or direct_url == "#":
                issues.append(f"missing_direct_job_url:{index}")
                continue
            parsed = posting_from_url(direct_url)
            if parsed["posting_id"] in seen_ids:
                issues.append(f"duplicate_direct_posting:{parsed['posting_id']}")
                continue
            seen_ids.add(parsed["posting_id"])
            postings.append({
                "posting_id": parsed["posting_id"],
                "url": direct_url,
                "source_kind": "direct",
                "main_index": index,
                "listing_title": identity["title"],
                "container": None,
                "child": None,
                "main_row": row,
            })
    listed_rows = len(seen_indexes)
    containers_count = len(containers)
    expected = listed_rows - containers_count + sum(len(c["children"]) for c in containers)
    if expected != len(postings):
        issues.append(f"membership_count_mismatch:{expected}!={len(postings)}")
    return {
        "postings": postings,
        "issues": issues,
        "notes": notes,
        "main_rows": listed_rows,
        "container_rows": containers_count,
        "container_children": sum(len(c["children"]) for c in containers),
        "expected_postings": len(postings),
        "expected_recomputed": expected,
        "containers": containers,
        "posting_ids": [p["posting_id"] for p in postings],
    }


def _parse_date(value):
    """Return the source timestamp only when it is a real ISO timestamp."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo else None


def detail_sections(value: dict) -> list[dict]:
    job_ad = value.get("jobAd")
    raw = job_ad.get("sections") if isinstance(job_ad, dict) else None
    if not isinstance(raw, dict):
        raise ValueError("Nagarro posting detail has no jobAd.sections")
    sections = []
    keys = list(SECTION_ORDER) + [key for key in raw if key not in SECTION_ORDER]
    for key in keys:
        entry = raw.get(key)
        if not isinstance(entry, dict):
            continue
        html = entry.get("text") or ""
        if not isinstance(html, str):
            raise ValueError(f"Nagarro posting section {key} is not HTML text")
        text = text_html(html)
        if text.strip():
            sections.append({
                "key": key,
                "title": normalized_title(entry.get("title")) or key,
                "html": html,
                "text": text,
                "normalized_chars": len(normalized_body_text(html)),
                "normalized_fnv1a64": fnv1a64(normalized_body_text(html)),
            })
    if not sections:
        raise ValueError("Nagarro posting detail has no non-empty description section")
    return sections


def parse_posting_detail(value, *, expected_id: str, expected_company: str = BOARD) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Nagarro posting detail is not an object")
    if str(value.get("id")) != str(expected_id):
        raise ValueError(f"Nagarro posting detail id mismatch: {value.get('id')} != {expected_id}")
    company = value.get("company")
    if not isinstance(company, dict) or company.get("identifier") != expected_company:
        raise ValueError("Nagarro posting detail is not from the linked Nagarro1 board")
    if value.get("active") is not True:
        raise ValueError("Nagarro posting detail is not active")
    if value.get("visibility") != "PUBLIC":
        raise ValueError("Nagarro posting detail is not public")
    name = normalized_title(value.get("name"))
    if not name:
        raise ValueError("Nagarro posting detail has no title")
    sections = detail_sections(value)
    location = value.get("location") if isinstance(value.get("location"), dict) else {}
    custom = {}
    for item in value.get("customField") or []:
        if isinstance(item, dict) and item.get("fieldLabel"):
            custom[item["fieldLabel"]] = item.get("valueLabel")
    released_raw = value.get("releasedDate")
    released = _parse_date(released_raw)
    employment = value.get("typeOfEmployment") if isinstance(value.get("typeOfEmployment"), dict) else {}
    department = value.get("department") if isinstance(value.get("department"), dict) else {}
    function = value.get("function") if isinstance(value.get("function"), dict) else {}
    industry = value.get("industry") if isinstance(value.get("industry"), dict) else {}
    experience = value.get("experienceLevel") if isinstance(value.get("experienceLevel"), dict) else {}
    language = value.get("language") if isinstance(value.get("language"), dict) else {}
    return {
        "id": str(value.get("id")),
        "name": name,
        "ref_number": value.get("refNumber"),
        "released_date": released,
        "released_date_raw": released_raw,
        "active": value.get("active"),
        "visibility": value.get("visibility"),
        "apply_url": value.get("applyUrl"),
        "posting_url": value.get("postingUrl"),
        "referral_url": value.get("referralUrl"),
        "location": {
            "city": location.get("city"),
            "country_code": location.get("country"),
            "country": custom.get("Country/Region"),
            "full_location": location.get("fullLocation"),
            "remote": location.get("remote"),
            "hybrid": location.get("hybrid"),
        },
        "custom_fields": custom,
        "type_of_employment": employment.get("label"),
        "department": department.get("label"),
        "function": function.get("label"),
        "industry": industry.get("label"),
        "experience_level": experience.get("label"),
        "language": language.get("code"),
        "sections": sections,
    }


def compose_description(sections: list[dict]) -> dict:
    if not sections:
        raise ValueError("Nagarro posting has no description sections to compose")
    html = "\n".join(
        "<section data-nagarro-section=\"{key}\"><h2>{title}</h2>\n{body}</section>".format(
            key=section["key"], title=section["title"], body=section["html"])
        for section in sections
    )
    text = text_html(html)
    normalized = normalized_body_text(html)
    return {
        "html": html,
        "text": text,
        "normalized_chars": len(normalized),
        "normalized_fnv1a64": fnv1a64(normalized),
    }


def _workplace(location: dict) -> str | None:
    if location.get("remote") is True:
        return "remote"
    if location.get("hybrid") is True:
        return "hybrid"
    if location.get("remote") is False and location.get("hybrid") is False:
        return "on-site"
    return None


class Nagarro(Adapter):
    """Collect only the postings Nagarro's own global job search publishes."""

    VERIFICATION_REVISION = "nagarro.company-jobsearch-azure-tables-linked-smartrecruiters.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tables = {}

    async def _get(self, url: str, **kwargs):
        started = time.perf_counter()
        response = await self.http.get(url, **kwargs)
        return response, max(0.0, time.perf_counter() - started)

    async def _official_entry(self):
        response, seconds = await self._get(OFFICIAL_ENTRY)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(status=response.status_code, request_seconds=seconds,
                      response_bytes=len(response.content),
                      response_sha256=sha256(response.content).hexdigest())
        return parsed

    async def _listing_page(self):
        response, seconds = await self._get(LISTING_URL)
        parsed = parse_job_search_page(response.text, str(response.url))
        parsed.update(status=response.status_code, request_seconds=seconds,
                      response_bytes=len(response.content),
                      response_sha256=sha256(response.content).hexdigest())
        return parsed

    async def _ui_bundle(self, url: str):
        response, seconds = await self._get(url)
        parsed = parse_ui_bundle(response.text, str(response.url))
        parsed.update(status=response.status_code, request_seconds=seconds,
                      response_bytes=len(response.content),
                      response_sha256=sha256(response.content).hexdigest())
        return parsed

    async def _table(self, url: str, *, table: str, select: str | None = None,
                     record: dict | None = None) -> dict:
        started = time.perf_counter()
        request_url = f"{url}&$select={select}" if select else url
        rows, pages, seen = [], [], set()
        while request_url:
            if not self.page_guard():
                rows_note = "page_budget_exhausted"
                if record is not None:
                    record.setdefault("issues", []).append(rows_note)
                break
            page_started = time.perf_counter()
            response = await self.http.request("GET", request_url, headers={"Accept": "application/json"})
            page_seconds = max(0.0, time.perf_counter() - page_started)
            parts = urlsplit(str(response.url))
            if parts.netloc != "nagcareersite.table.core.windows.net" or table.casefold() not in parts.path.casefold():
                raise ValueError(f"Nagarro {table} table request left its company route")
            value = response.json()
            page_rows = parse_table_page(value, table=table)
            identity_field = "index" if table == "Main" else None
            for row in page_rows:
                if identity_field and not isinstance(row.get(identity_field), int):
                    raise ValueError("Nagarro Main table row has no integer index")
                key = (row.get("index"), row.get("RowKey"))
                if key in seen:
                    raise ValueError(f"Nagarro {table} table repeated a row across pages")
                seen.add(key)
                rows.append(row)
            pages.append({
                "page": len(pages) + 1,
                "request_url": str(response.request.url),
                "final_url": str(response.url),
                "status": response.status_code,
                "request_seconds": page_seconds,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
                "row_count": len(page_rows),
                "continuation": bool(response.headers.get("x-ms-continuation-NextPartitionKey")
                                     and response.headers.get("x-ms-continuation-NextRowKey")),
            })
            next_partition = response.headers.get("x-ms-continuation-NextPartitionKey")
            next_row = response.headers.get("x-ms-continuation-NextRowKey")
            if next_partition and next_row:
                if len(page_rows) < 1000 and len(page_rows) != 0:
                    # The observed UI always continues only while both headers exist;
                    # short pages with a continuation are allowed and recorded.
                    pages[-1]["short_page_with_continuation"] = True
                request_url = (f"{url}{'&$select=' + select if select else ''}"
                               f"&NextPartitionKey={quote(next_partition, safe='')}"
                               f"&NextRowKey={quote(next_row, safe='')}")
            else:
                request_url = None
        return {
            "table": table,
            "rows": rows,
            "row_count": len(rows),
            "pages": pages,
            "page_count": len(pages),
            "request_seconds": max(0.0, time.perf_counter() - started),
        }

    def _make_job(self, posting: dict, fields: dict):
        row = posting["main_row"]
        raw = {
            "posting": {key: posting[key] for key in
                        ("posting_id", "url", "source_kind", "main_index", "listing_title", "container")},
            "main_row": row,
            "split_child": posting["child"],
            "detail_api": None,
            "detail": None,
        }
        return self.job(
            raw,
            title=posting["listing_title"],
            url=posting["url"],
            job_id=posting["posting_id"],
            requisition_id=((posting["child"] or {}).get("Job_Ref_ID") or row.get("Job_Ref_ID")),
            department=row.get(fields["expertise"]),
            employment_type=row.get(fields["istype"]),
            workplace_type=None,
        )

    async def _detail_job(self, job):
        posting = job.raw["posting"]
        posting_id = str(posting["posting_id"])
        url = POSTING_API.format(posting_id=posting_id)
        value = await self.http.json(url, headers={"Referer": LISTING_URL, "Accept": "application/json"})
        detail = parse_posting_detail(value, expected_id=posting_id)
        sections = detail["sections"]
        composed = compose_description(sections)
        listing_title = normalized_title(job.title)
        if listing_title.casefold() != detail["name"].casefold():
            self.result.errors.append(f"nagarro_listing_detail_title_conflict:{posting_id}")
        listing_ref = (job.requisition_id or "").strip()
        if listing_ref and listing_ref != (detail["ref_number"] or "").strip():
            self.result.errors.append(f"nagarro_listing_detail_requisition_conflict:{posting_id}")
        row = job.raw["main_row"]
        if detail["location"].get("country"):
            listing_country = normalized_title(row.get(self._fields["country"]))
            if listing_country.casefold() != detail["location"]["country"].casefold():
                self.result.errors.append(f"nagarro_listing_detail_country_conflict:{posting_id}")
        job.title = detail["name"]
        job.requisition_id = detail["ref_number"] or job.requisition_id
        job.apply_url = detail["apply_url"]
        job.description_html = composed["html"]
        job.description_text = composed["text"]
        job.description_method = DESCRIPTION_METHOD
        job.description_source_url = url
        job.date_posted = detail["released_date"]
        job.date_posted_raw = {"releasedDate": detail["released_date_raw"]} if detail["released_date_raw"] else None
        job.locations = [detail["location"]]
        job.department = row.get(self._fields["expertise"])
        job.team = detail["department"]
        job.employment_type = {
            "listing_job_type": row.get(self._fields["istype"]),
            "detail_type_of_employment": detail["type_of_employment"],
            "detail_experience_level": detail["experience_level"],
        }
        job.workplace_type = _workplace(detail["location"])
        job.salary = None
        job.qualifications = next((s["text"] for s in sections if s["key"] == "qualifications"), None)
        job.responsibilities = None
        job.experience = None
        job.education = None
        job.employer = {"name": "Nagarro", "ats_company_identifier": BOARD}
        job.language = detail["language"] or "en"
        job.raw["detail_api"] = value
        job.raw["detail"] = {
            "id": detail["id"],
            "name": detail["name"],
            "ref_number": detail["ref_number"],
            "released_date": detail["released_date_raw"],
            "active": detail["active"],
            "visibility": detail["visibility"],
            "apply_url": detail["apply_url"],
            "posting_url": detail["posting_url"],
            "location": detail["location"],
            "custom_fields": detail["custom_fields"],
            "type_of_employment": detail["type_of_employment"],
            "department": detail["department"],
            "function": detail["function"],
            "industry": detail["industry"],
            "experience_level": detail["experience_level"],
            "language": detail["language"],
            "sections": [
                {key: section[key] for key in ("key", "title", "normalized_chars", "normalized_fnv1a64")}
                for section in sections
            ],
            "description_normalized_chars": composed["normalized_chars"],
            "description_fnv1a64": composed["normalized_fnv1a64"],
            "salary_field_present": any(isinstance(value.get(key), str) and value.get(key).strip()
                                         for key in ("compensation", "salary")),
            "valid_through_published": "validThrough" in value,
        }
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_authority": (
                "Nagarro's own unfiltered /en/careers/job-search page renders its "
                "company-owned nagcareersite Azure Table Storage listing client-side."
            ),
            "detail_method": (
                "Each exact posting URL published by the company's Main/Split/Experience "
                "tables is fetched from the public SmartRecruiters posting record for its ID; "
                "description = companyDescription + jobDescription + qualifications + "
                "additionalInformation with employer section titles."
            ),
            "request_scope": (
                "Only nagcareersite Main/Split/Experience tables and the exact linked "
                "SmartRecruiters posting IDs they publish. The Nagarro1 board itself is not enumerated."
            ),
        })
        try:
            entry = await self._official_entry()
            listing = await self._listing_page()
            bundle = await self._ui_bundle(listing["listing_bundle_url"])
        except Exception as exc:
            self.result.errors.append(f"Nagarro official journey failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result
        self._fields = bundle["job_fields"]
        self._split_fields = bundle["split_fields"]
        self._experience_fields = bundle["experience_fields"]
        self.result.metadata.update({
            "official_entry": entry,
            "listing_page": listing,
            "ui_request_source": {key: bundle[key] for key in
                                  ("bundle_url", "main_select", "continuation_headers", "rows_per_call_cap")},
            "listing_membership_request": {
                "method": "GET",
                "main_url": bundle["table_urls"]["main"],
                "table": "Main",
                "filters_applied": False,
                "select_fields_used_by_ui": bundle["main_select"],
                "pagination": "Azure table continuation headers; absent headers end the listing",
                "scope": "Nagarro company careers tables only",
            },
        })
        try:
            main_ui = await self._table(bundle["table_urls"]["main"], table="Main", select=bundle["main_select"])
            main_full = await self._table(bundle["table_urls"]["main"], table="Main")
            split = await self._table(bundle["table_urls"]["split"], table="Split")
            experience = await self._table(bundle["table_urls"]["experience"], table="Experience")
            self.tables = {"main_ui": main_ui, "main_full": main_full, "split": split,
                           "experience": experience, "main_select": bundle["main_select"]}
        except Exception as exc:
            self.result.errors.append(f"Nagarro company listing table failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        ui_rows = main_ui["rows"]
        full_rows = main_full["rows"]
        self.result.metadata.update({
            "initial_listing_snapshot": {
                "main_ui": {k: v for k, v in main_ui.items() if k != "rows"},
                "main_full": {k: v for k, v in main_full.items() if k != "rows"},
                "split": {k: v for k, v in split.items() if k != "rows"},
                "experience": {k: v for k, v in experience.items() if k != "rows"},
                "main_indexes_ui": [row.get("index") for row in ui_rows],
                "main_indexes_full": [row.get("index") for row in full_rows],
                "split_rows": split["rows"],
                "experience_row_count": len(experience["rows"]),
            }
        })
        if [row.get("index") for row in ui_rows] != [row.get("index") for row in full_rows]:
            self.result.errors.append("nagarro_ui_and_full_table_membership_differ")
        shared_fields = [field for field in bundle["main_select"].split(",") if field != "index"]
        for ui_row, full_row in zip(ui_rows, full_rows):
            for field in shared_fields:
                if ui_row.get(field) != full_row.get(field):
                    self.result.errors.append(f"nagarro_ui_and_full_table_field_differs:{ui_row.get('index')}:{field}")
                    break
        self.result.metadata["main_rows"] = len(full_rows)
        self.result.metadata["main_ui_rows"] = len(ui_rows)

        membership = build_membership(full_rows, split["rows"], experience["rows"], bundle)
        self.result.errors.extend(membership["issues"])
        self.result.metadata["membership"] = {
            key: value for key, value in membership.items() if key not in ("postings", "main_row")
        }
        self.result.metadata["membership"]["listing_rows"] = len(full_rows)
        self.result.metadata["membership"]["postings"] = [
            {key: value for key, value in posting.items() if key != "main_row"}
            for posting in membership["postings"]
        ]
        self.result.expected_total = membership["expected_postings"]
        jobs = [self._make_job(posting, self._fields) for posting in membership["postings"]]
        if len(jobs) != membership["expected_recomputed"]:
            self.result.errors.append("nagarro_membership_job_count_mismatch")
        detail_started = time.perf_counter()
        self.result.jobs = await self.hydrate(jobs, self._detail_job)
        self.result.timings["detail_phase_seconds"] = max(0.0, time.perf_counter() - detail_started)

        # Final company-listing recheck: the page, bundle and all three tables.
        final = {}
        try:
            final["entry"] = await self._official_entry()
            final["listing"] = await self._listing_page()
            final["bundle"] = await self._ui_bundle(final["listing"]["listing_bundle_url"])
            final["main"] = await self._table(final["bundle"]["table_urls"]["main"], table="Main")
            final["split"] = await self._table(final["bundle"]["table_urls"]["split"], table="Split")
            final["experience"] = await self._table(final["bundle"]["table_urls"]["experience"], table="Experience")
        except Exception as exc:
            self.result.errors.append(f"Nagarro final listing recheck failed: {type(exc).__name__}: {exc}")
            final = {}
        if final:
            final_membership = build_membership(final["main"]["rows"], final["split"]["rows"],
                                                final["experience"]["rows"], final["bundle"])
            initial_ids = membership["posting_ids"]
            final_ids = final_membership["posting_ids"]
            initial_rows_by_index = {row.get("index"): row for row in full_rows}
            final_rows_by_index = {row.get("index"): row for row in final["main"]["rows"]}
            changed = {
                str(index): sorted(field for field in set(initial_rows_by_index[index]) | set(final_rows_by_index[index])
                                   if initial_rows_by_index[index].get(field) != final_rows_by_index[index].get(field))
                for index in initial_rows_by_index if index in final_rows_by_index
                and initial_rows_by_index[index] != final_rows_by_index[index]
            }
            membership_changed = initial_ids != final_ids
            table_urls_changed = bundle["table_urls"] != final["bundle"]["table_urls"]
            entry_ok = final["entry"]["listing_cta_data_url"] == LISTING_URL
            self.result.metadata["final_listing_snapshot"] = {
                "main_rows": len(final["main"]["rows"]),
                "main_pages": final["main"]["page_count"],
                "main_ids": [row.get("index") for row in final["main"]["rows"]],
                "split_row_count": len(final["split"]["rows"]),
                "experience_row_count": len(final["experience"]["rows"]),
            }
            self.tables["final_main"] = final["main"]
            self.tables["final_split"] = final["split"]
            self.result.metadata["membership_reconciliation"] = {
                "initial_posting_ids": initial_ids,
                "final_posting_ids": final_ids,
                "initial_main_rows": len(full_rows),
                "final_main_rows": len(final["main"]["rows"]),
                "initial_split_children": len(split["rows"]),
                "final_split_children": len(final["split"]["rows"]),
                "same_posting_membership": not membership_changed,
                "new_posting_ids": [i for i in final_ids if i not in initial_ids],
                "removed_posting_ids": [i for i in initial_ids if i not in final_ids],
                "main_rows_changed_fields": changed,
                "table_urls_unchanged": not table_urls_changed,
                "official_entry_cta_still_exact": entry_ok,
                "final_membership_issues": final_membership["issues"],
                "complete": (not membership_changed and not changed and not table_urls_changed
                             and entry_ok and not final_membership["issues"]),
            }
            if membership_changed:
                self.result.errors.append("nagarro_listing_membership_changed_during_collection")
            if changed:
                self.result.errors.append("nagarro_listing_fields_changed_during_collection")
            if table_urls_changed:
                self.result.errors.append("nagarro_listing_table_urls_changed_during_collection")
            if not entry_ok:
                self.result.errors.append("nagarro_official_entry_cta_changed_during_collection")
            if final_membership["issues"]:
                self.result.errors.extend(f"final:{issue}" for issue in final_membership["issues"])

        details = self.result.metadata.get("details") or {}
        verified = sum(job.description_verified for job in self.result.jobs)
        all_details = (len(self.result.jobs) == membership["expected_postings"] == details.get("total")
                       and details.get("failed", 0) == 0 and verified == len(self.result.jobs))
        complete = bool(
            not membership["issues"] and not self.result.errors and all_details
            and self.result.metadata.get("membership_reconciliation", {}).get("complete") is True
        )
        self.result.listing_complete = complete
        self.result.metadata.update({
            "job_ids": [job.job_id for job in self.result.jobs],
            "posting_count": len(self.result.jobs),
            "descriptions_verified": verified,
            "detail_failures": details.get("failed"),
            "all_details_verified": all_details,
            "current_native_membership_reconciled": complete,
            "out_of_scope_context": {
                "linked_smartrecruiters_board": BOARD,
                "note": "The Nagarro1 board total is deliberately not enumerated; only company-table posting IDs are used.",
            },
        })
        self.result.timings["source_collection_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
