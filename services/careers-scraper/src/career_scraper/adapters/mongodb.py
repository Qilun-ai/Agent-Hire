"""MongoDB's own careers search page, its published Greenhouse data, and its
company-hosted job detail routes.

Membership comes only from the 375 public rows the company's own
``/company/careers/see-jobs`` page publishes in its ``GreenhouseV2`` component
(the same rows its client-side 10-per-page widget paginates). That component's
declared Greenhouse feed is fetched only as a same-moment cross-check. Full
descriptions come from the exact company detail route (``/careers/jobs/<id>``)
that every listed card redirects to; the embedded Greenhouse application form
is retained as the apply URL, never submitted.
"""
from __future__ import annotations

from hashlib import sha256
from html import unescape
import json
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://www.mongodb.com/company/careers"
LISTING_URL = "https://www.mongodb.com/company/careers/see-jobs"
GREENHOUSE_FEED = "https://api.greenhouse.io/v1/boards/mongodb/jobs?content=true"
APPLY_FORM = "https://job-boards.greenhouse.io/embed/job_app?for=mongodb&token={job_id}"
DETAIL_URL = "https://www.mongodb.com/careers/jobs/{job_id}"
DETAIL_PATH = re.compile(r"^/careers/jobs/(\d+)$")
LISTING_CARD_HREF = re.compile(r"^/careers/job/?$")
ITEMS_PER_PAGE = 10
EMPLOYER_NAME = "MongoDB"
FEED_COMPONENT = "GreenhouseV2"
HEADING_HINTS = re.compile(
    r"^(?:what|about|why|responsibilit|qualif|requirement|preferred|benefit|"
    r"the role|the team|compensation|equal employment|req(?:uisition)? id|"
    r"our |who we are|we are|position|job description|overview)",
    re.I,
)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def decode_embedded_html(value: str | None) -> str:
    """Decode Greenhouse's HTML-escaped content exactly once.

    The board rows served inside MongoDB's page escape the whole HTML document
    (including documents that end in a table rather than a paragraph). Decode
    only when the value has escaped angle brackets and no literal tag opener,
    so normal detail-page HTML and technical text such as ``a < b`` are kept.
    """
    value = value or ""
    if "&lt;" in value and not re.search(r"<[A-Za-z!/]", value):
        value = unescape(value)
    return value


def normalized_body_text(html: str | None) -> str:
    """Visible description text with every run of whitespace collapsed."""
    return re.sub(r"\s+", " ", text_html(decode_embedded_html(html))).strip()


def compact_body_text(html: str | None) -> str:
    """Whitespace-free description text; used only for equality checks."""
    return re.sub(r"\s+", "", text_html(decode_embedded_html(html)))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def server_data(html: str) -> dict:
    """Decode the page's inline ``window.__serverData`` JSON document."""
    soup = BeautifulSoup(html or "", "lxml")
    node = soup.select_one("script#server-data")
    if node is None:
        raise ValueError("MongoDB page is missing its inline server-data document")
    raw = (node.string or node.get_text() or "").strip()
    match = re.match(r"^window\.__serverData\s*=\s*", raw)
    if match is None:
        raise ValueError("MongoDB server-data document no longer starts with window.__serverData")
    raw = raw[match.end():].strip()
    if raw.endswith(";"):
        raw = raw[:-1].strip()
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"MongoDB server-data document is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("MongoDB server-data document is not an object")
    return value


def greenhouse_component(data: dict) -> dict:
    components = data.get("components")
    if not isinstance(components, list):
        raise ValueError("MongoDB server-data document has no component list")
    found = [item for item in components
             if isinstance(item, dict) and item.get("key") == FEED_COMPONENT]
    if len(found) != 1:
        raise ValueError(
            f"MongoDB page no longer exposes exactly one {FEED_COMPONENT} job component")
    return found[0]


