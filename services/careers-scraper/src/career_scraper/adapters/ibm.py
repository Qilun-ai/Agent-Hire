"""IBM's own published careers journey.

Entry: ``https://www.ibm.com/careers/search`` (IBM's "Search Jobs" page; the
landing page ``https://www.ibm.com/careers`` links to it from "Search Jobs" and
"Explore all roles").  The page is a Carbon/Adobe-CMS shell whose client script
declares ``careerSearchScope = {scopeid: 'careers2'}`` and
``careerSearchAppId = {appid: 'careers'}`` and queries
``https://www-api.ibm.com/search/api/v2`` with an empty ``query.bool.must``
list, i.e. the whole IBM careers index with no keyword or facet filter.

Listing membership is that unfiltered search index.  Its default sort
(``pageviews_desc``) is *live*: pageview counters move while a deep pagination
walk is in progress, so offsets overlap and a single sweep silently drops
documents.  The adapter therefore unions the exact sort modes the page's own
sort dropdown sends (Title A-Z, Newest To Oldest, Oldest to Newest, Most
Relevant) until the union of distinct jobIds equals the search total, and it
verifies that one page beyond the total is empty.

Details are the exact ``careers.ibm.com`` JobDetail pages for those jobIds.  The
detail host is behind an AWS WAF JavaScript challenge for plain HTTP (HTTP 202
+ challenge script), so details are fetched from one WAF-warmed headless
Chromium context: the browser solves the JS challenge itself, then the same
context issues the two public GETs per posting (the JobDetail page for the
metadata sidebar / JSON-LD JobPosting / Apply link, and the JobDetailWidget
fragment that carries the full posting body).  No CAPTCHA is solved, no
challenge token is forged and no request is retried through a different
client; a 429 stops the detail phase.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now, text_html

LANDING_URL = "https://www.ibm.com/careers"
ENTRY_URL = "https://www.ibm.com/careers/search"
SEARCH_API_URL = "https://www-api.ibm.com/search/api/v2"
DETAIL_HOST = "careers.ibm.com"
SEARCH_PAGE_SIZE = 30
DETAIL_URL = "https://careers.ibm.com/en_US/careers/JobDetail?jobId={job_id}"
LISTING_JOB_URL = "https://careers.ibm.com/careers/JobDetail?jobId={job_id}"
WIDGET_URL = "https://careers.ibm.com/en_US/careers/JobDetailWidget?jobId={job_id}"
PRETTY_DETAIL_URL = "https://careers.ibm.com/en_US/careers/JobDetail/{slug}/{job_id}"
APPLY_URL = "https://careers.ibm.com/en_US/careers/Login?jobId={job_id}"
WIDGET_ID = "jobDetailWidget"

# The four sort modes the careers search dropdown publishes, with the exact
# request clauses the page sends when each mode is selected.
SORT_MODES = {
    "title_az": {"label": "Title A-Z", "value": "title.keyword_asc",
                 "sort": [{"title.keyword": "asc"}, {"_score": "desc"}]},
    "newest": {"label": "Newest To Oldest", "value": "dcdate_desc",
               "sort": [{"dcdate": "desc"}, {"_score": "desc"}]},
    "oldest": {"label": "Oldest to Newest", "value": "dcdate_asc",
               "sort": [{"dcdate": "asc"}, {"_score": "desc"}]},
    "relevant": {"label": "Most Relevant", "value": "pageviews_desc",
                 "sort": [{"_score": "desc"}, {"pageviews": "desc"}]},
}
# Static sorts first: they are stable across a sweep.  The live pageview sort
# is last because it is the one whose ordering drifts.
SWEEP_ORDER = ("title_az", "newest", "oldest", "relevant")

# Reporting-only aggregations, byte-identical to the ones the page requests.
_AGGS = {
    "field_keyword_172": {"filter": {"match_all": {}}, "aggs": {
        "field_keyword_17": {"terms": {"field": "field_keyword_17", "size": 6}},
        "field_keyword_17_count": {"cardinality": {"field": "field_keyword_17"}}}},
    "field_keyword_083": {"filter": {"match_all": {}}, "aggs": {
        "field_keyword_08": {"terms": {"field": "field_keyword_08", "size": 6}},
        "field_keyword_08_count": {"cardinality": {"field": "field_keyword_08"}}}},
    "field_keyword_184": {"filter": {"match_all": {}}, "aggs": {
        "field_keyword_18": {"terms": {"field": "field_keyword_18", "size": 6}},
        "field_keyword_18_count": {"cardinality": {"field": "field_keyword_18"}}}},
    "field_keyword_055": {"filter": {"match_all": {}}, "aggs": {
        "field_keyword_05": {"terms": {"field": "field_keyword_05", "size": 1000}},
        "field_keyword_05_count": {"cardinality": {"field": "field_keyword_05"}}}},
}
_SOURCE_FIELDS = ["_id", "title", "url", "description", "language", "entitled",
                  "field_keyword_17", "field_keyword_08", "field_keyword_18",
                  "field_keyword_19", "field_keyword_05", "dcdate", "pageviews"]

_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class DetailPhaseBlocked(RuntimeError):
    """Raised when the detail host answers with a challenge or rate limit."""


def normalized_text(value: str | None) -> str:
    """Browser-reference normalization: NFC, collapse whitespace, trim."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def title_slug(title: str | None) -> str:
    slug = re.sub(r"\s+", "-", str(title or "").strip())
    slug = re.sub(r"[^A-Za-z0-9\-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "job"


def pretty_detail_url(title: str | None, job_id: str | int) -> str:
    identity = str(job_id)
    if not re.fullmatch(r"\d+", identity):
        raise ValueError(f"IBM exposed jobId is not numeric: {job_id!r}")
    return PRETTY_DETAIL_URL.format(slug=title_slug(title), job_id=identity)


def detail_url(job_id: str | int) -> str:
    return DETAIL_URL.format(job_id=job_id)


def listing_job_url(job_id: str | int) -> str:
    """The exact job URL the careers search publishes for a jobId."""
    return LISTING_JOB_URL.format(job_id=job_id)


def widget_url(job_id: str | int) -> str:
    return WIDGET_URL.format(job_id=job_id)


def search_body(from_offset: int = 0, sort_key: str = "relevant") -> dict:
    """The exact request body the careers search page posts (plus ``from``)."""
    mode = SORT_MODES[sort_key]
    body = {
        "appId": "careers",
        "scopes": ["careers2"],
        "query": {"bool": {"must": []}},
        "aggs": json.loads(json.dumps(_AGGS)),
        "size": SEARCH_PAGE_SIZE,
        "sort": json.loads(json.dumps(mode["sort"])),
        "lang": "zz",
        "localeSelector": {},
        "sm": {"query": "", "lang": "zz"},
        "_source": list(_SOURCE_FIELDS),
    }
    if from_offset:
        body["from"] = from_offset
    return body


def parse_job_id(url: str) -> str:
    """Read the numeric jobId out of a listing detail URL."""
    parts = urlsplit(str(url))
    if parts.scheme != "https" or parts.hostname != DETAIL_HOST:
        raise ValueError(f"IBM listing exposes a non-careers.ibm.com detail URL: {url}")
    if not re.fullmatch(r"/(?:en_US/)?careers/JobDetail(?:/[^/]+)*", parts.path.rstrip("/")):
        raise ValueError(f"IBM listing detail URL has an unexpected path: {url}")
    values = parse_qs(parts.query).get("jobId", [])
    if len(values) != 1 or not re.fullmatch(r"\d+", values[0]):
        raise ValueError(f"IBM listing detail URL has no numeric jobId: {url}")
    return values[0]


def parse_official_entry(html: str, response_url: str = ENTRY_URL) -> dict:
    """Verify the server-rendered official careers search shell."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.hostname != "www.ibm.com" or parts.path.rstrip("/") != "/careers/search":
        raise ValueError(f"IBM official entry is not the inspected /careers/search URL: {response_url}")
    if "careerSearchScope" not in html or "scopeid: 'careers2'" not in html:
        raise ValueError("IBM careers search page no longer declares the inspected careers2 scope")
    if "careerSearchAppId" not in html or "appid: 'careers'" not in html:
        raise ValueError("IBM careers search page no longer declares the inspected careers appId")
    if "career-search" not in html:
        raise ValueError("IBM careers search page no longer mounts the inspected career-search component")
    missing_labels = [mode["label"] for mode in SORT_MODES.values() if mode["label"] not in html]
    if missing_labels:
        raise ValueError(f"IBM careers search page no longer publishes sort labels: {missing_labels}")
    if "Items per page" not in html or "%s1 – %s2 of %s3 items" not in html:
        raise ValueError("IBM careers search page no longer publishes its pagination UI")
    return {
        "url": response_url,
        "scopeid": "careers2",
        "appid": "careers",
        "component": "career-search",
        "sort_labels_published": [mode["label"] for mode in SORT_MODES.values()],
        "pagination_ui": "Items per page: 10/20/30 with '1 – N of M items' summary",
        "server_rendered_shell": True,
        "search_api_referenced": "www-api.ibm.com/search/api" in html,
        "no_results_message_is_talent_network": "talent network" in html,
    }


def parse_rendered_entry(html: str, response_url: str = ENTRY_URL) -> dict:
    """Verify the browser-rendered careers search UI (sort options + facets)."""
    soup = BeautifulSoup(html, "lxml")
    options = {}
    for select in soup.find_all("select"):
        for option in select.find_all("option"):
            value = (option.get("value") or "").strip()
            label = normalized_text(option.get_text(" ", strip=True))
            if value:
                options[value] = label
    missing = [mode["value"] for mode in SORT_MODES.values() if mode["value"] not in options]
    if missing:
        raise ValueError(f"rendered IBM careers search no longer offers sort values: {missing}")
    for mode in SORT_MODES.values():
        if options[mode["value"]] != mode["label"]:
            raise ValueError(
                f"rendered IBM sort {mode['value']} is labelled {options[mode['value']]!r}, "
                f"expected {mode['label']!r}")
    text = normalized_text(soup.get_text(" ", strip=True))
    summary = re.search(r"(\d[\d,]*)\s*–\s*(\d[\d,]*)\s*of\s*(\d[\d,]*)\s*items", text)
    if not summary:
        raise ValueError("rendered IBM careers search has no 'N - M of T items' summary")
    facet_labels = [label for label in ("Hybrid and Remote jobs", "Career Areas", "Experience level", "Location")
                    if label in text]
    if len(facet_labels) < 4:
        raise ValueError(f"rendered IBM careers search is missing facet labels: {facet_labels}")
    job_links = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(response_url, anchor["href"].strip())
        if urlsplit(href).hostname == DETAIL_HOST and "/JobDetail" in href:
            job_links.append(href)
    return {
        "url": response_url,
        "sort_options": options,
        "summary_text": summary.group(0),
        "first_item": int(summary.group(1).replace(",", "")),
        "last_item": int(summary.group(2).replace(",", "")),
        "total_items": int(summary.group(3).replace(",", "")),
        "items_per_page_options": list(dict.fromkeys(
            normalized_text(o.get_text(" ", strip=True))
            for select in soup.find_all("select") for o in select.find_all("option")
            if "Items per page" in o.get_text(" ", strip=True))),
        "facet_labels": facet_labels,
        "visible_job_links": len(job_links),
    }


def parse_landing_page(html: str, response_url: str = LANDING_URL) -> dict:
    """Verify IBM's careers landing page still publishes the search entry."""
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.hostname != "www.ibm.com" or parts.path.rstrip("/") != "/careers":
        raise ValueError(f"IBM careers landing page is not the inspected /careers URL: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    links = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(response_url, anchor["href"].strip())
        if href.rstrip("/") == ENTRY_URL:
            links.append({"text": normalized_text(anchor.get_text(" ", strip=True)), "href": href})
    if not links:
        raise ValueError("IBM careers landing page no longer links to /careers/search")
    return {"url": response_url, "search_entry_links": links, "link_count": len(links)}


def parse_search_page(payload: dict, requested_from: int = 0, sort_key: str = "relevant") -> dict:
    """Parse one careers search response and verify its pagination shape."""
    if not isinstance(payload, dict):
        raise ValueError("IBM search response is not an object")
    hits_block = payload.get("hits")
    if not isinstance(hits_block, dict):
        raise ValueError("IBM search response has no hits object")
    hits = hits_block.get("hits")
    total = hits_block.get("total")
    if not isinstance(hits, list) or not isinstance(total, dict):
        raise ValueError("IBM search response is missing hits or total")
    if total.get("relation") != "eq" or not isinstance(total.get("value"), int):
        raise ValueError("IBM search response total is not exact")
    if len(hits) > SEARCH_PAGE_SIZE:
        raise ValueError("IBM search page exceeds the observed page size")

    rows, seen, issues = [], set(), []
    expected_rows = min(SEARCH_PAGE_SIZE, max(0, total["value"] - requested_from))
    if len(hits) != expected_rows:
        issues.append(f"search_page_row_count_mismatch:{len(hits)}!={expected_rows}")
    for hit in hits:
        if not isinstance(hit, dict) or not isinstance(hit.get("_source"), dict):
            raise ValueError("IBM search hit lacks an object _source")
        source = hit["_source"]
        if not isinstance(source.get("title"), str) or not source["title"].strip():
            raise ValueError("IBM search hit lacks a title")
        job_id = parse_job_id(source.get("url"))
        if job_id in seen:
            issues.append(f"search_duplicate_job_id_within_page:{job_id}")
            continue
        seen.add(job_id)
        if not isinstance(source.get("description"), str):
            issues.append(f"search_hit_description_not_string:{job_id}")
        if not source.get("field_keyword_08"):
            issues.append(f"search_hit_area_of_work_missing:{job_id}")
        if not source.get("field_keyword_19"):
            issues.append(f"search_hit_location_missing:{job_id}")
        rows.append({"job_id": job_id, "hit": hit, "source": source})
    return {
        "rows": rows,
        "ids": [row["job_id"] for row in rows],
        "total": total["value"],
        "from": requested_from,
        "sort": sort_key,
        "page_size": SEARCH_PAGE_SIZE,
        "issues": issues,
    }


def _field_parts(field) -> tuple[str | None, str, str]:
    label_el = field.select_one(".article__content__view__field__label")
    value_el = field.select_one(".article__content__view__field__value")
    label = normalized_text(label_el.get_text(" ", strip=True)) if label_el else None
    if value_el is None:
        return label, "", ""
    value_html = value_el.decode_contents()
    return label, value_html, text_html(value_html).strip()


def _parse_date(value) -> str | None:
    """Normalize only dates actually published by the page; never guess."""
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", text)
    if match:
        return "-".join(match.groups())
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _money(value: str | None) -> float | None:
    if value is None:
        return None
    text = re.sub(r"[^0-9.\-]", "", str(value))
    if not re.search(r"\d", text):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fragment(html: str) -> str:
    """Return the widget fragment (the endpoint serves a bare <section>)."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.body
    if body is not None:
        return body.decode_contents().strip()
    return str(html).strip()


def parse_detail_page(html: str, job_id: str | int) -> dict:
    """Parse the JobDetail page: metadata sidebar, JSON-LD, Apply link, dates."""
    identity = str(job_id)
    if not re.fullmatch(r"\d+", identity):
        raise ValueError(f"IBM detail jobId is not numeric: {job_id!r}")
    soup = BeautifulSoup(html, "lxml")
    issues: list[str] = []

    jsonld_all, jsonld = [], {}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            issues.append("jsonld_not_parseable")
            continue
        jsonld_all.append(data)
        if isinstance(data, dict) and data.get("@type") == "JobPosting" and not jsonld:
            jsonld = data
    if not jsonld:
        raise ValueError(f"IBM JobDetail {identity} has no JSON-LD JobPosting")

    metadata_article = None
    for article in soup.find_all("article"):
        labels = [_field_parts(f)[0] for f in article.select(".article__content__view__field")]
        if "Job ID" in labels:
            metadata_article = article
            break
    if metadata_article is None:
        raise ValueError(f"IBM JobDetail {identity} has no metadata article")
    metadata, metadata_html = {}, {}
    for field in metadata_article.select(".article__content__view__field"):
        label, value_html, value_text = _field_parts(field)
        if label and value_text:
            metadata[label] = value_text
            metadata_html[label] = value_html
    if metadata.get("Job ID") != identity:
        raise ValueError(f"IBM JobDetail {identity} metadata Job ID does not match the listing")

    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    h1s = [normalized_text(h.get_text(" ", strip=True)) for h in soup.find_all("h1")]
    title_candidates = [
        ("metadata.Job Title", metadata.get("Job Title")),
        ("jsonld.title", jsonld.get("title")),
        ("page.h1", h1s[0] if h1s else None),
        ("page.title", re.sub(rf"\s+-\s+{re.escape(identity)}\s+-\s+IBM\s*$", "", page_title).strip() or None),
    ]
    detail_title = next((normalized_text(value) for _, value in title_candidates
                         if isinstance(value, str) and value.strip()), "")
    if not detail_title:
        issues.append("detail_title_missing")

    date_jsonld_raw = jsonld.get("datePosted")
    date_metadata_raw = metadata.get("Date posted")
    date_jsonld = _parse_date(date_jsonld_raw)
    date_metadata = _parse_date(date_metadata_raw)
    date_posted = date_jsonld or date_metadata
    if date_jsonld and date_metadata and date_jsonld != date_metadata:
        issues.append(f"posting_date_sources_disagree:{date_jsonld}!={date_metadata}")
    if date_posted is None and (date_jsonld_raw or date_metadata_raw):
        issues.append("posting_date_not_parseable")

    salary, salary_raw = None, {}
    for label, value in metadata.items():
        match = re.match(r"Projected (Minimum|Maximum) Salary per (\w+)", label)
        if match:
            salary_raw[label] = value
    if salary_raw:
        periods = {re.match(r"Projected (?:Minimum|Maximum) Salary per (\w+)", label).group(1)
                   for label in salary_raw}
        minimum_raw = next((value for label, value in salary_raw.items() if label.startswith("Projected Minimum")), None)
        maximum_raw = next((value for label, value in salary_raw.items() if label.startswith("Projected Maximum")), None)
        salary = {
            "minimum": _money(minimum_raw),
            "maximum": _money(maximum_raw),
            "raw_minimum": minimum_raw,
            "raw_maximum": maximum_raw,
            "period": periods.pop() if len(periods) == 1 else None,
            "currency": None,
            "currency_note": ("The JobDetail page labels projected annual salary but never states a "
                              "currency, so currency stays null and no conversion is attempted."),
        }

    address = {}
    job_location = jsonld.get("jobLocation")
    if isinstance(job_location, list) and job_location:
        job_location = job_location[0]
    if isinstance(job_location, dict) and isinstance(job_location.get("address"), dict):
        address = job_location["address"]
    city_raw = metadata.get("City / Township / Village") or address.get("addressLocality")
    state_raw = metadata.get("State / Province") or address.get("addressRegion")
    country = metadata.get("Country") or address.get("addressCountry")
    locations = []
    if city_raw or state_raw or country:
        locations.append({
            "city": city_raw,
            "state": state_raw,
            "country": country,
            "city_list": [part.strip() for part in str(city_raw).split(",") if part.strip()] if city_raw else [],
            "state_list": [part.strip() for part in str(state_raw).split(",") if part.strip()] if state_raw else [],
            "multiple_locations": bool(city_raw and "," in str(city_raw)),
        })

    apply_url = None
    for anchor in soup.find_all("a", href=True):
        href = urljoin("https://careers.ibm.com/", anchor["href"].strip())
        parts = urlsplit(href)
        if parts.hostname != DETAIL_HOST or not parts.path.rstrip("/").endswith("/careers/Login"):
            continue
        values = parse_qs(parts.query).get("jobId", [])
        if values == [identity]:
            apply_url = href
            break
    if apply_url is None:
        issues.append("apply_login_link_missing")

    experience = metadata.get("Years of Experience")
    language = normalized_text(soup.html.get("lang")) if soup.html and soup.html.get("lang") else None

    return {
        "job_id": identity,
        "detail_title": detail_title,
        "title_candidates": [{"source": source, "value": value} for source, value in title_candidates],
        "page_title": page_title,
        "h1s": h1s,
        "metadata": metadata,
        "metadata_html": metadata_html,
        "jsonld": jsonld,
        "jsonld_all": jsonld_all,
        "date_posted": date_posted,
        "date_posted_jsonld_raw": date_jsonld_raw,
        "date_posted_metadata_raw": date_metadata_raw,
        "date_modified": _parse_date(jsonld.get("dateModified")),
        "valid_through": _parse_date(jsonld.get("validThrough")),
        "salary": salary,
        "salary_raw": salary_raw,
        "locations": locations,
        "apply_url": apply_url,
        "experience": experience,
        "language": language,
        "issues": issues,
    }


def parse_detail_widget(html: str, job_id: str | int) -> dict:
    """Parse the JobDetailWidget fragment that carries the posted body."""
    identity = str(job_id)
    fragment = _fragment(html)
    soup = BeautifulSoup(fragment, "lxml")
    sections, issues = [], []
    by_label: dict[str, str] = {}
    for field in soup.select(".article__content__view__field"):
        label, value_html, value_text = _field_parts(field)
        if not value_text:
            continue
        sections.append({"label": label, "text": value_text, "html": value_html})
        if label:
            if label in by_label:
                issues.append(f"duplicate_section_label:{label}")
            else:
                by_label[label] = value_text
    description_html = fragment
    description_text = text_html(description_html)
    if not sections:
        raise ValueError(f"IBM JobDetailWidget {identity} has no description sections")
    if len(description_text.strip()) < 100:
        raise ValueError(f"IBM JobDetailWidget {identity} description is missing or implausibly short")
    return {
        "job_id": identity,
        "description_html": description_html,
        "description_text": description_text,
        "sections": sections,
        "sections_by_label": by_label,
        "unlabeled_sections": [section["text"] for section in sections if not section["label"]],
        "issues": issues,
    }


def parse_detail_bundle(detail_html: str, widget_html: str, job_id: str | int) -> dict:
    """Parse both halves of one posting and cross-check them."""
    detail = parse_detail_page(detail_html, job_id)
    widget = parse_detail_widget(widget_html, job_id)
    issues = detail["issues"] + widget["issues"]
    jsonld_text = text_html(detail["jsonld"].get("description") or "")
    responsibilities = widget["sections_by_label"].get("Your role and responsibilities")
    if responsibilities and jsonld_text:
        # The JobPosting description is the role-and-responsibilities section,
        # not the whole posting; a mismatch means the two halves disagree.
        if normalized_text(jsonld_text) != normalized_text(responsibilities):
            issues.append("jsonld_description_differs_from_widget_responsibilities")
    return {"detail": detail, "widget": widget, "issues": issues}


def build_job_fields(bundle: dict, *, search_source: dict | None = None) -> dict:
    """Map the parsed posting onto the shared Job fields."""
    detail, widget = bundle["detail"], bundle["widget"]
    metadata = detail["metadata"]
    sections = widget["sections_by_label"]
    education = {key: value for key, value in {
        "required": sections.get("Required education"),
        "preferred": sections.get("Preferred education"),
    }.items() if value}
    return {
        "title": detail["detail_title"] or (search_source or {}).get("title"),
        "apply_url": detail["apply_url"],
        "description_html": widget["description_html"],
        "description_text": widget["description_text"],
        "locations": detail["locations"],
        "department": metadata.get("Area of work") or (search_source or {}).get("field_keyword_08"),
        "employment_type": metadata.get("Employment type") or metadata.get("Contract type"),
        "workplace_type": metadata.get("Work arrangement") or (search_source or {}).get("field_keyword_17"),
        "date_posted": detail["date_posted"],
        "date_updated": detail["date_modified"],
        "valid_through": detail["valid_through"],
        "salary": detail["salary"],
        "qualifications": sections.get("Required technical and professional expertise"),
        "responsibilities": sections.get("Your role and responsibilities"),
        "experience": detail["experience"],
        "education": education or None,
        "employer": metadata.get("Company"),
        "language": detail["language"],
    }


class Ibm(Adapter):
    """Follow IBM's own careers search and the exact JobDetail postings."""

    VERIFICATION_REVISION = "ibm.own-careers-search-and-jobdetail-widget.1"
    SEARCH_API_URL = SEARCH_API_URL

    def __init__(self, http, company, source, options, detail_fetcher=None,
                 detail_cache_dir: Path | str | None = None, reuse_cached_details: bool = True):
        super().__init__(http, company, source, options)
        # async callable (job_id) -> {"detail_status", "widget_status",
        # "detail_html", "widget_html", "elapsed_seconds", "fetcher"}.
        # Defaults to a WAF-warmed Playwright context; tests inject fixtures.
        self.detail_fetcher = detail_fetcher
        self.detail_cache_dir = Path(detail_cache_dir) if detail_cache_dir else None
        self.reuse_cached_details = reuse_cached_details
        self._browser = None
        self._warmed = False
        self.detail_reused = 0
        self.detail_fetched = 0
        self.detail_request_seconds = 0.0
        self.detail_reused_seconds = 0.0
        if self.detail_cache_dir is not None:
            self.detail_cache_dir.mkdir(parents=True, exist_ok=True)

    # -- browser session ---------------------------------------------------
    async def _ensure_browser(self, warm_job_id: str):
        if self._browser is None:
            from playwright.async_api import async_playwright

            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=_BROWSER_UA, locale="en-US",
                viewport={"width": 1440, "height": 1000})
            page = await context.new_page()
            self._browser = (pw, browser, context, page)
            await self._warm(warm_job_id)
        return self._browser

    async def _warm(self, warm_job_id: str):
        """Let the real browser settle the AWS WAF JS challenge.

        The challenge answers HTTP 202 and then reloads the same URL from
        JavaScript, which can interrupt ``page.goto``; that interruption is
        expected here and is not treated as a failure.
        """
        _, _, _, page = self._browser
        last = None
        for _ in range(5):
            status = None
            try:
                response = await page.goto(detail_url(warm_job_id), wait_until="domcontentloaded", timeout=90000)
                status = response.status if response is not None else None
                if status == 429:
                    raise DetailPhaseBlocked(f"JobDetail {warm_job_id} returned HTTP 429 during warm-up")
            except DetailPhaseBlocked:
                raise
            except Exception as exc:  # challenge reloads interrupt the navigation
                last = exc
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)
            try:
                settled = await page.evaluate("() => document.body.innerText.length > 1500")
            except Exception as exc:  # navigation races
                last = exc
                settled = False
            if settled:
                self._warmed = True
                return
            last = last or ValueError(f"warm-up page stayed on the WAF challenge (last HTTP {status})")
        raise ValueError(f"IBM AWS WAF challenge did not settle: {last}")

    async def _close_browser(self):
        if self._browser is not None:
            pw, browser, _, _ = self._browser
            self._browser = None
            self._warmed = False
            try:
                await browser.close()
            finally:
                await pw.stop()

    async def _browser_fetch(self, job_id: str) -> dict:
        """Two public GETs per posting from the WAF-warmed browser context."""
        await self._ensure_browser(job_id)
        _, _, context, _ = self._browser
        started = time.perf_counter()
        detail_response = await context.request.get(detail_url(job_id), timeout=90000)
        detail_html = await detail_response.text()
        widget_response = await context.request.get(widget_url(job_id), timeout=90000)
        widget_html = await widget_response.text()
        return {
            "job_id": job_id,
            "detail_status": detail_response.status,
            "widget_status": widget_response.status,
            "detail_html": detail_html,
            "widget_html": widget_html,
            "elapsed_seconds": round(max(0.0, time.perf_counter() - started), 6),
            "fetcher": "playwright-chromium-context-request",
        }

    # -- raw archive / resume cache ---------------------------------------
    def _cache_path(self, job_id: str) -> Path | None:
        if self.detail_cache_dir is None:
            return None
        return self.detail_cache_dir / f"{job_id}.json.gz"

    def _read_cache(self, job_id: str) -> dict | None:
        path = self._cache_path(job_id)
        if path is None or not self.reuse_cached_details or not path.exists():
            return None
        try:
            record = json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError):
            return None
        if str(record.get("job_id")) != str(job_id):
            return None
        if record.get("detail_status") != 200 or record.get("widget_status") != 200:
            return None
        return record

    def _write_cache(self, record: dict):
        path = self._cache_path(str(record["job_id"]))
        if path is None:
            return
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(gzip.compress(json.dumps(record).encode("utf-8")))
        temp.replace(path)

    def _archive(self, record: dict):
        """Record both public GETs in the shared requests.jsonl evidence log."""
        output = getattr(self.http, "output", None)
        if not output:
            return
        output = Path(output)
        (output / "responses").mkdir(parents=True, exist_ok=True)
        lines = []
        for kind, url, status, body in (
            ("detail", detail_url(record["job_id"]), record["detail_status"], record["detail_html"]),
            ("widget", widget_url(record["job_id"]), record["widget_status"], record["widget_html"]),
        ):
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            lines.append({
                "url": url, "final_url": url, "method": "GET", "request_body": None,
                "status": status, "sha256": digest, "content_type": "text/html",
                "redirects": [], "fetched_at": record.get("fetched_at") or now(),
                "fetcher": record.get("fetcher"), "source_kind": f"exact_exposed_job_{kind}",
                "archive_file": str(self._cache_path(str(record["job_id"]))),
            })
        with (output / "requests.jsonl").open("a") as handle:
            for line in lines:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    # -- job assembly ------------------------------------------------------
    def _make_job(self, row: dict, record: dict):
        job_id = row["job_id"]
        source = row["source"]
        bundle = parse_detail_bundle(record["detail_html"], record["widget_html"], job_id)
        fields = build_job_fields(bundle, search_source=source)
        detail, widget = bundle["detail"], bundle["widget"]
        listing_title = source.get("title")
        title_differs = normalized_text(listing_title) != normalized_text(fields["title"])
        raw = {
            "search": source,
            "search_hit_id": row["hit"].get("_id"),
            "search_hit_index": row["hit"].get("_index"),
            "search_hit_sort": row["hit"].get("sort"),
            "search_score": row["hit"].get("_score"),
            "search_title": listing_title,
            "search_location": source.get("field_keyword_19"),
            "search_area_of_work": source.get("field_keyword_08"),
            "search_work_arrangement": source.get("field_keyword_17"),
            "search_position_type": source.get("field_keyword_18"),
            "search_country": source.get("field_keyword_05"),
            "search_dcdate": source.get("dcdate"),
            "search_description_snippet": source.get("description"),
            "listing_title_differs_from_detail": title_differs,
            "detail_fetch_url": detail_url(job_id),
            "listing_job_url": source.get("url"),
            "pretty_detail_url": pretty_detail_url(fields["title"], job_id),
            "widget_url": widget_url(job_id),
            "detail_status": record["detail_status"],
            "widget_status": record["widget_status"],
            "detail_fetched_at": record.get("fetched_at"),
            "detail_fetcher": record.get("fetcher"),
            "detail_request_seconds": record.get("elapsed_seconds"),
            "detail_title": detail["detail_title"],
            "title_candidates": detail["title_candidates"],
            "detail_page_title": detail["page_title"],
            "detail_sections": widget["sections"],
            "detail_unlabeled_sections": widget["unlabeled_sections"],
            "detail_metadata": detail["metadata"],
            "detail_metadata_html": detail["metadata_html"],
            "detail_jsonld": detail["jsonld"],
            "detail_issues": bundle["issues"],
            "date_posted_jsonld_raw": detail["date_posted_jsonld_raw"],
            "date_posted_metadata_raw": detail["date_posted_metadata_raw"],
            "salary_raw": detail["salary_raw"],
            "description_normalized_chars": len(normalized_text(widget["description_text"])),
            "description_fnv1a64": fnv1a64(normalized_text(widget["description_text"])),
        }
        job = self.job(
            raw,
            title=fields["title"] or listing_title,
            url=listing_job_url(job_id),
            job_id=job_id,
            requisition_id=None,
            apply_url=fields["apply_url"],
            description=fields["description_html"],
            method="ibm.own_careers.jobdetailwidget.sections",
            description_url=widget_url(job_id),
            locations=fields["locations"],
            department=fields["department"],
            team=None,
            employment_type=fields["employment_type"],
            workplace_type=fields["workplace_type"],
            date_posted=fields["date_posted"],
            date_posted_raw={
                "jsonld": detail["date_posted_jsonld_raw"],
                "metadata": detail["date_posted_metadata_raw"],
                "search_dcdate": source.get("dcdate"),
            },
            date_updated=fields["date_updated"],
            valid_through=fields["valid_through"],
            salary=fields["salary"],
            qualifications=fields["qualifications"],
            responsibilities=fields["responsibilities"],
            experience=fields["experience"],
            education=fields["education"],
            employer=fields["employer"],
            language=fields["language"],
        )
        job.description_text = fields["description_text"]
        job.record_identity = f"ibm:job:{job_id}"
        for issue in bundle["issues"]:
            job.warnings.append(issue)
        if title_differs:
            job.warnings.append("listing_title_differs_from_detail")
        return job

    def _unavailable_job(self, row: dict, reason: str):
        job_id = row["job_id"]
        source = row["source"]
        raw = {
            "search": source,
            "search_title": source.get("title"),
            "search_location": source.get("field_keyword_19"),
            "search_dcdate": source.get("dcdate"),
            "search_description_snippet": source.get("description"),
            "detail_fetch_failed": True,
            "detail_failure_reason": reason,
        }
        job = self.job(
            raw,
            title=source.get("title"),
            url=listing_job_url(job_id),
            job_id=job_id,
            requisition_id=None,
            apply_url=None,
            description="",
            method=None,
            description_url=None,
            locations=[{"search_location": source.get("field_keyword_19"),
                        "country": source.get("field_keyword_05")}],
            department=source.get("field_keyword_08"),
            workplace_type=source.get("field_keyword_17"),
            date_posted=None,
        )
        job.description_unavailable = True
        job.record_identity = f"ibm:job:{job_id}"
        job.warnings.append("detail_fetch_failed")
        return job

    # -- listing -----------------------------------------------------------
    async def _sweep(self, sort_key: str, pages: list) -> tuple[list[dict], list[str], int]:
        rows: list[dict] = []
        ids: list[str] = []
        total = None
        offset = 0
        while True:
            if not self.page_guard():
                self.result.errors.append(f"page_budget_exhausted_during_listing:{sort_key}")
                break
            payload = await self.http.json(
                SEARCH_API_URL, method="POST", json=search_body(offset, sort_key))
            page = parse_search_page(payload, offset, sort_key)
            pages.append({"sort": sort_key, "from": offset, "hits": len(page["rows"]),
                          "total": page["total"], "ids": page["ids"], "issues": page["issues"]})
            if total is None:
                total = page["total"]
            elif page["total"] != total:
                # The live board's total moves between pages; the page-level
                # totals are recorded and summarised by _collect_listing.
                self.result.metadata.setdefault("page_totals_observed", []).append(page["total"])
            rows.extend(page["rows"])
            ids.extend(page["ids"])
            if len(page["rows"]) < SEARCH_PAGE_SIZE:
                break
            offset += SEARCH_PAGE_SIZE
            if offset > 20000:
                self.result.errors.append(f"search_pagination_runaway:{sort_key}")
                break
        return rows, ids, (total if total is not None else 0)

    async def _collect_listing(self):
        started = time.perf_counter()
        pages: list[dict] = []
        sweeps: list[dict] = []
        union: dict[str, dict] = {}
        totals: list[int] = []
        for sort_key in SWEEP_ORDER:
            if not self.page_guard():
                self.result.errors.append("page_budget_exhausted_during_listing")
                break
            sweep_rows, ids, total = await self._sweep(sort_key, pages)
            totals.append(total)
            new_ids = [job_id for job_id in ids if job_id not in union]
            sweeps.append({
                "sort": sort_key, "sort_label": SORT_MODES[sort_key]["label"],
                "sort_value": SORT_MODES[sort_key]["value"],
                "rows": len(ids), "unique_rows": len(set(ids)),
                "new_ids": len(new_ids), "total": total,
            })
            for row in sweep_rows:
                union.setdefault(row["job_id"], row)
            self.result.metadata["listing_sweeps"] = sweeps
            if totals and len(union) >= max(totals):
                break
            if len(sweeps) > 1 and not new_ids:
                self.result.warnings.append(f"listing_sweep_added_no_new_ids:{sort_key}")

        advertised = sorted(set(totals))
        self.result.metadata["advertised_totals_observed"] = advertised
        # The live board's total moves while the sweeps run.  The reconciled
        # membership is the distinct jobId union; it must cover the largest
        # advertised total, otherwise the listing stays unreconciled.
        if advertised and len(union) >= advertised[-1]:
            expected_total = len(union)
            self.result.metadata["expected_total_basis"] = (
                "distinct jobId union across the page's own sort modes; the union covers "
                f"the largest advertised total ({advertised[-1]})")
        else:
            expected_total = advertised[-1] if advertised else None
            self.result.metadata["expected_total_basis"] = (
                "largest advertised total; the sweep union fell short")

        if expected_total is not None and all(page["from"] != (
                (expected_total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE) * SEARCH_PAGE_SIZE
                for page in pages):
            if not self.page_guard():
                self.result.errors.append("page_budget_exhausted_during_listing")
            else:
                beyond = ((expected_total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE) * SEARCH_PAGE_SIZE
                payload = await self.http.json(
                    SEARCH_API_URL, method="POST", json=search_body(beyond, "title_az"))
                page = parse_search_page(payload, beyond, "title_az")
                pages.append({"sort": "title_az", "from": beyond, "hits": len(page["rows"]),
                              "total": page["total"], "ids": page["ids"], "issues": page["issues"]})
                if page["rows"]:
                    self.result.errors.append(
                        f"search_exposes_rows_beyond_total:{len(page['rows'])}")

        listing_seconds = time.perf_counter() - started
        self.result.expected_total = expected_total
        self.result.metadata["search_pages"] = pages
        if expected_total is None or len(union) < expected_total:
            self.result.errors.append(
                f"search_membership_unreconciled:unique={len(union)} expected={expected_total}")
        return list(union.values()), listing_seconds

    # -- details -----------------------------------------------------------
    async def _fetch_one(self, job_id: str) -> dict:
        cached = self._read_cache(job_id)
        if cached is not None:
            self.detail_reused += 1
            cached["reused_from_cache"] = True
            return cached
        fetcher = self.detail_fetcher or self._browser_fetch
        last_error = None
        for attempt in range(4):
            try:
                record = await fetcher(job_id)
            except DetailPhaseBlocked:
                raise
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(2 + 2 * attempt)
                self._warmed = False
                continue
            record = dict(record)
            record["job_id"] = job_id
            record.setdefault("fetched_at", now())
            record.setdefault("fetcher", "playwright-chromium-context-request")
            status_ok = record.get("detail_status") == 200 and record.get("widget_status") == 200
            content_ok = (WIDGET_ID in (record.get("detail_html") or "")
                          and "article__content__view__field" in (record.get("widget_html") or ""))
            if status_ok and content_ok:
                self.detail_fetched += 1
                self.detail_request_seconds += float(record.get("elapsed_seconds") or 0.0)
                self._write_cache(record)
                self._archive(record)
                return record
            last_error = ValueError(
                f"JobDetail {job_id} detail={record.get('detail_status')} "
                f"widget={record.get('widget_status')} content_ok={content_ok}")
            if record.get("detail_status") == 429 or record.get("widget_status") == 429:
                raise DetailPhaseBlocked(f"JobDetail {job_id} returned HTTP 429")
            await asyncio.sleep(3 + 3 * attempt)
            self._warmed = False
        raise last_error or ValueError(f"JobDetail {job_id} fetch failed")

    async def _collect_details(self, rows: list[dict], chunk_size: int = 12, *, close_browser: bool = True):
        started = time.perf_counter()
        parse_started = 0.0
        failed: list[str] = []
        blocked: str | None = None
        sem = asyncio.Semaphore(max(1, self.options.detail_concurrency))
        warm_job_id = rows[0]["job_id"] if rows else None

        async def one(job_id):
            async with sem:
                return await self._fetch_one(job_id)

        try:
            for start in range(0, len(rows), chunk_size):
                if blocked:
                    for row in rows[start:]:
                        failed.append(row["job_id"])
                        self.add(self._unavailable_job(row, f"not_fetched_after_block:{blocked}"))
                    break
                chunk = rows[start:start + chunk_size]
                if self.detail_fetcher is None and warm_job_id and not self._warmed:
                    try:
                        await self._ensure_browser(warm_job_id)
                    except DetailPhaseBlocked as exc:
                        blocked = str(exc)
                        continue
                    except Exception as exc:
                        blocked = f"waf_warmup_failed:{type(exc).__name__}: {exc}"
                        continue
                results = await asyncio.gather(*(one(row["job_id"]) for row in chunk),
                                               return_exceptions=True)
                for row, outcome in zip(chunk, results):
                    if isinstance(outcome, DetailPhaseBlocked):
                        blocked = str(outcome)
                        failed.append(row["job_id"])
                        self.result.errors.append(f"JobDetail {row['job_id']}: {outcome}")
                        self.add(self._unavailable_job(row, str(outcome)))
                        continue
                    if isinstance(outcome, Exception):
                        failed.append(row["job_id"])
                        reason = f"{type(outcome).__name__}: {outcome}"
                        self.result.errors.append(f"JobDetail {row['job_id']} ({row['source'].get('title')}): {reason}")
                        self.add(self._unavailable_job(row, reason))
                        continue
                    parse_one = time.perf_counter()
                    try:
                        job = self._make_job(row, outcome)
                    except Exception as exc:
                        parse_started += time.perf_counter() - parse_one
                        failed.append(row["job_id"])
                        reason = f"{type(exc).__name__}: {exc}"
                        self.result.errors.append(f"JobDetail {row['job_id']} parse: {reason}")
                        self.add(self._unavailable_job(row, reason))
                        continue
                    parse_started += time.perf_counter() - parse_one
                    self.add(job)
                if self.options.checkpoint:
                    self.options.checkpoint(self.result)
        finally:
            if close_browser:
                await self._close_browser()
        detail_seconds = time.perf_counter() - started
        self.result.metadata.setdefault("detail_failed_jobIds", []).extend(failed)
        if blocked:
            self.result.metadata["detail_blocked"] = blocked
        return detail_seconds, parse_started, failed

    async def _final_reconciliation(self, known: set[str], max_rounds: int = 2, catch_up_cap: int = 100) -> dict:
        """Re-sweep after the detail phase and catch up postings added meanwhile."""
        info: dict = {"sort": "title_az", "rounds": [], "catch_up": []}
        for _ in range(max_rounds):
            pages: list[dict] = []
            if not self.page_guard():
                info["error"] = "page_budget_exhausted_during_final_recheck"
                return info
            sweep_rows, ids, total = await self._sweep("title_az", pages)
            new_ids = [job_id for job_id in ids if job_id not in known]
            info["rounds"].append({
                "sort": "title_az", "rows": len(ids), "unique": len(set(ids)),
                "total": total, "new_ids": new_ids,
                "not_returned_this_sweep": sorted(known - set(ids))[:50],
                "not_returned_note": (
                    "informational only: the live pageview/date ordering makes a single sweep "
                    "return duplicate rows, so absence from one sweep is not proof of removal"),
            })
            if not new_ids:
                break
            if len(new_ids) > catch_up_cap:
                info["catch_up_skipped"] = f"{len(new_ids)} new ids exceeds cap {catch_up_cap}"
                break
            rows_by_id = {row["job_id"]: row for row in sweep_rows}
            new_rows = [rows_by_id[job_id] for job_id in new_ids if job_id in rows_by_id]
            seconds, parse_seconds, failed = await self._collect_details(
                new_rows, close_browser=False)
            info["catch_up"].append({
                "ids": [row["job_id"] for row in new_rows],
                "seconds": round(seconds, 6), "parse_seconds": round(parse_seconds, 6),
                "failed": failed,
            })
            known.update(row["job_id"] for row in new_rows)
            self.result.metadata["detail_catch_up_seconds"] = round(
                self.result.metadata.get("detail_catch_up_seconds", 0.0) + seconds, 6)
        await self._close_browser()
        return info

    # -- run ---------------------------------------------------------------
    async def run(self):
        try:
            return await self._run_impl()
        finally:
            await self._close_browser()

    async def _run_impl(self):
        started = self.result._started_clock
        if self.company.careers_url.rstrip("/") != ENTRY_URL:
            raise ValueError(f"IBM dedicated flow must start from {ENTRY_URL}")
        if not self.page_guard():
            return self.result

        listing_started = time.perf_counter()
        landing = await self.http.get(LANDING_URL)
        landing_info = parse_landing_page(landing.text, str(landing.url))
        if not self.page_guard():
            return self.result
        entry = await self.http.get(ENTRY_URL)
        if entry.status_code != 200 or str(entry.url).rstrip("/") != ENTRY_URL:
            raise ValueError(f"IBM official careers search failed: HTTP {entry.status_code} -> {entry.url}")
        official = parse_official_entry(entry.text, str(entry.url))
        entry_seconds = time.perf_counter() - listing_started

        rows, listing_seconds = await self._collect_listing()
        if not rows:
            self.result.errors.append("search_returned_no_rows")
            self.result.listing_complete = False
            self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
            self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
            return self.result

        detail_seconds, parse_seconds, failed = await self._collect_details(rows)

        # Final membership recheck: re-sweep the static title sort and catch up
        # any posting that appeared while the detail phase was running.
        known = {row["job_id"] for row in rows}
        try:
            final_reconciliation = await self._final_reconciliation(known)
        except Exception as exc:
            final_reconciliation = {"error": f"{type(exc).__name__}: {exc}"}
            self.result.errors.append(f"final_listing_recheck_failed:{type(exc).__name__}: {exc}")
        rounds = final_reconciliation.get("rounds") or []
        last_round = rounds[-1] if rounds else {}
        advertised_totals = self.result.metadata.get("advertised_totals_observed") or []
        advertised_total = advertised_totals[0] if advertised_totals else self.result.expected_total
        if last_round and advertised_totals and last_round.get("total") != advertised_totals[-1]:
            self.result.warnings.append(
                f"search_total_drifted_during_run:{advertised_totals[-1]}->{last_round.get('total')}")
        if final_reconciliation.get("catch_up"):
            self.result.warnings.append("postings_added_during_run_were_caught_up")
        if final_reconciliation.get("catch_up_skipped"):
            self.result.warnings.append("postings_added_during_run_not_caught_up")
        if advertised_total is not None and len(rows) != advertised_total:
            self.result.warnings.append(
                f"membership_delta_vs_first_advertised_total:{len(rows)}!={advertised_total}")

        verified = sum(job.description_verified for job in self.result.jobs)
        collected_ids = [job.job_id for job in self.result.jobs if job.job_id]
        self.result.listing_complete = (
            not self.result.errors
            and self.result.expected_total is not None
            and len(rows) >= self.result.expected_total
            and len(collected_ids) == len(set(collected_ids))
            and len(self.result.jobs) == len(rows)
            and verified == len(self.result.jobs))
        self.result.timings["entry_phase_seconds"] = round(entry_seconds, 6)
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["detail_phase_seconds"] = round(detail_seconds, 6)
        self.result.timings["detail_parse_seconds"] = round(parse_seconds, 6)
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": ENTRY_URL,
            "official_landing_url": LANDING_URL,
            "search_api_url": SEARCH_API_URL,
            "official_entry": official,
            "official_landing": landing_info,
            "membership_authority": (
                "IBM's own /careers/search page posts an unfiltered careers2 query to "
                "www-api.ibm.com/search/api/v2 (query.bool.must is empty, no facet filter). "
                "The complete membership is every jobId that query's exact total counts; "
                "the adapter unions the page's own sort modes until the distinct jobId set "
                "equals that total and verifies the page beyond the total is empty."),
            "membership_note": (
                "The default Most Relevant sort orders by live pageview counters, which move "
                "during a deep pagination walk (a single 90-page sweep returned 2,231 distinct "
                "jobIds of 2,695). Static dcdate/title sorts plus the live sort are unioned; "
                "duplicates are dropped by jobId and the union must reach the exact total."),
            "listing_filter_behavior": (
                "no keyword and no facet filter applied; Remote/Hybrid, Career Areas, Experience "
                "level and Location aggregations are recorded as reporting-only evidence"),
            "ui_pagination": (
                "the page renders '1 - N of M items' with 10/20/30 items-per-page; the API "
                "paginates with from/size=30. One page beyond the total is requested to prove "
                "there is no hidden numeric pagination page."),
            "search_total": self.result.expected_total,
            "membership_reconciliation": {
                "advertised_total_first_sweep": advertised_total,
                "advertised_totals_observed": advertised_totals,
                "expected_total_basis": self.result.metadata.get("expected_total_basis"),
                "sweeps": self.result.metadata.get("listing_sweeps", []),
                "collected_ids": len(rows),
                "collected_jobs": len(self.result.jobs),
                "verified_descriptions": verified,
                "complete": bool(self.result.listing_complete),
                "final_recheck": final_reconciliation,
            },
            "listing_sweeps": self.result.metadata.get("listing_sweeps", []),
            "search_jobIds_in_order": [row["job_id"] for row in rows],
            "final_listing_reconciliation": final_reconciliation,
            "detail_url_pattern": (
                "listing exposes https://careers.ibm.com/careers/JobDetail?jobId=<id>; the "
                "canonical pretty URL is /en_US/careers/JobDetail/<slug>/<jobId> (slug ignored)"),
            "description_source": (
                "exact exposed posting's JobDetailWidget fragment (the same markup the JobDetail "
                "page renders into #jobDetailWidget), including every labeled role section and "
                "the posting's unlabeled IBM business-unit/about/EEO paragraphs. The search "
                "`description` is a truncated snippet and is never used as the description."),
            "detail_transport": (
                "careers.ibm.com answers plain HTTP clients with an AWS WAF JavaScript challenge "
                "(HTTP 202 + challenge script). Details are fetched from one WAF-warmed headless "
                "Chromium context (browser-context requests sharing the challenge cookie). No "
                "CAPTCHA was solved, no token was forged, and a 429 stops the detail phase."),
            "application_links": (
                "each page's Apply anchor (Login?jobId=<id>) is retained as apply_url; no login "
                "or application form was opened"),
            "dates": (
                "datePosted from the JobDetail JSON-LD JobPosting, cross-checked with the "
                "metadata 'Date posted' field and the search dcdate; no updated/validThrough or "
                "template dates are invented"),
            "salary": (
                "Projected Minimum/Maximum Salary metadata retained with exact raw strings; the "
                "page does not state a currency, so currency is null and no conversion is made"),
            "detail_concurrency": self.options.detail_concurrency,
            "detail_concurrency_note": (
                "two browser-context GETs per posting, detail_concurrency postings in flight, one "
                "page/context for the whole run"),
            "detail_requests_fetched": self.detail_fetched,
            "detail_records_reused_from_cache": self.detail_reused,
            "detail_fetch_seconds_sum": round(self.detail_request_seconds, 6),
            "detail_reused_seconds_sum": round(self.detail_reused_seconds, 6),
            "detail_failed_jobIds": failed,
            "listing_requests": len(self.result.metadata.get("search_pages", [])),
            "detail_requests": len(rows) * 2,
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
        })
        self.result.timings["source_snapshot_phase_seconds"] = round(
            time.perf_counter() - started, 6)
        return self.result
