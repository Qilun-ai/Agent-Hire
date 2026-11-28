"""Publicis Sapient's own careers site: its AEM job search and SSR job details.

Membership is the company's own `/job-search` React widget. That widget calls the
same-origin AEM endpoint `/bin/ps-redesign/careersJobsearch` with `searchType=/search`,
`start`/`rows` paging and no country filter unless the visitor's IP geolocation
pre-selects one. This module deliberately omits the geo filter so the global
published membership is collected. Each listing row's exact `/job-details/<id>-<slug>`
page server-renders the complete employer description in `data-react-props`.
"""
from __future__ import annotations

from datetime import datetime
import math
import re
import time
import unicodedata
from hashlib import sha256
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://careers.publicissapient.com/"
LISTING_URL = "https://careers.publicissapient.com/job-search"
LISTING_ORIGIN = "https://careers.publicissapient.com"
LISTING_API = LISTING_ORIGIN + "/bin/ps-redesign/careersJobsearch"
DETAIL_PATH = "/job-details/"
ICIMS_HOST = "sapient-publicisgroupe.icims.com"
FACET_FIELDS = ["countryName", "city", "teams", "experienceLevel", "remote", "typeOfEmployment"]
FACET_FIELDS_PARAM = ",".join(FACET_FIELDS)
RESULTS_PER_PAGE = 20
SEARCH_TYPE = "/search"
LISTING_RESULT_COMPONENT = "careers-job-search-result"
LISTING_HERO_COMPONENT = "careersHeroJobSearch"
DETAIL_COMPONENT = "careerjobdetailstemplate"
SEARCH_BUNDLE_NAME = "clientlib-careerssearchresults.js"

# The order is the order the detail component renders them.
SECTION_FIELDS = [
    ("jobDescriptionSection", "Job Description"),
    ("qualificationDescriptionSection", "Qualifications"),
    ("additionalInformationSection", "Additional Information"),
    ("companyDetailsSection", "Company Description"),
]
REQUIRED_STRING_FIELDS = [
    "id", "jobId", "name", "typeOfEmployment", "experienceLevel", "city", "region",
    "countryName", "displayLocation", "jobUrl", "releasedDate", "costCenterType",
    "costCenterName", "psCraft", "teams", "jobDetailUrl",
]
RELEASED_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
JOB_DETAIL_REF = re.compile(r"^/job-details/([^/]+)$")
ICIMS_APPLY = re.compile(r"^/jobs/(\d+)/job/login$")
SALARY_LINE = re.compile(
    r"(?:salary range|base salary range)(?:\s+for[^:.]{0,80})?\s*(?:is|:)\s*"
    r"(.+?)(?=\s+(?:The ranges?|The pay range|The salary range|Actual|As part|As a|An inclusive|"
    r"The base|Publicis|This range|Compensation|Benefits|We are|We're|Our|Why|Equal)\b|[.<]|$)",
    re.I,
)


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


BLOCK_TAGS = frozenset(
    "p div section article header footer main aside nav h1 h2 h3 h4 h5 h6 ul ol li dl dt dd "
    "table thead tbody tfoot tr td th blockquote pre figure figcaption form hr address".split()
)


def _browser_text(node) -> str:
    """Concatenate like Chromium innerText: block boundaries add space, inline tags do not."""
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    if node.name in {"script", "style", "noscript", "template"}:
        return ""
    inner = "".join(_browser_text(child) for child in node.children)
    if node.name == "br":
        return "\n"
    if node.name in BLOCK_TAGS:
        return f"\n{inner}\n"
    return inner


def browser_text(html: str | None) -> str:
    """Readable text with Chromium innerText-compatible inline/block spacing."""
    soup = BeautifulSoup(html or "", "lxml")
    value = "".join(_browser_text(child) for child in soup.children)
    value = unicodedata.normalize("NFC", value).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    return "\n".join(line for line in lines if line)


