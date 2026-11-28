"""Schlumberger/SLB's own careers listing and native job-description pages.

Membership comes from the official https://careers.slb.com/job-listing Coveo
Atomic search that the company publishes (public organisation id, public search
token, searchHub and `@source=="ATS_Jobs_Source - Prod"`).  Details are the
company's own server-rendered `/jobdescription.aspx?id=...` pages.  The apply
buttons on those pages may point at ChampionX Workday, SLB's SuccessFactors
route, or a generic corporate login; they are retained verbatim and never
enumerated.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from html import unescape
import re
from urllib.parse import (
    parse_qsl,
    quote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://careers.slb.com/"
LISTING_URL = "https://careers.slb.com/job-listing"
BASE_HOST = "careers.slb.com"
DETAIL_PATH = "/jobdescription.aspx"
EXPECTED_ORGANIZATION_ID = "schlumbergerproduction0cs2zrh7"
EXPECTED_SEARCH_HUB = "CoveoJobsHub"
EXPECTED_PIPELINE = "ATSJobsPipeline"
EXPECTED_SOURCE = "ATS_Jobs_Source - Prod"
SEARCH_API = f"https://{EXPECTED_ORGANIZATION_ID}.org.coveo.com/rest/search/v2"
PAGE_SIZE = 100
LISTING_FIELDS = [
    "title", "date", "country", "city", "category", "jobposteddate",
    "hierarchicalcategory", "jobexperiencelevel",
]
APPLY_BLOCK = "button-block"
DESCRIPTION_HEADINGS = {
    "responsibilities": re.compile(
        r"^(?:key |your |primary |main )?(?:responsibilities|duties|what you(?:'|’)ll do|"
        r"what you will do|the role|role description)\b", re.I),
    "qualifications": re.compile(
        r"^(?:minimum |preferred |basic |desired )?(?:qualifications|requirements|"
        r"skills(?: and experience)?|experience(?: required)?|education(?: required)?)\b", re.I),
    "salary": re.compile(r"^(?:salary|compensation|pay range|pay|remuneration)\b", re.I),
    "education": re.compile(r"^(?:education|academic background|degree)\b", re.I),
}
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?|pptx?)(?:[?#]|$)", re.I)
BOILERPLATE_MARKER = re.compile(
    r"(?:About Us|Our Commitment|Diversity and Inclusion|equal employment opportunity|"
    r"SLB is an equal|SLB offers equal|EQUAL EMPLOYMENT OPPORTUNITY|"
    r"We believe the best teams|We are committed to fair and equal|supplemental information)",
    re.I)
BOUNDARY_HEADING = re.compile(
    r"^(?:about us|about slb|our commitment|diversity and inclusion|equal employment|"
    r"slb is an equal|we believe the best teams|we are committed to fair and equal|"
    r"legal|disclaimer|important note|thank you for your interest)\b", re.I)
DATE_FORMAT = "%m/%d/%Y %I:%M:%S %p"
TEMPLATE_DATE = "1/1/0001"


def compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


PLACEHOLDER_DESCRIPTIONS = {"na", "n/a", "na na", "n/a n/a", "na, na"}


def _publisher_placeholder_text(body_text: str | None) -> str:
    text = compact(body_text)
    marker = BOILERPLATE_MARKER.search(text)
    if marker:
        text = compact(text[:marker.start()])
    return text


def is_publisher_placeholder(body_text: str | None) -> bool:
    """True when a published body is only a placeholder such as 'NA NA'."""
    text = _publisher_placeholder_text(body_text)
    return bool(text) and text.casefold() in PLACEHOLDER_DESCRIPTIONS


def parse_posted_date(value: str | None) -> str | None:
    """Parse the page's JSON-LD datePosted; the 0001 template value is unknown."""
    raw = compact(value)
    if not raw or raw.startswith(TEMPLATE_DATE):
        return None
    try:
        stamp = datetime.strptime(raw, DATE_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"SLB datePosted has an unexpected format: {value!r}") from exc
    return stamp.isoformat()


