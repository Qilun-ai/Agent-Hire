"""Meesho's official careers site (https://www.meesho.io/jobs).

Observed journey (see companies/meesho/journey-research.md):

- The entry `https://www.meesho.io/jobs` is a static Next.js export whose
  `__NEXT_DATA__` is empty; the listing is rendered client-side by the page
  bundle `/_next/static/chunks/pages/jobs-<hash>.js`.
- That bundle pins exactly one job source, `const P =
  "https://api.lever.co/v0/postings/meesho"`, and issues
  `fetch(P + "/?&limit=15")`, `fetch(P + "/?&skip=<loaded>&limit=15")` for every
  "Load more" click, and `fetch(P + "/")` (no limit) for the headline count and
  the search typeahead.  The unfiltered page renders 15 + 15 + 15 + 8 = 53 rows
  and the headline "53 jobs, across all teams & all locations"; the board total
  is 53.  Department tiles, the team multi-select, the internships toggle and
  the search box only filter that same full response.
- Each row links to the company's own detail route
  `/jobs/<slugified-title>?id=<uuid>`, which is server-rendered and embeds the
  same Lever posting object in `__NEXT_DATA__.props.pageProps.data`.  The page
  renders `description`, then every `lists[]` section as `<h3>` + `<ul>`, then
  `additional`, then a fixed "We'd love to get to know you." CTA that is not
  part of the posting.
- `description` + `lists[]` are the posting's role body.  `additional` is a
  corporate block (the same "About us / Culture and Total Rewards / disability
  inclusion" text on 33 rows, one longer variant, empty on 19); it is preserved
  verbatim under `job.raw` and deliberately excluded from the stored
  description, matching the project's role-description boundary.
- `https://www.meesho.io/jobs/open-recruitment` is a closed 2024 intern
  programme announcement ("We're not accepting any more applications.") and
  `/early-careers` is a role-family marketing page; neither publishes live
  postings, so neither contributes membership.

The adapter therefore: validates the entry page and that the shipped bundle
still requests exactly the `meesho` board, fetches the unfiltered board, walks
the rendered 15-per-page "Load more" requests and reconciles them with the full
response, then hydrates every posting from Meesho's own detail route and
rebuilds the description exactly as that page renders it.  Meesho publishes no
posting or expiry date, so date_posted/date_updated/valid_through stay null;
Lever's `createdAt` is preserved only at raw.listing.createdAt.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


ENTRY_URL = "https://www.meesho.io/jobs"
LISTING_API_URL = "https://api.lever.co/v0/postings/meesho"
DETAIL_URL_TEMPLATE = "https://www.meesho.io/jobs/{slug}?id={job_id}"
PAGE_LIMIT = 15

# The meesho.io Akamai edge answers the shared scraper User-Agent with a 371-byte
# "Access Denied" 403 (no challenge page).  The standard desktop Chrome client
# identity used by several other company adapters receives the normal page.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

BUNDLE_PATH_RE = re.compile(r'src="(/_next/static/chunks/pages/jobs-[^"]+\.js)"')
BOARD_REFERENCE = 'api.lever.co/v0/postings/meesho'
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

RESPONSIBILITY_HEADING_RE = re.compile(
    r"^(?:what (?:you|we)(?:'|’)?.{0,3}(?:will|’ll|'ll)? ?(?:be )?do(?:ing)?\b|"
    r"responsibilities|key responsibilities|the role)\s*:?\s*$",
    re.I,
)
QUALIFICATION_HEADING_RE = re.compile(
    r"^(?:what (?:you|we)(?:'|’)?.{0,3}(?:will|’ll|'ll)? ?(?:be )?(?:need|looking for)\b|"
    r"what we(?:'|’)re looking for|requirements|qualifications|"
    r"good to have|bonus points|must have|preferred)\s*:?\s*$",
    re.I,
)


def job_slug(title: str) -> str:
    """Mirror the shipped bundle's slug: lower-case, spaces to '-', drop the rest."""
    return re.sub(r"[^\w-]+", "", (title or "").lower().replace(" ", "-"), flags=re.ASCII)