def normalize_body_text(html: str | None) -> str:
    return re.sub(r"\s+", " ", browser_text(html)).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _react_props(html: str, component: str) -> dict:
    """Read one AEM component's single-quoted `data-react-props` JSON payload."""
    marker = f'data-cmp-is="{component}"'
    occurrences = html.count(marker)
    if occurrences != 1:
        raise ValueError(f"Expected exactly one {component} component, found {occurrences}")
    start = html.find(marker)
    attr = html.find("data-react-props=", start)
    if attr < 0:
        raise ValueError(f"{component} component has no data-react-props payload")
    quote_at = attr + len("data-react-props=")
    quote = html[quote_at:quote_at + 1]
    if quote not in {"'", '"'}:
        raise ValueError(f"{component} component uses an unexpected props quoting style")
    cursor = quote_at + 1
    while True:
        cursor = html.find(quote, cursor)
        if cursor < 0:
            raise ValueError(f"{component} component props payload is unterminated")
        following = html[cursor + 1:cursor + 2]
        if following in {">", " ", "\n", "\r", "\t", "/"}:
            break
        cursor += 1
    payload = html[quote_at + 1:cursor]
    try:
        value = _json_loads(payload)
    except ValueError as exc:
        raise ValueError(f"{component} component props payload is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{component} component props are not an object")
    return value


def _json_loads(payload: str):
    import html as html_module
    import json
    decoded = html_module.unescape(payload)
    try:
        return json.loads(decoded)
    except ValueError:
        # A few AEM fields double-escape embedded attribute quotes as \&#34;.
        return json.loads(decoded.replace('\\"', '"'))


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "careers.publicissapient.com" or parts.path not in {"", "/"}:
        raise ValueError("Publicis Sapient official entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ")) if soup.title else ""
    if "Careers at Publicis Sapient" not in title:
        raise ValueError("Publicis Sapient official entry title changed")
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical is None or canonical.get("href") != OFFICIAL_ENTRY:
        raise ValueError("Publicis Sapient official careers canonical URL changed")
    listing_links = []
    for anchor in soup.select("a[href]"):
        target = urljoin(response_url, anchor.get("href", ""))
        if target == LISTING_URL:
            listing_links.append(target)
    listing_links = list(dict.fromkeys(listing_links))
    if not listing_links:
        raise ValueError("Publicis Sapient official careers page no longer links to its own /job-search listing")
    group_links = sorted({
        urljoin(response_url, anchor.get("href", ""))
        for anchor in soup.select("a[href]")
        if urlsplit(urljoin(response_url, anchor.get("href", ""))).netloc == "careers.publicisgroupe.com"
    })
    return {
        "url": response_url,
        "title": title,
        "canonical": canonical.get("href"),
        "listing_links": listing_links,
        "parent_group_links_not_followed": group_links,
        "meta_description": (soup.select_one('meta[name="description"]') or {}).get("content")
        if soup.select_one('meta[name="description"]') else None,
    }


