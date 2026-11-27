"""Celigo's native careers cards and linked WordPress role pages."""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString

from .base import Adapter
from ..models import now


CAREERS_URL = "https://www.celigo.com/careers/"
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": "CompanyCareersScraper/0.1 (public job listings)",
}
GREENHOUSE_APPLY = re.compile(r"https://job-boards\.greenhouse\.io/celigo/jobs/\d+/?$")


def compact(value):
    return re.sub(r"\s+", " ", value or "").strip()


_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}


def dom_inner_text(node):
    """Approximate rendered innerText: preserve inline text nodes; separate block nodes."""
    chunks = []

    def visit(current):
        if isinstance(current, NavigableString):
            chunks.append(str(current))
            return
        if getattr(current, "name", None) in {"script", "style", "noscript"}:
            return
        name = getattr(current, "name", None)
        is_block = name in _BLOCK_TAGS or name == "br"
        if is_block and chunks and not chunks[-1].endswith(("\n", "\r", "\t", " ")):
            chunks.append("\n")
        for child in getattr(current, "children", ()):
            visit(child)
        if is_block:
            chunks.append("\n")

    visit(node)
    return re.sub(r"\s+", " ", "".join(chunks)).strip()


def title_key(value):
    return re.sub(r"[\s\-–—]+", " ", compact(value).casefold()).strip()


def structured_data(soup):
    found = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        found.extend(node for node in nodes if isinstance(node, dict))
    return found


def listing_rows(content, base_url=CAREERS_URL):
    """Parse only h3-linked native role cards and their adjacent published fields."""
    soup = BeautifulSoup(content, "lxml")
    rows = []
    seen = set()
    for heading in soup.find_all("h3"):
        anchor = heading.find("a") or heading.find_parent("a")
        if not anchor or not anchor.get("href"):
            continue
        url = urljoin(base_url, str(anchor["href"]))
        parsed = urlsplit(url)
        if parsed.netloc.lower() not in {"www.celigo.com", "celigo.com"} or not parsed.path.startswith("/careers/") or parsed.path.rstrip("/") == "/careers":
            continue
        if url in seen:
            continue
        column = heading.find_parent(class_="fwpl-col")
        items = [compact(node.get_text(" ", strip=True))
                 for node in column.find_all(class_="fwpl-item", recursive=False)] if column else []
        title = compact(heading.get_text(" ", strip=True))
        if len(items) < 3 or items[0] != title:
            rows.append({"title": title, "url": url, "department": None, "location": None,
                         "listing_metadata_missing": True})
        else:
            rows.append({"title": title, "url": url, "department": items[1], "location": items[2],
                         "listing_row_text": compact(column.get_text(" ", strip=True))})
        seen.add(url)
    return soup, rows


def pagination_links(soup, base_url=CAREERS_URL):
    links = []
    for node in soup.select("a[rel~='next'], .pagination a, .fwpl-pager a, .facetwp-pager a"):
        href = node.get("href")
        if href:
            links.append({"label": compact(node.get_text(" ", strip=True)),
                          "url": urljoin(base_url, str(href))})
    return links


