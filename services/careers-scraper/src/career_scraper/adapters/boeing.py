"""Boeing's own TalentBrew search-results AJAX and native job-detail journey.

Membership authority is the unfiltered global list rendered by the official
``https://jobs.boeing.com/search-jobs`` entry: its shipped ``search.js``
builds criteria from the page's own ``#search-results`` / ``#search-filters``
attributes and requests ``/search-jobs/results`` (GET, as used when the
query is under the 4096-char limit). This adapter replicates that exact
request shape with no facet filters, keywords or location. Full descriptions
come from each exact listed native detail page's ``#ats-description``; list
teasers, truncated locations and prose closing dates are never substituted
for the full body or structured dates. Apply links go to the exact
Workday/Springboard URL published on each detail page; no ATS board is
enumerated.
"""
import re
import time
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

VERIFICATION_REVISION = "boeing.native-talentbrew-search-results-and-jobdetail.1"

_JOB_ID = re.compile(r"^\d{9,}$")
_JOB_PATH = re.compile(r"^/job/[^/]+/[^/]+/185/\d+$")
_ORG_ID = "185"
_ENTRY_PATH = "/search-jobs"
_AJAX_URL = "/search-jobs/results"
_AJAX_POST_URL = "/search-jobs/resultspost"
_RECORDS_PER_PAGE = 15


def compact_body_hash(html):
    """Mirror the parent browser reference's NFC/whitespace-free body hash."""
    import hashlib
    import unicodedata

    value = unicodedata.normalize("NFC", text_html(html))
    value = re.sub(r"[\s﻿]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def native_job_url(entry_url, href):
    """Build the absolute native detail URL from one exact listing href."""
    if not href or not str(href).startswith("/job/"):
        raise ValueError(f"Boeing listing job link is not the observed native /job/ route: {href!r}")
    parsed = urlsplit(str(href))
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError(f"Boeing listing job link has an unexpected shape: {href!r}")
    if not _JOB_PATH.fullmatch(parsed.path):
        raise ValueError(f"Boeing listing job link is not the observed /job/<loc>/<slug>/185/<id> route: {href!r}")
    match = re.search(r"/185/(\d+)$", parsed.path)
    if not match or not _JOB_ID.fullmatch(match.group(1)):
        raise ValueError(f"Boeing listing job link has an invalid numeric ID: {href!r}")
    entry_parsed = urlsplit(str(entry_url))
    if entry_parsed.scheme != "https" or entry_parsed.hostname != "jobs.boeing.com":
        raise ValueError("Boeing native job URL must stay on https://jobs.boeing.com")
    return urljoin(str(entry_url), parsed.path)


def _int_attr(element, name, label):
    raw = element.get(name)
    if raw is None:
        raise ValueError(f"Boeing {label} is missing {name}")
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"Boeing {label} {name} is not an integer: {raw!r}") from exc


def _str_attr(element, name, label):
    raw = element.get(name)
    if raw is None:
        raise ValueError(f"Boeing {label} is missing {name}")
    return str(raw)