def parse_listing_page(html: str, response_url: str = LISTING_URL) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "careers.publicissapient.com" or parts.path != "/job-search":
        raise ValueError("Publicis Sapient listing redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalize_text(soup.title.get_text(" ")) if soup.title else ""
    if "Job Search Page" not in title:
        raise ValueError("Publicis Sapient listing title changed")
    result_props = _react_props(html, LISTING_RESULT_COMPONENT)
    hero_props = _react_props(html, LISTING_HERO_COMPONENT)
    if result_props.get("resultsPerPage") != RESULTS_PER_PAGE:
        raise ValueError("Publicis Sapient listing page size changed")
    script = soup.select_one(f'script[src*="{SEARCH_BUNDLE_NAME}"]')
    if script is None:
        raise ValueError("Publicis Sapient listing no longer loads the search-results bundle")
    bundle_url = urljoin(response_url, script.get("src", ""))
    bundle_parts = urlsplit(bundle_url)
    if (bundle_parts.scheme != "https" or bundle_parts.netloc != "careers.publicissapient.com"
            or not bundle_parts.path.endswith(SEARCH_BUNDLE_NAME)):
        raise ValueError("Publicis Sapient listing bundle URL is outside the company site")
    ssr_links = [
        anchor.get("href") for anchor in soup.select("a[href]")
        if urlsplit(urljoin(response_url, anchor.get("href", ""))).path.startswith(DETAIL_PATH)
    ]
    return {
        "url": response_url,
        "title": title,
        "results_per_page": result_props.get("resultsPerPage"),
        "job_details_page_prop": result_props.get("jobDetailsPage"),
        "results_count_template": result_props.get("resultsCountTemplate"),
        "filter_titles": {
            "country": result_props.get("countryTitle"),
            "city": result_props.get("cityTitle"),
            "teams": result_props.get("teamsTitle"),
            "experience_level": result_props.get("experienceLevelTitle"),
            "work_place_type": result_props.get("workPlaceTypeTitle"),
            "employment_type": result_props.get("employmentTypeTitle"),
        },
        "search_action_url": hero_props.get("searchActionUrl"),
        "trid": result_props.get("trid"),
        "search_bundle_url": bundle_url,
        "ssr_job_detail_links": len(ssr_links),
    }


def parse_search_bundle(js: str, response_url: str) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "careers.publicissapient.com"
            or not parts.path.endswith(SEARCH_BUNDLE_NAME)):
        raise ValueError("Publicis Sapient search bundle came from an unexpected URL")
    defaults = re.findall(r'facetField:"([A-Za-z]+)"', js)
    if defaults != FACET_FIELDS:
        raise ValueError(f"Publicis Sapient listing facet fields changed: {defaults}")
    if "/bin/ps-redesign/careersJobsearch" not in js:
        raise ValueError("Publicis Sapient search bundle no longer exposes its listing endpoint")
    if 'searchType:"/search"' not in js:
        raise ValueError("Publicis Sapient search bundle no longer requests searchType /search")
    if not all(token in js for token in ("start:", "rows:", "numFound")):
        raise ValueError("Publicis Sapient search bundle no longer implements start/rows/numFound")
    return {
        "url": response_url,
        "default_facet_fields": defaults,
        "endpoint": "/bin/ps-redesign/careersJobsearch",
        "uses_start_rows": True,
        "uses_num_found": True,
        "browser_geolocation_default_observed": "Geolocation detection failed" in js,
    }