def parse_detail(job, content, response_url, response_sha256, response_fetched_at=None):
    soup = BeautifulSoup(content, "lxml")
    article = soup.select_one("article.careers")
    entry = article.select_one(".entry-content") if article else None
    if not article or not entry:
        raise ValueError("native article.careers .entry-content is missing")

    body_html = entry.decode_contents()
    body_text = dom_inner_text(entry)
    if len(body_text) < 300:
        raise ValueError(f"full native description is too short ({len(body_text)} characters)")

    og_title = compact((soup.find("meta", property="og:title") or {}).get("content"))
    if og_title and title_key(og_title) != title_key(job.title):
        raise ValueError(f"native title differs from listing card: {job.title!r} / {og_title!r}")

    structured = structured_data(soup)
    job_posting = next((node for node in structured if (
        node.get("@type") == "JobPosting"
        or isinstance(node.get("@type"), list) and "JobPosting" in node["@type"]
    )), {})
    apply_links = []
    for anchor in soup.find_all("a", href=True):
        label = compact(anchor.get_text(" ", strip=True))
        href = urljoin(response_url, str(anchor["href"]))
        if "apply" in label.casefold() or GREENHOUSE_APPLY.fullmatch(href):
            apply_links.append({"label": label, "href": href})
    exact_apply = next((item["href"] for item in apply_links if GREENHOUSE_APPLY.fullmatch(item["href"])), None)
    greenhouse_id = re.search(r"/jobs/(\d+)(?:/|$)", exact_apply or "")
    article_id = article.get("id", "")
    wp_id = re.fullmatch(r"post-(\d+)", article_id)
    identifier = job_posting.get("identifier")
    requisition_id = identifier.get("value") if isinstance(identifier, dict) else identifier if isinstance(identifier, str) else None
    heading_labels = [compact(node.get_text(" ", strip=True)) for node in entry.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])]
    emphasis_labels = [compact(node.get_text(" ", strip=True)) for node in entry.find_all(["strong", "b"])]
    section_labels = list(dict.fromkeys([label for label in heading_labels + emphasis_labels if label and len(label) < 100]))

    job.job_id = greenhouse_id.group(1) if greenhouse_id else (wp_id.group(1) if wp_id else job.job_id)
    job.requisition_id = requisition_id
    job.apply_url = exact_apply
    job.description_html = body_html
    job.description_text = body_text
    job.description_method = "celigo.article.careers.entry-content"
    job.description_source_url = str(response_url)
    job.employment_type = job_posting.get("employmentType")
    job.date_posted = job_posting.get("datePosted")
    job.date_posted_raw = job_posting.get("datePosted")
    job.valid_through = job_posting.get("validThrough")
    job.salary = job_posting.get("baseSalary")
    job.qualifications = job_posting.get("qualifications")
    job.responsibilities = job_posting.get("responsibilities")
    hiring_org = job_posting.get("hiringOrganization")
    job.employer = hiring_org.get("name") if isinstance(hiring_org, dict) else None
    job.fetched_at = response_fetched_at or now()
    job.raw["native_page"] = {
        "canonical_url": (soup.find("link", rel="canonical") or {}).get("href"),
        "og_title": og_title,
        "title_matches_listing_ignoring_dash_style": not og_title or title_key(og_title) == title_key(job.title),
        "wordpress_post_id": wp_id.group(1) if wp_id else None,
        "greenhouse_job_id_from_exact_published_apply_link": greenhouse_id.group(1) if greenhouse_id else None,
        "apply_links": apply_links,
        "structured_data": structured,
        "job_posting_schema": job_posting,
        "content_selector": "article.careers .entry-content",
        "section_labels": section_labels,
        "response_sha256": response_sha256,
        "response_fetched_at": response_fetched_at,
    }
    job.validate()
    return job


