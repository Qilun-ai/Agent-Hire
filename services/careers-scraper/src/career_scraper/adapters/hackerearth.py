"""HackerEarth's own careers page, its exact embedded JazzHR board, and full job details.

The official https://www.hackerearth.com/careers/ page publishes one
``iframe#jazzhr-job-frame`` pointing at the company-scoped JazzHR board
``https://hackerearth.applytojob.com/apply/jobs/``.  Membership is exactly the
jobs that board lists (one server-rendered page; ``?page=2`` is identical).
Details are read from the exact listing links
(``/apply/jobs/details/<code>``), with the page's own canonical
``/apply/<code>/<slug>`` route fetched only to enrich public fields such as
experience and to cross-check the full description.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now, text_html


OFFICIAL_ENTRY = "https://www.hackerearth.com/careers/"
LISTING_URL = "https://hackerearth.applytojob.com/apply/jobs/"
SITE = "hackerearth.applytojob.com"
COMPANY_NAME = "HackerEarth"
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": "CompanyCareersScraper/0.1 (public job listings)",
}
JOB_DETAIL_PATH = re.compile(r"^/apply/jobs/details/([A-Za-z0-9]{10})/?$")
CANONICAL_DETAIL_PATH = re.compile(r"^/apply/([A-Za-z0-9]{10})(?:/[^/]+)?/?$")
ROW_ID = re.compile(r"^row_job_(\d{14}_[A-Z0-9]{16})$")
META_SEPARATOR = re.compile(r"\s+-\s+")


def compact(value) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def title_key(value) -> str:
    return re.sub(r"[\s\-–—]+", " ", compact(value).casefold()).strip()


def structured_data(soup) -> list[dict]:
    found = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        found.extend(node for node in nodes if isinstance(node, dict))
    return found


def job_posting(soup) -> dict:
    return next((node for node in structured_data(soup) if node.get("@type") == "JobPosting"
                 or isinstance(node.get("@type"), list) and "JobPosting" in node["@type"]), {})


def organization_name(soup) -> str | None:
    for node in structured_data(soup):
        if node.get("@type") == "Organization" and node.get("name"):
            return compact(node["name"])
    return None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Verify the company careers page still embeds the exact JazzHR board."""
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != "www.hackerearth.com"
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("HackerEarth official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    frames = soup.select("iframe#jazzhr-job-frame")
    if len(frames) != 1:
        raise ValueError("HackerEarth careers page no longer exposes exactly one JazzHR job-listing iframe")
    iframe = frames[0]
    src = urljoin(response_url, iframe.get("src") or "")
    if src != LISTING_URL:
        raise ValueError(f"HackerEarth JazzHR iframe URL changed: {src!r}")
    headings = [compact(node.get_text(" ", strip=True)) for node in soup.select("h1,h2,h3")]
    if not any(heading.casefold() == "current openings" for heading in headings):
        raise ValueError("HackerEarth careers page no longer publishes the 'Current openings' section")
    page_title = compact(soup.title.get_text(" ", strip=True)) if soup.title else ""
    organization = organization_name(soup)
    if not organization and "hackerearth" in page_title.casefold():
        organization = COMPANY_NAME
    return {
        "url": response_url,
        "iframe_id": iframe.get("id"),
        "iframe_name": iframe.get("name"),
        "iframe_src": src,
        "iframe_height": iframe.get("height"),
        "heading": "Current openings",
        "page_title": page_title,
        "organization": organization,
    }


def _row_from_widget_table(table_row) -> dict | None:
    anchor = table_row.select_one("a.job_title_link")
    if not anchor or not anchor.get("href"):
        return None
    href = compact(anchor.get("href"))
    match = JOB_DETAIL_PATH.fullmatch(urlsplit(href).path)
    if not match:
        return None
    cells = table_row.find_all("td", recursive=False)
    location = compact(cells[1].get_text(" ", strip=True)) if len(cells) > 1 else None
    row_id = table_row.get("id") or ""
    unique = ROW_ID.fullmatch(row_id)
    return {
        "code": match.group(1),
        "url": urljoin(LISTING_URL, href),
        "title": compact(anchor.get_text(" ", strip=True)),
        "location": location,
        "unique_code": f"job_{unique.group(1)}" if unique else None,
        "listing_row_id": row_id or None,
    }


def _row_from_classic_card(card) -> dict | None:
    anchor = card.select_one("h3.list-group-item-heading a") or card.find("a", href=True)
    if not anchor or not anchor.get("href"):
        return None
    href = compact(anchor.get("href"))
    match = CANONICAL_DETAIL_PATH.fullmatch(urlsplit(href).path)
    if not match:
        return None
    location_node = card.select_one("ul.list-inline")
    return {
        "code": match.group(1),
        "url": urljoin(LISTING_URL, href),
        "title": compact(anchor.get_text(" ", strip=True)),
        "location": compact(location_node.get_text(" ", strip=True)) if location_node else None,
        "unique_code": None,
        "listing_row_id": card.get("id") or None,
    }


def parse_listing(html: str, response_url: str = LISTING_URL) -> dict:
    """Parse the company-scoped JazzHR board; supports its two published layouts."""
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != SITE or parsed.path.rstrip("/") != "/apply/jobs":
        raise ValueError("HackerEarth JazzHR listing response came from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    if organization_name(soup) != COMPANY_NAME:
        raise ValueError("HackerEarth JazzHR listing company identity changed")

    layout = "widget_advanced" if soup.select_one("#resumator-widget-advanced") else "classic_cards"
    if layout == "widget_advanced":
        candidates = [_row_from_widget_table(tr) for tr in soup.select("table#jobs_table tr[id]")]
    else:
        candidates = [_row_from_classic_card(li) for li in soup.select("ul.list-group > li.list-group-item")]

    rows, seen, issues = [], {}, []
    for row in candidates:
        if row is None:
            continue
        code = row["code"]
        if code in seen:
            previous = seen[code]
            if (previous["url"], previous["title"]) != (row["url"], row["title"]):
                issues.append(f"conflicting_duplicate_job_code:{code}")
            continue
        seen[code] = row
        rows.append(row)
    pagination = []
    for anchor in soup.select("a[rel~='next'], .pagination a, #pagination a, .pager a"):
        href = anchor.get("href")
        if href:
            pagination.append({"label": compact(anchor.get_text(" ", strip=True)),
                               "url": urljoin(response_url, href)})
    filters = {
        "search_form": bool(soup.select_one("#form_search_jobs")),
        "advanced_search_form": bool(soup.select_one("#form_advanced_search_jobs")),
        "department_filter": bool(soup.select_one("#s_department")),
        "filters_applied": False,
    }
    return {
        "layout": layout,
        "organization": organization_name(soup),
        "page_title": compact(soup.title.get_text(" ", strip=True)) if soup.title else None,
        "rows": rows,
        "pagination_controls": pagination,
        "filters": filters,
        "issues": issues,
    }


def _split_meta(meta: str) -> tuple[str | None, str | None]:
    value = compact(meta)
    if not value:
        return None, None
    parts = META_SEPARATOR.split(value)
    if len(parts) >= 2:
        return compact(" - ".join(parts[:-1])), compact(parts[-1])
    return value, None


def parse_detail(job, content: str, response_url: str, response_sha256: str,
                 response_fetched_at: str | None = None):
    """Parse the full description from the exact listing-linked JazzHR detail page."""
    parsed = urlsplit(response_url)
    match = JOB_DETAIL_PATH.fullmatch(parsed.path) or CANONICAL_DETAIL_PATH.fullmatch(parsed.path)
    if parsed.netloc != SITE or match is None:
        raise ValueError("HackerEarth JazzHR detail response came from an unexpected route")
    code = match.group(1)
    soup = BeautifulSoup(content, "lxml")

    title_node = soup.select_one("h1.job_title")
    company_node = soup.select_one("h2.job_company")
    meta_node = soup.select_one("h3.job_meta")
    description_node = soup.select_one("#job_description_wrapper .job_description")
    if title_node is None or description_node is None:
        raise ValueError(f"HackerEarth JazzHR detail page is missing title/description: {response_url}")
    title = compact(title_node.get_text(" ", strip=True))
    if title_key(title) != title_key(job.title):
        raise ValueError(f"HackerEarth JazzHR detail title differs from listing: {job.title!r} / {title!r}")
    if company_node is not None and compact(company_node.get_text(" ", strip=True)) != COMPANY_NAME:
        raise ValueError("HackerEarth JazzHR detail company identity changed")

    body_html = description_node.decode_contents()
    body_text = text_html(body_html)
    if len(body_text) < 200:
        raise ValueError(f"HackerEarth JazzHR full description is too short: {len(body_text)} characters")

    meta = compact(meta_node.get_text(" ", strip=True)) if meta_node else ""
    location, display_type = _split_meta(meta)
    posting = job_posting(soup)
    canonical = soup.find("link", rel="canonical")
    canonical_url = urljoin(response_url, canonical.get("href")) if canonical and canonical.get("href") else None
    if canonical_url:
        canonical_match = CANONICAL_DETAIL_PATH.fullmatch(urlsplit(canonical_url).path)
        if urlsplit(canonical_url).netloc != SITE or canonical_match is None or canonical_match.group(1) != code:
            raise ValueError(f"HackerEarth JazzHR canonical detail URL is inconsistent: {canonical_url}")
    form = soup.find("form", id="form_submit_new_resume") or soup.find("form", action=True)
    form_action = urljoin(response_url, form.get("action")) if form and form.get("action") else None

    unique_code = posting.get("uniqueJobCode")
    job.job_id = code
    if unique_code:
        job.requisition_id = compact(unique_code)
    job.title = title
    job.apply_url = canonical_url or response_url
    job.description_html = body_html
    job.description_text = body_text
    job.description_method = "hackerearth.jazzhr_full_job_description"
    job.description_source_url = response_url
    job.locations = [location] if location else []
    job.employment_type = display_type or posting.get("employmentType")
    job.experience = posting.get("experienceRequirements")
    job.date_posted = posting.get("datePosted")
    job.date_posted_raw = posting.get("datePosted")
    job.valid_through = posting.get("validThrough")
    job.employer = (posting.get("hiringOrganization") or {}).get("name") if isinstance(
        posting.get("hiringOrganization"), dict) else COMPANY_NAME
    job.fetched_at = response_fetched_at or now()
    job.raw["jazzhr_detail"] = {
        "url": response_url,
        "canonical_url": canonical_url,
        "job_company": compact(company_node.get_text(" ", strip=True)) if company_node else None,
        "job_meta": meta,
        "job_posting_schema": posting,
        "application_form_action": form_action,
        "application_form_method": (form.get("method") or "GET").upper() if form else None,
        "content_selector": "#job_description_wrapper .job_description",
        "response_sha256": response_sha256,
        "response_fetched_at": job.fetched_at,
    }
    job.validate()
    return job


def parse_canonical_detail(job, content: str, response_url: str, response_sha256: str,
                           response_fetched_at: str | None = None) -> dict:
    """Read public enrichment fields and cross-check the full description."""
    parsed = urlsplit(response_url)
    match = CANONICAL_DETAIL_PATH.fullmatch(parsed.path)
    if parsed.netloc != SITE or match is None:
        raise ValueError("HackerEarth JazzHR canonical response came from an unexpected route")
    code = match.group(1)
    soup = BeautifulSoup(content, "lxml")
    title_node = soup.select_one("div.job-header h2")
    description_node = soup.select_one("#job-description")
    if title_node is None or description_node is None:
        raise ValueError(f"HackerEarth JazzHR canonical detail is missing title/description: {response_url}")
    title = compact(title_node.get_text(" ", strip=True))
    if title_key(title) != title_key(job.title):
        raise ValueError(f"HackerEarth JazzHR canonical title differs from listing: {job.title!r} / {title!r}")
    body_text = text_html(description_node.decode_contents())
    canonical = soup.find("link", rel="canonical")
    canonical_url = urljoin(response_url, canonical.get("href")) if canonical and canonical.get("href") else None
    experience_node = soup.select_one("#resumator-job-experience")
    employment_node = soup.select_one("#resumator-job-employment")
    experience = compact(experience_node.get_text(" ", strip=True)) if experience_node else None
    employment_type = compact(employment_node.get_text(" ", strip=True)) if employment_node else None
    return {
        "code": code,
        "url": response_url,
        "canonical_url": canonical_url,
        "title": title,
        "experience": experience,
        "employment_type": employment_type,
        "description_text_sha256": hashlib.sha256(body_text.encode()).hexdigest(),
        "description_text_matches": body_text == job.description_text,
        "response_sha256": response_sha256,
        "response_fetched_at": response_fetched_at or now(),
    }


class HackerEarth(Adapter):
    """HackerEarth's official careers page and its exact JazzHR company board."""

    VERIFICATION_REVISION = "2026-11-07.hackerearth.jazzhr-embed.1"

    async def run(self):
        if self.company.slug != "hackerearth":
            raise ValueError("HackerEarth adapter requires the HackerEarth company")
        if self.source.url != LISTING_URL:
            raise ValueError(f"HackerEarth adapter only starts at {LISTING_URL}")
        if self.source.evidence_url != OFFICIAL_ENTRY:
            raise ValueError(f"HackerEarth source evidence must point to {OFFICIAL_ENTRY}")

        collection_started = time.perf_counter()
        entry_started = time.perf_counter()
        entry_response = await self.http.get(OFFICIAL_ENTRY, headers=HEADERS)
        entry = parse_official_entry(entry_response.text, str(entry_response.url))
        entry["response_sha256"] = hashlib.sha256(entry_response.content).hexdigest()
        entry["status"] = entry_response.status_code
        entry["final_url"] = str(entry_response.url)
        entry_seconds = max(0.0, time.perf_counter() - entry_started)

        listing_started = time.perf_counter()
        listing_response = await self.http.get(LISTING_URL, headers=HEADERS)
        listing = parse_listing(listing_response.text, str(listing_response.url))
        listing_seconds = max(0.0, time.perf_counter() - listing_started)
        self.result.pages = 1
        self.result.expected_total = len(listing["rows"])
        self.result.metadata["verification_revision"] = self.VERIFICATION_REVISION
        self.result.metadata["official_journey"] = {
            "official_entry_url": OFFICIAL_ENTRY,
            "entry_final_url": entry["final_url"],
            "entry_status": entry["status"],
            "entry_response_sha256": entry["response_sha256"],
            "listing_iframe_id": entry["iframe_id"],
            "listing_iframe_src": entry["iframe_src"],
            "listing_url": LISTING_URL,
            "listing_authority": "The exact JazzHR job-listing iframe published by HackerEarth's own careers page.",
            "listing_layout": listing["layout"],
            "listing_page_title": listing["page_title"],
            "membership_selector": ("table#jobs_table tr[id^=row_job_] a.job_title_link"
                                    if listing["layout"] == "widget_advanced"
                                    else "ul.list-group > li.list-group-item h3 a"),
            "listing_rows": len(listing["rows"]),
            "pagination": ("none observed; the board serves all current roles in one response and ?page=2 "
                           "returns identical membership") if not listing["pagination_controls"] else "controls observed",
            "pagination_controls": listing["pagination_controls"],
            "filters": listing["filters"],
            "membership_basis": "Exactly the company-scoped hackerearth.applytojob.com board embedded by hackerearth.com/careers/.",
            "detail_route": "exact listing link /apply/jobs/details/<code>; canonical /apply/<code>/<slug> used only for enrichment/cross-check",
            "request_scope": "hackerearth.applytojob.com only; no JazzHR-wide or third-party discovery.",
        }
        self.result.metadata["current_listing"] = {
            "rows": listing["rows"],
            "issues": listing["issues"],
            "response_sha256": hashlib.sha256(listing_response.content).hexdigest(),
        }
        if not listing["rows"]:
            self.result.errors.append("hackerearth_listing_has_no_jobs")
            self.result.listing_complete = False
            return self.result
        if listing["issues"]:
            self.result.errors.extend(f"hackerearth_listing_{issue}" for issue in listing["issues"])

        expected_sig = [{key: row.get(key) for key in ("code", "url", "title", "location")}
                        for row in listing["rows"]]
        for row in listing["rows"]:
            job = self.job(
                {"jazzhr_listing": row},
                title=row["title"],
                url=row["url"],
                job_id=row["code"],
                requisition_id=row.get("unique_code"),
                locations=[row["location"]] if row.get("location") else [],
            )
            self.add(job)
        self.result.listing_complete = len(self.result.jobs) == len(listing["rows"]) and not self.result.errors
        if not self.result.listing_complete:
            return self.result

        async def detail(job):
            response = await self.http.get(job.url, headers=HEADERS)
            digest = hashlib.sha256(response.content).hexdigest()
            fetched_at = now()
            parse_detail(job, response.text, str(response.url), digest, fetched_at)
            canonical_url = (job.raw.get("jazzhr_detail") or {}).get("canonical_url")
            if canonical_url:
                try:
                    canonical_response = await self.http.get(canonical_url, headers=HEADERS)
                    enrichment = parse_canonical_detail(
                        job, canonical_response.text, str(canonical_response.url),
                        hashlib.sha256(canonical_response.content).hexdigest(), now())
                except Exception as exc:
                    job.warnings.append("canonical_enrichment_failed")
                    self.result.errors.append(
                        f"Canonical detail {canonical_url}: {type(exc).__name__}: {exc}")
                else:
                    if not enrichment["description_text_matches"]:
                        job.warnings.append("canonical_description_mismatch")
                        self.result.errors.append(
                            f"Canonical description differs from listing detail: {canonical_url}")
                    job.experience = job.experience or enrichment.get("experience")
                    job.raw["jazzhr_canonical"] = enrichment
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        for job in self.result.jobs:
            if "canonical_description_mismatch" in job.warnings:
                job.description_verified = False

        final_started = time.perf_counter()
        final_response = await self.http.get(LISTING_URL, headers=HEADERS)
        final_listing = parse_listing(final_response.text, str(final_response.url))
        final_seconds = max(0.0, time.perf_counter() - final_started)
        final_sig = [{key: row.get(key) for key in ("code", "url", "title", "location")}
                     for row in final_listing["rows"]]
        final_complete = final_sig == expected_sig and len(final_listing["rows"]) == self.result.expected_total
        self.result.metadata["final_membership_recheck"] = {
            "status": "complete" if final_complete else "mismatch",
            "row_count": len(final_listing["rows"]),
            "initial_rows": expected_sig,
            "final_rows": final_sig,
            "exact_order_and_fields_match": final_complete,
            "layout": final_listing["layout"],
            "response_sha256": hashlib.sha256(final_response.content).hexdigest(),
        }
        if not final_complete:
            self.result.listing_complete = False
            self.result.errors.append("hackerearth_final_listing_membership_mismatch")

        probe_started = time.perf_counter()
        probe_response = await self.http.get(LISTING_URL + "?page=2", headers=HEADERS)
        probe_listing = parse_listing(probe_response.text, str(probe_response.url))
        probe_sig = [{key: row.get(key) for key in ("code", "url", "title", "location")}
                     for row in probe_listing["rows"]]
        probe_same = probe_sig == expected_sig
        self.result.metadata["pagination_probe"] = {
            "url": LISTING_URL + "?page=2",
            "row_count": len(probe_listing["rows"]),
            "same_membership_as_page_1": probe_same,
            "response_sha256": hashlib.sha256(probe_response.content).hexdigest(),
        }
        if not probe_same:
            self.result.listing_complete = False
            self.result.errors.append("hackerearth_page_2_added_or_changed_membership")

        verified = sum(job.description_verified for job in self.result.jobs)
        self.result.listing_complete = (
            self.result.listing_complete
            and len(self.result.jobs) == self.result.expected_total
            and verified == len(self.result.jobs)
        )
        self.result.metadata["completed_membership"] = {
            "expected": self.result.expected_total,
            "collected": len(self.result.jobs),
            "verified_descriptions": verified,
            "job_ids": [job.job_id for job in self.result.jobs],
        }
        total = max(0.0, time.perf_counter() - collection_started)
        probe_seconds = max(0.0, time.perf_counter() - probe_started)
        self.result.timings["hackerearth_entry_seconds"] = round(entry_seconds, 6)
        self.result.timings["hackerearth_initial_listing_seconds"] = round(listing_seconds, 6)
        self.result.timings["hackerearth_final_listing_seconds"] = round(max(0.0, final_seconds), 6)
        self.result.timings["hackerearth_pagination_probe_seconds"] = round(probe_seconds, 6)
        self.result.timings["hackerearth_collection_wall_seconds"] = round(total, 6)
        return self.result