def _parse_entry(entry_html):
    soup = BeautifulSoup(entry_html, "lxml")
    results = soup.select_one("#search-results")
    if results is None:
        raise ValueError("Boeing official entry has no #search-results container")
    filters = soup.select_one("#search-filters")
    if filters is None:
        raise ValueError("Boeing official entry has no #search-filters container")
    total = _int_attr(results, "data-total-results", "search-results")
    total_jobs = _int_attr(results, "data-total-job-results", "search-results")
    if total < 0:
        raise ValueError("Boeing official entry total is negative")
    if total != total_jobs:
        raise ValueError(
            "Boeing official entry job/content totals differ; unfiltered job membership is ambiguous"
        )
    config = {
        "total": total,
        "total_pages": _int_attr(results, "data-total-pages", "search-results"),
        "current_page": _int_attr(results, "data-current-page", "search-results"),
        "records_per_page": _int_attr(results, "data-records-per-page", "search-results"),
        "ajax_url": _str_attr(results, "data-ajax-url", "search-results"),
        "ajax_post_url": _str_attr(results, "data-ajax-post-url", "search-results"),
        "sort_criteria": _int_attr(results, "data-sort-criteria", "search-results"),
        "sort_direction": _int_attr(results, "data-sort-direction", "search-results"),
        "search_type": _int_attr(results, "data-search-type", "search-results"),
        "results_type": _int_attr(results, "data-results-type", "search-results"),
        "active_facet_id": _int_attr(results, "data-active-facet-id", "search-results"),
        "distance": _int_attr(results, "data-distance", "search-results"),
        "keywords": _str_attr(results, "data-keywords", "search-results"),
        "location": _str_attr(results, "data-location", "search-results"),
        "results_module": _str_attr(results, "data-search-results-module-name", "search-results"),
        "organization_ids": _str_attr(results, "data-organization-ids", "search-results"),
        "refined_keywords": _str_attr(results, "data-refined-keywords", "search-results"),
        "filters_module": _str_attr(filters, "data-search-filters-module-name", "search-filters"),
        "radius_unit_type": _int_attr(filters, "data-radius-unit-type", "search-filters"),
        "ajax_char_limit": _int_attr(filters, "data-ajax-char-limit", "search-filters"),
        "filtered_categories": _str_attr(filters, "data-filtered-categories", "search-filters"),
        "filtered_locations": _str_attr(filters, "data-filtered-locations", "search-filters"),
    }
    if config["records_per_page"] != _RECORDS_PER_PAGE:
        raise ValueError("Boeing records-per-page changed from its observed 15")
    if config["ajax_url"] != _AJAX_URL or config["ajax_post_url"] != _AJAX_POST_URL:
        raise ValueError("Boeing search-results AJAX endpoints changed from the observed /search-jobs/results flow")
    if config["results_module"] != "Search Results" or config["filters_module"] != "Search Filters":
        raise ValueError("Boeing search/filter module names changed from the observed entry")
    if config["results_type"] != 0 or config["active_facet_id"] != 0:
        raise ValueError("Boeing official entry is not the observed unfiltered combined search")
    if config["keywords"] != "" or config["location"] != "" or config["organization_ids"] != "":
        raise ValueError("Boeing official entry carries unexpected search terms")
    if config["refined_keywords"] != "[]":
        raise ValueError("Boeing official entry carries refined keyword state")
    if config["filtered_categories"] != "" or config["filtered_locations"] != "":
        raise ValueError("Boeing official entry carries filtered facet state")
    org = soup.select_one('input[name="orgIds"]')
    if org is None or org.get("value") != _ORG_ID:
        raise ValueError("Boeing official search form orgIds is not the observed 185")
    if config["current_page"] != 1:
        raise ValueError("Boeing official entry is not on page 1")
    expected_pages = -(-total // config["records_per_page"]) if total else 0
    if config["total_pages"] != expected_pages:
        raise ValueError("Boeing official entry page count does not reconcile to its total")
    eager = _parse_rows_html(entry_html, "entry page 1")
    if total and len(eager) != min(config["records_per_page"], total):
        raise ValueError("Boeing official entry first page does not reconcile to its total")
    return config, eager


def _parse_rows_html(fragment_html, label):
    soup = BeautifulSoup(fragment_html or "", "lxml")
    results = soup.select_one("#search-results")
    if results is None:
        raise ValueError(f"Boeing {label} has no #search-results container")
    rows = []
    for link in soup.select(".search-results__job-link"):
        identity = link.get("data-job-id")
        href = link.get("href")
        title_node = link.select_one(".search-results__job-title")
        title = title_node.get_text(" ", strip=True) if title_node else link.get_text(" ", strip=True)
        item = link.find_parent("li")
        location_node = item.select_one(".search-results__job-info.location") if item else None
        date_node = item.select_one(".search-results__job-info.date") if item else None
        location = location_node.get_text(" ", strip=True) if location_node else None
        date_text = date_node.get_text(" ", strip=True) if date_node else None
        if not identity or not _JOB_ID.fullmatch(str(identity)):
            raise ValueError(f"Boeing {label} contains a missing or invalid numeric job ID")
        if not href or not _JOB_PATH.fullmatch(urlsplit(str(href)).path):
            raise ValueError(f"Boeing {label} ID {identity} has a non-native job link: {href!r}")
        if re.search(r"/185/(\d+)$", str(href)).group(1) != str(identity):
            raise ValueError(f"Boeing {label} ID {identity} link does not carry the same numeric ID")
        if not title:
            raise ValueError(f"Boeing {label} ID {identity} has no title")
        rows.append({
            "id": str(identity),
            "href": str(href),
            "title": title,
            "location": location,
            "date": date_text,
        })
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Boeing {label} repeats a company-published ID")
    return rows


def _parse_results_fragment(payload, label):
    if not isinstance(payload, dict):
        raise ValueError(f"Boeing {label} AJAX response is not a JSON object")
    results_html = payload.get("results")
    if not isinstance(results_html, str) or not results_html.strip():
        raise ValueError(f"Boeing {label} AJAX response has no results fragment")
    if payload.get("hasJobs") is not True:
        raise ValueError(f"Boeing {label} AJAX response reports no jobs")
    soup = BeautifulSoup(results_html, "lxml")
    results = soup.select_one("#search-results")
    if results is None:
        raise ValueError(f"Boeing {label} results fragment has no #search-results")
    total = _int_attr(results, "data-total-results", f"{label} results")
    total_jobs = _int_attr(results, "data-total-job-results", f"{label} results")
    if total != total_jobs:
        raise ValueError(f"Boeing {label} job/content totals differ")
    rows = _parse_rows_html(results_html, label)
    return rows, total, results


def _criteria_query(config, page):
    criteria = [
        ("ActiveFacetID", config["active_facet_id"]),
        ("CurrentPage", page),
        ("RecordsPerPage", config["records_per_page"]),
        ("Distance", config["distance"]),
        ("RadiusUnitType", config["radius_unit_type"]),
        ("Keywords", config["keywords"]),
        ("Location", config["location"]),
        ("Latitude", ""),
        ("Longitude", ""),
        ("ShowRadius", "False"),
        ("IsPagination", "False"),
        ("CustomFacetName", ""),
        ("FacetTerm", ""),
        ("FacetType", 0),
        ("SearchResultsModuleName", config["results_module"]),
        ("SearchFiltersModuleName", config["filters_module"]),
        ("SortCriteria", config["sort_criteria"]),
        ("SortDirection", config["sort_direction"]),
        ("SearchType", config["search_type"]),
        ("CategoryFacetTerm", ""),
        ("CategoryFacetType", ""),
        ("LocationFacetTerm", ""),
        ("LocationFacetType", ""),
        ("KeywordType", ""),
        ("LocationType", ""),
        ("LocationPath", ""),
        ("OrganizationIds", config["organization_ids"]),
        ("PostalCode", ""),
        ("ResultsType", config["results_type"]),
        ("fc", ""),
        ("fl", ""),
        ("fcf", ""),
        ("afc", ""),
        ("afl", ""),
        ("afcf", ""),
    ]
    return urlencode(criteria)


def _salary_from_description(description_text):
    lines = []
    for line in (description_text or "").splitlines():
        value = line.strip()
        if value.lower().startswith("summary pay range"):
            lines.append(value)
    if not lines:
        return None
    return {
        "source_text": "\n".join(lines),
        "source_field": "description_text (#ats-description)",
        "semantics": "US pay range(s) as published in the full description; preserve source text without conversion.",
    }


def _attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not re.search(r"\.(pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            continue
        # EEO poster boilerplate appears in every description; it is not a
        # role attachment and its text is not part of the posting.
        if "eeoc.gov" in urlsplit(href).hostname.lower():
            continue
        values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


def _info_value(section, css_class, label_prefix):
    node = section.select_one(f"span.job-description__job-info.{css_class}")
    if node is None:
        return None
    full = node.get_text(" ", strip=True)
    if full == label_prefix:
        return None
    prefix = f"{label_prefix} "
    if not full.startswith(prefix):
        raise ValueError(f"Boeing job detail {css_class} label changed: {full!r}")
    return full[len(prefix):].strip() or None


def _split_locations(location_text):
    if not location_text:
        return []
    return [{"location": part.strip()} for part in str(location_text).split(";") if part.strip()]


def _parse_detail_html(detail_html, *, identity, listing_title):
    soup = BeautifulSoup(detail_html or "", "lxml")
    section = soup.select_one("section.job-description")
    if section is None:
        raise ValueError(f"{identity}: Boeing detail has no section.job-description")
    if section.get("data-job-id") != identity:
        raise ValueError(f"{identity}: Boeing detail job ID changed")
    if section.get("data-org-id") != _ORG_ID:
        raise ValueError(f"{identity}: Boeing detail org changed from 185")
    title_node = section.select_one("h1.job-description__job-title")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    if title != listing_title:
        raise ValueError(f"{identity}: Boeing detail title does not match the company listing")
    location_node = section.select_one(".job-description__job-location")
    location_text = location_node.get_text(" ", strip=True) if location_node else None
    requisition = _info_value(section, "job-id", "Job ID")
    category = _info_value(section, "job-category", "Category")
    role_type = _info_value(section, "job-role-type", "Role Type")
    post_date = _info_value(section, "job-date", "Post Date")
    if not requisition:
        raise ValueError(f"{identity}: Boeing detail Job ID is missing")
    # Category may be published empty (observed on Taleo BIA details where
    # the span carries only its label); it then stays null, never invented.
    if not post_date:
        raise ValueError(f"{identity}: Boeing detail Post Date is missing")
    apply_link = section.select_one("a.job-apply.top")
    apply_url = apply_link.get("href") if apply_link else None
    if not apply_url or not str(apply_url).startswith("https://"):
        raise ValueError(f"{identity}: Boeing detail Apply Now link is missing")
    apply_str = str(apply_url)
    year_match = re.fullmatch(r"\d{4}-(\d+)", requisition or "")
    # Spirit iCIMS publishes Job ID 2026-17748 while the apply path
    # carries only 17748 (observed /jobs/17748/.../job/login); Springboard
    # Job IDs BOE-...E/B are lowercased in the apply path (observed).
    if requisition.lower() not in apply_str.lower() and not (
        year_match and year_match.group(1) in apply_str
    ):
        raise ValueError(f"{identity}: Boeing detail Apply Now link does not carry the published requisition")
    bottom = section.select_one("a.job-apply.bottom")
    if bottom is not None and bottom.get("href") != apply_url:
        raise ValueError(f"{identity}: Boeing detail top/bottom Apply links differ")
    description_node = section.select_one("#ats-description")
    if description_node is None:
        raise ValueError(f"{identity}: Boeing detail #ats-description is missing")
    description_html = description_node.decode_contents()
    description_text = text_html(description_html)
    if not description_html.strip():
        raise ValueError(f"{identity}: Boeing detail #ats-description is empty")
    if len(description_text.strip()) < 100:
        raise ValueError(f"{identity}: Boeing detail description is implausibly short")
    return {
        "title": title,
        "location_text": location_text,
        "requisition_id": requisition,
        "category": category,
        "role_type": role_type,
        "post_date": post_date,
        "apply_url": str(apply_url),
        "description_html": description_html,
        "description_text": description_text,
    }


class Boeing(Adapter):
    """Follow only Boeing's official TalentBrew search + native detail flow."""

    VERIFICATION_REVISION = VERIFICATION_REVISION

    async def _listing_page(self, config, page, label):
        if not self.page_guard():
            raise ValueError("Boeing native listing page budget exhausted")
        ajax_base = urljoin("https://jobs.boeing.com", _AJAX_URL)
        query = _criteria_query(config, page)
        response = await self.http.get(f"{ajax_base}?{query}")
        if str(response.url) != f"{ajax_base}?{query}":
            raise ValueError(f"Boeing {label} listing request redirected away from its inspected source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Boeing {label} listing response is not JSON") from exc
        rows, total, results = _parse_results_fragment(payload, label)
        current = _int_attr(results, "data-current-page", f"{label} results")
        total_pages = _int_attr(results, "data-total-pages", f"{label} results")
        if current != page:
            raise ValueError(f"Boeing {label} listing current page changed from {page} to {current}")
        expected_pages = -(-total // config["records_per_page"]) if total else 0
        if total_pages != expected_pages:
            raise ValueError(f"Boeing {label} listing page count does not reconcile to its total")
        if total == 0:
            if rows:
                raise ValueError(f"Boeing {label} listing reports zero total but carries rows")
        elif page < total_pages:
            if len(rows) != config["records_per_page"]:
                raise ValueError(f"Boeing {label} listing page {page} is not a full {config['records_per_page']}-row page")
        elif page == total_pages:
            expected_last = total - (page - 1) * config["records_per_page"]
            if len(rows) != expected_last:
                raise ValueError(f"Boeing {label} listing last page does not reconcile to its total")
        else:
            if rows:
                raise ValueError(f"Boeing {label} listing page {page} is beyond its last page")
        return rows, total, total_pages

    async def _paginate(self, config, *, label):
        """Enumerate unfiltered AJAX pages; reconcile by ID set."""
        rows_by_id = {}
        pages = []
        total = None
        page = 1
        while True:
            rows, page_total, total_pages = await self._listing_page(config, page, label)
            if total is None:
                total = page_total
            elif page_total != total:
                self.result.errors.append(f"boeing_native_total_changed_during_{label}_pagination")
                total = page_total
            pages.append({
                "label": label,
                "page": page,
                "hits": len(rows),
                "total": page_total,
                "total_pages": total_pages,
                "job_ids": [row["id"] for row in rows],
            })
            for row in rows:
                identity = row["id"]
                if identity in rows_by_id:
                    if rows_by_id[identity] != row:
                        raise ValueError(
                            f"Boeing native listing repeated ID {identity} with a changed row"
                        )
                    pages[-1].setdefault("duplicate_ids", []).append(identity)
                    continue
                rows_by_id[identity] = row
            if not rows and len(rows_by_id) < (total or 0):
                self.result.errors.append(f"boeing_native_pagination_ended_early_{label}")
                break
            if len(rows_by_id) >= (total or 0):
                break
            if page >= (total_pages or 0) and total:
                break
            page += 1
            if total == 0:
                break
        return rows_by_id, total, pages

    async def detail(self, job):
        """Fetch one exact listed native detail page."""
        response = await self.http.get(job.url)
        if str(response.url) != job.url:
            raise ValueError(f"{job.job_id}: Boeing detail redirected away from its exact listed URL")
        parsed = _parse_detail_html(response.text, identity=job.job_id, listing_title=job.title)
        description_html = parsed["description_html"]
        links = _attachment_links(description_html)
        job.raw["detail"] = {
            "title": parsed["title"],
            "location_text": parsed["location_text"],
            "requisition_id": parsed["requisition_id"],
            "category": parsed["category"],
            "role_type": parsed["role_type"],
            "post_date": parsed["post_date"],
            "apply_url": parsed["apply_url"],
        }
        job.raw["native_detail_url"] = job.url
        job.raw["attachment_links"] = links
        if links:
            job.warnings.append("role_attachment_links_present")
            self.result.errors.append(
                f"{job.job_id}: published role attachment links are present; full attachment text was not captured"
            )
        job.title = parsed["title"]
        job.description_html = description_html
        job.description_text = parsed["description_text"]
        job.description_method = "boeing.native_talentbrew.jobdetail.ats-description"
        job.description_source_url = job.url
        job.locations = _split_locations(parsed["location_text"])
        job.department = parsed["category"]
        job.workplace_type = parsed["role_type"]
        job.requisition_id = parsed["requisition_id"]
        # Detail Post Date is authoritative; listing date is retained in raw.
        job.date_posted = parsed["post_date"]
        job.date_posted_raw = {
            "listing": job.raw["listing"].get("date"),
            "detail": parsed["post_date"],
        }
        job.apply_url = parsed["apply_url"]
        job.salary = _salary_from_description(parsed["description_text"])
        lowered = parsed["description_text"].lower()
        pipeline_hits = [
            token for token in ("pipeline", "evergreen", "talent pool")
            if token in lowered
        ]
        if pipeline_hits:
            job.warnings.append("possible_pipeline_or_evergreen_language")
            job.raw["pipeline_language_hits"] = pipeline_hits
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (
            entry_parts.scheme != "https"
            or entry_parts.hostname != "jobs.boeing.com"
            or entry_parts.path != _ENTRY_PATH
            or entry_parts.query
            or entry_parts.fragment
        ):
            raise ValueError("Boeing dedicated flow must start from its official HTTPS /search-jobs entry")
        listing_started = time.perf_counter()
        entry = await self.http.get(entry_url)
        if str(entry.url) != entry_url:
            raise ValueError("Boeing official entry redirected away from /search-jobs")
        config, eager_rows = _parse_entry(entry.text)
        eager_ids = [row["id"] for row in eager_rows]
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "membership_authority": "Boeing official /search-jobs TalentBrew Search Results unfiltered list (its shipped search.js AJAX criteria), not any ATS board",
            "listing_request": "GET /search-jobs/results with mapSearchOptions criteria (RecordsPerPage 15, no FacetFilters/keywords/location), paginated by CurrentPage",
            "records_per_page": config["records_per_page"],
            "sort_criteria": config["sort_criteria"],
            "sort_direction": config["sort_direction"],
            "search_type": config["search_type"],
            "search_results_module": config["results_module"],
            "search_filters_module": config["filters_module"],
            "org_id": _ORG_ID,
            "entry_total": config["total"],
            "entry_total_pages": config["total_pages"],
            "eager_job_ids": eager_ids,
        })
        if config["total"] == 0:
            self.result.expected_total = 0
            self.result.pages = 1
            self.result.errors.append(
                "Boeing official listing returned zero visible jobs; native empty state requires independent review"
            )
            return self.result

        initial_rows, total, initial_pages = await self._paginate(config, label="initial")
        self.result.expected_total = total
        if not initial_rows:
            self.result.errors.append(
                "Boeing official embedded listing returned zero visible jobs; native empty state requires independent review"
            )
            return self.result
        if eager_ids and set(eager_ids) != set(initial_pages[0]["job_ids"]):
            self.result.errors.append("boeing_eager_first_page_differs_from_first_ajax_page")
        elif eager_ids and eager_ids != initial_pages[0]["job_ids"]:
            self.result.errors.append("boeing_eager_first_page_order_differs_from_first_ajax_page")

        final_snapshot_verified = True
        try:
            final_rows, final_total, final_pages = await self._paginate(config, label="final")
        except Exception as exc:
            final_snapshot_verified = False
            final_rows, final_total, final_pages = initial_rows, total, initial_pages
            self.result.errors.append(
                "Boeing final company-published listing snapshot failed; initial usable rows retained: "
                f"{type(exc).__name__}: {exc}"
            )
        listing_seconds = time.perf_counter() - listing_started

        changed_rows = sorted(
            identity
            for identity in set(final_rows) & set(initial_rows)
            if final_rows[identity] != initial_rows[identity]
        )
        if changed_rows:
            self.result.errors.append("boeing_listing_rows_changed_between_snapshots")
        if final_total != total or set(final_rows) != set(initial_rows):
            self.result.listing_complete = False
            self.result.errors.append("boeing_native_listing_id_set_changed")
        duplicate_initial = [
            identity for page in initial_pages for identity in page.get("duplicate_ids", [])
        ]
        duplicate_final = [
            identity for page in final_pages for identity in page.get("duplicate_ids", [])
        ]

        for identity, row in initial_rows.items():
            url = native_job_url(str(entry.url), row["href"])
            self.add(
                self.job(
                    {"listing": row},
                    title=row["title"],
                    job_id=identity,
                    url=url,
                    date_posted=row.get("date"),
                )
            )
            if self.limit():
                break

        self.result.metadata["initial_pages"] = initial_pages
        self.result.metadata["final_pages"] = final_pages
        if "sample_limit" in self.result.warnings:
            self.result.listing_complete = False
            self.result.metadata.update({
                "initial_job_ids": sorted(initial_rows),
                "final_job_ids": sorted(final_rows),
                "initial_total": total,
                "final_total": final_total,
                "final_snapshot_verified": final_snapshot_verified,
                "duplicate_ids_initial": sorted(set(duplicate_initial)),
                "duplicate_ids_final": sorted(set(duplicate_final)),
                "changed_rows_between_snapshots": changed_rows,
                "listing_phase_seconds": round(listing_seconds, 6),
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
                "company_published_job_ids": sorted(initial_rows),
            })
            return self.result

        self.result.jobs = await self.hydrate(self.result.jobs, lambda job: self.detail(job))
        # Records fetched after membership drift keep their initial listing rows;
        # drift itself is already recorded as an error and prevents certification.
        self.result.metadata.update({
            "initial_job_ids": sorted(initial_rows),
            "final_job_ids": sorted(final_rows),
            "initial_total": total,
            "final_total": final_total,
            "final_snapshot_verified": final_snapshot_verified,
            "duplicate_ids_initial": sorted(set(duplicate_initial)),
            "duplicate_ids_final": sorted(set(duplicate_final)),
            "changed_rows_between_snapshots": changed_rows,
            "description_source": "Each exact listed native detail page section.job-description div#ats-description",
            "source_has_structured_compensation_field": False,
            "salary_policy": "Exact Summary Pay Range lines in a published full description are retained verbatim; other compensation remains null.",
            "job_attachment_links": {
                job.job_id: job.raw.get("attachment_links", [])
                for job in self.result.jobs
                if job.raw.get("attachment_links")
            },
            "pipeline_language_hits": {
                job.job_id: job.raw["pipeline_language_hits"]
                for job in self.result.jobs
                if job.raw.get("pipeline_language_hits")
            },
            "listing_phase_seconds": round(listing_seconds, 6),
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "company_published_job_ids": sorted(initial_rows),
        })
        self.result.listing_complete = (
            not self.result.errors
            and len(initial_rows) == total
            and final_total == total
            and set(final_rows) == set(initial_rows)
            and "sample_limit" not in self.result.warnings
        )
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = round(listing_seconds, 6)
        return self.result