class Celigo(Adapter):
    """Native Celigo career list and direct, company-published detail pages."""

    VERIFICATION_REVISION = "2026-11-07.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._current_rows: dict[str, dict] = {}
        self._invalidated_cache_ids: list[str] = []

    def add(self, job):
        row = job.raw.get("native_listing") or {}
        self._current_rows[job.url] = row
        cached = self.options.cached_jobs.get(job.url)
        if cached:
            old = cached.raw.get("native_listing") or {}
            critical = ("title", "department", "location", "url")
            if any(old.get(field) != row.get(field) for field in critical):
                self.options.cached_jobs.pop(job.url, None)
                self._invalidated_cache_ids.append(str(cached.job_id or job.url))
            else:
                # The exact native detail URL and its published listing fields
                # identify the same post; allow the validated body cache hit.
                cached.record_identity = None
        super().add(job)

    async def run(self):
        if self.company.slug != "celigo":
            raise ValueError("Celigo adapter requires the Celigo company")
        if self.source.url != CAREERS_URL:
            raise ValueError(f"Celigo adapter only starts at {CAREERS_URL}")
        if self.source.evidence_url != CAREERS_URL:
            raise ValueError(f"Celigo source evidence must point to {CAREERS_URL}")

        collection_started = time.perf_counter()
        listing_started = time.perf_counter()
        response = await self.http.get(CAREERS_URL, headers=HEADERS)
        entry_sha = hashlib.sha256(response.content).hexdigest()
        soup, rows = listing_rows(response.content, str(response.url))
        pagination = pagination_links(soup, str(response.url))
        initial_listing_seconds = max(0.0, time.perf_counter() - listing_started)
        if not rows:
            self.result.errors.append("celigo_native_listing_has_no_role_links")
            self.result.listing_complete = False
            return self.result

        self.result.expected_total = len(rows)
        self.result.pages = 1
        if pagination:
            self.result.errors.append("celigo_unexpected_job_pagination_controls")
            self.result.listing_complete = False
            return self.result
        self.result.metadata["official_journey"] = {
            "official_entry_url": CAREERS_URL,
            "entry_response_sha256": entry_sha,
            "membership_selector": "h3 a under native /careers/ path",
            "card_fields_selector": ".fwpl-col > .fwpl-item: title, department, location",
            "listing_rows": len(rows),
            "pagination": "none observed; the official page publishes all role cards in one HTML response" if not pagination else "controls observed",
            "pagination_controls": pagination,
            "external_ats_script_observed": "Greenhouse job board embed script present in page source; not used for posting membership or detail collection.",
            "membership_basis": "Exact h3-linked native Celigo role pages published in the careers page.",
        }
        self.result.metadata["entry_html_job_scripts"] = [
            script.get("src") for script in soup.find_all("script", src=True)
            if "greenhouse" in str(script.get("src", "")).casefold()
        ]
        expected_sig = []
        for position, row in enumerate(rows, 1):
            if row.get("listing_metadata_missing"):
                self.result.errors.append(f"celigo_listing_metadata_missing:{row['url']}")
                continue
            row["listing_position"] = position
            expected_sig.append({key: row.get(key) for key in ("url", "title", "department", "location")})
            job = self.job(
                {"native_listing": row},
                title=row["title"],
                url=row["url"],
                locations=[row["location"]] if row.get("location") else [],
                department=row.get("department"),
            )
            self.add(job)
        self.result.listing_complete = len(self.result.jobs) == len(rows) and not self.result.errors
        if not self.result.listing_complete:
            return self.result

        async def detail(job):
            response = await self.http.get(job.url, headers=HEADERS)
            digest = hashlib.sha256(response.content).hexdigest()
            return parse_detail(job, response.content, str(response.url), digest)

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        missing_apply = [job.url for job in self.result.jobs if not job.apply_url]
        if missing_apply:
            self.result.errors.extend(f"celigo_native_apply_link_missing:{url}" for url in missing_apply)
        final_started = time.perf_counter()
        final_response = await self.http.get(CAREERS_URL, headers=HEADERS)
        final_sha = hashlib.sha256(final_response.content).hexdigest()
        _, final_rows = listing_rows(final_response.content, str(final_response.url))
        final_sig = [{key: row.get(key) for key in ("url", "title", "department", "location")} for row in final_rows]
        same_ordered_rows = expected_sig == final_sig
        final_complete = same_ordered_rows and len(final_rows) == self.result.expected_total
        self.result.metadata["final_membership_recheck"] = {
            "status": "complete" if final_complete else "mismatch",
            "entry_response_sha256": final_sha,
            "row_count": len(final_rows),
            "initial_rows": expected_sig,
            "final_rows": final_sig,
            "exact_order_and_fields_match": same_ordered_rows,
        }
        if not final_complete:
            self.result.listing_complete = False
            self.result.errors.append("celigo_final_native_membership_recheck_mismatch")
        self.result.metadata["archive_cache_invalidations"] = self._invalidated_cache_ids
        total = max(0.0, time.perf_counter() - collection_started)
        final_listing_seconds = max(0.0, time.perf_counter() - final_started)
        self.result.timings["celigo_initial_listing_seconds"] = round(initial_listing_seconds, 6)
        self.result.timings["celigo_final_listing_seconds"] = round(final_listing_seconds, 6)
        self.result.timings["celigo_listing_total_seconds"] = round(initial_listing_seconds + final_listing_seconds, 6)
        self.result.timings["celigo_collection_wall_seconds"] = round(total, 6)
        return self.result