def parse_api_page(value: dict, requested_page: int, *, rows_per_page: int = RESULTS_PER_PAGE) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Publicis Sapient listing API response is not an object")
    header = value.get("responseHeader")
    if not isinstance(header, dict) or header.get("status") != 0:
        raise ValueError("Publicis Sapient listing API reported a non-zero status")
    response = value.get("response")
    if not isinstance(response, dict):
        raise ValueError("Publicis Sapient listing API response is missing response")
    count, start, docs = response.get("numFound"), response.get("start"), response.get("docs")
    if not isinstance(count, int) or count < 0 or not isinstance(start, int) or not isinstance(docs, list):
        raise ValueError("Publicis Sapient listing API membership fields changed")
    if response.get("numFoundExact") is not True:
        raise ValueError("Publicis Sapient listing API no longer reports an exact total")
    if requested_page < 1 or start != (requested_page - 1) * rows_per_page:
        raise ValueError("Publicis Sapient listing API start does not match the requested page")
    expected_rows = min(rows_per_page, max(0, count - start))
    issues = []
    if len(docs) != expected_rows:
        issues.append(f"page_row_count_mismatch:{len(docs)}!={expected_rows}")
    parsed, seen = [], set()
    for doc in docs:
        if not isinstance(doc, dict):
            issues.append("listing_row_not_object")
            continue
        identity = doc.get("id")
        if not isinstance(identity, str) or not identity:
            issues.append("listing_row_missing_id")
            continue
        if identity in seen:
            issues.append(f"listing_row_duplicate_id:{identity}")
            continue
        missing = [key for key in REQUIRED_STRING_FIELDS
                   if not isinstance(doc.get(key), str) or not doc.get(key).strip()]
        if missing:
            issues.append(f"listing_row_missing_fields:{identity}:{','.join(missing)}")
            continue
        detail_path = urlsplit(urljoin(LISTING_ORIGIN, doc["jobDetailUrl"])).path
        if not detail_path.startswith(f"{DETAIL_PATH}{identity}-"):
            issues.append(f"listing_row_detail_url_not_company_route:{identity}")
            continue
        apply = urlsplit(doc["jobUrl"])
        apply_match = ICIMS_APPLY.fullmatch(apply.path)
        if (apply.scheme != "https" or apply.netloc != ICIMS_HOST or apply_match is None
                or apply_match.group(1) != doc["jobId"].split("-")[-1]):
            issues.append(f"listing_row_apply_url_changed:{identity}")
            continue
        if not RELEASED_DATE.fullmatch(doc["releasedDate"]):
            issues.append(f"listing_row_released_date_changed:{identity}")
            continue
        try:
            datetime.strptime(doc["releasedDate"], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            issues.append(f"listing_row_released_date_invalid:{identity}")
            continue
        seen.add(identity)
        parsed.append(dict(doc))
    if len(parsed) != expected_rows:
        issues.append(f"page_valid_row_count_mismatch:{len(parsed)}!={expected_rows}")
    return {
        "rows": parsed,
        "job_ids": [row["id"] for row in parsed],
        "count": count,
        "start": start,
        "page": requested_page,
        "rows_per_page": rows_per_page,
        "issues": issues,
    }


def parse_facets(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("response"), dict):
        raise ValueError("Publicis Sapient facet response is missing response")
    count = value["response"].get("numFound")
    facets = value.get("facets")
    if not isinstance(count, int) or not isinstance(facets, dict) or facets.get("count") != count:
        raise ValueError("Publicis Sapient facet response count does not match numFound")
    buckets, issues = {}, []
    for field in FACET_FIELDS:
        group = facets.get(field)
        rows = group.get("buckets") if isinstance(group, dict) else None
        if not isinstance(rows, list) or not rows:
            issues.append(f"facet_missing:{field}")
            continue
        pairs = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("val"), str) or not isinstance(row.get("count"), int):
                issues.append(f"facet_bucket_changed:{field}")
                continue
            pairs.append([row["val"], row["count"]])
        buckets[field] = pairs
    if "countryName" in buckets and sum(row[1] for row in buckets["countryName"]) != count:
        issues.append("country_facet_sum_does_not_match_num_found")
    return {"count": count, "buckets": buckets, "issues": issues}


def salary_evidence(html: str) -> dict | None:
    text = normalize_body_text(html)
    match = SALARY_LINE.search(text)
    if not match:
        return None
    return {"source_section": "Additional Information", "text": normalize_text(match.group(0))}