def detail_url(row: dict) -> str:
    return DETAIL_URL_TEMPLATE.format(slug=job_slug(row.get("text", "")), job_id=row["id"])


def compose_description(row: dict) -> str:
    """Rebuild the role body the official detail page renders.

    Only the posting's own `description` and `lists[]` sections belong to the
    role description.  The corporate `additional` block is returned separately
    by `generic_company_sections()` and kept under `job.raw`.
    """
    from html import escape

    parts = [row.get("description") or ""]
    for section in row.get("lists") or []:
        parts.append(f"<h2>{escape(section.get('text') or '')}</h2><ul>{section.get('content') or ''}</ul>")
    return "\n".join(parts)


def _compact(value: str) -> str:
    import unicodedata

    return re.sub(r"[\s\ufeff]", "", unicodedata.normalize("NFC", value or ""))


def generic_company_sections(row: dict) -> dict:
    """Return the corporate `additional` block verbatim, outside the role body."""
    html = row.get("additional") or ""
    plain = row.get("additionalPlain")
    text = text_html(html).strip() if html else (plain or "").strip()
    compact = _compact(text)
    heading = None
    match = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", html, re.S | re.I)
    if match:
        heading = text_html(match.group(1)).strip() or None
    return {
        "source_field": "additional",
        "heading": heading,
        "html": html,
        "plain": plain if isinstance(plain, str) else None,
        "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest() if text else None,
        "compact_sha256": hashlib.sha256(compact.encode()).hexdigest() if compact else None,
        "chars": len(text),
        "compact_chars": len(compact),
        "present": bool(html.strip()),
        "role_specific": False,
        "excluded_from_description": True,
    }


def parse_detail_page(html: str) -> dict:
    """Return `__NEXT_DATA__.props.pageProps.data` from a Meesho job detail page."""
    match = NEXT_DATA_RE.search(html or "")
    if not match:
        raise ValueError("Meesho detail page has no __NEXT_DATA__ block")
    import json

    payload = json.loads(match.group(1))
    data = (payload.get("props") or {}).get("pageProps", {}).get("data")
    if not isinstance(data, dict):
        raise ValueError("Meesho detail page __NEXT_DATA__ carries no pageProps.data")
    if payload.get("page") != "/jobs/[slug]":
        raise ValueError(f"unexpected Meesho detail page route: {payload.get('page')!r}")
    return data


def bundle_path(html: str) -> str | None:
    match = BUNDLE_PATH_RE.search(html or "")
    return match.group(1) if match else None


