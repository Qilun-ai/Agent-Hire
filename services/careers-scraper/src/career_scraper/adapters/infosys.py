"""Infosys' own careers journey: the India portal and the global careers site.

The official entry ``https://www.infosys.com/careers/apply.html`` publishes two
job listings: the Infosys Limited India portal (``career.infosys.com``) and the
global careers site (``digitalcareers.infosys.com``, SmartDreamers).  Both are
enumerated from the exact requests those pages themselves issue:

* India: the Angular bundle's ``getAlljobs`` call
  ``getCareerSearchJobs?sourceId=1,21&searchText=ALL`` returns every published
  row (with its full description fields inline) in one JSON array.
* Global: the site's own Typesense ``multi_search`` request against
  ``production_Infosys_jobs``; each real posting is then hydrated from its
  server-rendered ``company-job/description/reqid/<reqid>`` page because the
  index only carries a truncated ``summary``.

Akamai refuses headless Chromium on ``career.infosys.com`` but serves the same
HTML/API to a plain HTTP client, so the India flow is HTTP-only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from .base import Adapter
from ..models import Job, text_html


OFFICIAL_ENTRY = "https://www.infosys.com/careers/apply.html"
INDIA_LISTING_PAGE = "https://career.infosys.com/joblist?countrycode=IN&companyhiringtype=IL"
INDIA_RESULTS_PAGE = "https://career.infosys.com/jobs?companyhiringtype=IL&countrycode=IN"
INDIA_API_ROOT = "https://intapgateway.infosysapps.com/careersci/search/intapjbsrch/"
INDIA_SEARCH_API = INDIA_API_ROOT + "getCareerSearchJobs?sourceId=1,21&searchText=ALL"
INDIA_DETAIL_API = INDIA_API_ROOT + "getJobDesc?referenceCode={reference}"
INDIA_JOB_URL = "https://career.infosys.com/jobdesc?jobReferenceCode={reference}&sourceId=1"
GLOBAL_LISTING_PAGE = "https://digitalcareers.infosys.com/infosys/global-careers"
GLOBAL_SEARCH_API = ("https://search.smartdreamers.com/multi_search"
                     "?x-typesense-api-key=NWg8OnzBAL9tKphbA4j6NbaZRZDH65l9")
GLOBAL_COLLECTION = "production_Infosys_jobs"
GLOBAL_DETAIL_URL = ("https://digitalcareers.infosys.com/global-careers/company-job/"
                     "description/reqid/{reqid}")
GLOBAL_PER_PAGE = 100
COMPANY_NAME = "Infosys"
SOURCE_NAME = "infosys"
EMPLOYER = "Infosys Limited"
PORTALS = ("india", "global")

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
INDIA_API_HEADERS = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://career.infosys.com",
    "Referer": "https://career.infosys.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}
GLOBAL_SEARCH_HEADERS = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://digitalcareers.infosys.com",
    "Referer": GLOBAL_LISTING_PAGE,
    "Content-Type": "application/json",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}

INDIA_REQUIRED_FIELDS = (
    "postingTitle", "createdOn", "unit", "location", "postingDescription",
    "technicalRequirement", "additionalResponsibility", "postingId", "requisitionId",
    "referenceCode", "sourceId", "minExperienceLevel", "maxExperienceLevel",
    "rolesResponsibilities", "company", "country", "preferredSkills", "genericSkills",
    "educationalRequirement", "expiryDate", "publicationId", "hotjob",
    "companyHiringTypeId", "functionalArea",
)
# Section order observed in the rendered jobdesc component (.jobDesc).
INDIA_SECTIONS = (
    ("Educational Requirements", "educationalRequirement"),
    ("Service Line", "unit"),
    ("Responsibilities", "rolesResponsibilities"),
    ("Additional Responsibilities:", "additionalResponsibility"),
    ("Technical and Professional Requirements:", "technicalRequirement"),
    ("Preferred Skills:", "preferredSkills"),
    ("Generic Skills:", "genericSkills"),
)
GLOBAL_LIST_FIELDS = (
    "Job Role", "Work Location", "State / Region / Province", "Country", "Skills",
    "Domain", "Interest Group", "Company", "Requisition ID",
)
BOILERPLATE_MARKERS = ("<b>About Us</b>", "<strong>About Us</strong>", ">About Us<")


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_visible_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _paragraph(value: str | None) -> str:
    text = (value or "").replace("\r\n", "\n")
    if not text.strip():
        return ""
    return "<p>" + escape(text).replace("\n", "<br/>") + "</p>"


def india_job_url(reference_code: str) -> str:
    return INDIA_JOB_URL.format(reference=reference_code)


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Read the two company-published job journeys out of the official entry page."""
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "www.infosys.com" or parsed.path.rstrip("/") != "/careers/apply.html":
        raise ValueError(f"Infosys official entry redirected away from apply.html: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    india: list[str] = []
    global_links: set[str] = set()
    global_locations: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if href.startswith("https://career.infosys.com/joblist"):
            query = parse_qs(urlsplit(href).query)
            if (query.get("countrycode", [""])[0].upper() == "IN"
                    and query.get("companyhiringtype", [""])[0].upper() == "IL"):
                india.append(href)
        if href.startswith("https://digitalcareers.infosys.com/infosys/global-careers"):
            global_links.add(href)
            location = parse_qs(urlsplit(href).query).get("location", [""])[0]
            if location:
                global_locations.add(location)
    if not india:
        raise ValueError("Infosys apply page no longer publishes the India job-list link")
    if not global_links:
        raise ValueError("Infosys apply page no longer publishes the global careers links")
    return {
        "url": response_url,
        "title": normalized_title(soup.title.get_text(" ", strip=True) if soup.title else ""),
        "india_listing_url": INDIA_LISTING_PAGE,
        "india_link_occurrences": len(india),
        "global_listing_url": GLOBAL_LISTING_PAGE,
        "global_link_examples": sorted(global_links)[:5],
        "global_link_count": len(global_links),
        "global_location_values": sorted(global_locations),
    }


def parse_india_listing(payload: str | bytes, response_url: str = INDIA_SEARCH_API) -> dict:
    """Parse the unfiltered company search response the portal loads on /jobs."""
    data = json.loads(payload)
    if not isinstance(data, list) or not data:
        raise ValueError("Infosys India search response is not a non-empty JSON array")
    rows = []
    references = set()
    for index, row in enumerate(data):
        missing = [field for field in INDIA_REQUIRED_FIELDS if field not in row]
        if missing:
            raise ValueError(f"Infosys India row {index} lost fields: {missing}")
        reference = str(row["referenceCode"] or "").strip()
        if not reference:
            raise ValueError(f"Infosys India row {index} has no referenceCode")
        if reference in references:
            raise ValueError(f"Infosys India duplicate referenceCode {reference}")
        references.add(reference)
        rows.append(row)
    return {"url": response_url, "total": len(rows), "records": rows}


def india_description_html(record: dict) -> str:
    """Rebuild the job-description component content from the published fields."""
    parts = []
    posting = (record.get("postingDescription") or "").strip()
    if posting:
        parts.append("<h3>Job Description</h3>" + _paragraph(record.get("postingDescription")))
    for label, key in INDIA_SECTIONS:
        value = (record.get(key) or "").strip() if isinstance(record.get(key), str) else record.get(key)
        if not value:
            continue
        parts.append(f"<h3>{escape(label)}</h3>" + _paragraph(str(value)))
    return "\n".join(parts)


def india_date(value: str | None) -> tuple[str | None, str | None]:
    """ISO posting timestamp when the source field is a real per-posting date."""
    raw = (value or "").strip()
    if not raw:
        return None, None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None, raw
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat(), raw


def india_experience(record: dict) -> str | None:
    minimum, maximum = record.get("minExperienceLevel"), record.get("maxExperienceLevel")
    if minimum is None and maximum is None:
        return None
    if minimum is not None and maximum is not None:
        return f"{minimum} - {maximum} years"
    return f"{minimum if minimum is not None else maximum} years"


def parse_global_search(payload: str | bytes | dict, *, page: int, response_url: str = GLOBAL_SEARCH_API) -> dict:
    """Validate one page of the SmartDreamers Typesense search the site performs."""
    data = payload if isinstance(payload, dict) else json.loads(payload)
    if isinstance(data, dict) and "results" in data:
        results = data.get("results")
        if not results:
            raise ValueError(f"Infosys global search returned no result set: {data}")
        result = results[0]
    else:
        result = data  # a saved ``results[0]`` page from the same request
    if result.get("code") == 404:
        raise ValueError(f"Infosys global search index error: {result.get('error')}")
    if "hits" not in result or "found" not in result:
        raise ValueError("Infosys global search page is missing hits/found")
    params = result.get("request_params") or {}
    collection = params.get("collection_name")
    if collection and collection != GLOBAL_COLLECTION:
        raise ValueError(f"Infosys global search collection changed: {collection}")
    documents = [hit["document"] for hit in result["hits"]]
    return {
        "url": response_url,
        "page": page,
        "found": result["found"],
        "documents": documents,
    }


def is_global_posting(document: dict) -> bool:
    """The index also carries 18 SmartDreamers campaign objects without a public job."""
    return bool(document.get("reqid")) and bool(document.get("has_external")) and bool(document.get("external_id"))


def global_detail_url(reqid: str) -> str:
    return GLOBAL_DETAIL_URL.format(reqid=reqid)


def _split_boilerplate(container: Tag) -> tuple[str, str]:
    """Separate the employer's role description from the appended About Us / EEO template.

    The template is one paragraph that opens with ``<b>About Us</b>`` and also
    carries the ``EEO`` sentence; postings that write their own "About Us" section
    (or a "Who We Are" block) keep it, because it is part of the published role
    content rather than the duplicated footer.
    """
    template = None
    for paragraph in container.find_all("p"):
        bold = paragraph.find("b") or paragraph.find("strong")
        if bold is None:
            continue
        if normalized_title(bold.get_text(" ", strip=True)).lower().rstrip(":") != "about us":
            continue
        text = paragraph.get_text(" ", strip=True)
        if "EEO" in text or "equal employment opportunities" in text:
            template = paragraph
            break
    if template is None:
        return str(container), ""
    top = template
    while top.parent is not None and top.parent is not container:
        top = top.parent
    removed = []
    dropping = False
    for child in list(container.find_all(recursive=False)):
        if child is top:
            dropping = True
        if dropping:
            removed.append(str(child))
            child.extract()
    return str(container), "".join(removed)


def global_job(document: dict) -> Job:
    """Listing record for one real global posting; description comes from the detail page."""
    reqid = str(document["reqid"][0])
    url = global_detail_url(reqid)
    created = document.get("created_at")
    posted = None
    if isinstance(created, (int, float)):
        posted = datetime.fromtimestamp(created, timezone.utc).isoformat()
    locations = [normalized_title(value) for value in (document.get("work_locations_list") or document.get("work_location") or []) if normalized_title(value)]
    country = normalized_title((document.get("country") or [""])[0])
    if country:
        locations.append(country)
    employment = normalized_title((document.get("job_type") or [""])[0]) or None
    raw = {
        "portal": "global",
        "index_document": document,
        "summary_is_truncated_preview": True,
        "created_at_epoch": created,
        "work_authorization_raw": document.get("work_authorization"),
        "eeo_about_us_raw": document.get("eeo_about_us"),
        "detail_url": url,
    }
    return Job(
        company=COMPANY_NAME,
        source=SOURCE_NAME,
        title=normalized_title(document.get("title")),
        url=url,
        job_id=reqid,
        requisition_id=reqid,
        description_html="",
        description_method=None,
        description_source_url=None,
        locations=locations,
        department=normalized_title((document.get("domain") or [""])[0]) or None,
        team=normalized_title((document.get("interest_group") or [""])[0]) or None,
        employment_type=employment,
        date_posted=posted,
        date_posted_raw=created,
        employer=normalized_title((document.get("company") or [""])[0]) or None,
        raw=raw,
    )


PUBLISHER_NA_WARNING = (
    "Publisher body is 'NA': postingDescription='NA', rolesResponsibilities='NA'; "
    "the stored text is only labels plus educational requirement/service line/preferred skills, "
    "not a usable role description."
)


def india_job(record: dict) -> Job:
    """Listing record for one India posting; the search row already carries the description.

    A small number of Facilities/security requisitions publish 'NA' (not available) for
    both description-bearing fields. Their full published text (labels, educational
    requirement, service line, preferred skills) is still stored verbatim, but it is
    not a usable role description, so the record is kept collected-but-unusable.
    """
    reference = str(record["referenceCode"]).strip()
    url = india_job_url(reference)
    description_html = india_description_html(record)
    posted, posted_raw = india_date(record.get("createdOn"))
    raw = {
        "portal": "india",
        "reference_code": reference,
        "listing_row": record,
        "detail_api_url": INDIA_DETAIL_API.format(reference=reference),
        "posting_description_rendered_on_detail_page": False,
        "expiry_date_raw": record.get("expiryDate"),
        "apply_route": "in-page candidate flow; the public job page is the apply entry",
    }
    job = Job(
        company=COMPANY_NAME,
        source=SOURCE_NAME,
        title=normalized_title(record.get("postingTitle")),
        url=url,
        job_id=reference,
        requisition_id=str(record.get("requisitionId")) if record.get("requisitionId") is not None else None,
        description_html=description_html,
        description_method="company_search_api_inline",
        description_source_url=INDIA_SEARCH_API,
        locations=[normalized_title(record.get("location"))] if record.get("location") else [],
        department=record.get("functionalArea") or None,
        team=record.get("unit") or None,
        employment_type=None,
        date_posted=posted,
        date_posted_raw=posted_raw,
        qualifications=record.get("educationalRequirement") or None,
        responsibilities=record.get("rolesResponsibilities") or None,
        experience=india_experience(record),
        employer=record.get("company") or EMPLOYER,
        raw=raw,
    )
    posting = record.get("postingDescription")
    roles = record.get("rolesResponsibilities")
    if (
        isinstance(posting, str) and isinstance(roles, str)
        and posting.strip() == "NA" and roles.strip() == "NA"
    ):
        job.description_unavailable = True
        job.warnings.append(PUBLISHER_NA_WARNING)
    return job


def parse_global_detail(html: str, response_url: str, reqid: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one(".description-page-container")
    if container is None:
        raise ValueError(f"Infosys global job page lost its description container: {response_url}")
    breadcrumb = soup.select_one(".breadcrumbs")
    title = ""
    if breadcrumb is not None:
        segments = [normalized_title(part) for part in breadcrumb.get_text("|", strip=True).split("|")]
        segments = [part for part in segments if part]
        title = segments[-1] if segments else ""
    fields: dict[str, str] = {}
    left = container.select_one(".description-page-left")
    if left is not None:
        titles = left.select(".field-title")
        for label in titles:
            key = normalized_title(label.get_text(" ", strip=True))
            value = label.find_next_sibling("p", class_="field-value")
            if value is None:
                continue
            fields[key] = normalized_title(value.get_text(" ", strip=True))
    right = container.select_one(".description-page-right")
    description_html, boilerplate_html = ("", "")
    if right is not None:
        description_html, boilerplate_html = _split_boilerplate(right)
    apply_url = ""
    hidden = soup.select_one("input#apply_url")
    if hidden is not None and hidden.get("value"):
        apply_url = hidden["value"].strip()
    if not apply_url:
        link = soup.select_one("a.infosys-apply-link")
        if link is not None:
            apply_url = (link.get("href") or "").strip()
    if fields.get("Requisition ID") and fields["Requisition ID"] != reqid:
        raise ValueError(f"Infosys global job page requisition mismatch: {fields['Requisition ID']} != {reqid}")
    return {
        "url": response_url,
        "reqid": reqid,
        "title": title,
        "fields": fields,
        "description_html": description_html,
        "description_text": text_html(description_html),
        "boilerplate_html": boilerplate_html,
        "apply_url": apply_url or None,
    }


class Infosys(Adapter):
    """Both company-published Infosys listings; ``Source.options['portal']`` selects one."""

    portal = "india"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.portal = str((source.options or {}).get("portal") or "india").lower()
        if self.portal not in PORTALS:
            raise ValueError(f"Unknown Infosys portal {self.portal!r}; expected one of {PORTALS}")

    async def run(self):
        if self.portal == "global":
            return await self._global()
        return await self._india()

    # ---------------------------------------------------------------- India
    async def _india(self):
        started = len(self.result.jobs)
        response = await self.http.request("GET", INDIA_SEARCH_API, headers=INDIA_API_HEADERS)
        listing = parse_india_listing(response.text, str(response.url))
        self.result.expected_total = listing["total"]
        self.result.pages = 1
        self.result.metadata["listing"] = {
            "portal": "india",
            "page_url": INDIA_LISTING_PAGE,
            "rendered_results_url": INDIA_RESULTS_PAGE,
            "search_api": str(response.url),
            "search_api_total": listing["total"],
            "response_sha256": self._sha(response.content),
            "reference_codes": [str(row["referenceCode"]) for row in listing["records"]],
        }
        for row in listing["records"]:
            if self.limit():
                break
            self.add(india_job(row))
        self.result.listing_complete = (
            len(self.result.jobs) - started == listing["total"] and not self.result.errors
        )
        self.result.metadata["listing"]["collected"] = len(self.result.jobs) - started
        return self.result

    @staticmethod
    def _sha(payload: bytes) -> str:
        from hashlib import sha256
        return sha256(payload).hexdigest()

    # --------------------------------------------------------------- Global
    async def _global(self):
        started = len(self.result.jobs)
        documents: list[dict] = []
        pages = 0
        total = None
        page = 1
        while True:
            body = {"searches": [{
                "query_by": "title,reqid",
                "collection": GLOBAL_COLLECTION,
                "q": "*",
                "per_page": GLOBAL_PER_PAGE,
                "page": page,
            }]}
            response = await self.http.request("POST", GLOBAL_SEARCH_API, json=body, headers=GLOBAL_SEARCH_HEADERS)
            payload = parse_global_search(response.content, page=page, response_url=str(response.url))
            pages += 1
            total = payload["found"]
            documents.extend(payload["documents"])
            if len(payload["documents"]) < GLOBAL_PER_PAGE:
                break
            page += 1
        self.result.pages = pages
        postings = [document for document in documents if is_global_posting(document)]
        excluded = [document for document in documents if not is_global_posting(document)]
        self.result.expected_total = len(postings)
        self.result.metadata["listing"] = {
            "portal": "global",
            "page_url": GLOBAL_LISTING_PAGE,
            "search_api": GLOBAL_SEARCH_API,
            "collection": GLOBAL_COLLECTION,
            "per_page": GLOBAL_PER_PAGE,
            "index_documents": len(documents),
            "index_found": total,
            "non_posting_documents": len(excluded),
            "non_posting_titles": sorted({normalized_title(item.get("title")) for item in excluded})[:20],
            "postings": len(postings),
            "reqids": [str(document["reqid"][0]) for document in postings],
        }
        for document in postings:
            if self.limit():
                break
            self.add(global_job(document))
        await self.hydrate(self.result.jobs, self._global_detail)
        verified = sum(job.description_verified for job in self.result.jobs)
        self.result.metadata["listing"]["collected"] = len(self.result.jobs) - started
        self.result.metadata["listing"]["descriptions_verified"] = verified
        self.result.listing_complete = (
            len(self.result.jobs) - started == len(postings)
            and not self.result.errors
            and verified == len(self.result.jobs) - started
        )
        return self.result

    async def _global_detail(self, job):
        response = await self.http.request("GET", job.url, headers=BROWSER_HEADERS)
        detail = parse_global_detail(response.text, str(response.url), job.job_id)
        expected = normalized_title(job.title)
        observed = normalized_title(detail["title"])
        if observed and observed != expected:
            job.warnings.append("detail_title_differs_from_listing")
        job.description_html = detail["description_html"]
        job.description_text = detail["description_text"]
        job.description_method = "company_detail_page_html"
        job.description_source_url = job.url
        job.apply_url = detail["apply_url"]
        job.raw["detail"] = {
            "url": detail["url"],
            "title": detail["title"],
            "fields": detail["fields"],
            "boilerplate_html": detail["boilerplate_html"],
        }
        fields = detail["fields"]
        if fields.get("Job Role"):
            job.raw["job_role"] = fields["Job Role"]
        if fields.get("Skills"):
            job.raw["skills"] = fields["Skills"]
        if fields.get("State / Region / Province"):
            job.raw["state_region_province"] = fields["State / Region / Province"]
        job.qualifications = _section_text(detail["description_text"], "Qualifications")
        job.responsibilities = _section_text(detail["description_text"], "Key Responsibilities")
        if not (job.description_text or "").strip():
            job.description_unavailable = True
            job.warnings.append("detail_description_empty")
        return job


def _section_text(body: str, heading: str) -> str | None:
    """Pull one labelled block out of a plain-text job description."""
    if not body:
        return None
    lines = [line.strip() for line in body.splitlines()]
    collected: list[str] = []
    collecting = False
    for line in lines:
        if not line:
            if collecting:
                collected.append("")
            continue
        if line == heading:
            collecting = True
            continue
        if collecting:
            if line in {"Key Responsibilities", "Qualifications", "Additional Required Qualifications",
                        "Skills", "About Us", "EEO", "Preferred Skills"}:
                break
            collected.append(line)
    text = "\n".join(collected).strip()
    return text or None
