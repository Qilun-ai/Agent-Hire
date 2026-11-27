"""Citibank's own TalentBrew/Radancy server-rendered search journey.

Membership authority is Citibank's official unfiltered listing at
https://jobs.citi.com/search-jobs (org ``287``, host ``jobs.citi.com``),
paged with ``?p=N`` (15 rows per page, default Date Posted sort). Full
descriptions come from ``.ats-description`` on each exact ``/job/.../287/...``
detail URL the listing publishes; the JSON-LD ``JobPosting`` block is retained
as corroboration. The Workday (``citi.wd5.myworkdayjobs.com``) per-job apply
link is retained only; no Workday board is enumerated and no universal ATS
discovery is used.

Observed quirks (see ``companies/citibank/research/``):

- The bare ``/search-jobs`` response is CDN-cached and stale (observed
  ``3118`` results, page timestamp ``06/11/2026``) while ``?p=1`` is live
  (observed ``3113`` results, fresh timestamp) and matches the company-shipped
  AJAX page one. The collector therefore checks live page one first.
- The 208-page SSR walk is churn-prone: Date Posted ordering shifts while the
  walk is in flight (observed 4 repeated and 3 skipped IDs during a 296s walk).
  The page ships a show-all AJAX action (``pagination-show-all`` sets
  ``RecordsPerPage = TotalResults`` and ``CurrentPage = 1``); its one response
  is a coherent unfiltered snapshot and is used as membership authority. If
  that official action is unavailable, the paged SSR walk remains a fallback
  that records churn loudly rather than certifying an incoherent list.
- The in-page Next href renders as ``/search-jobs&p=N`` (missing ``?``)
  which redirects home; the observed working SSR form is ``?p=N``.
- Each ``.ats-description`` opens with identical TalentBrew evergreen
  template nodes (``h2.jd-evergreen-hl`` "Discover your future at Citi" plus
  ``p.jd-evergreen-text``, then a second ``jd-evergreen-hl`` "Job Overview"
  heading). After stripping those template nodes the remaining body equals
  the JSON-LD ``JobPosting.description`` text exactly (10/10 inspected
  details). Descriptions stored here are the stripped role body; the stripped
  template nodes are retained in ``raw.source_ui`` so nothing is lost, and
  the parser raises if the stripped body no longer equals the JSON-LD twin.
- Standard footer policy links (EEO poster, EEO policy, accessibility, and
  the Illinois AI supplement) travel inside every description. They are not
  role attachments; any other PDF/DOC link in a description fails that
  job's detail loudly for manual review.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4 import NavigableString

from .base import Adapter
from ..models import text_html

ORG_ID = "287"
BASE = "https://jobs.citi.com"
ENTRY_PATH = "/search-jobs"
DETAIL_DESCRIPTION_METHOD = "citibank.native_job_page.ats-description"
CARD_ROUTE = re.compile(r"^/job/[^/]+/[^/]+/287/\d+$")
APPLY_HOST = "citi.wd5.myworkdayjobs.com"
# Standard footer policy documents that ship inside descriptions: the EEOC
# poster, the Illinois AI supplement, and jurisdiction legal notices (e.g.
# the San Francisco Fair Chance Ordinance poster). Any other PDF/DOC link
# is treated as a role attachment needing manual capture.
POLICY_LINK_MARKERS = (
    "eeoc",
    "eeo_aa",
    "eeo-aa-policy",
    "know your rights",
    "accessibility",
    "illinois",
    "fair chance",
    "fairchance",
    "fco",
    "mhlw",
    "/policies/",
)
POSTED_FORMATS = ("%b. %d, %Y", "%b %d, %Y", "%B %d, %Y", "%b. %d,%Y")
CLOSE_FORMATS = ("%b %d, %Y", "%b. %d, %Y", "%B %d, %Y", "%b. %d,%Y")


def _parse_date(value, formats):
    """Parse a visible source date to ISO; None when unparseable."""
    value = (value or "").strip()
    if not value:
        return None
    # Source also uses the four-letter "Sept" abbreviation ("Sept 24, 2026").
    value = re.sub(r"(?i)\bsept(?=\s+\d)", "Sep", value)
    from datetime import datetime

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _norm_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _is_policy_link(href, label=""):
    """Whether a description link is a standard legal-notice document."""
    host = (urlsplit(href).hostname or "").lower()
    if host in {"www.eeoc.gov", "eeoc.gov"} or host.endswith(".gov") or host.endswith(".gov.uk"):
        return True
    if host.endswith(".go.jp"):
        return True
    if "sfgov" in host:
        return True
    lowered = f"{href.lower()} {label.lower()}"
    return any(marker in lowered for marker in POLICY_LINK_MARKERS)


def _role_attachment_links(description_html):
    """Non-policy PDF/DOC links inside a description (role attachments)."""
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            continue
        label = anchor.get_text(" ", strip=True)
        if _is_policy_link(href, label):
            continue
        values.append({"url": href, "label": label or None})
    return values


def _h2_value(h2):
    """Return the free-text value immediately after an ``<h2>`` label node.

    Citibank renders ``<h2>Label</h2>Value`` where the value is normally a
    bare text node; an empty value is rendered as a dashed separator ``<p>``.
    """
    node = h2.next_sibling
    if node is None:
        return None
    if isinstance(node, NavigableString):
        value = str(node).strip()
        return value or None
    # Tag follows the label: only a real (non-separator) payload counts.
    text = node.get_text(" ", strip=True)
    if text and text.strip("-").strip():
        return text
    return None


class Citibank(Adapter):
    """Follow Citibank's own paged SSR search and exact native detail pages."""

    VERIFICATION_REVISION = "citibank.talentbrew-showall-and-native-details.3"
    CAREERS_HOST = "jobs.citi.com"

    def _check_url(self, url, *, path_prefix):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname != self.CAREERS_HOST:
            raise ValueError(f"Citibank page left its official host: {url}")
        if not parsed.path.startswith(path_prefix):
            raise ValueError(f"Citibank page has an unexpected path: {url}")
        return str(url)

    # ---- listing -----------------------------------------------------
    @classmethod
    def parse_listing_page(cls, html, page_url):
        soup = BeautifulSoup(html, "lxml")
        section = soup.select_one("section#search-results")
        if section is None:
            raise ValueError(f"Citibank listing page {page_url} has no #search-results")
        try:
            total = int(section.get("data-total-job-results"))
            total_results = int(section.get("data-total-results"))
            total_pages = int(section.get("data-total-pages"))
            current = int(section.get("data-current-page"))
            per_page = int(section.get("data-records-per-page"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Citibank listing page {page_url} lacks pagination attributes") from exc
        if total != total_results:
            raise ValueError(
                f"Citibank listing page {page_url} total fields disagree: "
                f"data-total-job-results={total} vs data-total-results={total_results}"
            )
        sort_criteria = section.get("data-sort-criteria")
        sort_direction = section.get("data-sort-direction")
        if (sort_criteria, sort_direction) != ("5", "1"):
            raise ValueError(
                f"Citibank listing page {page_url} sort changed from observed "
                f"Date Posted (5/1): {sort_criteria}/{sort_direction}"
            )
        rows = []
        for li in soup.select("li.sr-job-item"):
            anchor = li.select_one("a.sr-job-item__link")
            if anchor is None or not anchor.get("href") or not anchor.get("data-job-id"):
                raise ValueError(f"Citibank listing page {page_url} has an unreadable row")
            href = anchor.get("href")
            job_id = anchor.get("data-job-id")
            if not CARD_ROUTE.fullmatch(urlsplit(href).path):
                raise ValueError(f"Citibank listing row has an unexpected detail route: {href}")
            if urlsplit(href).path.rsplit("/", 1)[-1] != job_id:
                raise ValueError(f"Citibank listing row ID does not match its detail route: {href}")
            title = anchor.get_text(" ", strip=True)
            if not title:
                raise ValueError(f"Citibank listing row {job_id} has no title")
            type_el = li.select_one(".sr-job-type")
            listing_job_type = type_el.get_text(" ", strip=True) if type_el else None
            loc_el = li.select_one(".sr-job-location")
            listing_location = loc_el.get_text(" ", strip=True) if loc_el else None
            if not listing_location:
                raise ValueError(f"Citibank listing row {job_id} has no location")
            rows.append({
                "job_id": job_id,
                "href": href,
                "title": title,
                "listing_job_type": listing_job_type,
                "listing_location": listing_location,
                "page_url": page_url,
            })
        return rows, {
            "total": total,
            "total_pages": total_pages,
            "current": current,
            "per_page": per_page,
            "sort_criteria": sort_criteria,
            "sort_direction": sort_direction,
        }

    # ---- detail ------------------------------------------------------
    @classmethod
    def parse_detail(cls, html, *, expected_job_id, listing_row, detail_url):
        parsed_url = urlsplit(str(detail_url))
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "jobs.citi.com"
            or not CARD_ROUTE.fullmatch(parsed_url.path)
            or parsed_url.path.rsplit("/", 1)[-1] != expected_job_id
        ):
            raise ValueError(
                f"Citibank detail URL does not match expected job ID {expected_job_id}: {detail_url}"
            )
        soup = BeautifulSoup(html, "lxml")
        heading = soup.select_one("h1.job-title-heading")
        if heading is None:
            raise ValueError(f"Citibank detail {detail_url} has no job-title heading")
        title = heading.get_text(" ", strip=True)
        if not title:
            raise ValueError(f"Citibank detail {detail_url} has an empty job-title heading")

        info = {}
        for div in soup.select(".job-description__desc-job-info"):
            term_el = div.find("h2")
            detail_el = div.find("p", class_="job-description__desc-detail")
            if term_el is None or detail_el is None:
                continue
            term = _norm_text(term_el.get_text(" ", strip=True))
            info[term] = _norm_text(detail_el.get_text(" ", strip=True)) or None

        # JSON-LD JobPosting (structured twin of the rendered body). A few
        # live pages embed bare control characters in the JSON strings, so
        # Python's strict parse fails; ``strict=False`` admits those pages
        # and yields the same fields. The rendered heading stays the title
        # authority (one observed page encodes quotes differently in JSON-LD
        # than in the heading); the JSON-LD value is retained in raw.
        ld = None
        ld_el = soup.select_one('script[type="application/ld+json"]')
        if ld_el is not None and ld_el.string:
            try:
                ld = json.loads(ld_el.string, strict=False)
            except ValueError:
                ld = None
        if ld is not None and ld.get("@type") != "JobPosting":
            ld = None
        if ld is not None and ld.get("url") != detail_url:
            raise ValueError(f"Citibank detail {expected_job_id} JSON-LD url differs")
        ld_req = (ld.get("identifier") or {}).get("value") if ld is not None else None
        req = info.get("Job Req Id:")
        if req is None:
            raise ValueError(f"Citibank detail {expected_job_id} has no Job Req Id")
        if ld_req is not None and str(ld_req) != str(req):
            raise ValueError(
                f"Citibank detail {expected_job_id} req mismatch: {req} vs {ld_req}"
            )

        desc_el = soup.select_one(".ats-description")
        if desc_el is None:
            raise ValueError(f"Citibank detail {expected_job_id} has no .ats-description")
        # Retention: full rendered inner HTML before template stripping.
        ats_full_html = desc_el.decode_contents()
        # Evergreen template variants observed live: the full three-node
        # preamble (Discover + text + Job Overview) on most pages, and a
        # one-node variant (Job Overview heading only) on some Polish
        # postings. Strip whatever template nodes are present; the
        # stripped-equals-JSON-LD check below is the real guard.
        preamble_nodes = desc_el.select("h2.jd-evergreen-hl, p.jd-evergreen-text")
        preamble_html_parts = [str(node) for node in preamble_nodes]
        preamble_text = _norm_text(" ".join(node.get_text(" ", strip=True) for node in preamble_nodes))
        for node in preamble_nodes:
            node.decompose()
        description_html = desc_el.decode_contents()
        description_text = text_html(description_html)
        if len(description_text.strip()) < 100:
            raise ValueError(f"Citibank detail {expected_job_id} description is implausibly short")
        ld_description_html = (ld.get("description") if ld is not None else None) or None
        if ld_description_html is not None and _norm_text(description_text) != _norm_text(
            text_html(ld_description_html)
        ):
            raise ValueError(
                f"Citibank detail {expected_job_id} stripped body no longer equals "
                f"its JSON-LD description twin"
            )
        role_attachments = _role_attachment_links(description_html)
        if role_attachments:
            raise ValueError(
                f"Citibank detail {expected_job_id} has non-policy role attachment links "
                f"needing manual capture: {role_attachments}"
            )

        # Apply link: per-job Workday URL carrying the requisition ID.
        apply_anchors = [a for a in soup.select("a.job-apply") if a.get("data-apply-url") or a.get("href")]
        if not apply_anchors:
            raise ValueError(f"Citibank detail {expected_job_id} has no apply link")
        apply_url = apply_anchors[0].get("data-apply-url") or apply_anchors[0].get("href")
        apply_parsed = urlsplit(apply_url)
        if apply_parsed.scheme != "https" or apply_parsed.hostname != APPLY_HOST:
            raise ValueError(f"Citibank detail {expected_job_id} apply link left the inspected issuer")
        if str(req) not in apply_url:
            raise ValueError(f"Citibank detail {expected_job_id} apply link does not carry its req ID")

        # Structured family/time/salary/close block inside the description.
        h2_values = {}
        for h2 in desc_el.select("h2"):
            label = _norm_text(h2.get_text(" ", strip=True))
            if label and label not in h2_values:
                h2_values[label] = _h2_value(h2)
        job_family_group = h2_values.get("Job Family Group:") or None
        job_family = h2_values.get("Job Family:") or None
        time_type = h2_values.get("Time Type:") or None
        primary_location_raw = h2_values.get("Primary Location:") or None
        h2_salary = h2_values.get("Primary Location Full Time Salary Range:") or None
        hourly = h2_values.get("The hourly rate corresponding to the annual range is:") or None
        close_raw = h2_values.get("Anticipated Posting Close Date:") or None

        # The desc-list salary uses US formatting ("$120,800.00") while the
        # in-body h2 uses locale formatting (non-breaking-space thousands,
        # comma decimals, currency suffix); on a few postings the two ranges
        # genuinely differ. The desc-list value is the stored salary; the
        # h2 twin is retained verbatim in raw for audit.
        desc_salary = info.get("Primary Location Full Time Salary Range:")
        salary_text = desc_salary or h2_salary
        salary = None
        if salary_text:
            salary = {
                "source_text": salary_text,
                "source_field": "detail desc-list Primary Location Full Time Salary Range",
                "semantics": (
                    "Source annual salary range retained verbatim; no conversion. "
                    "A $0.00 range is the source placeholder and is retained as published, "
                    "not treated as zero pay or converted."
                ),
            }
            if hourly:
                salary["hourly_range"] = hourly

        location_s = info.get("Location(s):")
        if not location_s:
            raise ValueError(f"Citibank detail {expected_job_id} has no Location(s)")
        ld_locations = (ld.get("jobLocation") or []) if ld is not None else []
        detail_job_type = info.get("Job Type:")
        listing_job_type = (listing_row or {}).get("listing_job_type")
        workplace_type = detail_job_type or listing_job_type

        # A systematic one-day display-vs-ISO difference exists on older
        # postings (visible "Nov. 03, 2026" vs JSON-LD "2026-11-04", like
        # the BlackRock quirk). Both values are retained; the ISO field is
        # the normalized posting date.
        date_posted_raw = info.get("Posted:")
        ld_date = ld.get("datePosted") if ld is not None else None
        parsed_posted = _parse_date(date_posted_raw, POSTED_FORMATS)
        date_posted = ld_date or parsed_posted
        # One live posting carries a corrupted close-date token ("lis 04,
        # 2026"); an unparseable close date is retained verbatim in raw with
        # a null normalized date rather than failing an otherwise complete
        # detail (unknown dates stay null; no date is fabricated).
        valid_through = _parse_date(close_raw, CLOSE_FORMATS) if close_raw else None
        org = ((ld.get("hiringOrganization") or {}).get("name") if ld is not None else None)
        addresses = []
        for place in ld_locations:
            if isinstance(place, dict):
                addresses.append(place.get("address"))
        return {
            "title": title,
            "description_html": description_html,
            "description_text": description_text,
            "ats_full_html": ats_full_html,
            "preamble_html_parts": preamble_html_parts,
            "preamble_text": preamble_text,
            "requisition_id": str(req),
            "apply_url": apply_url,
            "info": info,
            "date_posted": date_posted,
            "date_posted_raw": date_posted_raw,
            "valid_through": valid_through,
            "close_date_raw": close_raw,
            "jsonld": ld,
            "locations": [{"location": location_s}],
            "job_family_group": job_family_group,
            "job_family": job_family,
            "time_type": time_type,
            "primary_location_raw": primary_location_raw,
            "h2_salary_raw": h2_salary,
            "workplace_type": workplace_type,
            "detail_job_type": detail_job_type,
            "listing_job_type": listing_job_type,
            "salary": salary,
            "employer": org or "CITI",
            "addresses": addresses,
            "detail_url": detail_url,
        }

    def _make_job(self, row, detail):
        detail_url = urljoin(BASE + "/", row["href"]) if not row["href"].startswith("http") else row["href"]
        raw = {
            "listing": row,
            "listing_page_url": row["page_url"],
            "detail": {
                "info": detail["info"],
                "jsonld": detail["jsonld"],
                "addresses": detail["addresses"],
                "apply_url": detail["apply_url"],
                "job_family_group": detail["job_family_group"],
                "job_family": detail["job_family"],
                "time_type": detail["time_type"],
                "primary_location_raw": detail["primary_location_raw"],
                "h2_salary_raw": detail["h2_salary_raw"],
                "close_date_raw": detail["close_date_raw"],
            },
            "source_ui": {
                "listing_href": row["href"],
                "listing_location": row["listing_location"],
                "listing_job_type": row["listing_job_type"],
                "detail_url": detail_url,
                "evergreen_preamble_text": detail["preamble_text"],
                "evergreen_preamble_html_parts": detail["preamble_html_parts"],
                "ats_description_full_html": detail["ats_full_html"],
                "role_attachment_links": _role_attachment_links(detail["description_html"]),
            },
        }
        job = self.job(
            raw,
            title=detail["title"],
            url=detail_url,
            job_id=row["job_id"],
            requisition_id=detail["requisition_id"],
            apply_url=detail["apply_url"],
            description=detail["description_html"],
            method=DETAIL_DESCRIPTION_METHOD,
            description_url=detail_url,
            locations=detail["locations"],
            department=detail["job_family_group"],
            team=None,
            employment_type=detail["time_type"],
            workplace_type=detail["workplace_type"],
            date_posted=detail["date_posted"],
            date_posted_raw=detail["date_posted_raw"],
            date_updated=None,
            valid_through=detail["valid_through"],
            salary=detail["salary"],
            employer=detail["employer"],
        )
        job.description_text = detail["description_text"]
        job.record_identity = f"citibank:{row['job_id']}"
        if detail["title"] != row["title"]:
            job.warnings.append("listing_title_differs_from_detail_title")
        return job

    @staticmethod
    def _show_all_params(html, total):
        """Criteria for Citibank's own show-all AJAX action.

        Values mirror the rendered ``#search-results`` / ``#search-filters``
        data attributes consumed by the site's ``search.js``; only
        ``RecordsPerPage`` is set to the live unfiltered total, exactly as
        the published ``.pagination-show-all`` handler does.
        """
        soup = BeautifulSoup(html, "lxml")
        section = soup.select_one("section#search-results")
        filters = soup.select_one("section#search-filters")
        if section is None:
            raise ValueError("Citibank show-all needs the rendered #search-results attributes")
        return {
            "ActiveFacetID": section.get("data-active-facet-id", "0"),
            "CurrentPage": "1",
            "RecordsPerPage": str(total),
            "Distance": section.get("data-distance", "50"),
            "RadiusUnitType": (filters.get("data-radius-unit-type", "2") if filters else "2"),
            "Keywords": "",
            "Location": "",
            "ShowRadius": section.get("data-show-radius", "False"),
            "IsPagination": "False",
            "CustomFacetName": "",
            "FacetTerm": "",
            "FacetType": section.get("data-facet-type", "0"),
            "SearchResultsModuleName": section.get("data-search-results-module-name", "Search Results"),
            "SearchFiltersModuleName": (
                filters.get("data-search-filters-module-name", "Search Filters") if filters else "Search Filters"
            ),
            "SortCriteria": section.get("data-sort-criteria", "5"),
            "SortDirection": section.get("data-sort-direction", "1"),
            "SearchType": section.get("data-search-type", "1"),
            "CategoryFacetTerm": "",
            "CategoryFacetType": "0",
            "LocationFacetTerm": "",
            "LocationFacetType": "0",
            "KeywordType": "",
            "LocationType": "",
            "LocationPath": "",
            "OrganizationIds": ORG_ID,
            "RefinedKeywords": "",
            "AjaxCharLimit": "4096",
            "ResultsType": section.get("data-results-type", "0"),
        }

    async def _fetch_show_all(self, page1_html, initial_total):
        """Fetch one coherent unfiltered jobs snapshot via the site's show-all action."""
        url = f"{BASE}{ENTRY_PATH}/results"
        total = initial_total
        attempts = []
        for attempt in range(1, 5):
            params = self._show_all_params(page1_html, total)
            response = await self.http.get(url, params=params)
            self._check_url(response.url, path_prefix=ENTRY_PATH)
            try:
                payload = response.json()
            except ValueError as exc:
                raise ValueError(f"Citibank show-all returned non-JSON: {exc}") from exc
            if not isinstance(payload, dict) or not payload.get("results"):
                raise ValueError("Citibank show-all response has no results payload")
            rows, meta = self.parse_listing_page(payload["results"], str(response.url))
            attempts.append({
                "attempt": attempt,
                "records_per_page": meta["per_page"],
                "total": meta["total"],
                "total_pages": meta["total_pages"],
                "current_page": meta["current"],
                "rows": len(rows),
            })
            if meta["current"] != 1:
                raise ValueError(f"Citibank show-all returned page {meta['current']}, expected 1")
            if meta["total"] <= 0:
                raise ValueError("Citibank show-all reports zero jobs")
            # The live total can change between the page-1 check and this
            # request. If RecordsPerPage is now too small, retry with the
            # total the show-all response itself returned.
            if meta["total_pages"] != 1 or len(rows) != meta["total"]:
                total = max(meta["total"], total)
                continue
            return rows, meta, str(response.url), attempts
        raise ValueError(f"Citibank show-all did not return one coherent snapshot: {attempts}")

    async def _collect_ssr_listing(self, page1_url, page_rows, first_meta):
        """Fallback: walk every official ?p=N page (live churn is recorded)."""
        total = first_meta["total"]
        total_pages = first_meta["total_pages"]
        per_page = first_meta["per_page"]
        all_pages = [(1, page1_url, page_rows)]
        for page in range(2, total_pages + 1):
            if not self.page_guard():
                self.result.errors.append("citibank_page_budget_exhausted")
                break
            url = f"{BASE}{ENTRY_PATH}?p={page}"
            response = await self.http.get(url)
            self._check_url(response.url, path_prefix=ENTRY_PATH)
            if str(response.url) != url:
                raise ValueError(f"Citibank page {page} redirected: {response.url}")
            page_rows, meta = self.parse_listing_page(response.text, url)
            if meta["current"] != page:
                self.result.errors.append(
                    f"citibank_page_mismatch: expected page {page}, got {meta['current']}"
                )
            if meta["total"] != total or meta["total_pages"] != total_pages:
                self.result.errors.append(
                    f"citibank_listing_total_changed: page {page} total "
                    f"{meta['total']}/{meta['total_pages']} != page-1 {total}/{total_pages}"
                )
            all_pages.append((page, url, page_rows))
        rows_by_id = {}
        page_order = []
        for page, url, page_rows in all_pages:
            for row in page_rows:
                if row["job_id"] in rows_by_id:
                    self.result.errors.append(
                        f"citibank_listing_repeats_id: {row['job_id']} on page {page}"
                    )
                    continue
                rows_by_id[row["job_id"]] = row
                page_order.append(row["job_id"])
        pages_reconciled = {page: len(page_rows) for page, _, page_rows in all_pages}
        if len(rows_by_id) != total:
            self.result.errors.append(
                f"citibank_listing_count_mismatch: expected {total}, unique rows {len(rows_by_id)}"
            )
        terminal_size = pages_reconciled.get(total_pages, 0)
        expected_last = total - per_page * (total_pages - 1)
        if terminal_size != expected_last:
            self.result.errors.append(
                f"citibank_terminal_page_mismatch: expected {expected_last}, got {terminal_size}"
            )
        return rows_by_id, page_order, pages_reconciled, total, total_pages, per_page

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        entry_parts = urlsplit(entry_url)
        if (
            entry_parts.scheme != "https"
            or entry_parts.hostname != self.CAREERS_HOST
            or entry_parts.path.rstrip("/") != ENTRY_PATH
        ):
            raise ValueError("Citibank dedicated flow must start from its official /search-jobs entry")

        # ---- listing: Citibank's own unfiltered show-all search -------
        # The SSR ?p=N pages are snapshots of a live Date-Posted-sorted
        # result set; postings can move between pages while a 208-page walk
        # is in flight, producing skipped and repeated IDs. The page itself
        # ships an official "show all" AJAX action (search.js
        # .pagination-show-all sets RecordsPerPage = TotalResults,
        # CurrentPage = 1), whose single response is one coherent snapshot
        # with the same totals, rows, org scope and sort. Use that as the
        # membership authority; keep the paged SSR walk as a documented
        # fallback if the company's AJAX action is unavailable.
        listing_started = time.perf_counter()
        page1_url = f"{BASE}{ENTRY_PATH}?p=1"
        first_response = await self.http.get(page1_url)
        self._check_url(first_response.url, path_prefix=ENTRY_PATH)
        if str(first_response.url) != page1_url:
            raise ValueError(f"Citibank page 1 redirected: {first_response.url}")
        page_rows, first_meta = self.parse_listing_page(first_response.text, page1_url)
        page1_total = first_meta["total"]
        if page1_total <= 0 or first_meta["total_pages"] <= 0 or first_meta["per_page"] <= 0:
            raise ValueError("Citibank listing reports zero jobs; independent review required")

        listing_method = "official_ajax_show_all"
        show_all_url = None
        show_all_attempts = []
        show_first_page_matches = None
        try:
            show_rows, show_meta, show_all_url, show_all_attempts = await self._fetch_show_all(
                first_response.text, page1_total
            )
        except Exception as exc:
            listing_method = "ssr_pages_fallback"
            self.result.warnings.append(
                f"citibank_show_all_failed_fallback_ssr: {type(exc).__name__}: {exc}"
            )
            rows_by_id, page_order, pages_reconciled, total, total_pages, per_page = (
                await self._collect_ssr_listing(page1_url, page_rows, first_meta)
            )
        else:
            total = show_meta["total"]
            total_pages = show_meta["total_pages"]
            per_page = show_meta["per_page"]
            rows_by_id = {}
            page_order = []
            for row in show_rows:
                if row["job_id"] in rows_by_id:
                    self.result.errors.append(
                        f"citibank_listing_repeats_id: {row['job_id']} in show-all response"
                    )
                    continue
                rows_by_id[row["job_id"]] = row
                page_order.append(row["job_id"])
            pages_reconciled = {1: len(show_rows)}
            show_first_page_matches = page_order[: len(page_rows)] == [r["job_id"] for r in page_rows]
            if not show_first_page_matches:
                self.result.warnings.append(
                    "citibank_show_all_page1_drift: live page-1 rows changed between the SSR "
                    "check and the show-all snapshot; show-all membership used"
                )
            self.result.pages = 1
            if len(rows_by_id) != total:
                self.result.errors.append(
                    f"citibank_listing_count_mismatch: expected {total}, unique rows {len(rows_by_id)}"
                )
            if total_pages != 1 or len(show_rows) != total:
                self.result.errors.append(
                    f"citibank_show_all_not_single_snapshot: total {total}, pages {total_pages}, "
                    f"rows {len(show_rows)}"
                )
        listing_seconds = time.perf_counter() - listing_started

        # ---- details: fetch every exact published URL ----
        detail_started = time.perf_counter()

        async def detail_for(job):
            url = job.raw["source_ui"]["detail_url"]
            response = await self.http.get(url)
            self._check_url(response.url, path_prefix="/job/")
            parsed = self.parse_detail(
                response.text,
                expected_job_id=job.job_id,
                listing_row=job.raw["listing"],
                detail_url=str(response.url),
            )
            if str(response.url) != url:
                raise ValueError(f"Citibank detail {job.job_id} redirected")
            fresh = self._make_job(job.raw["listing"], parsed)
            fresh.description_verified = job.description_verified
            return fresh

        # Resume cache guard: a previously saved detail may only be reused
        # while the live listing still publishes the same title for that
        # exact published detail URL. A changed listing title forces a fresh
        # fetch so a rolling snapshot never certifies stale content.
        cache_titles_rejected = 0
        for row in rows_by_id.values():
            detail_url = urljoin(BASE + "/", row["href"]) if not row["href"].startswith("http") else row["href"]
            cached = self.options.cached_jobs.get(detail_url)
            if cached is not None and cached.title != row["title"]:
                self.options.cached_jobs.pop(detail_url, None)
                cache_titles_rejected += 1

        pending = []
        for row in [rows_by_id[jid] for jid in page_order]:
            detail_url = urljoin(BASE + "/", row["href"]) if not row["href"].startswith("http") else row["href"]
            pending.append(
                self.job(
                    {"listing": row, "source_ui": {"detail_url": detail_url}},
                    title=row["title"],
                    url=detail_url,
                    job_id=row["job_id"],
                )
            )
            pending[-1].record_identity = f"citibank:{row['job_id']}"
        hydrated = await self.hydrate(pending, detail_for)
        for job in hydrated:
            # Keep the listing evidence on every record aligned with the
            # current show-all snapshot, including verified-cache hits whose
            # raw listing row came from an earlier paged snapshot.
            row = rows_by_id[job.job_id]
            job.raw["listing"] = row
            job.raw["listing_page_url"] = row["page_url"]
            source_ui = job.raw.setdefault("source_ui", {})
            source_ui["listing_href"] = row["href"]
            source_ui["listing_location"] = row["listing_location"]
            source_ui["listing_job_type"] = row["listing_job_type"]
            self.add(job)
        detail_seconds = time.perf_counter() - detail_started

        self.result.expected_total = total
        self.result.pages = 1 if listing_method == "official_ajax_show_all" else total_pages
        self.result.listing_complete = len(rows_by_id) == total and not any(
            e.startswith(
                (
                    "citibank_listing_count_mismatch",
                    "citibank_terminal_page_mismatch",
                    "citibank_page_budget_exhausted",
                    "citibank_listing_total_changed",
                    "citibank_page_mismatch",
                    "citibank_listing_repeats_id",
                    "citibank_show_all_not_single_snapshot",
                )
            )
            for e in self.result.errors
        )
        failed_details = [e for e in self.result.errors if e.startswith("Detail ")]
        if failed_details:
            self.result.listing_complete = False
        self.result.metadata.update(
            {
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": entry_url,
                "live_page1_url": page1_url,
                "membership_authority": (
                    "Citibank official /search-jobs unfiltered listing (org 287, TalentBrew); "
                    "official show-all AJAX action returns one coherent snapshot"
                    if listing_method == "official_ajax_show_all" else
                    "Citibank official /search-jobs unfiltered listing (org 287, TalentBrew, ?p=N pages fallback)"
                ),
                "org_id": ORG_ID,
                "listing_filter_behavior": "No keywords/location/facet filters applied; default Date Posted sort (criteria 5, direction 1)",
                "ui_pagination": (
                    "Show-all AJAX action (RecordsPerPage = live total, CurrentPage = 1) advertised by the "
                    "official page; SSR ?p=N pages were observed to skip/repeat IDs during a 208-page walk "
                    "because Date Posted ordering shifts while the walk is in flight"
                    if listing_method == "official_ajax_show_all" else
                    "Server-rendered ?p=N pages, 15 rows per page; bare /search-jobs is CDN-cached/stale while ?p=1 is live"
                ),
                "listing_method": listing_method,
                "show_all_request_url": show_all_url,
                "show_all_attempts": show_all_attempts,
                "show_all_first_page_matches_ssr_page1": show_first_page_matches,
                "page1_total": page1_total,
                "show_all_total": total if listing_method == "official_ajax_show_all" else None,
                "total": total,
                "total_pages": total_pages,
                "records_per_page": per_page,
                "page_row_counts": pages_reconciled,
                "listed_job_ids_in_order": page_order,
                "description_source": ".ats-description role body on each exact published /job/... page with the three evergreen template nodes stripped (stripped body must equal JSON-LD JobPosting.description); full un-stripped inner HTML retained in raw",
                "salary_policy": "Annual range retained verbatim from the desc-list salary field; $0.00 placeholder retained as published; hourly equivalent retained in salary when present; no conversion",
                "collection_wall_seconds": round(time.perf_counter() - started, 6),
                "listing_phase_seconds": round(listing_seconds, 6),
                "detail_phase_wall_seconds": round(detail_seconds, 6),
                "detail_failures": failed_details,
                "cache_reuse_policy": "Verified cached detail pages are reused only when the live listing publishes the same title for the same exact detail URL; title changes force a fresh fetch.",
                "cache_titles_rejected": cache_titles_rejected,
            }
        )
        self.result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        return self.result
