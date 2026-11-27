"""Barclays's own TalentBrew search journey: unfiltered listing + native job details.

Membership authority is Barclays's official global listing at
https://search.jobs.barclays/search-jobs. The page's company-shipped
``search.js`` (tbcdn.talentbrew.com) builds a criteria object from the
``#search-results`` data attributes and requests
``GET /search-jobs/results?`` + ``$.bindQueryParams(criteria)`` (from the
company-shipped ``tb-corepack.js``), falling back to
``POST /search-jobs/resultspost`` with ``JSON.stringify(criteria)`` when the
serialized query exceeds the 4096-character limit. Pagination only changes
``CurrentPage``. Detail URLs are fetched exactly as exposed on the listing
cards (``/job/{city}/{slug}/13015/{job_id}``); the Workday sign-in/account
link on the page is not a listing authority and is never followed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now, text_html

ENTRY_URL = "https://search.jobs.barclays/search-jobs"
HOST = "search.jobs.barclays"
TENANT_ID = "13015"
SEARCH_SCRIPT_TOKENS = (
    "bindQueryParams",
    "data-ajax-url",
    "data-ajax-post-url",
    "AjaxCharLimit",
)
DETAIL_DESCRIPTION_METHOD = "barclays.talentbrew.ats_description"
CARD_ROUTE = re.compile(r"^/job/[^/]+/[^/]+/13015/(\d+)$")
UNDEFINED = object()


def _js_value(value):
    if value is UNDEFINED:
        return ""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    return value


def bind_query_params(criteria: dict) -> str:
    """Replicate ``$.bindQueryParams`` (tb-corepack.js) for the criteria object.

    Non-traditional mode: plain objects flatten with dot notation
    (``Name.Sub=value``), arrays with ``Name[index]=value``, ``null`` values
    are omitted (``typeof null == 'object'`` with no enumerable keys),
    ``undefined`` serializes as an empty value, and spaces use ``+``.
    """
    parts: list[str] = []

    def emit(name, value):
        value = _js_value(value)
        # encodeURIComponent leaves A-Za-z0-9-_.!~*'() unescaped (incl. [ ]).
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
            return  # typeof null == 'object': no enumerable keys, omitted
        else:
            emit(name, value)

    for key, value in criteria.items():
        build(key, value)
    return "&".join(parts).replace("%20", "+")


def search_criteria(entry_attrs: dict, *, current_page: int) -> dict:
    """Build the exact unfiltered criteria ``search.js`` sends for a page.

    Mirrors ``mapSearchOptions()`` + the criteria copy loop (which excludes
    ``TotalPages``/``TotalResults``/``StaticFacets``) + ``facetFilters()``
    (no checked checkboxes on the unfiltered journey) for the observed
    Barclays entry attributes. ``TotalContentResults`` is absent from the
    markup so the JS ``intOrDefault`` yields ``undefined``; consequently
    ``TotalContentPages`` is ``ceil(undefined / RecordsPerPage)`` = ``NaN``.
    """
    records_per_page = int(entry_attrs.get("data-records-per-page") or 16)
    options = {
        "ActiveFacetID": int(entry_attrs.get("data-active-facet-id") or 0),
        "CurrentPage": current_page,
        "RecordsPerPage": records_per_page,
        "TotalContentResults": UNDEFINED,
        "Distance": int(entry_attrs.get("data-distance") or 50),
        "RadiusUnitType": 0,
        "Keywords": "",
        "Location": "",
        "ShowRadius": entry_attrs.get("data-show-radius") or "False",
        "IsPagination": "False",
        "FacetTerm": "",
        "FacetType": 0,
        "FacetFilters": [],
        "SearchResultsModuleName": entry_attrs.get("data-search-results-module-name") or "Search Results",
        "SearchFiltersModuleName": "Search Filters",
        "SortCriteria": int(entry_attrs.get("data-sort-criteria") or 0),
        "SortDirection": int(entry_attrs.get("data-sort-direction") or 0),
        "SearchType": int(entry_attrs.get("data-search-type") or 5),
        "ResultsType": int(entry_attrs.get("data-results-type") or 0),
        "fc": "",
        "fl": "",
        "fcf": "",
        "afc": "",
        "afl": "",
        "afcf": "",
        # ceil(undefined / 16): JavaScript Math.ceil(NaN) is NaN.
        "TotalContentPages": float("nan"),
        "PostalCode": UNDEFINED,
    }
    # `typeof null == 'object'` properties in mapSearchOptions (empty
    # stringOrDefault fallbacks, empty latitude/longitude) are omitted by
    # bindQueryParams, so only undefined-valued keys survive as `Key=`.
    return options


def _attrs(element) -> dict:
    return {key: value for key, value in (element.attrs or {}).items()}


def parse_listing_cards(results_html: str) -> tuple[dict, list[dict]]:
    """Parse the ``results`` fragment: wrapper attrs + exact job cards."""
    soup = BeautifulSoup(results_html, "lxml")
    wrapper = soup.select_one("#search-results")
    attrs = _attrs(wrapper) if wrapper else {}
    cards = []
    for anchor in soup.select("a[data-job-id][href]"):
        href = anchor.get("href", "")
        job_id = anchor.get("data-job-id", "")
        match = CARD_ROUTE.match(urlsplit(href).path)
        if not match or match.group(1) != job_id:
            continue
        strong = anchor.find("strong")
        title = strong.get_text(" ", strip=True) if strong else anchor.get_text(" ", strip=True)
        card = anchor.find_parent("div", class_="list-item--card") or anchor
        location_el = card.select_one(".job-location")
        location = location_el.get_text(" ", strip=True) if location_el else ""
        date_el = card.select_one(".job-date span")
        date_text = date_el.get_text(" ", strip=True) if date_el else ""
        cards.append({
            "href": href,
            "job_id": job_id,
            "title": title,
            "location": location,
            "card_date_raw": date_text,
        })
    return attrs, cards


def _meta(soup, name, *, attr="name"):
    node = soup.find("meta", attrs={attr: name})
    return (node.get("content") or "").strip() if node else ""


def parse_detail(html: str, *, job_id: str) -> dict:
    """Extract the full public fields from a native TalentBrew job detail page.

    The role body is ``section.job-description`` whose only child is
    ``div.ats-description``; its inner HTML is the complete description.
    Corporate disclosure sections around it are not part of the body.
    """
    soup = BeautifulSoup(html, "lxml")
    section = soup.select_one("section.job-description")
    ats = section.select_one(".ats-description") if section else soup.select_one(".ats-description")
    if ats is None:
        raise ValueError(f"Barclays detail {job_id}: native section.job-description/ats-description missing")
    description_html = str(ats.decode_contents() or "").strip() if hasattr(ats, "decode_contents") else ""
    if not description_html:
        # lxml decode_contents fallback via prettify of children
        description_html = "".join(str(child) for child in ats.children).strip()
    title_el = soup.select_one("h1.job-details--title")
    title = title_el.get_text(" ", strip=True) if title_el else ""
    location_el = soup.select_one("p.job-details--location")
    location = location_el.get_text(" ", strip=True) if location_el else ""
    personalization = _meta(soup, "personalization-categories-locations")
    employment_type = None
    try:
        entries = json.loads(personalization) if personalization else []
    except ValueError:
        entries = []
    for entry in entries if isinstance(entries, list) else []:
        custom = entry.get("CustomFacet") or {}
        if custom.get("CustomFacetTerm") == "hours_per_week" and custom.get("CustomFacetValue"):
            employment_type = custom["CustomFacetValue"]
            break
    return {
        "title": title,
        "location": location,
        "description_html": description_html,
        "requisition_id": _meta(soup, "job-ats-req-id") or None,
        "apply_url": _meta(soup, "search-job-apply-url", attr="name") or None,
        "category": _meta(soup, "gtm_tbcn_jobcategory") or None,
        "category_ids": _meta(soup, "job-category-ids") or None,
        "location_ids": _meta(soup, "job-location-ids") or None,
        "employment_type": employment_type,
        "current_job_id": _meta(soup, "search-analytics-currentJobId") or None,
        "gtm_job_id": _meta(soup, "gtm_jobid") or None,
        "gtm_first_index_raw": _meta(soup, "gtm_firstindex") or None,
        "personalization": personalization,
    }


class Barclays(Adapter):
    """Unfiltered TalentBrew listing membership + exact native detail bodies."""

    VERIFICATION_REVISION = "barclays.talentbrew.unfiltered-listing-native-detail.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        parsed = urlsplit(self.source.url)
        if (
            self.source.url.rstrip("/") != ENTRY_URL
            or parsed.scheme != "https"
            or parsed.hostname != HOST
        ):
            raise ValueError(f"Barclays dedicated flow must start from its official entry {ENTRY_URL}")

    async def _get_entry(self):
        response = await self.http.get(ENTRY_URL)
        if str(response.url).rstrip("/") != ENTRY_URL:
            raise ValueError(f"Barclays official entry redirected: {response.url}")
        soup = BeautifulSoup(response.text, "lxml")
        wrapper = soup.select_one("#search-results")
        if wrapper is None:
            raise ValueError("Barclays official entry has no #search-results listing wrapper")
        attrs = _attrs(wrapper)
        script = soup.find("script", src=re.compile(r"search\.js$"))
        script_url = urljoin(ENTRY_URL, script.get("src")) if script and script.get("src") else None
        return response, attrs, script_url

    async def _fetch_search_script(self, script_url):
        if not script_url:
            raise ValueError("Barclays entry does not load the TalentBrew search.js")
        response = await self.http.get(script_url)
        text = response.text
        missing = [token for token in SEARCH_SCRIPT_TOKENS if token not in text]
        if missing:
            raise ValueError(f"Barclays search.js no longer matches the inspected flow; missing {missing}")
        return str(response.url), text

    async def _listing_page(self, *, ajax_url: str, entry_attrs: dict, page: int):
        criteria = search_criteria(entry_attrs, current_page=page)
        query = bind_query_params(criteria)
        response = await self.http.get(f"{ajax_url}?{query}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Barclays listing page {page} was not JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), str):
            raise ValueError(f"Barclays listing page {page} has no results fragment")
        attrs, cards = parse_listing_cards(payload["results"])
        return response, criteria, payload, attrs, cards

    async def run(self):
        self.result.metadata["verification_revision"] = self.VERIFICATION_REVISION
        self.result.metadata["membership_authority"] = (
            "Barclays official unfiltered listing at search.jobs.barclays/search-jobs; "
            "company-shipped search.js GET /search-jobs/results with bindQueryParams criteria, "
            "pagination by CurrentPage only"
        )
        collection_started = now()
        entry, entry_attrs, script_url = await self._get_entry()
        script_final_url, _script_text = await self._fetch_search_script(script_url)
        ajax_url = urljoin(ENTRY_URL, entry_attrs.get("data-ajax-url") or "/search-jobs/results")
        if urlsplit(ajax_url).hostname != HOST or urlsplit(ajax_url).path != "/search-jobs/results":
            raise ValueError(f"Barclays entry exposes an unexpected listing endpoint: {ajax_url}")
        total_results = int(entry_attrs.get("data-total-results") or 0)
        total_pages = int(entry_attrs.get("data-total-pages") or 0)
        records_per_page = int(entry_attrs.get("data-records-per-page") or 16)
        self.result.expected_total = total_results
        self.result.metadata.update({
            "official_entry_url": str(entry.url),
            "entry_response_bytes": len(entry.content),
            "entry_attrs": entry_attrs,
            "search_script_url": script_final_url,
            "listing_ajax_url": ajax_url,
            "entry_total_results": total_results,
            "entry_total_pages": total_pages,
            "records_per_page": records_per_page,
            "tenant_id": _meta(BeautifulSoup(entry.text, "lxml"), "site-tenant-id") or TENANT_ID,
            "collection_started_at": collection_started,
        })

        seen_ids: set[str] = set()
        page_summaries = []
        listing_jobs = []
        for page in range(1, total_pages + 1):
            if not self.page_guard():
                break
            response, criteria, _payload, attrs, cards = await self._listing_page(
                ajax_url=ajax_url, entry_attrs=entry_attrs, page=page
            )
            page_total = int(attrs.get("data-total-results") or -1)
            page_total_pages = int(attrs.get("data-total-pages") or -1)
            current_page = int(attrs.get("data-current-page") or -1)
            if page_total != total_results or page_total_pages != total_pages or current_page != page:
                self.result.errors.append(
                    f"barclays_listing_page_disagreement: request={page} current={current_page} "
                    f"total={page_total} pages={page_total_pages}"
                )
                break
            if not cards:
                self.result.errors.append(f"barclays_listing_empty_page:{page}")
                break
            for card in cards:
                if card["job_id"] in seen_ids:
                    self.result.errors.append(f"barclays_duplicate_public_id:{card['job_id']}")
                    continue
                seen_ids.add(card["job_id"])
                detail_url = urljoin(ENTRY_URL, card["href"])
                job = self.job(
                    {"listing_card": card, "listing_page": page,
                     "listing_request_url": str(response.url)},
                    title=card["title"],
                    url=detail_url,
                    job_id=card["job_id"],
                    locations=[card["location"]] if card["location"] else [],
                    date_posted=None,
                    date_posted_raw=card["card_date_raw"] or None,
                )
                # Card dates are day+month without a year; keep normalized
                # dates null rather than fabricating a year.
                job.raw["listing_card_date_raw"] = card["card_date_raw"] or None
                listing_jobs.append(job)
            page_summaries.append({
                "page": page,
                "request_url": str(response.url),
                "request_query": bind_query_params(criteria),
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
                "current_page": current_page,
                "total_results": page_total,
                "total_pages": page_total_pages,
                "records_per_page": int(attrs.get("data-records-per-page") or records_per_page),
                "card_ids": [card["job_id"] for card in cards],
            })
        self.result.metadata["listing_pages"] = page_summaries
        self.result.metadata["listing_card_ids"] = sorted(seen_ids)
        if len(seen_ids) == total_results and len(page_summaries) == total_pages and not self.result.errors:
            self.result.listing_complete = True
        else:
            self.result.listing_complete = False

        async def detail(job):
            response = await self.http.get(job.url)
            if str(urlsplit(str(response.url)).path) != urlsplit(job.url).path or not CARD_ROUTE.match(urlsplit(str(response.url)).path):
                raise ValueError(f"Barclays detail for {job.job_id} redirected to an unexpected route: {response.url}")
            fields = parse_detail(response.text, job_id=str(job.job_id))
            if fields["current_job_id"] not in (None, str(job.job_id)) or fields["gtm_job_id"] not in (None, str(job.job_id)):
                raise ValueError(f"Barclays detail {job.job_id} carries a mismatched native job ID")
            if fields["title"] and fields["title"] != job.title:
                # Keep the detail title as canonical but record the conflict.
                job.raw["listing_title"] = job.title
                job.warnings.append("listing_detail_title_conflict")
            job.raw["detail"] = {
                "title": fields["title"],
                "location": fields["location"],
                "requisition_id": fields["requisition_id"],
                "apply_url": fields["apply_url"],
                "category": fields["category"],
                "category_ids": fields["category_ids"],
                "location_ids": fields["location_ids"],
                "employment_type": fields["employment_type"],
                "gtm_first_index_raw": fields["gtm_first_index_raw"],
                "personalization": fields["personalization"],
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
            }
            if fields["title"]:
                job.title = fields["title"]
            if fields["location"]:
                job.locations = [fields["location"]]
            job.requisition_id = fields["requisition_id"]
            job.apply_url = fields["apply_url"]
            job.department = fields["category"]
            job.employment_type = fields["employment_type"]
            job.description_html = fields["description_html"]
            job.description_method = DETAIL_DESCRIPTION_METHOD
            job.description_source_url = str(response.url)
            job.fetched_at = now()
            body = text_html(job.description_html)
            if len(body.strip()) < 100:
                job.description_html = ""
                job.description_text = ""
                job.description_sha256 = None
                job.description_verified = False
                job.description_unavailable = True
                for warning in ("source_description_missing", "description_not_full_field"):
                    if warning not in job.warnings:
                        job.warnings.append(warning)
            return job

        self.result.jobs = list(listing_jobs)
        hydrated = await self.hydrate(self.result.jobs, detail)
        self.result.jobs = list(hydrated)
        self.result.metadata["listing_membership"] = {
            "source_total": total_results,
            "source_total_pages": total_pages,
            "records_per_page": records_per_page,
            "collected_ids": len(seen_ids),
            "page_count": len(page_summaries),
            "first_id": page_summaries[0]["card_ids"][0] if page_summaries else None,
            "last_id": page_summaries[-1]["card_ids"][-1] if page_summaries else None,
        }
        unavailable = sorted(job.job_id for job in self.result.jobs if job.description_unavailable)
        self.result.metadata["source_empty_role_ids"] = unavailable
        self.result.metadata["source_empty_role_count"] = len(unavailable)
        return self.result