def parse_detail_page(html: str, expected_row: dict, response_url: str) -> dict:
    path = urlsplit(response_url).path
    match = JOB_DETAIL_REF.fullmatch(path)
    if match is None or not match.group(1).startswith(f"{expected_row['id']}-"):
        raise ValueError("Publicis Sapient detail URL is not the exact exposed /job-details route")
    props = _react_props(html, DETAIL_COMPONENT)
    soup = BeautifulSoup(html, "lxml")
    page_lang = soup.html.get("lang") if soup.html is not None else None
    title = normalize_text(props.get("title"))
    if title != normalize_text(expected_row.get("name")):
        raise ValueError("Publicis Sapient detail title no longer matches its listing row")
    if props.get("releasedDate") != expected_row.get("releasedDate"):
        raise ValueError("Publicis Sapient detail releasedDate no longer matches its listing row")
    for key in ("countryName", "region", "teams", "typeOfEmployment"):
        if props.get(key) != expected_row.get(key):
            raise ValueError(f"Publicis Sapient detail {key} no longer matches its listing row")
    apply = (props.get("primaryCta") or {}).get("ctaLinkUrl")
    if apply != expected_row.get("jobUrl"):
        raise ValueError("Publicis Sapient detail apply URL no longer matches its listing row")
    header = props.get("description") or ""
    header_match = re.match(r"Job ID:\s*([^|]+?)\s*\|\s*(.*?)\s*\|\s*(.+?)\s*$", header, re.S)
    if header_match is None or header_match.group(1).strip() != expected_row.get("jobId"):
        raise ValueError("Publicis Sapient detail header Job ID no longer matches its listing row")
    if normalize_text(header_match.group(3)) != normalize_text(expected_row.get("typeOfEmployment")):
        raise ValueError("Publicis Sapient detail header employment type no longer matches its listing row")

    sections, description_parts = [], []
    for field, heading in SECTION_FIELDS:
        section = props.get(field)
        body = section.get("body") if isinstance(section, dict) else None
        if not isinstance(body, str) or not normalize_body_text(body):
            continue
        sections.append({
            "field": field,
            "heading": heading,
            "body_html": body,
            "body_text_chars": len(normalize_body_text(body)),
        })
        description_parts.append(f'<h2>{heading}</h2><div class="rich-text">{body}</div>')
    if not any(section["field"] == "jobDescriptionSection" for section in sections):
        raise ValueError("Publicis Sapient detail has no non-empty Job Description section")
    description_html = "".join(description_parts)
    description_text = browser_text(description_html)
    normalized = normalize_body_text(description_html)
    if len(normalized) < 100:
        raise ValueError("Publicis Sapient detail description is empty or truncated")
    qualification = next((section for section in sections
                          if section["field"] == "qualificationDescriptionSection"), None)
    salary = salary_evidence(next((section["body_html"] for section in sections
                                    if section["field"] == "additionalInformationSection"), ""))
    related = props.get("relatedJobsDetails") or []
    if not isinstance(related, list):
        related = []
    return {
        "url": response_url,
        "title": title,
        "job_id_header": header_match.group(1).strip(),
        "location_header": normalize_text(header_match.group(2)),
        "header_employment_type": normalize_text(header_match.group(3)),
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(normalized),
        "description_fnv1a64": fnv1a64(normalized),
        "description_sha256": sha256(description_html.encode()).hexdigest(),
        "sections": sections,
        "qualification_html": qualification["body_html"] if qualification else None,
        "salary": salary,
        "apply_url": apply,
        "meta_description": props.get("metaDescription"),
        "eyebrow": props.get("eyebrow"),
        "hrms": [props.get("hrms"), props.get("hrms2"), props.get("hrms3")],
        "related_jobs": related,
        "page_lang": page_lang,
        "expired_job_text_present": bool(props.get("expiredJobText")),
    }


def refresh_derived_fields(job) -> None:
    """Recompute section-derived fields from the stored detail, including resumed records."""
    detail = job.raw.get("detail") or {}
    sections = detail.get("sections") or []
    additional = next((section for section in sections
                       if section.get("field") == "additionalInformationSection"), None)
    qualification = next((section for section in sections
                          if section.get("field") == "qualificationDescriptionSection"), None)
    salary = salary_evidence(additional["body_html"]) if additional else None
    detail["salary"] = salary
    job.salary = salary["text"] if salary else None
    job.qualifications = qualification["body_html"] if qualification else None
    job.description_text = browser_text(job.description_html)
    if detail:
        detail["description_text"] = job.description_text


def _api_params(page: int | None = None) -> dict:
    params = {
        "searchType": SEARCH_TYPE,
        "lang": "en",
        "facetFields": FACET_FIELDS_PARAM,
        "q": "",
    }
    if page is None:
        params.update(start="0", rows="0")
    else:
        params.update(start=str((page - 1) * RESULTS_PER_PAGE), rows=str(RESULTS_PER_PAGE))
    return params