def _department_job_ids(nodes) -> list[int]:
    identities: list[int] = []

    def walk(items):
        if not isinstance(items, list):
            raise ValueError("MongoDB department node is not a list")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("MongoDB department node is not an object")
            for job in item.get("jobs") or []:
                identity = job.get("id") if isinstance(job, dict) else None
                if not isinstance(identity, int):
                    raise ValueError("MongoDB department tree contains a job without a numeric id")
                identities.append(identity)
            if item.get("children"):
                walk(item["children"])

    walk(nodes)
    return identities


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.mongodb.com"
            or parsed.path.rstrip("/") != "/company/careers"):
        raise ValueError("MongoDB official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "MongoDB Careers" not in title:
        raise ValueError("MongoDB official careers entry title changed")
    links = []
    for anchor in soup.select("a[href]"):
        if normalized_title(anchor.get_text(" ", strip=True)) == "See Open Jobs":
            links.append(urljoin(response_url, anchor.get("href", "")))
    if not links:
        raise ValueError("MongoDB official careers entry no longer links to See Open Jobs")
    if any(link != LISTING_URL for link in links):
        raise ValueError("MongoDB See Open Jobs link changed away from the inspected search page")
    return {"url": response_url, "title": title, "see_jobs_urls": links}


def _validate_job_row(row: dict, index: int) -> None:
    identity = row.get("id")
    if not isinstance(identity, int) or identity <= 0:
        raise ValueError(f"MongoDB listing row {index} has no numeric job id")
    if normalized_title(row.get("title")) == "":
        raise ValueError(f"MongoDB listing row {identity} has no title")
    if row.get("company_name") != EMPLOYER_NAME:
        raise ValueError(f"MongoDB listing row {identity} is not scoped to MongoDB")
    location = row.get("location")
    if not isinstance(location, dict) or normalized_title(location.get("name")) == "":
        raise ValueError(f"MongoDB listing row {identity} has no location name")
    absolute = urlsplit(row.get("absolute_url") or "")
    if (absolute.scheme != "https" or absolute.netloc != "www.mongodb.com"
            or absolute.path not in ("/careers/job/", "/careers/job")
            or parse_qs(absolute.query) != {"gh_jid": [str(identity)]}
            or absolute.fragment):
        raise ValueError(f"MongoDB listing row {identity} has an unexpected job link")
    if not isinstance(row.get("content"), str) or len(normalized_body_text(row["content"])) < 100:
        raise ValueError(f"MongoDB listing row {identity} has no full description content")
    for field in ("metadata", "departments", "offices"):
        if not isinstance(row.get(field), list):
            raise ValueError(f"MongoDB listing row {identity} is missing its {field} list")
    for field in ("first_published", "updated_at", "application_deadline"):
        # Unknown dates stay null; a present value must be the source string.
        if row.get(field) is not None and not isinstance(row.get(field), str):
            raise ValueError(f"MongoDB listing row {identity} has an invalid {field} value")


def parse_listing_page(html: str, response_url: str = LISTING_URL) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.mongodb.com"
            or parsed.path != "/company/careers/see-jobs" or parsed.query):
        raise ValueError("MongoDB job listing redirected away from its inspected search route")
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Careers - All Teams | MongoDB" not in title:
        raise ValueError("MongoDB search-jobs page title changed")
    data = server_data(html)
    component = greenhouse_component(data)
    props = component.get("props") or {}
    if props.get("feed") != GREENHOUSE_FEED:
        raise ValueError("MongoDB Greenhouse component feed URL changed")
    payload = props.get("data")
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ValueError("MongoDB Greenhouse component data payload changed")
    jobs_block = payload[0].get("grnhsJobs")
    if not isinstance(jobs_block, dict):
        raise ValueError("MongoDB Greenhouse component is missing grnhsJobs")
    rows = jobs_block.get("jobs")
    meta = jobs_block.get("meta")
    if not isinstance(rows, list) or not isinstance(meta, dict):
        raise ValueError("MongoDB Greenhouse component jobs/meta shape changed")
    total = meta.get("total")
    if not isinstance(total, int) or total != len(rows) or total <= 0:
        raise ValueError("MongoDB Greenhouse component meta.total does not match its rows")

    seen: set[int] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"MongoDB listing row {index} is not an object")
        _validate_job_row(row, index)
        identity = row["id"]
        if identity in seen:
            raise ValueError(f"MongoDB listing repeats job id {identity}")
        seen.add(identity)

    tree = (payload[0].get("grnhsDepts") or {}).get("departments")
    if not isinstance(tree, list):
        raise ValueError("MongoDB department tree is missing")
    tree_ids = _department_job_ids(tree)
    if len(tree_ids) != len(set(tree_ids)):
        raise ValueError("MongoDB department tree repeats a job id")
    if set(tree_ids) != seen:
        raise ValueError("MongoDB department tree membership differs from the flat job list")

    cards = []
    for anchor in soup.select('a[automation-testid="flora-ListItem"][href]'):
        target = urljoin(response_url, anchor.get("href", ""))
        parts = urlsplit(target)
        query = parse_qs(parts.query)
        if (parts.scheme != "https" or parts.netloc != "www.mongodb.com"
                or not LISTING_CARD_HREF.fullmatch(parts.path)
                or set(query) != {"gh_jid"} or len(query["gh_jid"]) != 1):
            continue
        card_soup = BeautifulSoup(str(anchor), "lxml")
        for node in card_soup(["svg", "style"]):
            node.decompose()
        text = [part.strip() for part in card_soup.get_text("\n").splitlines() if part.strip()]
        cards.append({
            "id": query["gh_jid"][0],
            "href": target,
            "text": " | ".join(text),
            "title": text[0] if text else None,
            "location": text[1] if len(text) > 1 else None,
            "employment": text[2] if len(text) > 2 else None,
        })
    expected_cards = min(ITEMS_PER_PAGE, total)
    if len(cards) != expected_cards:
        raise ValueError(
            f"MongoDB first listing page renders {len(cards)} cards, expected {expected_cards}")
    first_ids = [str(row["id"]) for row in rows[:expected_cards]]
    if [card["id"] for card in cards] != first_ids:
        raise ValueError("MongoDB server-rendered cards are not the first listing rows in order")

    pagination = soup.select_one('[automation-testid="flora-Pagination-Default"]')
    labels = ([node.get("aria-label") for node in pagination.select("[aria-label]")]
              if pagination is not None else [])
    numbered = [int(match[1]) for label in labels
                if isinstance(label, str) for match in [re.fullmatch(r"page (\d+)", label)] if match]
    page_count = (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    if not numbered or max(numbered) != page_count or min(numbered) != 1:
        raise ValueError("MongoDB pagination control no longer exposes the expected last page")
    if page_count <= 4 and numbered != list(range(1, page_count + 1)):
        raise ValueError("MongoDB pagination control changed its numbered pages")

    facets = soup.select_one('[automation-testid="flora-FacetedFilters"]')
    facet_text = facets.get_text(" ", strip=True) if facets is not None else ""
    search_visible = bool(facets is not None and re.search(r"\bSearch\b", facet_text))
    clear_all = bool(facets is not None and any(
        "Clear all" in normalized_title(node.get_text(" ", strip=True))
        for node in facets.select("button,a")))
    return {
        "url": response_url,
        "page_title": title,
        "component_title": props.get("title"),
        "feed": props.get("feed"),
        "rows": rows,
        "job_ids": [str(row["id"]) for row in rows],
        "total": total,
        "page_size": ITEMS_PER_PAGE,
        "page_count": page_count,
        "pagination_labels": labels,
        "first_page_cards": cards,
        "departments": tree,
        "department_job_ids": tree_ids,
        "filters": {
            "faceted": props.get("facetedFilters"),
            "keyword_search_visible": search_visible,
            "clear_all_visible": clear_all,
            "checkbox_count": len(facets.select('input[type="checkbox"]')) if facets is not None else 0,
            "ssr_text": facet_text,
        },
    }


def parse_job_detail_page(html: str, response_url: str, expected_id: int | str | None = None) -> dict:
    parsed = urlsplit(response_url)
    match = DETAIL_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "www.mongodb.com" or match is None or parsed.query:
        raise ValueError("MongoDB job detail response is not the inspected company detail route")
    job_id = int(match[1])
    if expected_id is not None and str(job_id) != str(expected_id):
        raise ValueError("MongoDB detail page job id does not match the listed job")
    soup = BeautifulSoup(html or "", "lxml")
    node = soup.select_one("script#data-job")
    if node is None:
        raise ValueError("MongoDB detail page is missing its data-job document")
    try:
        detail = json.loads(node.string or node.get_text() or "")
    except ValueError as exc:
        raise ValueError(f"MongoDB data-job document is not valid JSON: {exc}") from exc
    if not isinstance(detail, dict) or detail.get("id") != job_id:
        raise ValueError("MongoDB data-job id does not match the detail route")
    title = normalized_title(detail.get("title"))
    if not title:
        raise ValueError("MongoDB data-job has no title")
    page_title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if page_title != title:
        raise ValueError("MongoDB detail page title differs from its data-job title")
    location = detail.get("location")
    if not isinstance(location, dict) or not normalized_title(location.get("name")):
        raise ValueError("MongoDB data-job has no location")
    content = detail.get("content")
    if not isinstance(content, str) or len(normalized_body_text(content)) < 100:
        raise ValueError("MongoDB data-job has no full description content")
    metadata = detail.get("salary")
    if not isinstance(metadata, list):
        raise ValueError("MongoDB data-job is missing its metadata array")

    summary_node = soup.select_one("script#data-grnhs")
    if summary_node is None:
        raise ValueError("MongoDB detail page is missing its data-grnhs summary")
    summary = json.loads(summary_node.string or summary_node.get_text() or "")
    summary_jobs = summary.get("jobs") if isinstance(summary, dict) else None
    if not isinstance(summary_jobs, list):
        raise ValueError("MongoDB data-grnhs summary has no jobs list")
    summary_row = next((row for row in summary_jobs
                        if isinstance(row, dict) and row.get("id") == job_id), None)
    if summary_row is None:
        raise ValueError("MongoDB detail summary does not contain the routed job")
    if normalized_title(summary_row.get("title")) != title:
        raise ValueError("MongoDB detail summary title differs from data-job")

    apply_iframe = soup.select_one("iframe#grnhse_iframe")
    apply_src = None
    if apply_iframe is not None:
        apply_src = urljoin(response_url, apply_iframe.get("src", ""))
        parts = urlsplit(apply_src)
        if (parts.scheme != "https" or parts.netloc != "job-boards.greenhouse.io"
                or parts.path != "/embed/job_app"
                or parse_qs(parts.query).get("for") != ["mongodb"]
                or parse_qs(parts.query).get("token") != [str(job_id)]):
            raise ValueError("MongoDB detail application iframe changed away from its job form")
    return {
        "job_id": job_id,
        "url": response_url,
        "page_title": page_title,
        "title": title,
        "location": location,
        "content": content,
        "metadata": metadata,
        "summary_jobs_count": len(summary_jobs),
        "summary_job": summary_row,
        "summary_departments": summary.get("depts") or [],
        "summary_offices": summary.get("offices") or [],
        "apply_iframe_src": apply_src,
    }


def parse_greenhouse_feed(value, response_url: str = GREENHOUSE_FEED) -> dict:
    """Validate the feed declared by MongoDB's own widget (cross-check only)."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "api.greenhouse.io"
            or parsed.path != "/v1/boards/mongodb/jobs"):
        raise ValueError("MongoDB declared Greenhouse feed redirected away from its inspected route")
    if not isinstance(value, dict):
        raise ValueError("MongoDB Greenhouse feed is not an object")
    rows = value.get("jobs")
    meta = value.get("meta")
    if not isinstance(rows, list) or not isinstance(meta, dict):
        raise ValueError("MongoDB Greenhouse feed is missing jobs or meta")
    total = meta.get("total")
    if not isinstance(total, int) or total != len(rows):
        raise ValueError("MongoDB Greenhouse feed meta.total does not match its jobs")
    seen: set[int] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"MongoDB Greenhouse feed row {index} is not an object")
        _validate_job_row(row, index)
        if row["id"] in seen:
            raise ValueError(f"MongoDB Greenhouse feed repeats job id {row['id']}")
        seen.add(row["id"])
    return {"url": response_url, "rows": rows, "job_ids": [str(row["id"]) for row in rows], "total": total}


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body beneath it."""
    html = decode_embedded_html(html)
    soup = BeautifulSoup(f'<div data-mongodb-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-mongodb-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections: list[dict] = []
    current = None
    for block in blocks:
        heading = ""
        text = normalized_title(block.get_text(" ", strip=True))
        if block.name in heading_tags:
            heading = text
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == text and len(text) <= 120:
                heading = text
            elif (len(text) <= 90 and not text.endswith(".") and text
                  and HEADING_HINTS.match(text)):
                heading = text
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
    if not sections:
        sections.append({"heading": None, "html": html or "", "text": text_html(html)})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def _metadata_values(metadata: list, name: str) -> list:
    values = []
    for item in metadata:
        if not isinstance(item, dict) or item.get("name") != name:
            continue
        value = item.get("value")
        if isinstance(value, list):
            values.extend(value)
        elif value not in (None, ""):
            values.append(value)
    return values


def _metadata_one(metadata: list, name: str):
    values = _metadata_values(metadata, name)
    return values[0] if values else None


def _salary_ranges(metadata: list) -> list[dict] | None:
    ranges = []
    for item in metadata:
        value = item.get("value") if isinstance(item, dict) else None
        if (isinstance(item, dict) and item.get("value_type") == "currency_range"
                and isinstance(value, dict) and value.get("unit")):
            ranges.append({
                "field": item.get("name"),
                "unit": value.get("unit"),
                "min": value.get("min_value"),
                "max": value.get("max_value"),
            })
    return ranges or None


def apply_form_url(job_id: int | str) -> str:
    return APPLY_FORM.format(job_id=job_id)


def detail_url(job_id: int | str) -> str:
    return DETAIL_URL.format(job_id=job_id)


def _qualification_sections(sections: list[dict]) -> list[dict]:
    return [section for section in sections if section.get("heading") and any(
        token in section["heading"].lower()
        for token in ("qualif", "requirement", "what we are looking for",
                      "what you'll need", "what you will need", "who you are"))]


def _responsibility_sections(sections: list[dict]) -> list[dict]:
    return [section for section in sections if section.get("heading") and any(
        token in section["heading"].lower()
        for token in ("responsibilit", "what you will do", "what you'll do", "the role"))]


class MongoDB(Adapter):
    """Read only MongoDB's own see-jobs page, its declared feed and company details."""

    VERIFICATION_REVISION = "mongodb.official-see-jobs-greenhouse-embed.1"

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _listing(self):
        started = time.perf_counter()
        response = await self.http.get(LISTING_URL)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != LISTING_URL:
            raise ValueError("MongoDB search-jobs page redirected away from its inspected URL")
        parsed = parse_listing_page(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _feed_crosscheck(self):
        started = time.perf_counter()
        response = await self.http.get(GREENHOUSE_FEED)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_greenhouse_feed(response.json(), str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    def _listing_job(self, row: dict, position: int) -> object:
        identity = str(row["id"])
        location_name = normalized_title(row["location"].get("name"))
        metadata = row.get("metadata") or []
        departments = [d.get("name") for d in row.get("departments") or []
                       if isinstance(d, dict) and d.get("name")]
        job = self.job(
            {
                "listing": row,
                "listing_source_url": LISTING_URL,
                "listing_position": {
                    "zero_based_index": position,
                    "page": position // ITEMS_PER_PAGE + 1,
                    "position_on_page": position % ITEMS_PER_PAGE + 1,
                },
                "apply_form_url": apply_form_url(identity),
            },
            title=row["title"],
            url=detail_url(identity),
            job_id=identity,
            requisition_id=row.get("requisition_id"),
            apply_url=apply_form_url(identity),
            description=row.get("content") or "",
            method="company_listing_embedded_greenhouse_content",
            description_url=LISTING_URL,
            locations=[part.strip() for part in location_name.split(";") if part.strip()],
            department=departments or None,
            team=_metadata_values(metadata, "Team") or None,
            employment_type=_metadata_one(metadata, "Employment Type"),
            workplace_type=_metadata_values(metadata, "Working Model Eligibility") or None,
            date_posted=row.get("first_published"),
            date_posted_raw={
                "first_published": row.get("first_published"),
                "updated_at": row.get("updated_at"),
                "application_deadline": row.get("application_deadline"),
            },
            date_updated=row.get("updated_at"),
            valid_through=row.get("application_deadline") or None,
            salary=_salary_ranges(metadata),
            employer=row.get("company_name"),
            language=row.get("language"),
        )
        job.description_text = text_html(job.description_html)
        job.raw["classification"] = {
            "job_type": _metadata_one(metadata, "Job Type"),
            "job_level": _metadata_one(metadata, "Job Level"),
            "region": _metadata_one(metadata, "Region"),
            "position_id": _metadata_one(metadata, "Position ID"),
            "department_id": _metadata_one(metadata, "Department ID"),
            "is_pipeline": _metadata_one(metadata, "Job Type") in ("Pipeline", "GTM Upskill Pipeline"),
        }
        job.raw["listing_department_summary"] = {
            "departments": row.get("departments") or [],
            "offices": row.get("offices") or [],
        }
        return job

    async def _detail(self, job):
        identity = str(job.job_id)
        listing = job.raw["listing"]
        request_url = listing["absolute_url"]
        fallback_url = detail_url(identity)
        fetched_url = request_url
        fallback_used = False
        fallback_error = None
        request_started = time.perf_counter()
        try:
            response = await self.http.get(request_url)
        except FetchError as exc:
            # The listing links the company's /careers/job/?gh_jid= route, which
            # redirects to /careers/jobs/<id>. On an edge/upstream failure of the
            # redirecting route, retry the exact redirect target for the same job.
            fallback_used = True
            fallback_error = f"{type(exc).__name__}: {exc}"
            fetched_url = fallback_url
            response = await self.http.get(fallback_url)
        seconds = max(0.0, time.perf_counter() - request_started)
        parsed = parse_job_detail_page(response.text, str(response.url), expected_id=identity)
        listing_compact = compact_body_text(listing.get("content"))
        detail_compact = compact_body_text(parsed["content"])
        comparisons = {
            "title_matches_listing": parsed["title"] == normalized_title(job.title),
            "location_matches_listing": parsed["location"] == listing.get("location"),
            "metadata_matches_listing": parsed["metadata"] == listing.get("metadata"),
            "description_matches_listing_after_whitespace_normalization": detail_compact == listing_compact,
            "summary_jobs_count": parsed["summary_jobs_count"],
            "summary_job_matches": parsed["summary_job"].get("title") == parsed["title"],
        }
        if not comparisons["title_matches_listing"]:
            raise ValueError(f"MongoDB detail title differs from the listing row: {identity}")
        if not comparisons["location_matches_listing"]:
            raise ValueError(f"MongoDB detail location differs from the listing row: {identity}")
        if not comparisons["metadata_matches_listing"]:
            raise ValueError(f"MongoDB detail metadata differs from the listing row: {identity}")
        sections = description_sections(parsed["content"])
        qualifications = _qualification_sections(sections)
        responsibilities = _responsibility_sections(sections)
        description_text = normalized_body_text(parsed["content"])
        job.description_html = parsed["content"]
        job.description_text = description_text
        job.description_method = "company_detail_page.data-job.content"
        job.description_source_url = str(response.url)
        job.apply_url = apply_form_url(identity)
        job.qualifications = qualifications or None
        job.responsibilities = responsibilities or None
        job.raw["detail"] = {
            "source_field": "data-job.content",
            "request_url": request_url,
            "fetched_url": fetched_url,
            "fallback_to_direct_detail_route": fallback_used,
            "fallback_reason": fallback_error,
            "final_url": str(response.url),
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "request_seconds": seconds,
            "data_job_id": parsed["job_id"],
            "data_job_title": parsed["title"],
            "data_job_location": parsed["location"],
            "data_job_metadata": parsed["metadata"],
            "summary_jobs_count": parsed["summary_jobs_count"],
            "summary_job": parsed["summary_job"],
            "summary_departments": parsed["summary_departments"],
            "summary_offices": parsed["summary_offices"],
            "apply_iframe_src": parsed["apply_iframe_src"],
            "comparisons": comparisons,
            "description_sha256": sha256(parsed["content"].encode()).hexdigest(),
            "description_text_sha256": sha256(description_text.encode()).hexdigest(),
            "description_compact_sha256": sha256(detail_compact.encode()).hexdigest(),
            "description_normalized_chars": len(description_text),
            "description_compact_chars": len(detail_compact),
            "description_fnv1a64": fnv1a64(description_text),
            "description_sections": sections,
        }
        job.raw["description_matches_listing"] = comparisons[
            "description_matches_listing_after_whitespace_normalization"]
        return job

    async def run(self):
        run_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_authority": (
                "The 375 public rows in the GreenhouseV2 component of MongoDB's own "
                "/company/careers/see-jobs page; the same client-side paginated cards "
                "rendered 10 per page over 38 pages."
            ),
            "listing_request": {
                "method": "GET",
                "path": "/company/careers/see-jobs",
                "query": None,
                "filters_applied": False,
                "pagination": "client-side React state; no per-page URL or XHR observed",
                "page_size": ITEMS_PER_PAGE,
            },
            "declared_feed_cross_check": GREENHOUSE_FEED,
            "detail_routing": (
                "Each listing absolute_url (/careers/job/?gh_jid=<id>) redirects to "
                "/careers/jobs/<id>, which embeds script#data-job with the full content."
            ),
            "apply_routing": (
                "The hydrated company detail page embeds iframe#grnhse_iframe with "
                "https://job-boards.greenhouse.io/embed/job_app?for=mongodb&token=<id>; "
                "that form URL is retained and never submitted."
            ),
            "scope": (
                "Only MongoDB's own page rows and their company detail routes; no "
                "Greenhouse-wide discovery, guessed slug, parent board or aggregator."
            ),
        })
        try:
            entry = await self._official_entry()
            listing = await self._listing()
            feed = await self._feed_crosscheck()
        except Exception as exc:
            self.result.errors.append(f"MongoDB source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_seconds"] = max(
                0.0, time.perf_counter() - run_started)
            self.result.timings["adapter_run_wall_seconds"] = max(
                0.0, time.perf_counter() - run_started)
            return self.result

        self.result.timings["source_collection_seconds"] = max(
            0.0, time.perf_counter() - run_started)
        self.result.expected_total = listing["total"]
        self.result.pages = listing["page_count"]
        self.result.metadata["official_entry"] = entry
        self.result.metadata["initial_listing"] = {
            key: value for key, value in listing.items()
            if key not in ("rows", "departments", "department_job_ids")
        }
        self.result.metadata["initial_listing"]["row_count"] = len(listing["rows"])
        self.result.metadata["initial_feed_cross_check"] = {
            key: value for key, value in feed.items() if key != "rows"}
        feed_field_differences: dict[str, list[str]] = {}
        feed_date_differences: list[dict] = []
        for page_row, feed_row in zip(listing["rows"], feed["rows"]):
            identity = str(page_row["id"])
            changed = sorted(
                key for key in set(page_row) | set(feed_row)
                if key != "updated_at" and page_row.get(key) != feed_row.get(key)
            )
            if changed:
                feed_field_differences[identity] = changed
            if page_row.get("updated_at") != feed_row.get("updated_at"):
                feed_date_differences.append({
                    "job_id": identity,
                    "company_page_updated_at": page_row.get("updated_at"),
                    "live_feed_updated_at": feed_row.get("updated_at"),
                })
        self.result.metadata["initial_feed_cross_check"].update({
            "row_field_differences_excluding_updated_at": feed_field_differences,
            "updated_at_differences_due_to_page_cache": feed_date_differences,
        })
        feed_membership_matches = (
            feed["total"] == listing["total"]
            and feed["job_ids"] == listing["job_ids"]
            and not feed_field_differences
        )
        self.result.metadata["feed_matches_company_page_rows"] = feed_membership_matches
        if not feed_membership_matches:
            self.result.errors.append(
                "MongoDB declared feed no longer matches the company page rows exactly")

        jobs = [self._listing_job(row, index) for index, row in enumerate(listing["rows"])]
        if len(jobs) != listing["total"]:
            self.result.errors.append("mongodb_listing_rows_not_retained")

        limit = self.options.max_jobs
        if limit and limit < len(jobs):
            jobs = jobs[:limit]
            self.result.warnings.append("sample_limit")
            self.result.listing_complete = False

        await self.hydrate(jobs, self._detail)
        self.result.jobs = jobs
        hydrated = self.result.metadata.get("details") or {}
        self.result.metadata["resumed_snapshot"] = {
            "cached_jobs_available": len(self.options.cached_jobs),
            "verified_description_cache_hits": hydrated.get("verified_cache_hits", 0),
            "detail_pages_fetched_this_run": hydrated.get("fetched_jobs", 0),
            "reused_existing_verified_descriptions": bool(
                hydrated.get("verified_cache_hits", 0)),
        }

        final = None
        final_entry = None
        final_started = time.perf_counter()
        try:
            final_entry = await self._official_entry()
            final = await self._listing()
        except Exception as exc:
            self.result.errors.append(f"MongoDB final listing recheck failed: {type(exc).__name__}: {exc}")
        self.result.timings["final_recheck_seconds"] = max(
            0.0, time.perf_counter() - final_started)

        jobs_by_id = {str(job.job_id): job for job in jobs}
        unresolved_descriptions = [
            identity for identity, job in jobs_by_id.items()
            if job.raw.get("description_matches_listing") is False
        ]
        resolved_updates: dict[str, list[str]] = {}
        unresolved_changes: dict[str, list[str]] = {}
        if final is not None:
            final_by_id = {str(row["id"]): row for row in final["rows"]}
            initial_by_id = {str(row["id"]): row for row in listing["rows"]}
            for identity in listing["job_ids"]:
                if identity not in final_by_id:
                    continue
                changed = sorted(
                    key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                    if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                )
                if not changed:
                    continue
                job = jobs_by_id.get(identity)
                detail = (job.raw.get("detail") if job else None) or {}
                final_compact = compact_body_text(final_by_id[identity].get("content"))
                detail_refreshed = bool(detail) and (
                    final_by_id[identity].get("title") == detail.get("data_job_title")
                    and final_by_id[identity].get("location") == detail.get("data_job_location")
                    and final_by_id[identity].get("metadata") == detail.get("data_job_metadata")
                    and sha256(final_compact.encode()).hexdigest()
                    == detail.get("description_compact_sha256")
                )
                if detail_refreshed:
                    resolved_updates[identity] = changed
                else:
                    unresolved_changes[identity] = changed
            unresolved_descriptions = [
                identity for identity in unresolved_descriptions
                if identity not in resolved_updates
            ]
            same_ids = final["job_ids"] == listing["job_ids"]
            same_total = final["total"] == listing["total"]
            entry_same = (final_entry or {}).get("see_jobs_urls") == entry["see_jobs_urls"]
            complete = (same_ids and same_total and entry_same and not unresolved_changes
                        and not unresolved_descriptions and len(jobs) == listing["total"])
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_listing"] = {
                key: value for key, value in final.items()
                if key not in ("rows", "departments", "department_job_ids")
            }
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": listing["job_ids"],
                "final_ids": final["job_ids"],
                "initial_count": listing["total"],
                "final_count": final["total"],
                "initial_pages": listing["page_count"],
                "final_pages": final["page_count"],
                "same_ids_and_order": same_ids,
                "same_total": same_total,
                "official_entry_still_links_to_listing": entry_same,
                "feed_matched_company_page_rows": feed_membership_matches,
                "changed_fields_by_job_id": unresolved_changes,
                "resolved_mid_run_updates_fields": resolved_updates,
                "unresolved_description_mismatches": unresolved_descriptions,
                "new_job_ids": [i for i in final["job_ids"] if i not in listing["job_ids"]],
                "removed_job_ids": [i for i in listing["job_ids"] if i not in final["job_ids"]],
                "complete": complete,
            }
            if not same_ids or not same_total:
                self.result.errors.append("mongodb_listing_membership_changed_during_collection")
            if not entry_same:
                self.result.errors.append("mongodb_official_entry_link_changed_during_collection")
            if unresolved_changes:
                self.result.errors.append("mongodb_source_fields_changed_during_collection")
        else:
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": listing["job_ids"],
                "complete": False,
                "reason": "final listing recheck unavailable",
            }

        if unresolved_descriptions:
            for identity in unresolved_descriptions:
                job = jobs_by_id.get(identity)
                self.result.errors.append(
                    f"mongodb_description_mismatch_between_listing_and_detail:{identity}")
                if job is not None and "description_not_full_field" not in job.warnings:
                    job.warnings.append("description_not_full_field")

        self.result.metadata["detail_hydration_mode"] = (
            "One company detail request per listed job; full body is data-job.content. "
            "The listing page embedded the same full body as its own cross-check."
        )
        self.result.metadata["detail_stats"] = self.result.metadata.get("details", {})
        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.metadata["page_count"] = listing["page_count"]
        self.result.metadata["item_count"] = listing["total"]
        self.result.metadata["pipeline_jobs"] = sum(
            1 for job in jobs if job.raw.get("classification", {}).get("is_pipeline"))
        reconciliation = self.result.metadata["membership_reconciliation"]
        self.result.listing_complete = bool(
            reconciliation.get("complete") and len(jobs) == listing["total"]
            and not unresolved_descriptions)
        self.result.timings["adapter_run_wall_seconds"] = max(
            0.0, time.perf_counter() - run_started)
        return self.result
