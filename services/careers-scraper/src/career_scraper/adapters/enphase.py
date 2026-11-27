"""Enphase Energy's own careers listing API and /job/<jid> detail journey."""
import hashlib
import html
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html

JID = re.compile(r"^o[A-Za-z0-9]{7}$")
APPLY = re.compile(
    r"^http://app\.jobvite\.com/CompanyJobs/Careers\.aspx\?c=qu49Vfwm&j=([A-Za-z0-9]+)&k=Apply$"
)
# fetch("/api/v2/jobs" + t) and the two init fetches, exactly as shipped.
FETCH_LITERAL = re.compile(r'fetch\("(/api/v2/jobs[^"]*)"')
JOB_DETAIL_PAGE = re.compile(r'jobDetailPage:"(/job)"')
PAGER_CONTROLS = ("goToPage", "updateQueryStringParameter", "current_page")

ENTRY_HOST = "enphase.com"
COMPONENT_SCRIPT_PATH = ("/themes/custom/smalcode/dist/career-search-form.js")
LISTING_PATH = "/api/v2/jobs"
CATEGORIES_PATH = "/api/v2/jobs/categories"
LOCATIONS_PATH = "/api/v2/jobs/locations"
DETAIL_PATH = "/job/"


def compact_body_hash(text):
    """Whitespace-free body hash of a text block (reference-comparison form)."""
    value = unicodedata.normalize("NFC", text or "")
    value = re.sub(r"[\s ﻿]", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unescape(value):
    return html.unescape(str(value)) if value is not None else None


def _salary_from_description(text):
    """Retain the exact description line stating the pay-range sentence."""
    lines = [
        line.strip()
        for line in (text or "").splitlines()
        if re.search(r"range for this position", line, re.I)
    ]
    if not lines:
        return None
    return {"source_text": " ".join(lines), "source_field": "description__value"}


def _job_attachment_links(description_html):
    soup = BeautifulSoup(description_html or "", "lxml")
    values = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            values.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return values


class Enphase(Adapter):
    """Follow exactly the company-shipped listing API and native detail routes."""

    VERIFICATION_REVISION = "enphase.native-careers-listing-api-and-job-detail.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def _check_enphase_url(url):
        parsed = urlsplit(str(url))
        if parsed.scheme != "https" or parsed.hostname not in {"enphase.com", "www.enphase.com"}:
            raise ValueError(f"Enphase company page left the official HTTPS site: {url}")
        return parsed

    @staticmethod
    def _listing_api_from_script(script):
        """Return the exact listing/categories/locations paths shipped in JS."""
        literals = sorted(set(FETCH_LITERAL.findall(script)))
        expected = {LISTING_PATH, CATEGORIES_PATH, LOCATIONS_PATH}
        if set(literals) != expected:
            raise ValueError(
                "Enphase career-search-form.js no longer fetches exactly its observed "
                f"listing/categories/locations endpoints: {literals}"
            )
        detail = JOB_DETAIL_PAGE.search(script)
        if not detail or detail.group(1) != DETAIL_PATH.rstrip("/") or "getJobviteLink" not in script:
            raise ValueError("Enphase career-search-form.js jobDetailPage route changed")
        if any(token not in script for token in PAGER_CONTROLS):
            raise ValueError("Enphase career-search-form.js pager control changed")
        return literals

    @staticmethod
    def _page_url(origin, listing_path, page):
        # The shipped initial load is the bare path; page switches add ?page=N.
        if page == 0:
            return origin + listing_path
        return f"{origin}{listing_path}?page={page}"

    @staticmethod
    def _check_payload(payload, snapshot_name):
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            raise ValueError(f"Enphase {snapshot_name} listing response has no rows array")
        pager = payload.get("pager")
        if not isinstance(pager, dict):
            raise ValueError(f"Enphase {snapshot_name} listing response has no pager")
        if not (isinstance(pager.get("current_page"), int)
                and str(pager.get("total_items", "")).isdigit()
                and isinstance(pager.get("total_pages"), int)
                and isinstance(pager.get("items_per_page"), int)):
            raise ValueError(f"Enphase {snapshot_name} listing pager shape changed: {pager}")
        return pager

    @staticmethod
    def _check_rows(rows, snapshot_name):
        ids = [row.get("jid") if isinstance(row, dict) else None for row in rows]
        if any(not isinstance(identity, str) or not JID.fullmatch(identity) for identity in ids):
            raise ValueError(f"Enphase {snapshot_name} listing contains a missing or invalid jid")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Enphase {snapshot_name} listing repeats a company-published jid")
        for row in rows:
            if not row.get("name") or not row.get("location") or not row.get("requisitionid"):
                raise ValueError(f"Enphase {snapshot_name} listing contains an unidentified job")
            if not isinstance(row.get("applyUrl"), str):
                raise ValueError(f"Enphase {snapshot_name} listing is missing applyUrl")
            apply = _unescape(row["applyUrl"])
            match = APPLY.fullmatch(apply or "")
            if not match or match.group(1) != row["jid"]:
                raise ValueError(f"Enphase listed jid {row['jid']} has an unexpected applyUrl")

    async def _listing_page(self, origin, page, label):
        url = self._page_url(origin, LISTING_PATH, page)
        response = await self.http.get(url)
        if str(response.url) != url:
            raise ValueError(f"Enphase {label} listing page {page} redirected away from its source")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"Enphase {label} listing page {page} is not JSON") from exc
        return url, payload

    async def _listing_rest(self, origin, pager, first, label):
        """Fetch pages 1..total_pages-1 after page 0; return (pages, rows)."""
        total_pages, total_items = pager["total_pages"], int(pager["total_items"])
        pages = [first]
        rows = list(first[2]["rows"])
        for page in range(1, total_pages):
            url, payload = await self._listing_page(origin, page, label)
            page_pager = self._check_payload(payload, label)
            if (page_pager["current_page"] != page
                    or int(page_pager["total_items"]) != total_items
                    or page_pager["total_pages"] != total_pages
                    or page_pager["items_per_page"] != pager["items_per_page"]):
                raise ValueError(f"Enphase {label} listing page {page} pager disagrees: {page_pager}")
            pages.append((page, url, payload))
            rows.extend(payload["rows"])
        n = pager["items_per_page"]
        if n < 1 or total_items < 0 or len(rows) != total_items:
            raise ValueError(f"Enphase {label} listing rows do not reconcile to total_items")
        expected = [n] * (total_pages - 1) + [total_items - n * (total_pages - 1)]
        actual = [len(payload["rows"]) for _, _, payload in pages]
        if actual != expected or any(count < 1 or count > n for count in actual):
            raise ValueError(f"Enphase {label} listing page row counts are inconsistent: {actual}")
        self._check_rows(rows, label)
        return pages, rows

    async def _listing_snapshot(self, origin, label):
        """Fetch every unfiltered page once; return (pages, rows) in UI order."""
        first_url, payload = await self._listing_page(origin, 0, label)
        pager = self._check_payload(payload, label)
        total_pages = pager["total_pages"]
        if total_pages < 1 or pager["current_page"] != 0:
            raise ValueError(f"Enphase {label} listing pager is inconsistent: {pager}")
        pages, rows = await self._listing_rest(origin, pager, (0, first_url, payload), label)
        return pages, rows, pager

    async def _detail_check(self, origin, row):
        """Parse the server-rendered native detail page for one exact jid."""
        jid = row["jid"]
        detail_url = origin + DETAIL_PATH + jid
        response = await self.http.get(detail_url)
        if str(response.url) != detail_url:
            raise ValueError(f"Enphase exact detail route redirected: {detail_url}")
        self._check_enphase_url(response.url)
        soup = BeautifulSoup(response.text, "lxml")
        heading = soup.find("h1")
        if not heading or heading.get_text(" ", strip=True) != _unescape(row["name"]):
            raise ValueError(f"{jid}: native detail h1 differs from the listed title")
        intro = heading.parent
        intro_text = intro.get_text(" ", strip=True)
        if f"Requisition ID: {row['requisitionid']}" not in intro_text:
            raise ValueError(f"{jid}: native detail requisition ID differs from the listing")
        location_heading = intro.find("h5")
        if not location_heading or location_heading.get_text(" ", strip=True) != _unescape(row["location"]):
            raise ValueError(f"{jid}: native detail location differs from the listing")
        bodies = soup.find_all("div", class_="jobvite-detail__content")
        bodies = [body for body in bodies if body.get_text(" ", strip=True)]
        if len(bodies) != 1:
            raise ValueError(f"{jid}: native detail has no single populated description body")
        detail_text = text_html(bodies[0].decode_contents())
        listing_text = text_html(html.unescape(row.get("description__value") or ""))
        if detail_text != listing_text:
            raise ValueError(f"{jid}: native detail body differs from the listing description")
        apply_href = None
        for anchor in soup.select(".jobvite-detail__apply a[href]"):
            apply_href = anchor.get("href", "").strip()
            break
        if apply_href != _unescape(row["applyUrl"]):
            raise ValueError(f"{jid}: native detail apply link differs from the listing applyUrl")
        return {
            "url": detail_url,
            "title": heading.get_text(" ", strip=True),
            "requisition_id": row["requisitionid"],
            "location": _unescape(row["location"]),
            "detail_text_sha256": hashlib.sha256(detail_text.encode()).hexdigest(),
            "detail_body_compact_sha256": compact_body_hash(detail_text),
            "apply_url": apply_href,
        }

    def _make_job(self, row, *, origin, listing_page_url, detail_check=None,
                  initial_row=None, membership_changed=False):
        jid = row["jid"]
        native_url = origin + DETAIL_PATH + jid
        apply_url = _unescape(row["applyUrl"])
        raw_description = row.get("description__value") or ""
        description_html = html.unescape(raw_description)
        description_text = text_html(description_html)
        if not description_html.strip():
            self.result.errors.append(f"{jid}: Enphase public listing description__value is empty")
        elif len(description_text.strip()) < 100:
            self.result.errors.append(f"{jid}: Enphase public listing description is implausibly short")
        salary = _salary_from_description(description_text)
        raw = {
            "listing": row,
            "listing_page_url": listing_page_url,
            "source_ui": {
                "native_job_url": native_url,
                "listed_apply_url": apply_url,
                "rendered_title": _unescape(row["name"]),
                "rendered_requisition_id": row["requisitionid"],
                "rendered_location": _unescape(row["location"]),
                "rendered_category": _unescape(row["category"]),
                "listing_api_url": origin + LISTING_PATH,
                "detail_check": detail_check,
                "attachment_links": _job_attachment_links(description_html),
                "salary_from_description": salary,
            },
        }
        if initial_row is not None and initial_row != row:
            raw["initial_listing"] = initial_row
        job = self.job(
            raw,
            title=_unescape(row["name"]) or row["name"],
            url=native_url,
            job_id=jid,
            requisition_id=row["requisitionid"],
            apply_url=apply_url,
            description=description_html,
            method="enphase.official_careers.listing_api.description__value",
            description_url=listing_page_url,
            locations=[{"location": _unescape(row["location"])}],
            department=_unescape(row["category"]),
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            salary=salary,
        )
        job.description_text = description_text
        job.record_identity = f"enphase:{jid}"
        if membership_changed:
            job.warnings.append("membership_changed_during_collection")
        return job

    async def run(self):
        started = self.result._started_clock
        entry_url = self.company.careers_url
        if not self.page_guard():
            return self.result
        parts = urlsplit(entry_url)
        if (parts.scheme != "https" or parts.hostname not in {"enphase.com", "www.enphase.com"}
                or parts.path.rstrip("/") != "/careers" or parts.query or parts.fragment):
            raise ValueError("Enphase dedicated flow must start from its official HTTPS /careers entry")
        entry = await self.http.get(entry_url)
        self._check_enphase_url(entry.url)
        if urlsplit(str(entry.url)).path.rstrip("/") != "/careers":
            raise ValueError("Enphase official entry redirected away from /careers")
        soup = BeautifulSoup(entry.text, "lxml")
        wrapper = soup.select_one('.career-search-wrapper')
        if not wrapper or "career({ 'variant': 'full', 'language': 'en' })" not in (wrapper.get("x-data") or ""):
            raise ValueError("Enphase official page no longer ships the full-variant career component")
        for token in ("getJobviteLink", "goToPage", "pager['current_page']", "pager['total_pages']"):
            if token not in entry.text:
                raise ValueError("Enphase official page no longer ships its observed card/pager bindings")
        script_nodes = [node for node in soup.find_all("script", src=True)
                        if COMPONENT_SCRIPT_PATH.split("/")[-1] in node.get("src", "")]
        if len(script_nodes) != 1:
            raise ValueError("Enphase official page no longer loads its career-search-form script")
        script_url = urljoin(entry_url, script_nodes[0]["src"])
        script_response = await self.http.get(script_url)
        self._check_enphase_url(script_response.url)
        if not str(script_response.url).startswith(entry_url.rsplit("/careers", 1)[0] + COMPONENT_SCRIPT_PATH):
            raise ValueError("Enphase career component script resolved to an unexpected URL")
        listing_literals = self._listing_api_from_script(script_response.text)
        origin = f"https://{parts.hostname}"

        # Shipped init() order: first listing call, then category/location dropdowns.
        # Remaining pages and the final snapshot follow the goToPage shapes.
        first_url, first_payload = await self._listing_page(origin, 0, "initial")
        initial_pager = self._check_payload(first_payload, "initial")
        if initial_pager["total_pages"] < 1 or initial_pager["current_page"] != 0:
            raise ValueError(f"Enphase initial listing pager is inconsistent: {initial_pager}")
        facet_data = {}
        for path in (CATEGORIES_PATH, LOCATIONS_PATH):
            url = origin + path
            response = await self.http.get(url)
            if str(response.url) != url:
                raise ValueError(f"Enphase facet endpoint redirected: {url}")
            try:
                facet_data[path] = response.json()
            except ValueError as exc:
                raise ValueError(f"Enphase facet endpoint is not JSON: {url}") from exc
        for path, key in ((CATEGORIES_PATH, "category"), (LOCATIONS_PATH, "location")):
            entries = facet_data[path]
            if (not isinstance(entries, list) or not entries
                    or any(not isinstance(item, dict) or not isinstance(item.get(key), str)
                           for item in entries)):
                raise ValueError(f"Enphase {key} facet response shape changed")
        initial_pages, initial_rows = await self._listing_rest(
            origin, initial_pager, (0, first_url, first_payload), "initial")
        initial_by_id = {row["jid"]: row for row in initial_rows}
        initial_ids = [row["jid"] for row in initial_rows]
        if not initial_rows:
            self.result.expected_total = 0
            self.result.pages = initial_pager["total_pages"]
            self.result.errors.append(
                "Enphase official listing returned zero jobs; empty membership requires review")
            return self.result

        detail_started = time.perf_counter()
        representative = {row["jid"]: row for row in initial_rows[:2]}
        detail_checks = {}
        detail_errors = []
        for jid, row in representative.items():
            try:
                detail_checks[jid] = await self._detail_check(origin, row)
            except Exception as exc:
                detail_errors.append(f"{jid}: {type(exc).__name__}: {exc}")
        if detail_errors:
            self.result.errors.extend(detail_errors)
        detail_ui_seconds = time.perf_counter() - detail_started

        final_snapshot_verified = True
        try:
            final_pages, final_rows, final_pager = await self._listing_snapshot(origin, "final")
        except Exception as exc:
            final_snapshot_verified = False
            final_pages, final_rows, final_pager = initial_pages, initial_rows, initial_pager
            self.result.errors.append(
                "Enphase final company-published listing snapshot failed; initial rows retained: "
                f"{type(exc).__name__}: {exc}")
        final_by_id = {row["jid"]: row for row in final_rows}
        final_ids = [row["jid"] for row in final_rows]
        changed_membership = final_snapshot_verified and set(initial_ids) != set(final_ids)
        if changed_membership:
            self.result.errors.append("enphase_official_listing_membership_changed_between_snapshots")
        changed_rows = [jid for jid in final_by_id.keys() & initial_by_id.keys()
                        if final_by_id[jid] != initial_by_id[jid]]

        page_url_by_jid = {}
        for page, url, payload in final_pages:
            for row in payload["rows"]:
                page_url_by_jid[row["jid"]] = url
        all_ids = list(dict.fromkeys(final_ids + [jid for jid in initial_ids if jid not in final_by_id]))
        mapping_started = time.perf_counter()
        for jid in all_ids:
            final_row = final_by_id.get(jid)
            initial_row = initial_by_id.get(jid)
            row = final_row or initial_row
            assert row is not None
            self.add(self._make_job(
                row, origin=origin,
                listing_page_url=page_url_by_jid.get(jid, self._page_url(origin, LISTING_PATH, 0)),
                detail_check=detail_checks.get(jid),
                initial_row=initial_row if initial_row != row else None,
                membership_changed=changed_membership and jid not in final_by_id,
            ))
        detail_mapping_seconds = time.perf_counter() - mapping_started
        self.result.expected_total = int(final_pager["total_items"])
        self.result.pages = len(final_pages)
        # An observed final total is still recorded, but disagreement between the
        # two captured company-published snapshots cannot certify completeness.
        self.result.listing_complete = (not self.result.errors) if final_snapshot_verified else False

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry.url),
            "career_component_script_url": str(script_response.url),
            "career_component_fetch_literals": listing_literals,
            "listing_api_url": origin + LISTING_PATH,
            "categories_api_url": origin + CATEGORIES_PATH,
            "locations_api_url": origin + LOCATIONS_PATH,
            "membership_authority": "Enphase career({variant: full}) component on /careers; its exact unfiltered public listing API, paginated page=0..total_pages-1",
            "listing_filter_behavior": "unfiltered membership; category/location/description/name/requisitionid and keyword filters are user-driven and not applied",
            "ui_pagination": "0-based ?page=N buttons (label index+1); prev/next guard current_page vs total_pages-1; initial load is the bare listing path",
            "department_filters": [_unescape(item["category"]) for item in facet_data[CATEGORIES_PATH]],
            "location_filters": [_unescape(item["location"]) for item in facet_data[LOCATIONS_PATH]],
            "initial_pages": [url for _, url, _ in initial_pages],
            "final_pages": [url for _, url, _ in final_pages],
            "initial_pager": initial_pager,
            "final_pager": final_pager,
            "final_snapshot_verified": final_snapshot_verified,
            "initial_visible_ids": initial_ids,
            "final_visible_ids": final_ids,
            "source_rows_changed_between_snapshots": sorted(changed_rows),
            "representative_detail_checks": detail_checks,
            "representative_detail_check_count": len(detail_checks),
            "description_source": "Each exact listed row's description__value (one HTML-unescape); native /job/<jid> pages are server-rendered and agree byte-for-text for the checked representatives.",
            "source_has_structured_salary_or_date_field": False,
            "salary_policy": "Exact line containing 'range for this position' in the published description is retained with source label; absence stays null.",
            "job_attachment_links": {row["jid"]: _job_attachment_links(html.unescape(row.get("description__value") or ""))
                                      for row in final_rows
                                      if _job_attachment_links(html.unescape(row.get("description__value") or ""))},
            "collection_wall_seconds": round(time.perf_counter() - started, 6),
            "request_count": 2 + len(initial_pages) + 2 + len(detail_checks) + len(final_pages),
        })
        self.result.timings["detail_phase_seconds"] = round(detail_mapping_seconds, 6)
        self.result.timings["representative_detail_ui_phase_seconds"] = round(detail_ui_seconds, 6)
        self.result.timings["source_snapshot_phase_seconds"] = self.result.metadata["collection_wall_seconds"]
        return self.result