class PublicisSapient(Adapter):
    """Read the company's own AEM listing and its exact rendered detail pages."""

    VERIFICATION_REVISION = "publicis-sapient.aem-jobsearch-detail-props.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.detail_request_seconds = 0.0

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != OFFICIAL_ENTRY:
            raise ValueError("Publicis Sapient official entry redirected away from its exact URL")
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _listing_page(self):
        started = time.perf_counter()
        response = await self.http.get(LISTING_URL)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != LISTING_URL:
            raise ValueError("Publicis Sapient /job-search listing redirected away from its exact URL")
        parsed = parse_listing_page(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _search_bundle(self, bundle_url: str):
        started = time.perf_counter()
        response = await self.http.get(bundle_url)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_search_bundle(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _facets(self):
        started = time.perf_counter()
        response = await self.http.get(LISTING_API, params=_api_params())
        seconds = max(0.0, time.perf_counter() - started)
        parts = urlsplit(str(response.url))
        if parts.scheme != "https" or parts.netloc != "careers.publicissapient.com" or parts.path != "/bin/ps-redesign/careersJobsearch":
            raise ValueError("Publicis Sapient facet request redirected outside its company listing route")
        parsed = parse_facets(response.json())
        parsed.update(url=str(response.url), status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content),
                      request_params=parse_qs(parts.query))
        return parsed

    async def _api_page(self, page: int):
        started = time.perf_counter()
        response = await self.http.get(LISTING_API, params=_api_params(page))
        seconds = max(0.0, time.perf_counter() - started)
        parts = urlsplit(str(response.url))
        if parts.scheme != "https" or parts.netloc != "careers.publicissapient.com" or parts.path != "/bin/ps-redesign/careersJobsearch":
            raise ValueError("Publicis Sapient listing request redirected outside its company listing route")
        parsed = parse_api_page(response.json(), page)
        parsed.update(
            url=str(response.url),
            request_url=str(response.request.url),
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed

    async def _api_snapshot(self):
        started = time.perf_counter()
        pages, rows, seen, issues = [], [], set(), []
        count = total_pages = None
        page = 1
        while True:
            if not self.page_guard():
                issues.append("listing_page_budget_exhausted")
                break
            value = await self._api_page(page)
            issues.extend(value["issues"])
            if count is None:
                count, total_pages = value["count"], math.ceil(value["count"] / RESULTS_PER_PAGE)
                if count < 1 or total_pages < 1:
                    raise ValueError("Publicis Sapient listing advertises no jobs")
            elif value["count"] != count:
                issues.append(f"listing_total_changed_mid_scan:{value['count']}!={count}")
            for row in value["rows"]:
                identity = row["id"]
                if identity in seen:
                    issues.append(f"listing_duplicate_id_across_pages:{identity}")
                    continue
                seen.add(identity)
                rows.append(row)
            pages.append(value)
            if count is not None and page >= math.ceil(count / RESULTS_PER_PAGE):
                break
            page += 1
        if count is not None and len(rows) != count:
            issues.append(f"listing_total_does_not_match_rows:{len(rows)}!={count}")
        if total_pages is not None and len(pages) != total_pages:
            issues.append(f"listing_page_count_does_not_match:{len(pages)}!={total_pages}")
        return {
            "rows": rows,
            "job_ids": [row["id"] for row in rows],
            "count": count,
            "total_pages": total_pages,
            "pages": [{key: value for key, value in page.items() if key not in {"rows", "job_ids"}}
                      | {"job_ids": page["job_ids"]} for page in pages],
            "issues": list(dict.fromkeys(issues)),
            "request_seconds": max(0.0, time.perf_counter() - started),
        }

    def _make_job(self, row: dict):
        detail_url = urljoin(LISTING_ORIGIN, row["jobDetailUrl"])
        job = self.job(
            {
                "listing": dict(row),
                "requisition": {
                    "job_id": row["id"],
                    "jobId": row["jobId"],
                    "location_variant": row["id"] != row["jobId"],
                    "note": "One published listing row per location. Rows sharing jobId share the iCIMS requisition/apply URL.",
                },
            },
            title=normalize_text(row["name"]),
            url=detail_url,
            job_id=row["id"],
            requisition_id=row["jobId"],
            apply_url=row["jobUrl"],
            description="",
            method=None,
            description_url=detail_url,
            locations=[row["displayLocation"]],
            department=row["teams"],
            team=row["psCraft"],
            employment_type=row["typeOfEmployment"],
            workplace_type=None,
            date_posted=row["releasedDate"],
            date_posted_raw=row["releasedDate"],
            date_updated=None,
            valid_through=None,
            salary=None,
            employer="Publicis Sapient",
            responsibilities=None,
            qualifications=None,
        )
        job.company_scope_verified = self.company.scope == "company"
        return job

    async def _detail(self, job):
        started = time.perf_counter()
        try:
            response = await self.http.get(job.url)
            self.detail_request_seconds += max(0.0, time.perf_counter() - started)
            if str(response.url) != job.url:
                raise ValueError("detail page redirected away from its exact /job-details URL")
            parsed = parse_detail_page(response.text, job.raw["listing"], str(response.url))
        except Exception as exc:
            job.raw["detail_failure"] = f"{type(exc).__name__}: {exc}"
            raise
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "publicis_sapient.job_details.react_props.sections"
        job.description_source_url = job.url
        job.salary = parsed["salary"]["text"] if parsed["salary"] else None
        job.qualifications = parsed["qualification_html"]
        job.raw["detail"] = {
            **parsed,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "status": response.status_code,
            "request_seconds": max(0.0, time.perf_counter() - started),
        }
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_api": LISTING_API,
            "listing_authority": (
                "Publicis Sapient's own careers domain. /job-search is its React job listing; "
                "its bundle calls the same-origin /bin/ps-redesign/careersJobsearch endpoint."
            ),
            "listing_membership_request": {
                "method": "GET",
                "path": "/bin/ps-redesign/careersJobsearch",
                "query_shape": {
                    "searchType": SEARCH_TYPE,
                    "lang": "en",
                    "facetFields": FACET_FIELDS_PARAM,
                    "q": "",
                    "start": "(page-1)*20",
                    "rows": RESULTS_PER_PAGE,
                },
                "filters_applied": False,
                "geo_filter_omitted": True,
                "scope": "Publicis Sapient's own listing only; no Publicis Groupe-wide board and no iCIMS board enumeration.",
            },
            "detail_method": (
                "Each exact /job-details/<row id>-<slug> page server-renders the employer's "
                "Job Description, Qualifications, Additional Information and Company Description sections "
                "in data-react-props; all non-empty sections are retained."
            ),
            "request_scope": (
                "careers.publicissapient.com listing, its own search bundle, and the exact job-detail URLs "
                "returned by that listing. sapient-publicisgroupe.icims.com is the application destination only."
            ),
            "apply_destination": "https://sapient-publicisgroupe.icims.com (application only; never enumerated)",
        })
        try:
            initial_entry = await self._official_entry()
            initial_listing = await self._listing_page()
            bundle = await self._search_bundle(initial_listing["search_bundle_url"])
            facets = await self._facets()
            initial = await self._api_snapshot()
        except Exception as exc:
            self.result.errors.append(
                f"Publicis Sapient source request failed: {type(exc).__name__}: {exc}"
            )
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        self.result.expected_total = initial["count"]
        self.result.metadata["initial_official_entry"] = initial_entry
        self.result.metadata["initial_listing_page"] = initial_listing
        self.result.metadata["search_bundle"] = bundle
        self.result.metadata["initial_facets"] = facets
        self.result.metadata["initial_api_listing"] = {
            key: value for key, value in initial.items() if key != "rows"
        }
        self.result.errors.extend(initial["issues"])
        self.result.errors.extend(facets["issues"])
        if initial["count"] != facets["count"]:
            self.result.errors.append("publicis_sapient_listing_total_and_facet_count_mismatch")
        if initial_listing["results_per_page"] != RESULTS_PER_PAGE:
            self.result.errors.append("publicis_sapient_listing_page_size_changed")
        if bundle["default_facet_fields"] != FACET_FIELDS:
            self.result.errors.append("publicis_sapient_listing_facet_fields_changed")

        jobs = [self._make_job(row) for row in initial["rows"]]
        hydrated = await self.hydrate(jobs, self._detail)
        for job in hydrated:
            refresh_derived_fields(job)
        failed = [
            {"id": job.job_id, "url": job.url,
             "reason": job.raw.get("detail_failure") or "; ".join(job.warnings) or "description_unverified"}
            for job in hydrated if not job.description_verified
        ]
        self.result.metadata["detail_failures"] = failed
        self.result.metadata["requisition_counts"] = {
            "listing_rows": len(initial["rows"]),
            "unique_requisitions": len({row["jobId"] for row in initial["rows"]}),
            "rows_that_are_extra_location_variants": sum(
                1 for row in initial["rows"] if row["id"] != row["jobId"]
            ),
        }

        final = None
        try:
            final_entry = await self._official_entry()
            final_listing = await self._listing_page()
            final_bundle = await self._search_bundle(final_listing["search_bundle_url"])
            final_facets = await self._facets()
            final = await self._api_snapshot()
        except Exception as exc:
            self.result.errors.append(
                f"Publicis Sapient final source recheck failed: {type(exc).__name__}: {exc}"
            )
            final_entry = final_listing = final_bundle = final_facets = None

        if final is None or final_entry is None:
            final_matches = False
        else:
            changed_fields = {}
            initial_by_id = {row["id"]: row for row in initial["rows"]}
            final_by_id = {row["id"]: row for row in final["rows"]}
            for identity in initial["job_ids"]:
                if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]:
                    changed_fields[identity] = sorted(
                        key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                    )
            same_ids = initial["job_ids"] == final["job_ids"]
            counts_match = (initial["count"], initial["total_pages"]) == (final["count"], final["total_pages"])
            same_listing_page = (
                initial_listing["results_per_page"] == final_listing["results_per_page"]
                and initial_listing["search_bundle_url"] == final_listing["search_bundle_url"]
            )
            same_bundle = bundle["default_facet_fields"] == final_bundle["default_facet_fields"]
            same_facets = facets["count"] == final_facets["count"]
            final_matches = (
                same_ids and counts_match and not changed_fields
                and not final["issues"] and not final_facets["issues"]
                and same_listing_page and same_bundle and same_facets
            )
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_listing_page"] = final_listing
            self.result.metadata["final_search_bundle"] = final_bundle
            self.result.metadata["final_facets"] = final_facets
            self.result.metadata["final_api_listing"] = {
                key: value for key, value in final.items() if key != "rows"
            }
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["job_ids"],
                "final_ids": final["job_ids"],
                "initial_count": initial["count"],
                "final_count": final["count"],
                "initial_pages": initial["total_pages"],
                "final_pages": final["total_pages"],
                "same_ids_and_order": same_ids,
                "all_row_fields_match": not changed_fields,
                "changed_fields_by_id": changed_fields,
                "new_job_ids": [identity for identity in final["job_ids"] if identity not in initial_by_id],
                "removed_job_ids": [identity for identity in initial["job_ids"] if identity not in final_by_id],
                "listing_page_unchanged": same_listing_page,
                "search_bundle_unchanged": same_bundle,
                "facet_count_unchanged": same_facets,
                "complete": final_matches,
            }
            if not same_ids or not counts_match:
                self.result.errors.append(
                    "publicis_sapient_listing_membership_changed_during_collection"
                )
            if changed_fields:
                self.result.errors.append(
                    "publicis_sapient_source_fields_changed_during_collection"
                )
            if not same_listing_page or not same_bundle or not same_facets:
                self.result.errors.append(
                    "publicis_sapient_listing_configuration_changed_during_collection"
                )
            self.result.errors.extend(final["issues"])
            self.result.errors.extend(final_facets["issues"])

        self.result.jobs = hydrated
        self.result.timings["detail_request_seconds_sum"] = self.detail_request_seconds
        self.result.timings["initial_listing_and_facets_seconds"] = (
            initial_entry["seconds"] + initial_listing["seconds"] + bundle["seconds"]
            + facets["seconds"] + initial["request_seconds"]
        )
        self.result.timings["final_membership_recheck_seconds"] = (
            (final_entry["seconds"] + final_listing["seconds"] + final_bundle["seconds"]
             + final_facets["seconds"] + final["request_seconds"])
            if final is not None and final_entry is not None and final_listing is not None
            and final_bundle is not None and final_facets is not None else 0.0
        )
        self.result.metadata["job_ids"] = [job.job_id for job in hydrated]
        self.result.metadata["current_native_membership_reconciled"] = bool(
            final_matches and len(hydrated) == initial["count"]
        )
        self.result.listing_complete = bool(
            final_matches and len(hydrated) == initial["count"]
            and len(hydrated) == facets["count"]
        )
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result