def page_title(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    node = soup.find("title")
    return node.get_text(strip=True) if node else ""


def section_roles(row: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Split a posting's own `lists[]` sections by the heading they publish."""
    responsibilities, qualifications, other = [], [], []
    for section in row.get("lists") or []:
        heading = (section.get("text") or "").strip()
        rendered = text_html(section.get("content") or "")
        item = {"heading": heading, "html": section.get("content") or "", "text": rendered}
        if RESPONSIBILITY_HEADING_RE.match(heading):
            responsibilities.append(item)
        elif QUALIFICATION_HEADING_RE.match(heading):
            qualifications.append(item)
        else:
            other.append(item)
    return responsibilities, qualifications, other


def _locations(row: dict) -> list[str]:
    categories = row.get("categories") or {}
    values = []
    for value in [categories.get("location")] + list(categories.get("allLocations") or []):
        if isinstance(value, str) and value.strip() and value.strip() not in values:
            values.append(value.strip())
    return values


class Meesho(Adapter):
    async def run(self):
        started = time.perf_counter()
        entry_started = time.perf_counter()
        entry = await self.http.get(ENTRY_URL, headers=BROWSER_HEADERS)
        entry_seconds = time.perf_counter() - entry_started
        html = entry.text
        self.result.metadata.update({
            "official_entry_url": ENTRY_URL,
            "entry_status": entry.status_code,
            "entry_final_url": str(entry.url),
            "entry_title": page_title(html),
            "entry_seconds": round(entry_seconds, 6),
            "listing_api_url": LISTING_API_URL,
            "page_limit": PAGE_LIMIT,
            "detail_route_template": DETAIL_URL_TEMPLATE,
        })

        bundle = bundle_path(html)
        scope = {"bundle_path": bundle, "bundle_requests_meesho_board": None}
        if bundle:
            bundle_started = time.perf_counter()
            bundle_response = await self.http.get("https://www.meesho.io" + bundle,
                                                 headers={**BROWSER_HEADERS, "Accept": "*/*"})
            scope.update({
                "bundle_url": "https://www.meesho.io" + bundle,
                "bundle_status": bundle_response.status_code,
                "bundle_bytes": len(bundle_response.content),
                "bundle_sha256": hashlib.sha256(bundle_response.content).hexdigest(),
                "bundle_seconds": round(time.perf_counter() - bundle_started, 6),
                "bundle_requests_meesho_board": BOARD_REFERENCE in bundle_response.text,
            })
            other_boards = sorted(set(re.findall(r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)", bundle_response.text)))
            scope["bundle_lever_boards"] = other_boards
            if BOARD_REFERENCE not in bundle_response.text:
                self.result.errors.append(f"official_jobs_bundle_no_longer_requests_{BOARD_REFERENCE}")
            elif other_boards not in (["meesho"], []):
                self.result.errors.append(f"official_jobs_bundle_requests_other_boards: {other_boards}")
        else:
            self.result.errors.append("official_jobs_page_has_no_listing_bundle")
        if "meesho" not in page_title(html).lower():
            self.result.errors.append("official_entry_title_does_not_name_meesho")
        self.result.metadata["scope_verification"] = scope

        listing_started = time.perf_counter()
        full_rows = await self.http.json(LISTING_API_URL)
        if not isinstance(full_rows, list):
            raise ValueError("Meesho board response is no longer a JSON array")
        expected_total = len(full_rows)
        full_ids = [str(row.get("id")) for row in full_rows]
        if len(set(full_ids)) != len(full_ids):
            self.result.errors.append("listing_repeats_job_ids")

        page_sizes, paged_ids = [], []
        skip = 0
        while True:
            if not self.page_guard():
                break
            page = await self.http.json(LISTING_API_URL, params={"limit": PAGE_LIMIT, "skip": skip})
            if not isinstance(page, list):
                raise ValueError(f"Meesho board page skip={skip} is no longer a JSON array")
            page_sizes.append(len(page))
            paged_ids.extend(str(row.get("id")) for row in page)
            skip += PAGE_LIMIT
            if len(page) < PAGE_LIMIT:
                break
        listing_seconds = time.perf_counter() - listing_started
        self.result.expected_total = expected_total
        self.result.timings["listing_phase_seconds"] = listing_seconds
        self.result.metadata.update({
            "page_sizes": page_sizes,
            "pages_loaded": len(page_sizes),
            "full_board_rows": expected_total,
            "paginated_rows": len(paged_ids),
            "pagination_matches_full_board": paged_ids == full_ids,
            "listing_phase_seconds": round(listing_seconds, 6),
        })
        if paged_ids != full_ids:
            self.result.errors.append("pagination_mismatch_with_full_board")
            self.result.listing_complete = False

        for row in full_rows:
            categories = row.get("categories") or {}
            responsibilities, qualifications, other = section_roles(row)
            job = self.job(
                row,
                title=row.get("text"),
                url=detail_url(row),
                job_id=row.get("id"),
                description=compose_description(row),
                method="meesho.jobs.detail_page_role_sections",
                description_url=detail_url(row),
                apply_url=row.get("applyUrl"),
                locations=_locations(row),
                department=categories.get("department"),
                team=categories.get("team"),
                employment_type=categories.get("commitment"),
                workplace_type=row.get("workplaceType"),
                # Lever's `createdAt` is the board record's creation timestamp, not
                # a posting date: Meesho's listing rows and detail pages publish no
                # date at all.  Canonical dates therefore stay null and the raw
                # value is kept only at raw.listing.createdAt (date_posted_raw is
                # deliberately null so nothing downstream reads it as a date).
                date_posted=None,
                date_posted_raw=None,
                date_updated=None,
                valid_through=None,
                qualifications=[item["text"] for item in qualifications] or None,
                responsibilities=[item["text"] for item in responsibilities] or None,
            )
            job.raw = {
                "listing": row,
                "hosted_url": row.get("hostedUrl"),
                "country": row.get("country"),
                "additional": row.get("additional") or "",
                "additional_plain": row.get("additionalPlain"),
                "generic_company_sections": generic_company_sections(row),
                "sections": {
                    "responsibilities": responsibilities,
                    "qualifications": qualifications,
                    "other": other,
                },
            }
            self.add(job)
            if self.limit():
                break

        async def detail(job):
            detail_started = time.perf_counter()
            attempts = 0
            retried_transient = False
            while True:
                attempts += 1
                response = await self.http.get(job.url, headers=BROWSER_HEADERS)
                try:
                    data = parse_detail_page(response.text)
                    break
                except ValueError:
                    # Meesho's edge occasionally serves a 200 "Document" error
                    # shell instead of the Next.js page.  Retry that transient
                    # response a bounded number of times; a real missing page
                    # still fails closed after the final attempt.
                    if attempts >= 3:
                        raise
                    retried_transient = True
                    await asyncio.sleep(1.5 * attempts)
            if str(data.get("id")) != str(job.job_id):
                raise ValueError(f"detail page id {data.get('id')!r} != listing id {job.job_id!r}")
            listing = job.raw["listing"]
            if (data.get("text") or "").strip() != job.title.strip():
                raise ValueError(f"detail page title {data.get('text')!r} != listing title {job.title!r}")
            description = compose_description(data)
            job.description_html = description
            job.description_text = text_html(description)
            job.description_method = "meesho.jobs.detail_page_role_sections"
            job.description_source_url = job.url
            responsibilities, qualifications, other = section_roles(data)
            job.qualifications = [item["text"] for item in qualifications] or None
            job.responsibilities = [item["text"] for item in responsibilities] or None
            job.raw["detail"] = data
            job.raw["additional"] = data.get("additional") or ""
            job.raw["additional_plain"] = data.get("additionalPlain")
            job.raw["generic_company_sections"] = generic_company_sections(data)
            job.raw["detail_page"] = {
                "url": str(response.url),
                "requested_url": job.url,
                "status": response.status_code,
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
                "response_bytes": len(response.content),
                "elapsed_seconds": round(time.perf_counter() - detail_started, 6),
                "attempts": attempts,
                "retried_transient_error_page": retried_transient,
                "description_source_field": "description + lists[] (role sections only)",
                "description_normalized_chars": len(text_html(description)),
                "corporate_additional_excluded": True,
                "corporate_additional_chars": len(text_html(data.get("additional") or "")),
                "description_matches_listing_row": data == listing,
                "moved_or_removed": False,
            }
            job.raw["sections"] = {
                "responsibilities": responsibilities,
                "qualifications": qualifications,
                "other": other,
            }
            if data != listing:
                job.warnings.append("detail_page_differs_from_listing_row")
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        self.result.metadata["hydration"] = {
            "jobs": len(self.result.jobs),
            "detail_route": DETAIL_URL_TEMPLATE,
        }
        self.result.metadata["reconciliation"] = {
            "advertised_jobs": expected_total,
            "paginated_rows": len(paged_ids),
            "collected_jobs": len(self.result.jobs),
            "ids_match": [str(job.job_id) for job in self.result.jobs] == full_ids,
        }
        self.result.listing_complete = (
            not self.result.errors
            and "sample_limit" not in self.result.warnings
            and len(self.result.jobs) == expected_total
            and paged_ids == full_ids
        )
        self.result.metadata["wall_seconds"] = round(time.perf_counter() - started, 6)
        return self.result
