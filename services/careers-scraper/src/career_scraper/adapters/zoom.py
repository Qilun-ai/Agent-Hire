"""Zoom's first-party Duda careers site and its embedded ShazamMe job widget.

The ledger URL https://careers.zoom.us/jobs/search now 404s; the current journey is
https://www.careers.zoom.us/ -> /job-results (ShazamMe job-results widget) ->
/job-details/<slug>-<id> detail pages -> /job-application?jobID=<uuid> apply form.
The widget fetches one company-scoped JSON feed
(https://shazamme.io/job-results/<ExternalUid>); the UI paginates client-side over
the same response. Full descriptions are inline (`fullDescription`) and the exact
detail pages render the identical body server-side.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import re
import time
import unicodedata
from urllib.parse import urlsplit, urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.careers.zoom.us/"
LISTING_PAGE_URL = "https://www.careers.zoom.us/job-results"
DETAIL_BASE = "https://www.careers.zoom.us/job-details/"
APPLY_BASE = "https://www.careers.zoom.us/job-application"
DUDA_SITE_ID = "22d89a44"
SITE_ID = "a5bcebfa-981b-46cd-b46f-ad91a50505b8"
FEED_URL = f"https://shazamme.io/job-results/{SITE_ID}?"
EMPLOYER_NAME = "Zoom Communications, Inc."
JOB_URL_PATH = re.compile(r"^/job-details/[a-z0-9-]+-(\d+)$", re.I)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
REFERENCE_RE = re.compile(r"^R\d+$")
PARAMS_RE = re.compile(r"window\.Parameters\s*=\s*window\.Parameters\s*\|\|\s*\{([^}]*)\}", re.S)


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _duda_identity(html: str) -> dict:
    match = PARAMS_RE.search(html or "")
    if not match:
        raise ValueError("Zoom careers page no longer exposes Duda window.Parameters identity")
    block = match.group(1)
    alias = re.search(r"SiteAlias:\s*'([^']+)'", block)
    uid = re.search(r"ExternalUid:\s*'([^']+)'", block)
    home = re.search(r"HomeUrl:\s*'([^']+)'", block)
    return {
        "site_alias": alias.group(1) if alias else None,
        "external_uid": uid.group(1) if uid else None,
        "home_url": home.group(1) if home else None,
    }


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.careers.zoom.us"
            or parsed.path != "/"):
        raise ValueError("Zoom official careers entry redirected away from its inspected URL")
    identity = _duda_identity(html)
    if identity["site_alias"] != DUDA_SITE_ID or identity["home_url"] != OFFICIAL_ENTRY:
        raise ValueError("Zoom official careers page Duda identity changed")
    soup = BeautifulSoup(html, "lxml")
    hrefs = {a.get("href", "") for a in soup.select("a[href]")}
    if "/job-results" not in hrefs:
        raise ValueError("Zoom official careers homepage no longer links to /job-results")
    if "sdk.shazamme.io/js/shazamme-" not in html:
        raise ValueError("Zoom official careers homepage no longer loads ShazamMe scripts")
    return {
        "url": response_url,
        "duda_site_alias": identity["site_alias"],
        "external_uid": identity["external_uid"],
        "home_url": identity["home_url"],
        "listing_page_linked": "/job-results",
    }


def parse_listing_page(html: str, response_url: str = LISTING_PAGE_URL) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.careers.zoom.us"
            or parsed.path != "/job-results"):
        raise ValueError("Zoom listing page response came from an unexpected route")
    identity = _duda_identity(html)
    if identity["site_alias"] != DUDA_SITE_ID or identity["external_uid"] != SITE_ID:
        raise ValueError("Zoom listing page Duda identity changed")
    soup = BeautifulSoup(html, "lxml")
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical is None or urljoin(response_url, canonical.get("href", "")) != LISTING_PAGE_URL:
        raise ValueError("Zoom listing page canonical URL changed")
    widget_scripts = sorted(set(re.findall(r"sdk\.shazamme\.io/js/widget/[^<\s]*", html)))
    if not widget_scripts or '" + NAME + "/widget.min.js' not in html:
        raise ValueError("Zoom listing page no longer exposes the ShazamMe job-results widget")
    text = soup.get_text(" ", strip=True)
    return {
        "url": response_url,
        "duda_site_alias": identity["site_alias"],
        "external_uid": identity["external_uid"],
        "canonical": LISTING_PAGE_URL,
        "widget_scripts": widget_scripts,
        "page_title": (soup.title.get_text(" ", strip=True) if soup.title else ""),
        "shows_search_headline": "Job Search" in text,
    }


def parse_posted_date(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        raise ValueError(f"Zoom postedDate format changed: {value!r}")


def parse_feed(value) -> dict:
    """Validate and scope-check the ShazamMe job-results feed for Zoom Careers."""
    if not isinstance(value, list) or not value:
        raise ValueError("Zoom ShazamMe feed response is not a non-empty list")
    rows, seen, issues = [], set(), []
    for item in value:
        row = item.get("data") if isinstance(item, dict) else None
        if not isinstance(row, dict):
            issues.append("zoom_feed_row_missing_data_object")
            continue
        identity = str(row.get("jobID") or "")
        if not UUID_RE.match(identity):
            issues.append("zoom_feed_row_missing_uuid_jobid")
            continue
        if identity in seen:
            issues.append(f"zoom_feed_duplicate_jobid:{identity}")
            continue
        if row.get("advertiserName") != "Zoom" or row.get("siteName") != "Zoom Careers":
            issues.append(f"zoom_feed_row_wrong_advertiser_or_site:{identity}")
            continue
        if row.get("siteID") != SITE_ID or row.get("dudaSiteID") != DUDA_SITE_ID:
            issues.append(f"zoom_feed_row_wrong_site_scope:{identity}")
            continue
        if row.get("activeStatus") is not True:
            issues.append(f"zoom_feed_row_not_active:{identity}")
            continue
        reference = str(row.get("referenceNumber") or "")
        if not REFERENCE_RE.match(reference):
            issues.append(f"zoom_feed_row_missing_reference:{identity}")
            continue
        if not normalized_title(row.get("jobName")):
            issues.append(f"zoom_feed_row_missing_title:{identity}")
            continue
        job_url = str(row.get("jobURL") or "")
        parts = urlsplit(job_url)
        if (parts.scheme != "https" or parts.netloc not in ("www.careers.zoom.us", "careers.zoom.us")
                or JOB_URL_PATH.fullmatch(parts.path) is None):
            issues.append(f"zoom_feed_row_bad_job_url:{identity}")
            continue
        description_html = row.get("fullDescription")
        if not isinstance(description_html, str) or len(normalized_body_text(description_html)) < 100:
            issues.append(f"zoom_feed_row_description_empty_or_truncated:{identity}")
            continue
        parse_posted_date(row.get("postedDate"))  # raises if format changed
        seen.add(identity)
        rows.append(dict(row))
    if not rows:
        raise ValueError("Zoom ShazamMe feed exposes no in-scope Zoom Careers rows")
    return {"rows": rows, "job_ids": [str(r["jobID"]) for r in rows],
            "count": len(rows), "issues": issues}


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored <p><b>heading</b></p> block and the body below it."""
    soup = BeautifulSoup(f'<div data-zoom-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-zoom-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            heading = block.get_text(" ", strip=True)
        elif block.name == "p":
            strong = block.find("strong", recursive=False) or block.find("b", recursive=False)
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    else:
        sections.append({"heading": None, "html": "", "text": ""})
    return sections


def parse_detail_page(html: str, expected_row: dict, expected_url: str) -> dict:
    """Check an exact first-party detail page renders the feed fullDescription body."""
    parts = urlsplit(expected_url)
    if (parts.scheme != "https" or parts.netloc not in ("www.careers.zoom.us", "careers.zoom.us")
            or JOB_URL_PATH.fullmatch(parts.path) is None
            or expected_url != str(expected_row.get("jobURL") or "")):
        raise ValueError("Zoom representative detail URL is not an exact exposed job-details link")
    soup = BeautifulSoup(html, "lxml")
    expected = normalized_body_text(expected_row.get("fullDescription", ""))
    body = next((node for node in soup.select("div.dmCustomWidget")
                 if normalized_body_text(str(node)) == expected), None)
    if body is None:
        raise ValueError("Zoom exact detail page body does not match the feed fullDescription")
    title_tag = soup.title
    expected_title = normalized_title(expected_row.get("jobName"))
    actual_title = normalized_title(title_tag.get_text(" ", strip=True)) if title_tag else ""
    if expected_title not in actual_title:
        raise ValueError("Zoom exact detail page title does not match the feed jobName")
    apply_hrefs = [urljoin(expected_url, a.get("href") or "")
                   for a in soup.select("a[href]") if "job-application" in (a.get("href") or "")]
    apply_hrefs += [urljoin(expected_url, h) for h in
                    re.findall(r'href="(/job-application\?jobID=[^"]+)"', html or "")]
    matches_apply = any(urlsplit(h).path == "/job-application"
                        and urlsplit(h).query == f"jobID={expected_row.get('jobID')}"
                        for h in apply_hrefs)
    return {
        "url": expected_url,
        "title": actual_title,
        "description_normalized_chars": len(expected),
        "description_fnv1a64": fnv1a64(expected),
        "matches_feed_description_text": True,
        "apply_now_hrefs": apply_hrefs,
        "apply_href_matches_feed_jobid": matches_apply,
    }


def _salary_value(row: dict):
    salary = row.get("salary")
    if isinstance(salary, str) and salary.strip():
        return salary.strip()
    return None


class Zoom(Adapter):
    """Read only Zoom's own /job-results ShazamMe widget feed and its job-details pages."""

    VERIFICATION_REVISION = "zoom.shazamme-company-widget-feed.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)

    async def _entry(self):
        started = time.perf_counter()
        # Duda serves a stripped variant to non-browser Accept headers; the
        # inspected journey (identity + ShazamMe scripts) is the HTML variant.
        response = await self.http.get(OFFICIAL_ENTRY, headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        seconds = max(0.0, time.perf_counter() - started)
        entry = parse_official_entry(response.text, str(response.url))
        entry.update(status=response.status_code, seconds=seconds,
                     response_sha256=sha256(response.content).hexdigest(),
                     response_bytes=len(response.content))
        return entry

    async def _listing_page(self):
        started = time.perf_counter()
        response = await self.http.get(LISTING_PAGE_URL, headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != LISTING_PAGE_URL:
            raise ValueError("Zoom listing page redirected away from /job-results")
        parsed = parse_listing_page(response.text, str(response.url))
        parsed.update(status=response.status_code, seconds=seconds,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _feed(self):
        started = time.perf_counter()
        response = await self.http.get(FEED_URL, headers={"Referer": LISTING_PAGE_URL})
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != FEED_URL:
            raise ValueError("Zoom ShazamMe feed redirected away from its company-scoped route")
        parsed = parse_feed(response.json())
        parsed.update(status=response.status_code, request_seconds=seconds,
                      request_url=str(response.request.url), final_url=str(response.url),
                      response_bytes=len(response.content),
                      response_sha256=sha256(response.content).hexdigest())
        return parsed

    def _make_job(self, row: dict):
        identity = str(row["jobID"])
        description_html = row.get("fullDescription") or ""
        description_text = text_html(description_html)
        sections = description_sections(description_html)
        responsibility_sections = [
            s for s in sections
            if s["heading"] and any(t in s["heading"].lower()
                                     for t in ("responsibilit", "what you will do", "what you'll do"))
        ]
        qualification_sections = [
            s for s in sections
            if s["heading"] and any(t in s["heading"].lower()
                                     for t in ("qualif", "requirement", "what you will need",
                                               "what you'll need", "what we're looking for",
                                               "what we’re looking for", "who you are"))
        ]
        salary_sections = [
            s for s in sections
            if s["heading"] and "salary" in s["heading"].lower()
        ]
        job_url = str(row["jobURL"])
        job = self.job(
            {"listing": dict(row)},
            title=normalized_title(row.get("jobName")),
            url=job_url,
            job_id=identity,
            requisition_id=str(row.get("referenceNumber") or None),
            apply_url=f"{APPLY_BASE}?jobID={identity}",
            description=description_html,
            method="zoom.shazamme.job-results.fullDescription",
            description_url=FEED_URL,
            locations=[row["city"]] if row.get("city") else [],
            department=row.get("category") or None,
            employment_type=row.get("workType") or row.get("workTypeLabel") or None,
            date_posted=parse_posted_date(row.get("postedDate")),
            date_posted_raw=row.get("postedDate"),
            date_updated=None,
            valid_through=None,
            salary=_salary_value(row),
            employer=EMPLOYER_NAME,
            language="en",
            responsibilities=responsibility_sections or None,
            qualifications=qualification_sections or None,
        )
        job.description_text = description_text
        normalized = normalized_body_text(description_html)
        job.raw["detail"] = {
            "source_field": "fullDescription",
            "description_sha256": sha256(description_html.encode()).hexdigest(),
            "description_normalized_chars": len(normalized),
            "description_fnv1a64": fnv1a64(normalized),
            "description_sections": sections,
            "salary_sections": salary_sections,
            "salary_fields": {
                "salary": row.get("salary"),
                "salaryFrom": row.get("salaryFrom"),
                "salaryTo": row.get("salaryTo"),
                "currencyCode": row.get("currencyCode"),
                "currencySymbol": row.get("currencySymbol"),
            },
            "dates": {
                "postedDate": row.get("postedDate"),
                "addedOnUTC": row.get("addedOnUTC"),
                "changedOnUTC": row.get("changedOnUTC"),
                "expiryDate": row.get("expiryDate"),
                "jobEndDate": row.get("jobEndDate"),
                "jobStartDate": row.get("jobStartDate"),
                "posting_date_mapping": "postedDate (%d-%b-%Y) -> date_posted ISO",
                "closing_date_mapping": None,
            },
            "application_route": {
                "apply_now_url": f"{APPLY_BASE}?jobID={identity}",
                "detail_page_apply_control": "Apply Now",
            },
            "locations_source": {"city": row.get("city"), "country": row.get("country"),
                                 "fullAddress": row.get("fullAddress")},
            "feed_row_sha256": sha256(
                __import__("json").dumps(row, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
        }
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_PAGE_URL,
            "listing_authority": "Zoom's own /job-results page ShazamMe job-results widget feed.",
            "listing_membership_request": {
                "method": "GET",
                "path": "/job-results/<ExternalUid>",
                "host": "shazamme.io",
                "query_shape": "none",
                "filters_applied": False,
                "scope": "Zoom Careers ExternalUid only (a5bcebfa-...); the same response the "
                         "company page's widget renders; UI pagination is client-side.",
            },
            "detail_method": "Full fullDescription is returned inline by the company-scoped widget "
                             "feed; exact first-party detail pages render the identical body "
                             "(representative first/last pages verified rendered).",
            "request_scope": "Only the Zoom Careers widget feed and the exact company pages that "
                             "publish it; no ShazamMe-wide discovery, no other ATS board.",
        })
        try:
            initial_entry = await self._entry()
            initial_page = await self._listing_page()
            initial = await self._feed()
        except Exception as exc:
            self.result.errors.append(f"Zoom source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started)
            return self.result

        self.result.expected_total = initial["count"]
        self.result.metadata["initial_official_entry"] = initial_entry
        self.result.metadata["initial_listing_page"] = initial_page
        self.result.metadata["initial_feed"] = {k: v for k, v in initial.items() if k != "rows"}
        self.result.errors.extend(initial["issues"])

        jobs, detail_parse_seconds = [], 0.0
        for row in initial["rows"]:
            started = time.perf_counter()
            jobs.append(self._make_job(row))
            detail_parse_seconds += max(0.0, time.perf_counter() - started)
        if len(jobs) != initial["count"]:
            self.result.errors.append("zoom_retained_rows_do_not_match_widget_feed_count")

        final, final_matches = None, False
        try:
            final_entry = await self._entry()
            final_page = await self._listing_page()
            final = await self._feed()
        except Exception as exc:
            self.result.errors.append(f"Zoom final source recheck failed: {type(exc).__name__}: {exc}")
        if final is not None:
            initial_by_id = {str(r["jobID"]): r for r in initial["rows"]}
            final_by_id = {str(r["jobID"]): r for r in final["rows"]}
            changed_fields = {}
            for identity in initial["job_ids"]:
                if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]:
                    changed_fields[identity] = sorted(
                        k for k in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(k) != final_by_id[identity].get(k))
            same_ids = initial["job_ids"] == final["job_ids"]
            counts_match = initial["count"] == final["count"]
            entry_same = (initial_entry["external_uid"] == final_entry["external_uid"] == SITE_ID
                          and initial_entry["duda_site_alias"] == final_entry["duda_site_alias"]
                          == DUDA_SITE_ID)
            page_same = (initial_page["widget_scripts"] == final_page["widget_scripts"]
                         and initial_page["canonical"] == final_page["canonical"] == LISTING_PAGE_URL)
            final_matches = (same_ids and counts_match and not changed_fields
                             and not final["issues"] and entry_same and page_same)
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_listing_page"] = final_page
            self.result.metadata["final_feed"] = {k: v for k, v in final.items() if k != "rows"}
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["job_ids"],
                "final_ids": final["job_ids"],
                "initial_count": initial["count"],
                "final_count": final["count"],
                "same_ids_and_order": same_ids,
                "all_row_fields_match": not changed_fields,
                "changed_fields_by_job_id": changed_fields,
                "new_job_ids": [i for i in final["job_ids"] if i not in initial["job_ids"]],
                "removed_job_ids": [i for i in initial["job_ids"] if i not in final["job_ids"]],
                "official_entry_identity_still_exact": entry_same,
                "listing_page_widget_still_exact": page_same,
                "complete": final_matches,
            }
            if not entry_same:
                self.result.errors.append("zoom_official_entry_identity_changed_during_collection")
            if not page_same:
                self.result.errors.append("zoom_listing_page_widget_changed_during_collection")
            if not same_ids or not counts_match:
                self.result.errors.append("zoom_listing_membership_changed_during_collection")
            if changed_fields:
                self.result.errors.append("zoom_source_fields_changed_during_collection")
            self.result.errors.extend(final["issues"])

        self.result.jobs = jobs
        self.result.timings["inline_detail_parse_seconds_sum"] = detail_parse_seconds
        self.result.timings["detail_phase_seconds"] = detail_parse_seconds
        self.result.metadata["detail_hydration_mode"] = (
            "Full fullDescription is inline in the exact company-scoped widget feed response; "
            "no detail fetch per row. Representative first/last detail pages are verified "
            "rendered separately (browser-reference-worker.json)."
        )
        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.metadata["feed_count"] = initial["count"]
        self.result.metadata["current_native_membership_reconciled"] = final_matches
        self.result.listing_complete = bool(final_matches and len(jobs) == initial["count"])
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started)
        return self.result
