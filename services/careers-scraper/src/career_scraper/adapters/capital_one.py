"""Capital One's official TalentBrew search page and native job details.

The official Search Jobs page publishes a company-scoped SearchAsService
listing. Its loaded ``search.js`` uses ``/search-jobs/results`` and the
``tb-corepack.js`` ``bindQueryParams`` serializer. The current interactive
stepper state is enumerated without keyword/location/facet filters; each
listed role is hydrated only from its exact Capital One detail URL. The
Workday URL on a detail page is retained as the exact Apply destination and
is never used as a source of job membership.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape
import hashlib
import json
import math
import re
import time
from pathlib import PurePosixPath
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from ..attachments import extract_attachment
from .base import Adapter


OFFICIAL_ENTRY = "https://www.capitalonecareers.com/search-jobs"
HOST = "www.capitalonecareers.com"
SITE_ID = "57198"
TENANT_ID = "234"
PRIMARY_JOB_ORG_ID = "1732"
PAGE_SIZE = 15
SEARCH_RESULTS_PATH = "/search-jobs/results"
SEARCH_RESULTS_POST_PATH = "/search-jobs/resultspost"
SEARCH_JS_URL = "https://tbcdn.talentbrew.com/js/client/search.js"
COREPACK_URL = "https://tbcdn.talentbrew.com/bundles/tb-corepack.js"
SEARCH_SCRIPT_TOKENS = ("bindQueryParams", "data-ajax-url", "data-ajax-post-url", "searchOptions.CurrentPage")
COREPACK_SCRIPT_TOKENS = ("bindQueryParams=function", "encodeURIComponent", "buildQueryParams")
VERIFICATION_REVISION = "capital_one.official-searchjobs-stepper-xhr-all-orgs-native-detail.2"
JOB_PATH = re.compile(r"^/job/[^/]+/[^/]+/(?P<organization_id>\d+)/(?P<job_id>\d+)$")
JOB_ID = re.compile(r"^\d{8,}$")
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
})


class _Undefined:
    pass


UNDEFINED = _Undefined()


def _int_attr(element, name: str, label: str) -> int:
    raw = element.get(name)
    if raw is None:
        raise ValueError(f"Capital One {label} is missing {name}")
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"Capital One {label} {name} is not an integer: {raw!r}") from exc


def _str_attr(element, name: str, label: str) -> str:
    raw = element.get(name)
    if raw is None:
        raise ValueError(f"Capital One {label} is missing {name}")
    return str(raw)


def _meta(soup: BeautifulSoup, name: str, *, attr: str = "name") -> str | None:
    node = soup.find("meta", attrs={attr: name})
    value = node.get("content") if node else None
    return str(value).strip() if value is not None else None


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _inner_text(root: Tag) -> str:
    """Approximate browser innerText without inserting spaces between inline tags."""
    pieces: list[str] = []

    def visit(node):
        if isinstance(node, NavigableString):
            pieces.append(str(node))
            return
        if not isinstance(node, Tag) or node.name in {"script", "style", "noscript"}:
            return
        if node.name in BLOCK_TAGS:
            pieces.append("\n")
        if node.name == "br":
            pieces.append("\n")
        else:
            for child in node.children:
                visit(child)
        if node.name in BLOCK_TAGS:
            pieces.append("\n")

    visit(root)
    return "".join(pieces).strip()


def _rendered_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    return _compact(_inner_text(soup))


def rendered_fnv1a64(value: str) -> str:
    checksum = 14695981039346656037
    for character in value:
        checksum = ((checksum ^ ord(character)) * 1099511628211) & ((1 << 64) - 1)
    return f"{checksum:016x}"


def _inner_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "lxml")
    pieces: list[str] = []

    def visit(node):
        if isinstance(node, NavigableString):
            pieces.append(str(node))
            return
        if not isinstance(node, Tag) or node.name in {"script", "style", "noscript"}:
            return
        if node.name in BLOCK_TAGS:
            pieces.append("\n")
        if node.name == "br":
            pieces.append("\n")
        else:
            for child in node.children:
                visit(child)
        if node.name in BLOCK_TAGS:
            pieces.append("\n")

    visit(soup)
    return [_compact(line) for line in "".join(pieces).splitlines() if line.strip()]


def _float_or_default(value, default):
    if value is None or value == "":
        return default
    try:
        return f"{float(value):.5f}"
    except (TypeError, ValueError):
        return default


def _parsed_refined_keywords(value: str) -> list:
    try:
        parsed = json.loads(value or "[]")
    except ValueError as exc:
        raise ValueError("Capital One refined keyword state is not valid JSON") from exc
    if not isinstance(parsed, list):
        raise ValueError("Capital One refined keyword state is not a list")
    return parsed


def search_criteria(config: dict, *, current_page: int, is_pagination: bool = True) -> dict:
    """Mirror the official search.js mapSearchOptions/stepper criteria."""
    content_results_raw = config.get("data-total-content-results")
    if content_results_raw is None:
        content_results = UNDEFINED
        content_pages = float("nan")
    else:
        content_results = int(content_results_raw)
        content_pages = math.ceil(content_results / int(config["data-records-per-page"]))
    return {
        "ActiveFacetID": int(config["data-active-facet-id"]),
        "CurrentPage": current_page,
        "RecordsPerPage": int(config["data-records-per-page"]),
        "TotalContentResults": content_results,
        "Distance": int(config["data-distance"]),
        "RadiusUnitType": int(config.get("data-radius-unit-type") or 0),
        "Keywords": config.get("data-keywords") or "",
        "Location": config.get("data-location") or "",
        "Latitude": _float_or_default(config.get("data-latitude"), None),
        "Longitude": _float_or_default(config.get("data-longitude"), None),
        "ShowRadius": config.get("data-show-radius") or "False",
        "IsPagination": "True" if is_pagination else "False",
        "CustomFacetName": config.get("data-custom-facet-name") or "",
        "FacetTerm": config.get("data-facet-term") or "",
        "FacetType": int(config["data-facet-type"]),
        "FacetFilters": [],
        "SearchResultsModuleName": config["data-search-results-module-name"],
        "SearchFiltersModuleName": config.get("data-search-filters-module-name") or "Search Filters",
        "SortCriteria": int(config["data-sort-criteria"]),
        "SortDirection": int(config["data-sort-direction"]),
        "SearchType": int(config["data-search-type"]),
        "CategoryFacetTerm": None,
        "CategoryFacetType": None,
        "LocationFacetTerm": None,
        "LocationFacetType": None,
        "KeywordType": None,
        "LocationType": None,
        "LocationPath": None,
        "OrganizationIds": None,
        "RefinedKeywords": _parsed_refined_keywords(config.get("data-refined-keywords", "[]")),
        "PostalCode": UNDEFINED,
        "ResultsType": int(config["data-results-type"]),
        "fc": config.get("data-filtered-categories", ""),
        "fl": config.get("data-filtered-locations", ""),
        "fcf": config.get("data-filtered-custom-facet", ""),
        "afc": config.get("data-filtered-advanced-categories", ""),
        "afl": config.get("data-filtered-advanced-locations", ""),
        "afcf": config.get("data-filtered-advanced-custom-facet", ""),
        "TotalContentPages": content_pages,
    }


def bind_query_params(criteria: dict) -> str:
    """Replicate the loaded ``$.bindQueryParams`` serializer."""
    parts: list[str] = []

    def emit(name, value):
        if value is UNDEFINED or value is None:
            value = ""
        elif isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, float) and math.isnan(value):
            value = "NaN"
        safe = "-_.!~*'()"
        parts.append(f"{quote(str(name), safe=safe)}={quote(str(value), safe=safe)}")

    def build(name, value):
        if isinstance(value, list):
            for index, item in enumerate(value):
                build(f"{name}[{index}]", item)
        elif isinstance(value, dict):
            for key, item in value.items():
                build(f"{name}.{key}", item)
        elif value is None:
            return
        else:
            emit(name, value)

    for key, value in criteria.items():
        if key in {"TotalPages", "TotalResults", "StaticFacets"}:
            continue
        build(key, value)
    return "&".join(parts).replace("%20", "+")


def _criteria_evidence(criteria: dict) -> dict:
    """Return a JSON-safe copy while preserving JS undefined/NaN distinctions."""
    def value(item):
        if item is UNDEFINED:
            return "<undefined>"
        if isinstance(item, float) and math.isnan(item):
            return "NaN"
        if isinstance(item, list):
            return [value(part) for part in item]
        if isinstance(item, dict):
            return {key: value(part) for key, part in item.items()}
        return item

    return {key: value(item) for key, item in criteria.items()}


def _parse_location_objects(value) -> list[dict]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    locations = []
    for item in items:
        if not isinstance(item, dict):
            continue
        address = item.get("address", {}) if isinstance(item.get("address"), dict) else {}
        locations.append({
            "locality": address.get("addressLocality"),
            "region": address.get("addressRegion"),
            "country": address.get("addressCountry"),
            "street_address": address.get("streetAddress"),
            "postal_code": address.get("postalCode"),
            "display": ", ".join(str(v) for v in (address.get("addressLocality"), address.get("addressRegion")) if v),
        })
    return locations


def _salary_from_lines(lines: list[str]):
    expression = re.compile(
        r"^(?P<location>[^:\n]+):\s*(?P<symbol>\$)(?P<minimum>[\d,]+)\s*[-–]\s*\$"
        r"(?P<maximum>[\d,]+)(?:\s+for\s+(?P<title>.+))?$"
    )
    values = []
    for line in lines:
        match = expression.match(line.strip())
        if match:
            values.append({
                "location": match.group("location").strip(),
                "minimum": int(match.group("minimum").replace(",", "")),
                "maximum": int(match.group("maximum").replace(",", "")),
                "currency_symbol": match.group("symbol"),
                "basis": "annual full-time",
                "source_text": line.strip(),
            })
    return values or None


def _extract_section(text: str, start_marker: str, stop_markers: tuple[str, ...]):
    start = text.find(start_marker)
    if start < 0:
        return None
    start += len(start_marker)
    stops = [index for marker in stop_markers if (index := text.find(marker, start)) >= 0]
    end = min(stops) if stops else len(text)
    value = text[start:end].strip()
    return value or None


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _parse_cards(results_html: str, *, page_label: str) -> list[dict]:
    soup = BeautifulSoup(results_html or "", "lxml")
    rows = []
    for anchor in soup.select("a[data-job-id][href]"):
        job_id = str(anchor.get("data-job-id", "")).strip()
        parsed = urlsplit(anchor.get("href", ""))
        match = JOB_PATH.fullmatch(parsed.path)
        if (not JOB_ID.fullmatch(job_id) or not match
                or match.group("job_id") != job_id):
            raise ValueError(f"Capital One {page_label} has an invalid native job link: {anchor.get('href')!r}")
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError(f"Capital One {page_label} exposed an unexpected job link origin: {anchor.get('href')!r}")
        title_node = anchor.select_one("h2")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        date_node = anchor.select_one(".job-date-posted")
        location_node = anchor.select_one(".job-location")
        if not title:
            raise ValueError(f"Capital One {page_label} job {job_id} is missing its title")
        rows.append({
            "job_id": job_id,
            "href": parsed.path,
            "title": title,
            "listing_date_raw": date_node.get_text(" ", strip=True) if date_node else None,
            "listing_location_raw": location_node.get_text(" ", strip=True) if location_node else None,
            # Capital One publishes most roles under job org 1732 but also
            # legitimate roles (e.g. Mexico/student programs) under 31238.
            # Keep the org id from the exact listed href and verify it on the
            # detail page instead of assuming the primary org.
            "organization_id": match.group("organization_id"),
        })
    ids = [row["job_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Capital One {page_label} repeats a job ID")
    return rows


def _entry_identity(html: str, final_url: str) -> tuple[dict, list[dict], dict]:
    parsed_url = urlsplit(final_url)
    soup = BeautifulSoup(html or "", "lxml")
    if parsed_url.scheme != "https" or parsed_url.hostname != HOST or parsed_url.path != "/search-jobs":
        raise ValueError("Capital One scraper must start from its official HTTPS /search-jobs entry")
    if soup.title is None or "Capital One" not in soup.title.get_text(" ", strip=True):
        raise ValueError("Capital One official search entry title is missing")
    if _meta(soup, "hostName") != HOST or _meta(soup, "career-site-full-domain") != HOST:
        raise ValueError("Capital One official search metadata host identity changed")
    if (_meta(soup, "site-id") != SITE_ID or _meta(soup, "site-organization-id") != TENANT_ID
            or _meta(soup, "site-tenant-id") != TENANT_ID
            or _meta(soup, "search-client-type") != "SearchAsService"):
        raise ValueError("Capital One official listing site identity changed")
    wrapper = soup.select_one("#search-results")
    filters = soup.select_one("#search-filters")
    if wrapper is None or filters is None:
        raise ValueError("Capital One official entry lacks search-results/search-filters wrappers")
    total = _int_attr(wrapper, "data-total-results", "search-results")
    total_jobs = _int_attr(wrapper, "data-total-job-results", "search-results")
    if total != total_jobs or total < 0:
        raise ValueError("Capital One SSR job/content totals are invalid or ambiguous")
    config = {key: str(value) for key, value in wrapper.attrs.items() if key.startswith("data-")}
    config.update({key: str(value) for key, value in filters.attrs.items() if key.startswith("data-")})
    config.update({
        "ssr_total": total,
        "ssr_total_pages": _int_attr(wrapper, "data-total-pages", "search-results"),
        "ssr_current_page": _int_attr(wrapper, "data-current-page", "search-results"),
        "ssr_records_per_page": _int_attr(wrapper, "data-records-per-page", "search-results"),
        "ajax_url": _str_attr(wrapper, "data-ajax-url", "search-results"),
        "ajax_post_url": _str_attr(wrapper, "data-ajax-post-url", "search-results"),
        "ajax_char_limit": _int_attr(filters, "data-ajax-char-limit", "search-filters"),
        "site_id": _meta(soup, "site-id"),
        "tenant_id": _meta(soup, "site-tenant-id"),
        "organization_id": _meta(soup, "site-organization-id"),
        "site_page_type": _meta(soup, "career-site-page-type"),
        "search_client_type": _meta(soup, "search-client-type"),
    })
    ajax = urlsplit(urljoin(OFFICIAL_ENTRY, config["ajax_url"]))
    post = urlsplit(urljoin(OFFICIAL_ENTRY, config["ajax_post_url"]))
    if (ajax.scheme != "https" or ajax.hostname != HOST or ajax.path != SEARCH_RESULTS_PATH
            or post.scheme != "https" or post.hostname != HOST or post.path != SEARCH_RESULTS_POST_PATH):
        raise ValueError("Capital One official page AJAX endpoints changed from the observed same-site results routes")
    if (config["ssr_current_page"] != 1 or config["ssr_records_per_page"] != PAGE_SIZE
            or config["ssr_total_pages"] != math.ceil(total / PAGE_SIZE)):
        raise ValueError("Capital One SSR pagination metadata is internally inconsistent")
    if (config["data-keywords"] or config["data-location"] or config["data-organization-ids"]
            or config["data-active-facet-id"] != "0" or config["data-results-type"] != "0"
            or config["data-refined-keywords"] != "[]"
            or any(config.get(f"data-filtered-{part}", "") for part in
                   ("categories", "locations", "custom-facet", "advanced-categories", "advanced-locations", "advanced-custom-facet"))):
        raise ValueError("Capital One official SSR page is not the unfiltered global jobs listing")
    org = soup.select_one('form.search-form input[name="orgIds"]')
    if org is None or org.get("value") != TENANT_ID:
        raise ValueError("Capital One official search form organization ID changed")
    if any(box.has_attr("checked") for box in soup.select(".filter-checkbox")):
        raise ValueError("Capital One official SSR page has a selected facet filter")
    script_urls = [urljoin(OFFICIAL_ENTRY, node.get("src", "")) for node in soup.select("script[src]")]
    search_script = next((value for value in script_urls if value.endswith("/js/client/search.js")), None)
    corepack_script = next((value for value in script_urls if value.endswith("/bundles/tb-corepack.js")), None)
    if not search_script or not corepack_script:
        raise ValueError("Capital One official entry does not load its observed TalentBrew search client/corepack")
    ssr_rows = _parse_cards(str(wrapper), page_label="server-rendered page 1")
    if len(ssr_rows) != min(PAGE_SIZE, total):
        raise ValueError("Capital One server-rendered first page rows do not reconcile to its page size")
    analytics_total = _meta(soup, "search-analytics-total-jobs")
    analytics_ids_raw = _meta(soup, "search-analytics-jobIds")
    try:
        analytics_ids = list(json.loads(analytics_ids_raw).keys()) if analytics_ids_raw else []
    except (TypeError, ValueError) as exc:
        raise ValueError("Capital One SSR analytics job-ID field is not valid JSON") from exc
    if analytics_ids and set(analytics_ids) != {row["job_id"] for row in ssr_rows}:
        raise ValueError("Capital One SSR analytics job IDs do not match the visible first-page rows")
    config["ssr_analytics_total"] = int(analytics_total) if analytics_total and analytics_total.isdigit() else None
    config["ssr_analytics_job_ids"] = analytics_ids
    return config, ssr_rows, {"search_script_url": search_script, "corepack_script_url": corepack_script}


def _parse_results_payload(payload: dict, *, requested_page: int) -> tuple[dict, list[dict]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), str) or not isinstance(payload.get("filters"), str):
        raise ValueError(f"Capital One page {requested_page} results payload lacks results/filters HTML")
    if payload.get("hasJobs") is not True:
        raise ValueError(f"Capital One dynamic page {requested_page} does not report jobs")
    soup = BeautifulSoup(payload["results"], "lxml")
    wrapper = soup.select_one("#search-results")
    filter_soup = BeautifulSoup(payload["filters"], "lxml")
    filter_wrapper = filter_soup.select_one("#search-filters")
    if wrapper is None or filter_wrapper is None:
        raise ValueError(f"Capital One dynamic page {requested_page} lacks result/filter wrappers")
    total = _int_attr(wrapper, "data-total-results", f"dynamic page {requested_page}")
    total_jobs = _int_attr(wrapper, "data-total-job-results", f"dynamic page {requested_page}")
    total_pages = _int_attr(wrapper, "data-total-pages", f"dynamic page {requested_page}")
    current_page = _int_attr(wrapper, "data-current-page", f"dynamic page {requested_page}")
    per_page = _int_attr(wrapper, "data-records-per-page", f"dynamic page {requested_page}")
    if total != total_jobs or per_page != PAGE_SIZE or current_page != requested_page:
        raise ValueError(f"Capital One dynamic page {requested_page} changed total/job/page-size/current-page metadata")
    if total_pages != math.ceil(total / PAGE_SIZE):
        raise ValueError(f"Capital One dynamic page {requested_page} has an inconsistent total page count")
    if (wrapper.get("data-keywords", "") or wrapper.get("data-location", "")
            or wrapper.get("data-organization-ids", "") or wrapper.get("data-active-facet-id") != "0"
            or wrapper.get("data-results-type") != "0" or wrapper.get("data-refined-keywords") != "[]"
            or wrapper.get("data-search-type") != "5"
            or wrapper.get("data-search-results-module-name") != "Search Results"
            or wrapper.get("data-ajax-url") != SEARCH_RESULTS_PATH
            or wrapper.get("data-ajax-post-url") != SEARCH_RESULTS_POST_PATH):
        raise ValueError(f"Capital One dynamic page {requested_page} carries search/filter state or changed endpoints")
    if any(box.has_attr("checked") for box in filter_soup.select(".filter-checkbox")):
        raise ValueError(f"Capital One dynamic page {requested_page} carries a selected facet filter")
    for name in ("data-filtered-categories", "data-filtered-locations", "data-filtered-custom-facet",
                 "data-filtered-advanced-categories", "data-filtered-advanced-locations",
                 "data-filtered-advanced-custom-facet"):
        if filter_wrapper.get(name, ""):
            raise ValueError(f"Capital One dynamic page {requested_page} has a filtered facet state")
    rows = _parse_cards(payload["results"], page_label=f"dynamic page {requested_page}")
    expected_records = min(PAGE_SIZE, max(0, total - (requested_page - 1) * PAGE_SIZE))
    if len(rows) != expected_records:
        raise ValueError(f"Capital One dynamic page {requested_page} expected {expected_records} rows, got {len(rows)}")
    return {"total": total, "total_pages": total_pages, "current_page": current_page,
            "records_per_page": per_page, "results_type": wrapper.get("data-results-type"),
            "active_facet_id": wrapper.get("data-active-facet-id")}, rows


def _salary_from_lines(lines: list[str]):
    expression = re.compile(
        r"^(?P<location>[^:\n]+):\s*(?P<symbol>\$)(?P<minimum>[\d,]+)\s*[-–]\s*\$"
        r"(?P<maximum>[\d,]+)(?:\s+for\s+(?P<title>.+))?$"
    )
    ranges = []
    for line in lines:
        match = expression.match(line.strip())
        if match:
            ranges.append({
                "location": match.group("location").strip(),
                "minimum": int(match.group("minimum").replace(",", "")),
                "maximum": int(match.group("maximum").replace(",", "")),
                "currency_symbol": match.group("symbol"),
                "basis": "annual full-time",
                "source_text": line.strip(),
            })
    return ranges or None


def _extract_section(text: str, start_marker: str, stop_markers: tuple[str, ...]):
    start = text.find(start_marker)
    if start < 0:
        return None
    start += len(start_marker)
    stops = [index for marker in stop_markers if (index := text.find(marker, start)) >= 0]
    end = min(stops) if stops else len(text)
    value = text[start:end].strip()
    return value or None


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        pass
    match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _parse_detail(html: str, *, expected_job_id: str, expected_title: str, expected_url: str,
                  expected_org_id: str = PRIMARY_JOB_ORG_ID) -> dict:
    soup = BeautifulSoup(html or "", "lxml")
    section = soup.select_one("section.job-description")
    ats = section.select_one(".ats-description") if section else None
    if section is None or ats is None:
        raise ValueError(f"Capital One detail {expected_job_id} lacks its full .ats-description field")
    if (section.get("data-job-id") != expected_job_id
            or section.get("data-org-id") != str(expected_org_id)):
        raise ValueError(
            f"Capital One detail {expected_job_id} has a different job/org identity "
            f"({section.get('data-job-id')}/{section.get('data-org-id')} != "
            f"{expected_job_id}/{expected_org_id})"
        )
    h1 = section.select_one("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    if title != expected_title:
        raise ValueError(f"Capital One detail {expected_job_id} title does not match its exact listing row")
    current_id = _meta(soup, "search-analytics-currentJobId")
    gtm_id = _meta(soup, "gtm_jobid")
    meta_req_id = _meta(soup, "job-ats-req-id")
    gtm_req_id = _meta(soup, "gtm_reqid")

    jsonld = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            parsed = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        entities = parsed if isinstance(parsed, list) else [parsed]
        jsonld = next((item for item in entities if isinstance(item, dict) and item.get("@type") == "JobPosting"), None)
        if jsonld:
            break
    if jsonld is None:
        raise ValueError(f"Capital One detail {expected_job_id} has no public JobPosting JSON-LD")
    identifier = jsonld.get("identifier") if isinstance(jsonld.get("identifier"), dict) else {}
    req_id = identifier.get("value")
    if not req_id or req_id != meta_req_id or req_id != gtm_req_id:
        raise ValueError(f"Capital One detail {expected_job_id} requisition fields disagree")
    if current_id != expected_job_id or gtm_id != expected_job_id:
        raise ValueError(f"Capital One detail {expected_job_id} page/analytics job IDs disagree")
    if jsonld.get("url") != expected_url:
        raise ValueError(f"Capital One detail {expected_job_id} canonical job URL changed")

    apply_meta = _meta(soup, "search-job-apply-url")
    apply_link = section.select_one("a[data-selector-name='job-apply-link'][href]")
    apply_url = apply_link.get("href") if apply_link else None
    apply_data_url = apply_link.get("data-apply-url") if apply_link else None
    apply_parts = urlsplit(apply_url or "")
    if (not apply_url or apply_meta != apply_url or apply_data_url != apply_url
            or apply_parts.scheme != "https" or apply_parts.hostname != "capitalone.wd12.myworkdayjobs.com"
            or not apply_parts.path.startswith("/Capital_One/job/")
            or not apply_parts.path.endswith("/apply")):
        raise ValueError(f"Capital One detail {expected_job_id} apply link is absent or inconsistent")

    description_html = ats.decode_contents().strip()
    description_text = _rendered_text(description_html)
    if len(description_text) < 100:
        raise ValueError(f"Capital One detail {expected_job_id} has a short/truncated .ats-description")
    visible_date = section.select_one(".job-description__info .job-date")
    posted_label = visible_date.get_text(" ", strip=True) if visible_date else None
    label_date = _parse_date(posted_label)
    schema_date = _parse_date(jsonld.get("datePosted"))
    # Both values are published by Capital One; some reposted/refreshed roles
    # carry an older JSON-LD datePosted than the visible "Posted" label. Keep
    # both exact values, prefer the visible label, and flag the disagreement
    # instead of discarding an otherwise complete official detail.
    date_posted = label_date or schema_date
    date_posted_conflict = bool(label_date and schema_date and label_date != schema_date)
    location_text_node = section.select_one(".job-description__secondary-info .job-location")
    location_text = location_text_node.get_text(" ", strip=True) if location_text_node else ""
    display_locations = [part.strip() for part in re.split(r"\s*\|\s*", location_text) if part.strip()]
    locations = _parse_location_objects(jsonld.get("jobLocation"))
    category_node = next((item for item in section.select(".job-description-overview__item")
                          if (item.select_one(".job-description-overview__title")
                              and item.select_one(".job-description-overview__title").get_text(" ", strip=True) == "Category")), None)
    category_text = category_node.select_one(".job-description-overview__text") if category_node else None
    category = category_text.get_text(" ", strip=True) if category_text else _meta(soup, "gtm_tbcn_jobcategory")
    experience_node = next((item for item in section.select(".job-description-overview__item")
                            if (item.select_one(".job-description-overview__title")
                                and item.select_one(".job-description-overview__title").get_text(" ", strip=True) == "Experience")), None)
    experience_text = experience_node.select_one(".job-description-overview__text") if experience_node else None
    experience = experience_text.get_text(" ", strip=True) if experience_text else None
    body_lines = _inner_lines(description_html)
    responsibilities = _extract_section(
        description_text,
        "Responsibilities include, but are not limited to, the following:",
        ("Basic Qualifications:",),
    )
    qualifications = _extract_section(
        description_text,
        "Basic Qualifications:",
        ("At this time, Capital One", "The minimum and maximum full-time annual salaries"),
    )
    hiring_organization = jsonld.get("hiringOrganization")
    employer = (hiring_organization.get("name")
                if isinstance(hiring_organization, dict) else None)
    if not employer:
        # Some student/internship details omit hiringOrganization from their
        # JSON-LD; the page's own official site name is still public evidence.
        site_name = _meta(soup, "og:site_name", attr="property")
        if site_name:
            employer = re.sub(r"\s+Careers$", "", site_name).strip() or site_name
    relevant_meta = {name: _meta(soup, name) for name in (
        "search-analytics-currentJobId", "search-job-apply-url", "search-job-mobile-apply-url",
        "search-job-feed-id", "job-category-ids", "job-location-ids", "job-ats-req-id",
        "job-tbcn-job-title", "gtm_jobid", "gtm_reqid", "gtm_orgid", "gtm_tbcn_division",
        "gtm_tbcn_jobcategory", "gtm_tbcn_location", "gtm_tbcn_jobtitle", "gtm_firstindex",
        "site-id", "site-tenant-id", "site-organization-id", "site-page-id", "site-page-type",
    )}
    return {
        "title": title,
        "requisition_id": req_id,
        "apply_url": apply_url,
        "date_posted": date_posted,
        "date_posted_raw": {"visible_label": posted_label, "schema_datePosted": jsonld.get("datePosted")},
        "date_posted_conflict": date_posted_conflict,
        "valid_through": _parse_date(jsonld.get("validThrough")),
        "locations": locations,
        "display_locations": display_locations,
        "department": category or None,
        "experience": experience,
        "employment_type": jsonld.get("industry") or jsonld.get("employmentType"),
        "workplace_type": None,
        "salary": _salary_from_lines(body_lines),
        "qualifications": qualifications,
        "responsibilities": responsibilities,
        "education": None,
        "employer": employer,
        "language": _meta(soup, "site-current-language") or None,
        "description_html": description_html,
        "description_text": description_text,
        "description_sha256": hashlib.sha256(description_html.encode("utf-8")).hexdigest(),
        "jsonld": jsonld,
        "source_meta": relevant_meta,
        "detail_page_timestamp": _meta(soup, "site-page-timestamp"),
    }


class CapitalOne(Adapter):
    """Current Capital One stepper-state listings and exact native details."""

    VERIFICATION_REVISION = VERIFICATION_REVISION

    async def _get_official_entry(self):
        response = await self.http.get(
            OFFICIAL_ENTRY,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        if str(response.url).rstrip("/") != OFFICIAL_ENTRY:
            raise ValueError(f"Capital One official Search Jobs entry redirected: {response.url}")
        config, rows, script_info = _entry_identity(response.text, str(response.url))
        return response, config, rows, script_info

    async def _verify_client_scripts(self, script_info: dict):
        search_response = await self.http.get(script_info["search_script_url"])
        corepack_response = await self.http.get(script_info["corepack_script_url"])
        missing_search = [token for token in SEARCH_SCRIPT_TOKENS if token not in search_response.text]
        missing_corepack = [token for token in COREPACK_SCRIPT_TOKENS if token not in corepack_response.text]
        if missing_search or missing_corepack:
            raise ValueError(
                f"Capital One TalentBrew request builder changed; missing search={missing_search} corepack={missing_corepack}"
            )
        return {
            "search_script_url": str(search_response.url),
            "search_script_sha256": hashlib.sha256(search_response.content).hexdigest(),
            "corepack_script_url": str(corepack_response.url),
            "corepack_script_sha256": hashlib.sha256(corepack_response.content).hexdigest(),
        }

    async def _listing_page(self, *, endpoint: str, config: dict, page: int):
        criteria = search_criteria(config, current_page=page, is_pagination=True)
        query = bind_query_params(criteria)
        if len(query) > config["ajax_char_limit"]:
            raise ValueError("Capital One unfiltered query unexpectedly exceeds the observed AJAX GET limit")
        url = f"{endpoint}?{query}"
        response = await self.http.get(url, headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": OFFICIAL_ENTRY,
        })
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Capital One AJAX page {page} did not return JSON") from exc
        summary, rows = _parse_results_payload(payload, requested_page=page)
        return response, criteria, payload, summary, rows

    def _add_listing_rows(self, rows: list[dict], *, source_page: int):
        for row in rows:
            url = urljoin(OFFICIAL_ENTRY, row["href"])
            raw = {"listing": {**row, "listing_page": source_page}}
            job = self.job(
                raw, title=row["title"], url=url, job_id=row["job_id"],
                locations=[row["listing_location_raw"]] if row.get("listing_location_raw") else [],
                date_posted_raw={"listing_date": row.get("listing_date_raw")},
            )
            # A listing row alone has no full description; only a successful
            # exact detail fetch clears this pending state.
            job.description_unavailable = True
            self.add(job)

    async def _detail(self, job):
        response = await self.http.get(
            job.url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                     "Referer": OFFICIAL_ENTRY},
        )
        listing = job.raw.get("listing") if isinstance(job.raw.get("listing"), dict) else job.raw
        expected_org_id = str(listing.get("organization_id") or "")
        if not expected_org_id.isdigit():
            raise ValueError(f"Capital One detail {job.job_id} has no listed organization id")
        detail = _parse_detail(response.text, expected_job_id=str(job.job_id),
                               expected_title=job.title, expected_url=job.url,
                               expected_org_id=expected_org_id)
        identity = detail["source_meta"]
        checks = {
            "detail_listing_job_id_matches": identity.get("search-analytics-currentJobId") == job.job_id,
            "detail_gtm_job_id_matches": identity.get("gtm_jobid") == job.job_id,
            "detail_title_matches": detail["title"] == job.title,
            "detail_url_matches": detail["jsonld"].get("url") == job.url,
            "detail_requisition_ids_match": (detail["requisition_id"] == identity.get("gtm_reqid")
                                             == identity.get("job-ats-req-id")),
            "detail_apply_links_match": detail["apply_url"] == identity.get("search-job-apply-url"),
            "detail_description_present": len(detail["description_text"]) >= 100,
        }
        job.raw["detail_identity_audit"] = checks
        job.raw["detail"] = {
            "source_url": str(response.url),
            "source_status": response.status_code,
            "source_response_sha256": hashlib.sha256(response.content).hexdigest(),
            "source_response_archive": f"responses/{hashlib.sha256(response.content).hexdigest()}.gz",
            "source_content_type": response.headers.get("content-type"),
            "identity": {"job_id": job.job_id, "requisition_id": detail["requisition_id"],
                         "title": detail["title"], "url": job.url, "apply_url": detail["apply_url"]},
            "source_meta": detail["source_meta"],
            "jsonld": {key: value for key, value in detail["jsonld"].items()
                       if key not in {"description", "educationRequirements"}},
            "jsonld_fields_archived_in_raw_response": ["description", "educationRequirements"],
            "jsonld_field_note": "The complete official HTML/JSON-LD response is preserved by source_response_sha256; duplicate long text is represented by the top-level job.description_html/text.",
            "overview": {"category": detail["department"], "experience": detail["experience"]},
            "display_locations": detail["display_locations"],
            "description_source_selector": ".ats-description",
            "description_source_sha256": detail["description_sha256"],
            "detail_page_timestamp": detail["detail_page_timestamp"],
            "attachments": [],
        }
        job.title = detail["title"]
        job.requisition_id = detail["requisition_id"]
        job.apply_url = detail["apply_url"]
        job.description_html = detail["description_html"]
        job.description_text = detail["description_text"]
        job.description_method = "capital_one.official_search_jobs.ats_description"
        job.description_source_url = str(response.url)
        job.date_posted = detail["date_posted"]
        job.date_posted_raw = {**(job.date_posted_raw or {}), **detail["date_posted_raw"],
                               "sources_disagree": detail["date_posted_conflict"]}
        if detail["date_posted_conflict"]:
            job.warnings.append("capital_one_visible_vs_schema_date_posted_disagree")
        job.valid_through = detail["valid_through"]
        job.locations = detail["locations"] or ([{"display": value} for value in detail["display_locations"]])
        job.department = detail["department"]
        job.team = None
        job.employment_type = detail["employment_type"]
        job.workplace_type = detail["workplace_type"]
        job.salary = detail["salary"]
        job.qualifications = detail["qualifications"]
        job.responsibilities = detail["responsibilities"]
        job.education = detail["education"]
        job.experience = detail["experience"]
        job.employer = detail["employer"]
        job.language = detail["language"]
        if not all(checks.values()):
            for name, passed in checks.items():
                if not passed:
                    self.result.errors.append(f"capital_one_{name}_failed:{job.job_id}")
            job.warnings.append("capital_one_exact_detail_unverified")
            job.description_unavailable = True
            job.description_verified = False
        else:
            job.description_unavailable = False
        return job

    async def _fetch_role_attachments(self):
        suffixes = {".pdf", ".doc", ".docx", ".rtf"}
        for job in self.result.jobs:
            detail = job.raw.get("detail", {})
            if detail.get("attachments_reviewed") is True:
                continue
            soup = BeautifulSoup(job.description_html or "", "lxml")
            attachments = []
            seen = set()
            for anchor in soup.select("a[href]"):
                url = urljoin(job.url, anchor.get("href", ""))
                path = urlsplit(url).path
                if PurePosixPath(path).suffix.lower() not in suffixes or url in seen:
                    continue
                seen.add(url)
                try:
                    response = await self.http.get(url)
                    extracted = extract_attachment(response.content, PurePosixPath(path).name)
                    attachments.append({
                        "url": url, "label": anchor.get_text(" ", strip=True),
                        "source_field": ".ats-description", "content_type": response.headers.get("content-type"),
                        **extracted,
                    })
                    if extracted.get("text"):
                        job.description_html += "\n<pre>" + escape(extracted["text"]) + "</pre>"
                except Exception as exc:
                    self.result.errors.append(
                        f"capital_one_attachment_fetch_failed:{job.job_id}:{url}:{type(exc).__name__}:{exc}"
                    )
                    job.warnings.append("capital_one_role_attachment_unverified")
                    job.description_unavailable = True
                    job.description_verified = False
            detail["attachments"] = attachments
            detail["attachments_reviewed"] = True
            job.raw["detail"] = detail
            if attachments:
                job.description_text = _rendered_text(job.description_html)
                job.validate()

    async def _membership_pass(self, *, endpoint: str, config: dict, expected_total: int,
                               total_pages: int, count_result_pages: bool):
        started = time.perf_counter()
        ids, errors, pages = [], [], []
        for page in range(1, total_pages + 1):
            if count_result_pages and not self.page_guard():
                errors.append("capital_one_initial_page_budget_exhausted")
                break
            try:
                _response, _criteria, _payload, summary, rows = await self._listing_page(
                    endpoint=endpoint, config=config, page=page
                )
            except Exception as exc:
                errors.append(f"capital_one_final_page_failed:{page}:{type(exc).__name__}:{exc}")
                break
            if summary["total"] != expected_total:
                errors.append(f"capital_one_total_changed_on_page_{page}:{expected_total}->{summary['total']}")
            if summary["total_pages"] != total_pages or summary["current_page"] != page:
                errors.append(f"capital_one_final_pagination_changed_on_page_{page}")
            page_ids = [row["job_id"] for row in rows]
            ids.extend(page_ids)
            pages.append({"page": page, "total": summary["total"], "row_count": len(rows),
                          "first_id": page_ids[0] if page_ids else None,
                          "last_id": page_ids[-1] if page_ids else None, "ids": page_ids,
                          "rows": [{key: row[key] for key in
                                    ("job_id", "href", "title", "organization_id")} for row in rows]})
        duplicate_ids = sorted({job_id for job_id in ids if ids.count(job_id) > 1})
        if duplicate_ids:
            errors.append(f"capital_one_final_duplicate_ids:{','.join(duplicate_ids)}")
        unique_ids = sorted(set(ids))
        if len(unique_ids) != expected_total:
            errors.append(f"capital_one_final_membership_count:{len(unique_ids)}/{expected_total}")
        return {
            "total": expected_total,
            "page_count": len(pages),
            "page_size": PAGE_SIZE,
            "row_count": len(ids),
            "unique_count": len(unique_ids),
            "ids": unique_ids,
            "ids_sha256": hashlib.sha256("\n".join(unique_ids).encode()).hexdigest(),
            "pages": pages,
            "errors": errors,
            "duration_seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    async def run(self):
        if self.company.careers_url != OFFICIAL_ENTRY or self.source.url != OFFICIAL_ENTRY:
            raise ValueError(f"Capital One adapter must start at {OFFICIAL_ENTRY}")
        entry_started = time.perf_counter()
        entry, ssr_config, ssr_rows, script_info = await self._get_official_entry()
        self.result.timings["official_entry_seconds"] = round(max(0.0, time.perf_counter() - entry_started), 6)
        script_started = time.perf_counter()
        client_proof = await self._verify_client_scripts(script_info)
        self.result.timings["client_script_fetch_seconds"] = round(max(0.0, time.perf_counter() - script_started), 6)
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_authority": "Capital One's official Search Jobs TalentBrew SearchAsService page; native stepper XHR only.",
            "request_endpoint": urljoin(OFFICIAL_ENTRY, ssr_config["ajax_url"]),
            "request_method": "GET",
            "request_state": "IsPagination=True, set by the official native page-jump stepper; no filters, keywords, or location.",
            "site_identity": {key: ssr_config[key] for key in
                              ("site_id", "tenant_id", "organization_id", "site_page_type", "search_client_type")},
            "client_scripts": client_proof,
            "independent_browser_reference": "companies/capital-one/browser-reference-parent.json",
            "server_rendered_snapshot": {
                "total": ssr_config["ssr_total"], "pages": ssr_config["ssr_total_pages"],
                "page_size": ssr_config["ssr_records_per_page"],
                "first_page_ids": [row["job_id"] for row in ssr_rows],
                "first_id": ssr_rows[0]["job_id"], "last_id": ssr_rows[-1]["job_id"],
                "analytics_total": ssr_config["ssr_analytics_total"],
                "analytics_ids": ssr_config["ssr_analytics_job_ids"],
                "source_page_timestamp": _meta(BeautifulSoup(entry.text, "lxml"), "site-page-timestamp"),
            },
        })
        endpoint = urljoin(OFFICIAL_ENTRY, ssr_config["ajax_url"])
        initial_start = time.perf_counter()
        if not self.page_guard():
            self.result.errors.append("capital_one_initial_page_budget_exhausted")
            self.result.listing_complete = False
            return self.result
        first_response, first_criteria, _payload, first_summary, first_rows = await self._listing_page(
            endpoint=endpoint, config=ssr_config, page=1
        )
        dynamic_total = first_summary["total"]
        dynamic_pages = first_summary["total_pages"]
        self.result.expected_total = dynamic_total
        if dynamic_total <= 0:
            self.result.errors.append("capital_one_current_stepper_listing_empty")
            self.result.listing_complete = False
            return self.result
        if dynamic_total != ssr_config["ssr_total"]:
            self.result.warnings.append(
                f"capital_one_ssr_total_differs_from_current_stepper_xhr:{ssr_config['ssr_total']}->{dynamic_total}"
            )
        self.result.metadata["current_stepper_snapshot"] = {
            "total": dynamic_total, "pages": dynamic_pages, "page_size": PAGE_SIZE,
            "page_one_request_url": str(first_response.url),
            "page_one_first_id": first_rows[0]["job_id"] if first_rows else None,
            "page_one_last_id": first_rows[-1]["job_id"] if first_rows else None,
            "page_one_ids": [row["job_id"] for row in first_rows],
            "query_criteria": _criteria_evidence(first_criteria),
            "is_pagination": "True",
            "root_terminal_reference": "companies/capital-one/browser-reference-parent.json",
        }
        initial_ids = [row["job_id"] for row in first_rows]
        page_summaries = [{
            "page": 1, "total": first_summary["total"], "row_count": len(first_rows),
            "first_id": first_rows[0]["job_id"] if first_rows else None,
            "last_id": first_rows[-1]["job_id"] if first_rows else None,
            "ids": [row["job_id"] for row in first_rows],
            "rows": [{key: row[key] for key in
                      ("job_id", "href", "title", "organization_id")} for row in first_rows],
        }]
        initial_errors = []
        expected_page_count = math.ceil(dynamic_total / PAGE_SIZE)
        if dynamic_pages != expected_page_count:
            initial_errors.append(f"capital_one_dynamic_page_count:{dynamic_pages}/{expected_page_count}")
        self._add_listing_rows(first_rows, source_page=1)
        for page in range(2, dynamic_pages + 1):
            if not self.page_guard():
                initial_errors.append("capital_one_initial_page_budget_exhausted")
                break
            try:
                _response, _criteria, _payload, summary, rows = await self._listing_page(
                    endpoint=endpoint, config=ssr_config, page=page
                )
            except Exception as exc:
                initial_errors.append(f"capital_one_page_failed:{page}:{type(exc).__name__}:{exc}")
                break
            if summary["total"] != dynamic_total:
                initial_errors.append(f"capital_one_total_changed_on_page_{page}:{dynamic_total}->{summary['total']}")
            if summary["total_pages"] != dynamic_pages or summary["current_page"] != page:
                initial_errors.append(f"capital_one_page_state_changed:{page}")
            expected_rows = min(PAGE_SIZE, dynamic_total - (page - 1) * PAGE_SIZE)
            if len(rows) != expected_rows:
                initial_errors.append(f"capital_one_page_row_count_{page}:{len(rows)}/{expected_rows}")
            page_ids = [row["job_id"] for row in rows]
            initial_ids.extend(page_ids)
            page_summaries.append({"page": page, "total": summary["total"], "row_count": len(rows),
                                   "first_id": page_ids[0] if page_ids else None,
                                   "last_id": page_ids[-1] if page_ids else None, "ids": page_ids,
                                   "rows": [{key: row[key] for key in
                                             ("job_id", "href", "title", "organization_id")} for row in rows]})
            self._add_listing_rows(rows, source_page=page)
        initial_duplicates = sorted({job_id for job_id in initial_ids if initial_ids.count(job_id) > 1})
        if initial_duplicates:
            initial_errors.append(f"capital_one_initial_duplicate_listing_ids:{','.join(initial_duplicates)}")
        unique_initial_ids = sorted(set(initial_ids))
        if len(unique_initial_ids) != dynamic_total:
            initial_errors.append(f"capital_one_initial_membership_count:{len(unique_initial_ids)}/{dynamic_total}")
        self.result.timings["initial_listing_seconds"] = round(max(0.0, time.perf_counter() - initial_start), 6)
        self.result.metadata["initial_membership"] = {
            "total": dynamic_total, "pages": dynamic_pages, "page_size": PAGE_SIZE,
            "row_count": len(initial_ids), "unique_count": len(unique_initial_ids),
            "ids": unique_initial_ids,
            "ids_sha256": hashlib.sha256("\n".join(unique_initial_ids).encode()).hexdigest(),
            "page_summaries": page_summaries,
            "errors": initial_errors,
        }
        self.result.errors.extend(initial_errors)
        self.result.listing_complete = not initial_errors and len(unique_initial_ids) == dynamic_total
        if not self.result.listing_complete:
            # Membership was not closed, so do not fetch a large detail set
            # against an ambiguous snapshot. Preserve exact listing rows/errors.
            return self.result

        async def detail(job):
            return await self._detail(job)

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        await self._fetch_role_attachments()
        recheck = await self._membership_pass(endpoint=endpoint, config=ssr_config,
                                              expected_total=dynamic_total,
                                              total_pages=dynamic_pages,
                                              count_result_pages=False)
        self.result.timings["final_membership_seconds"] = recheck.pop("duration_seconds")
        recheck["same_id_set_as_initial"] = set(unique_initial_ids) == set(recheck["ids"])
        if not recheck["same_id_set_as_initial"]:
            recheck["errors"].append("capital_one_initial_final_membership_changed")
        self.result.metadata["final_membership"] = recheck
        if recheck["errors"]:
            self.result.errors.extend(recheck["errors"])
            self.result.listing_complete = False
        self.result.timings["initial_listing_and_page1_seconds"] = self.result.timings.get("initial_listing_seconds", 0.0)
        return self.result