def iso_from_ms(value) -> str | None:
    if not isinstance(value, int) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def normalise_listing_url(uri: str) -> dict:
    """Return the exact listing row URL in the form the rendered page uses.

    Coveo emits raw Unicode and spaces inside the `location` query value.  The
    browser percent-encodes those.  Keeping the semantic query values and
    percent-encoding them avoids both an invalid request URL and the non-ASCII
    `DelfiBackPage` cookie echo that otherwise breaks httpx's cookie header.
    """
    parts = urlsplit(uri or "")
    if parts.scheme != "https" or parts.netloc != BASE_HOST:
        raise ValueError(f"SLB listing row left the company careers host: {uri!r}")
    if parts.path.rstrip("/").lower() != DETAIL_PATH:
        raise ValueError(f"SLB listing row is not a native job-description page: {uri!r}")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = {}
    for key, value in pairs:
        query.setdefault(key, value)
    identity = compact(query.get("id"))
    if not identity:
        raise ValueError(f"SLB listing row has no native id: {uri!r}")
    normalised_query = urlencode(pairs, quote_via=quote, safe="|")
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, normalised_query, ""))
    return {
        "url": url,
        "id": identity,
        "location_query": query.get("location"),
        "query_pairs": pairs,
    }


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != BASE_HOST or parts.path.rstrip("/") != "":
        raise ValueError("SLB official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = compact(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "SLB" not in title:
        raise ValueError("SLB official careers entry title changed")
    links = {urljoin(response_url, a.get("href", "")) for a in soup.select("a[href]")}
    if LISTING_URL not in links:
        raise ValueError("SLB official careers entry no longer links to its job listing")
    return {
        "url": response_url,
        "title": title,
        "listing_link": LISTING_URL,
        "find_a_job_links": sorted(
            urljoin(response_url, a.get("href", "")) for a in soup.select("a[href]")
            if compact(a.get_text(" ", strip=True)).casefold() == "find a job"
        ),
    }


def parse_listing_config(html: str, response_url: str = LISTING_URL) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != BASE_HOST or parts.path.rstrip("/") != "/job-listing":
        raise ValueError("SLB listing page redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = compact(soup.title.get_text(" ", strip=True) if soup.title else "")
    interface = soup.select("atomic-search-interface")
    if len(interface) != 1:
        raise ValueError("SLB listing no longer exposes exactly one Atomic search interface")
    interface = interface[0]

    def hidden(selector):
        node = soup.select_one(selector)
        value = compact(node.get("value")) if node else ""
        if not value:
            raise ValueError(f"SLB listing configuration is missing {selector}")
        return value

    config = {
        "organization_id": hidden("#organizationId"),
        "access_token": hidden("#accessToken"),
        "search_hub": hidden("#searchHub"),
        "source": hidden("#searchsource"),
        "pipeline": compact(interface.get("pipeline")),
        "title": title,
        "results_per_page_initial": compact(
            (soup.select_one("atomic-results-per-page").get("initial-choice")
             if soup.select_one("atomic-results-per-page") else "")
        ),
        "facets": [
            {
                "field": compact(node.get("field")),
                "label": compact(node.get("label")),
                "number_of_values": compact(node.get("number-of-values")),
            }
            for node in soup.select("atomic-facet")
        ],
        "sort_expressions": [
            {"label": compact(node.get("label")), "expression": compact(node.get("expression"))}
            for node in soup.select("atomic-sort-expression")
        ],
        "pagination_control": bool(soup.select_one("atomic-pager")),
    }
    if (config["organization_id"] != EXPECTED_ORGANIZATION_ID
            or config["search_hub"] != EXPECTED_SEARCH_HUB
            or config["pipeline"] != EXPECTED_PIPELINE
            or config["source"] != EXPECTED_SOURCE):
        raise ValueError("SLB listing public Coveo identity/pipeline/source changed")
    if not config["pagination_control"]:
        raise ValueError("SLB listing pagination control is missing")
    return config


def search_body(config: dict, first_result: int, number_of_results: int = PAGE_SIZE) -> dict:
    return {
        "q": "",
        "searchHub": config["search_hub"],
        "pipeline": config["pipeline"],
        "cq": '@source==$"' + config["source"] + '"',
        "numberOfResults": number_of_results,
        "firstResult": first_result,
        "locale": "en-US",
        "tab": "default",
        "fieldsToInclude": LISTING_FIELDS,
    }


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [compact(str(item)) for item in value if compact(str(item))]
    return [compact(str(value))] if compact(str(value)) else []


def parse_search_page(value, *, requested_first: int, page_size: int = PAGE_SIZE) -> dict:
    if not isinstance(value, dict):
        raise ValueError("SLB listing API response is not an object")
    if value.get("error") or value.get("errors"):
        raise ValueError(f"SLB listing API returned an error: {value.get('error') or value.get('errors')}")
    total = value.get("totalCountFiltered", value.get("totalCount"))
    if not isinstance(total, int) or total < 0:
        raise ValueError("SLB listing API response has no valid totalCount")
    results = value.get("results")
    if not isinstance(results, list):
        raise ValueError("SLB listing API response has no results array")
    expected_rows = max(0, min(page_size, total - requested_first))
    if len(results) != expected_rows:
        raise ValueError(
            f"SLB listing page at {requested_first} returned {len(results)} rows, expected {expected_rows} of {total}"
        )
    rows, seen, issues = [], set(), []
    for result in results:
        if not isinstance(result, dict):
            issues.append("SLB listing row is not an object")
            continue
        title = compact(result.get("title"))
        if not title:
            issues.append("SLB listing row is missing a title")
            continue
        try:
            identity = normalise_listing_url(result.get("uri"))
        except ValueError as exc:
            issues.append(str(exc))
            continue
        if identity["url"] in seen:
            issues.append(f"SLB listing contains a duplicate exact job row: {identity['url']}")
            continue
        raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
        source = compact(raw.get("source") or raw.get("syssource"))
        if source and source != EXPECTED_SOURCE:
            issues.append(f"SLB listing row source changed: {identity['id']} -> {source!r}")
            continue
        posted = raw.get("jobposteddate")
        if posted is not None and (not isinstance(posted, int) or posted <= 0):
            issues.append(f"SLB listing row has an invalid jobposteddate: {identity['id']}")
            posted = None
        rows.append({
            "url": identity["url"],
            "listing_uri": result.get("uri"),
            "id": identity["id"],
            "location_query": identity["location_query"],
            "query_pairs": identity["query_pairs"],
            "title": title,
            "country": _as_list(raw.get("country")),
            "city": compact(raw.get("city")) or None,
            "category": _as_list(raw.get("category")),
            "hierarchicalcategory": _as_list(raw.get("hierarchicalcategory")),
            "jobexperiencelevel": _as_list(raw.get("jobexperiencelevel")),
            "jobposteddate": posted,
            "coveo_date": raw.get("date"),
            "permament_id": raw.get("permanentid"),
            "raw": raw,
            "result_fields": {
                key: result.get(key) for key in
                ("uri", "clickUri", "uniqueId", "title", "excerpt", "firstSentences", "score", "raw")
            },
        })
        seen.add(identity["url"])
    return {
        "total": total,
        "requested_first": requested_first,
        "page_size": page_size,
        "rows": rows,
        "issues": issues,
    }


def _hero_fields(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    hero = soup.select_one("section.hero-primary")
    if hero is None:
        raise ValueError("SLB native job page has no hero section")
    location = category = None
    for heading in hero.select("h2"):
        text = compact(heading.get_text(" ", strip=True))
        if not text:
            continue
        if heading.select_one("span.icon-font-location-base"):
            location = text
        elif heading.select_one("span.icon-font-account-circle-blue-career"):
            category = text
        elif location is None and category is not None:
            location = text
        elif category is None:
            category = text
    return location, category


def _description_html(soup: BeautifulSoup) -> str:
    section = soup.select_one("section.job-description-redesign")
    if section is None:
        raise ValueError("SLB native job page has no job-description-redesign section")
    chunks = []
    for child in section.children:
        if not getattr(child, "name", None):
            continue
        classes = child.get("class") or []
        if APPLY_BLOCK in classes or child.name in {"script", "style", "noscript"}:
            continue
        chunks.append(str(child))
    return "".join(chunks)


def _bold_only(node, text: str) -> bool:
    """True when a paragraph's entire visible text is a bold heading label."""
    bold = node.find_all(["strong", "b"])
    if not bold:
        return False
    bold_text = compact(" ".join(tag.get_text(" ", strip=True) for tag in bold))
    return bool(bold_text) and bold_text == text and len(text) <= 90


def _text_sections(body_html: str) -> dict:
    soup = BeautifulSoup(body_html or "", "lxml")
    container = soup.body if soup.body is not None else soup
    sections, current = [], None
    for child in container.children:
        if not getattr(child, "name", None):
            continue
        if child.name in {"script", "style", "noscript"}:
            continue
        text = compact(child.get_text(" ", strip=True))
        heading = None
        if child.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            heading = text or None
        elif child.name == "p" and text:
            if BOUNDARY_HEADING.match(text):
                heading = text
            elif len(text) <= 120:
                if text.endswith(":") or _bold_only(child, text):
                    heading = text
                else:
                    for pattern in DESCRIPTION_HEADINGS.values():
                        if pattern.match(text):
                            heading = text
                            break
        if heading:
            current = {"heading": heading, "html": str(child), "chars": len(text)}
            sections.append(current)
        elif current is not None:
            current["html"] += str(child)
            current["chars"] += len(text)

    def clean_section(html):
        text = text_html(html)
        marker = BOILERPLATE_MARKER.search(text)
        if marker:
            text = text[:marker.start()].rstrip(" .,;:-")
        return text

    def collect(kind):
        values = []
        for section in sections:
            if DESCRIPTION_HEADINGS[kind].match(section["heading"]):
                cleaned = clean_section(section["html"])
                if cleaned:
                    values.append(cleaned)
        return "\n".join(values) if values else None

    return {
        "sections": sections,
        "responsibilities": collect("responsibilities"),
        "qualifications": collect("qualifications"),
        "salary": collect("salary"),
        "education": collect("education"),
    }


def _jsonld_fields(html: str) -> dict:
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I,
    )
    job_blocks = [block for block in blocks if '"JobPosting"' in block]
    if len(job_blocks) != 1:
        raise ValueError("SLB native job page does not expose exactly one JobPosting JSON-LD block")
    block = job_blocks[0]

    def one(pattern):
        found = re.findall(pattern, block, re.S)
        return compact(found[0]) if found else None

    employment_type = one(r'"employmentType"\s*:\s*"([^"\n]*)"')
    if employment_type in {None, "", ",", '"'}:
        employment_type = None
    return {
        "block_length": len(block),
        "title": one(r'"title"\s*:\s*"([^"]*)"'),
        "date_posted_raw": one(r'"datePosted"\s*:\s*"([^"]*)"'),
        "description_is_null": bool(re.search(r'"description"\s*:\s*null', block)),
        "employment_type_raw": employment_type,
        "apply_url": one(r'"applyUrl"\s*:\s*"([^"]*)"'),
        "page_url": one(r'"pageUrl"\s*:\s*"([^"]*)"'),
        "employer": one(r'"hiringOrganization"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]*)"'),
        "job_location": one(r'"jobLocation"\s*:\s*"([^"]*)"'),
        "occupational_category": one(r'"occupationalCategory"\s*:\s*"([^"]*)"'),
    }


def parse_job_detail(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.find("h1")
    title = compact(title_node.get_text(" ", strip=True) if title_node else "")
    if not title:
        raise ValueError("SLB native job page has no h1 title")
    location, category = _hero_fields(soup)
    body_html = _description_html(soup)
    body_text = text_html(body_html)
    section_data = _text_sections(body_html)
    apply_urls = []
    for anchor in soup.select("a[href]"):
        if compact(anchor.get_text(" ", strip=True)).casefold() == "apply now":
            href = compact(anchor.get("href"))
            if href and href not in apply_urls:
                apply_urls.append(href)
    jsonld = _jsonld_fields(html)
    attachments = sorted({
        urljoin(page_url, href) for href in re.findall(r'href=["\']([^"\']+)["\']', body_html)
        if ATTACHMENT_SUFFIX.search(href)
    })
    return {
        "title": title,
        "location": location,
        "category": category,
        "description_html": body_html,
        "description_text": body_text,
        "description_chars": len(body_text),
        "description_sha256": sha256(body_text.encode()).hexdigest() if body_text else None,
        "sections": section_data["sections"],
        "responsibilities": section_data["responsibilities"],
        "qualifications": section_data["qualifications"],
        "salary_section": section_data["salary"],
        "education": section_data["education"],
        "apply_urls": apply_urls,
        "attachments": attachments,
        "jsonld": jsonld,
    }


def apply_identifiers(urls: list[str]) -> dict:
    identifiers = {}
    for url in urls:
        workday = re.search(r"_(R\d+)(?:/|$)", url)
        if workday:
            identifiers.setdefault("workday_requisition", workday.group(1))
        successfactors = re.search(r"[?&]pid=(\d+)", url)
        if successfactors:
            identifiers.setdefault("successfactors_pid", successfactors.group(1))
    return identifiers


class Schlumberger(Adapter):
    """The company's own SLB careers listing + native job pages."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config: dict | None = None
        self.stop_reason: str | None = None
        self.listing_passes: list[dict] = []

    async def _request(self, method: str, url: str, **kwargs):
        try:
            return await self.http.request(method, url, **kwargs)
        finally:
            # SLB echoes the decoded request URL in DelfiBackPage; raw Unicode
            # location values would otherwise poison every later Cookie header.
            self.http.client.cookies.clear()

    async def _listing_config(self) -> dict:
        response = await self._request("GET", LISTING_URL)
        if str(response.url).split("#", 1)[0] != LISTING_URL:
            raise ValueError("SLB listing page redirected away from its inspected URL")
        self.result.metadata["listing_page_sha256"] = sha256(response.content).hexdigest()
        return parse_listing_config(response.text, str(response.url))

    async def _search_page(self, first: int, *, config: dict | None = None) -> dict:
        config = config or self.config
        assert config is not None
        response = await self._request(
            "POST", SEARCH_API,
            params={"organizationId": config["organization_id"]},
            json=search_body(config, first),
            headers={
                "Authorization": f"Bearer {config['access_token']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            value = response.json()
        except ValueError as exc:
            raise ValueError("SLB listing API returned non-JSON content") from exc
        return parse_search_page(value, requested_first=first, page_size=PAGE_SIZE)

    async def _collect_listing(self, config: dict, label: str) -> dict:
        started = __import__("time").perf_counter()
        rows, order, totals, pages = [], [], [], []
        first, total = 0, None
        while total is None or first < total:
            page = await self._search_page(first, config=config)
            if total is None:
                total = page["total"]
            elif page["total"] != total:
                self.result.errors.append(
                    f"listing_total_changed_during_{label}: {total} -> {page['total']}"
                )
                total = page["total"]
            totals.append(page["total"])
            pages.append({"first": first, "rows": len(page["rows"]), "total": page["total"]})
            for row in page["rows"]:
                if row["url"] in order:
                    self.result.errors.append(f"listing_duplicate_row_across_pages: {row['url']}")
                    continue
                order.append(row["url"])
                rows.append(row)
            first += PAGE_SIZE
            if not page["rows"]:
                break
        self.listing_passes.append({
            "label": label,
            "total": total,
            "rows": len(rows),
            "pages": pages,
            "seconds": round(__import__("time").perf_counter() - started, 6),
        })
        return {"total": total, "rows": rows, "urls": order, "pages": pages}

    async def detail(self, job):
        if self.stop_reason:
            job.description_unavailable = True
            job.description_method = "schlumberger.native_detail.skipped_after_429"
            job.description_source_url = job.url
            job.warnings.append("detail_skipped_after_429")
            return job
        response = await self._request("GET", job.url)
        identity = normalise_listing_url(str(response.url))
        if identity["id"] != job.job_id:
            raise ValueError(
                f"SLB native detail id changed: listing={job.job_id!r} final={identity['id']!r}"
            )
        parsed = parse_job_detail(response.text, str(response.url))
        listing = job.raw["listing"]
        listing_title = job.title
        detail_title = parsed["title"]
        if compact(listing_title) != compact(detail_title):
            job.warnings.append("listing_detail_title_conflict")
            job.raw["listing_title"] = listing_title
        job.title = detail_title

        detail_location = parsed["location"]
        listing_locations = job.locations
        if listing_locations and detail_location:
            listing_display = compact((listing_locations[0] or {}).get("location"))
            if listing_display and listing_display != detail_location:
                job.warnings.append("listing_detail_location_conflict")
                job.raw["listing_location"] = listing_display
        elif not listing_locations and detail_location:
            job.locations = [{"location": detail_location}]
        elif not job.locations:
            job.warnings.append("location_not_published")

        if parsed["category"]:
            job.raw["detail_category"] = parsed["category"]
        if not job.department:
            job.department = parsed["category"]

        body_text = parsed["description_text"]
        placeholder = bool(body_text) and is_publisher_placeholder(body_text)
        if body_text and not placeholder:
            job.description_html = parsed["description_html"]
            job.description_text = body_text
            job.description_method = "schlumberger.native_detail.job-description-redesign"
            job.description_source_url = job.url
            job.description_unavailable = False
            job.raw["posting_kind"] = "posted_role"
        elif placeholder:
            job.description_html = parsed["description_html"]
            job.description_text = body_text
            job.description_method = "schlumberger.native_detail.publisher_placeholder_body"
            job.description_source_url = job.url
            job.description_unavailable = False
            job.warnings.extend(["publisher_placeholder_description", "description_not_full_field"])
            job.raw["posting_kind"] = "publisher_placeholder_description"
        else:
            job.description_html = ""
            job.description_text = ""
            job.description_method = "schlumberger.native_detail.publisher_empty_body"
            job.description_source_url = job.url
            job.description_unavailable = True
            program_like = bool(
                re.search(r"^(?:early careers|internship|student intern|technical sales student)", job.title, re.I)
            ) or not job.date_posted
            if program_like:
                job.warnings.extend(["publisher_empty_description", "publisher_empty_program_template"])
                job.raw["posting_kind"] = "publisher_empty_program_template"
            else:
                job.warnings.extend(["publisher_empty_description", "publisher_empty_posting"])
                job.raw["posting_kind"] = "publisher_empty_posting"

        detail_date_raw = parsed["jsonld"]["date_posted_raw"]
        detail_date = parse_posted_date(detail_date_raw)
        listing_date_raw = listing.get("jobposteddate")
        listing_date = iso_from_ms(listing_date_raw)
        if detail_date and listing_date and detail_date != listing_date:
            job.warnings.append("listing_detail_date_conflict")
        job.date_posted = detail_date or listing_date
        if body_text and job.raw.get("posting_kind") not in {"publisher_placeholder_description"}:
            job.raw["posting_kind"] = "posted_role" if job.date_posted else "undated_evergreen_intake_with_body"
        job.date_posted_raw = {
            "jsonld_datePosted": detail_date_raw,
            "coveo_jobposteddate": listing_date_raw,
            "source": "jsonld_datePosted" if detail_date else ("coveo_jobposteddate" if listing_date else None),
        }
        job.apply_url = parsed["apply_urls"][0] if parsed["apply_urls"] else None
        if not job.apply_url:
            job.warnings.append("apply_url_missing")
        elif "slb.com/login" in job.apply_url:
            job.warnings.append("apply_route_is_generic_login")
        job.employer = parsed["jsonld"]["employer"] or "SLB"
        job.employment_type = None
        job.valid_through = None
        job.date_updated = None
        job.salary = parsed["salary_section"]
        job.qualifications = parsed["qualifications"]
        job.responsibilities = parsed["responsibilities"]
        job.education = parsed["education"]
        job.language = "en"
        job.raw["apply_identifiers"] = apply_identifiers(parsed["apply_urls"])
        job.raw["detail"] = {
            "url": job.url,
            "final_url": str(response.url),
            "title": detail_title,
            "location": detail_location,
            "category": parsed["category"],
            "description_chars": parsed["description_chars"],
            "description_sha256": parsed["description_sha256"],
            "description_sections": [
                {"heading": section["heading"], "chars": section["chars"]} for section in parsed["sections"]
            ],
            "apply_urls": parsed["apply_urls"],
            "attachments": parsed["attachments"],
            "jsonld": parsed["jsonld"],
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
        }
        return job

    async def run(self):
        import time

        if not self.page_guard():
            return self.result
        overall_started = self.result._started_clock
        entry_started = time.perf_counter()
        entry = await self._request("GET", OFFICIAL_ENTRY)
        entry_info = parse_official_entry(entry.text, str(entry.url))
        entry_seconds = time.perf_counter() - entry_started
        self.result.metadata["official_entry"] = entry_info

        listing_started = time.perf_counter()
        config = await self._listing_config()
        self.config = config
        first_pass = await self._collect_listing(config, "first_pass")
        listing_seconds = time.perf_counter() - listing_started
        total = first_pass["total"]
        if total is None or total <= 0 or first_pass["total"] != len(first_pass["rows"]):
            self.result.errors.append(
                f"listing_membership_not_closed: total={first_pass['total']} rows={len(first_pass['rows'])}"
            )

        for index, row in enumerate(first_pass["rows"]):
            locations = []
            country = row["country"][0] if row["country"] else None
            city = row["city"]
            if country and city:
                locations = [{"country": country, "city": city, "location": f"{country}, {city}"}]
            elif country:
                locations = [{"country": country, "location": country}]
            elif city:
                locations = [{"city": city, "location": city}]
            self.add(self.job(
                {"listing": row, "listing_index": index, "listing_uri_raw": row["listing_uri"]},
                title=row["title"],
                job_id=row["id"],
                url=row["url"],
                apply_url=None,
                locations=locations,
                department=", ".join(row["hierarchicalcategory"]) or None,
                experience=", ".join(row["jobexperiencelevel"]) or None,
                date_posted=iso_from_ms(row["jobposteddate"]),
                date_posted_raw={"coveo_jobposteddate": row["jobposteddate"]},
            ))
        self.result.expected_total = total

        detail_started = time.perf_counter()
        self.result.jobs = await self.hydrate(self.result.jobs, self.detail)
        detail_seconds = time.perf_counter() - detail_started

        final_started = time.perf_counter()
        final_config = await self._listing_config()
        final_pass = await self._collect_listing(final_config, "final_pass")
        final_seconds = time.perf_counter() - final_started
        initial_urls = first_pass["urls"]
        final_urls = final_pass["urls"]
        added = [url for url in final_urls if url not in set(initial_urls)]
        removed = [url for url in initial_urls if url not in set(final_urls)]
        order_equal = initial_urls == final_urls
        if final_pass["total"] != total:
            self.result.errors.append(
                f"listing_total_changed_before_final_proof: {total} -> {final_pass['total']}"
            )
        if added or removed:
            self.result.errors.append(
                f"listing_membership_changed_before_final_proof: added={len(added)} removed={len(removed)}"
            )
        if not order_equal and not added and not removed:
            self.result.errors.append("listing_order_changed_before_final_proof")
        self.result.pages = max(1, len(first_pass["pages"]))
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["final_proof_seconds"] = round(final_seconds, 6)
        self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)

        bodies = [job.description_text for job in self.result.jobs]
        empty_jobs = [job for job in self.result.jobs if not job.description_text]
        title_conflicts = [
            {"id": job.job_id, "url": job.url,
             "listing": job.raw.get("listing_title"), "detail": job.title}
            for job in self.result.jobs if "listing_detail_title_conflict" in job.warnings
        ]
        apply_patterns = {"workday": 0, "successfactors": 0, "generic_login": 0, "other": 0, "missing": 0}
        for job in self.result.jobs:
            apply = job.apply_url or ""
            if not apply:
                apply_patterns["missing"] += 1
            elif "myworkdayjobs.com" in apply:
                apply_patterns["workday"] += 1
            elif "apply.slb.com" in apply:
                apply_patterns["successfactors"] += 1
            elif "slb.com/login" in apply:
                apply_patterns["generic_login"] += 1
            else:
                apply_patterns["other"] += 1
        self.result.listing_complete = (
            not self.result.errors
            and total == len(first_pass["rows"])
            and final_pass["total"] == total
            and len(final_pass["rows"]) == total
            and order_equal
        )
        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "organization_id": config["organization_id"],
            "search_hub": config["search_hub"],
            "pipeline": config["pipeline"],
            "source": config["source"],
            "access_token_sha256": sha256(config["access_token"].encode()).hexdigest(),
            "listing_total_first_pass": total,
            "listing_total_final_pass": final_pass["total"],
            "listing_rows_first_pass": len(first_pass["rows"]),
            "listing_rows_final_pass": len(final_pass["rows"]),
            "listing_urls_first_pass_sha256": sha256("\n".join(initial_urls).encode()).hexdigest(),
            "listing_urls_final_pass_sha256": sha256("\n".join(final_urls).encode()).hexdigest(),
            "listing_pages": len(first_pass["pages"]),
            "listing_page_rows": first_pass["pages"],
            "distinct_base_ids": len({job.job_id for job in self.result.jobs}),
            "location_variant_rows": len(self.result.jobs) - len({job.job_id for job in self.result.jobs}),
            "rows_with_posting_date": sum(1 for job in self.result.jobs if job.date_posted),
            "rows_without_posting_date": sum(1 for job in self.result.jobs if not job.date_posted),
            "rows_with_description_body": sum(1 for job in self.result.jobs if job.description_text),
            "rows_publisher_empty": len(empty_jobs),
            "publisher_empty_ids": sorted({job.job_id for job in empty_jobs}),
            "description_chars_total": sum(len(body) for body in bodies),
            "title_conflict_count": len(title_conflicts),
            "title_conflicts": title_conflicts[:50],
            "apply_patterns": apply_patterns,
            "listing_identical_before_after_details": order_equal,
            "listing_added_urls": added[:20],
            "listing_removed_urls": removed[:20],
            "stop_reason": self.stop_reason,
            "timings": {
                "official_entry_seconds": round(entry_seconds, 6),
                "listing_seconds": round(listing_seconds, 6),
                "detail_seconds": round(detail_seconds, 6),
                "final_proof_seconds": round(final_seconds, 6),
                "run_seconds": round(time.perf_counter() - overall_started, 6),
            },
        })
        return self.result
