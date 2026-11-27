"""Check Point's own Next.js careers listing and exact company job pages.

The official entry ``https://careers.checkpoint.com/`` redirects (308) to the
company's native listing at ``/jobs``.  That page is a server-rendered Next.js
document whose ``script#__NEXT_DATA__`` carries ``props.pageProps.result``:
``numberOfMatches`` (253 observed), ``numberOfPages`` (26), the facet list and
10 full ``jobPostingsList`` rows per page.  Pagination is the visible
Previous/Next control and the one-based ``?page=N`` URL.  Every listing row
already carries the complete employer-authored ``information``,
``responsibility`` and ``description`` HTML, and the exact public detail page
``/jobs/{id}/{slug}`` carries the same 23-field object under
``props.pageProps.job``.

The membership authority is only this company listing (plus its own
``sitemap.xml`` job URLs as an independent completeness cross-check).  Each
row's ``applyUrl`` points at SmartRecruiters; that board is retained as the
application URL and is never enumerated, because Check Point's own listing is
the company-published membership.

The CDN answers non-browser User-Agents with a 919-byte CloudFront 403.  The
standard desktop Chrome User-Agent used by the other adapters receives the
normal 200 response.  This is UA-keyed filtering, not a CAPTCHA; the adapter
sends the desktop UA, never solves challenges and stops on 403/429.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from html import escape
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Comment, NavigableString, Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://careers.checkpoint.com/"
LISTING_URL = "https://careers.checkpoint.com/jobs"
SITEMAP_URL = "https://careers.checkpoint.com/sitemap.xml"
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
APPLY_HOST = "jobs.smartrecruiters.com"
PAGE_SIZE = 10
MAX_LISTING_PAGES = 200
JOB_PATH = re.compile(r"^/jobs/(\d+)/([a-z0-9-]+)$")
SOURCE_FIELDS = (
    "id", "title", "department", "city", "cityState", "country", "seniority",
    "jobOrderTypeCode", "jobOrderTypeName", "responsibility", "description",
    "information", "applyUrl", "isCareerPortal", "datePublishedDisplay",
    "dateCreatedSort", "dateModifiedSort", "daysOld", "region", "refNumber",
    "score", "datePosted", "slug",
)
# The detail page renders these headings above the three employer HTML fields.
DESCRIPTION_SECTIONS = (
    ("information", "Why Join Us?"),
    ("responsibility", "Key Responsibilities"),
    ("description", "Qualifications"),
)
BLOCK_TAGS = frozenset({
    "html", "body", "p", "div", "section", "article", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr",
    "td", "th", "blockquote", "pre", "hr",
})


def request_headers(referer: str | None = None) -> dict[str, str]:
    """The standard desktop-browser request shape used by the reviewed source."""
    headers = {
        "User-Agent": DESKTOP_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def parse_next_data(html: str) -> dict:
    """Return the page's ``__NEXT_DATA__`` JSON object or fail closed."""
    soup = BeautifulSoup(html or "", "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        raise ValueError("Check Point page has no __NEXT_DATA__ payload")
    value = json.loads(script.string or script.get_text() or "")
    if not isinstance(value, dict):
        raise ValueError("Check Point __NEXT_DATA__ payload is not an object")
    return value


def page_props(html: str) -> dict:
    value = parse_next_data(html).get("props", {}).get("pageProps")
    if not isinstance(value, dict):
        raise ValueError("Check Point page has no props.pageProps object")
    return value


def normalized_title(value: str | None) -> str:
    """Collapse the source's occasional trailing non-breaking space for display."""
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_date(raw: str | None) -> str | None:
    """Drop the published trailing dot, keep the exact ISO local timestamp."""
    value = (raw or "").strip().rstrip(".")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return None


def normalized_body_text(markup: str | None) -> str:
    """Render employer HTML the way the page does: block breaks, no inline gaps.

    ``get_text(" ")`` inserts a space at every inline tag boundary, which turns
    ``<strong>the</strong>`` into ``t he`` and changes the published text.  This
    walker inserts separators only for block elements and ``<br>``, matching the
    rendered detail body exactly (verified against the parent browser reference).
    """
    soup = BeautifulSoup(markup or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    pieces: list[str] = []

    def walk(node):
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            pieces.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        name = node.name.lower()
        if name == "br":
            pieces.append(" ")
            return
        block = name in BLOCK_TAGS
        if block:
            pieces.append(" ")
        for child in node.children:
            walk(child)
        if block:
            pieces.append(" ")

    walk(soup)
    return re.sub(r"\s+", " ", "".join(pieces)).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for character in value:
        digest = ((digest ^ ord(character)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def validate_row(row: dict, *, where: str) -> None:
    if not isinstance(row, dict):
        raise ValueError(f"Check Point {where} row is not an object")
    missing = [field for field in SOURCE_FIELDS if field not in row]
    if missing:
        raise ValueError(f"Check Point {where} row is missing fields: {missing}")
    identity = row.get("id")
    if not isinstance(identity, str) or not re.fullmatch(r"\d+", identity):
        raise ValueError(f"Check Point {where} row has no numeric string id")
    for field in ("title", "slug", "datePosted"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f"Check Point {where} row {identity} has no {field}")
    apply_url = row.get("applyUrl")
    if (not isinstance(apply_url, str)
            or urlsplit(apply_url).netloc != APPLY_HOST):
        raise ValueError(f"Check Point {where} row {identity} has an unexpected applyUrl")
    if row.get("isCareerPortal") != 1:
        raise ValueError(f"Check Point {where} row {identity} is not career-portal public")


def parse_listing_page(html: str, requested_page: int,
                       response_url: str = LISTING_URL) -> dict:
    """Parse one official listing page and validate its membership metadata."""
    parsed = urlsplit(response_url)
    if parsed.netloc != "careers.checkpoint.com" or parsed.path != "/jobs":
        raise ValueError("Check Point listing response left the official /jobs route")
    query = dict(item.split("=", 1) for item in parsed.query.split("&") if "=" in item)
    expected_page = None if requested_page == 1 else str(requested_page)
    if query.get("page") != expected_page:
        raise ValueError("Check Point listing response page parameter changed")

    props = page_props(html)
    result = props.get("result")
    if not isinstance(result, dict):
        raise ValueError("Check Point listing page has no result object")
    count, pages = result.get("numberOfMatches"), result.get("numberOfPages")
    rows = result.get("jobPostingsList")
    if not isinstance(count, int) or count < 0:
        raise ValueError("Check Point listing numberOfMatches is invalid")
    if not isinstance(pages, int) or pages < 1 or pages > MAX_LISTING_PAGES:
        raise ValueError("Check Point listing numberOfPages is invalid")
    if not isinstance(rows, list):
        raise ValueError("Check Point listing jobPostingsList is not a list")
    if len(rows) > PAGE_SIZE:
        raise ValueError("Check Point listing page exceeds its observed page size")

    issues: list[str] = []
    for index, row in enumerate(rows):
        validate_row(row, where=f"listing page {requested_page} row {index}")
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        issues.append("checkpoint_duplicate_id_within_listing_page")
    expected_rows = PAGE_SIZE if requested_page < pages else count - (pages - 1) * PAGE_SIZE
    if expected_rows < 0 or expected_rows > PAGE_SIZE or len(rows) != expected_rows:
        issues.append(
            f"checkpoint_listing_page_row_count_mismatch:{requested_page}:"
            f"{len(rows)}!={expected_rows}"
        )
    query_state = props.get("queryState")
    if not isinstance(query_state, dict):
        issues.append("checkpoint_listing_query_state_missing")
    else:
        if query_state.get("page") != requested_page - 1:
            issues.append("checkpoint_listing_query_state_page_mismatch")
        if query_state.get("q") not in ("", None) or query_state.get("filters") not in ({}, None):
            issues.append("checkpoint_listing_query_state_is_filtered")
    facets = result.get("facetsList")
    if not isinstance(facets, dict):
        issues.append("checkpoint_listing_facets_missing")

    return {
        "rows": rows,
        "count": count,
        "pages": pages,
        "page": requested_page,
        "page_size": len(rows),
        "ids": ids,
        "facets": facets,
        "query_state": query_state,
        "issues": issues,
    }


def description_sections(row: dict) -> list[dict]:
    """Return the non-empty employer sections in rendered order."""
    sections = []
    for field, heading in DESCRIPTION_SECTIONS:
        html = row.get(field)
        if not isinstance(html, str) or not html.strip():
            continue
        sections.append({
            "field": field,
            "heading": heading,
            "html": html,
            "text": normalized_body_text(html),
        })
    if not sections:
        raise ValueError("Check Point posting has no non-empty description section")
    return sections


def description_html(row: dict, sections: list[dict] | None = None) -> str:
    """Exact rendered body: each non-empty section behind its page heading."""
    sections = sections if sections is not None else description_sections(row)
    return "".join(
        f"<h2>{escape(section['heading'])}</h2>{section['html']}"
        for section in sections
    )


def job_url(row: dict) -> str:
    return f"https://careers.checkpoint.com/jobs/{row['id']}/{row['slug']}"


def parse_detail_page(html: str, expected_row: dict, expected_url: str) -> dict:
    """Validate one exact company detail page against its listing row."""
    props = page_props(html)
    detail = props.get("job")
    if not isinstance(detail, dict):
        raise ValueError("Check Point detail page has no props.pageProps.job")
    validate_row(detail, where="detail")
    if detail["id"] != expected_row["id"]:
        raise ValueError("Check Point detail id differs from the listing id")
    if detail["title"] != expected_row["title"]:
        raise ValueError("Check Point detail title differs from the listing title")
    if detail["slug"] != expected_row["slug"]:
        raise ValueError("Check Point detail slug differs from the listing slug")
    canonical = props.get("__canonical")
    if canonical != expected_url:
        raise ValueError("Check Point detail canonical URL differs from the exposed job URL")
    changed = [
        field for field in SOURCE_FIELDS
        if field != "score" and detail.get(field) != expected_row.get(field)
    ]
    sections = description_sections(detail)
    body_html = description_html(detail, sections)
    body_text = normalized_body_text(body_html)

    soup = BeautifulSoup(html, "lxml")
    title_h1 = soup.find("h1")
    h1 = title_h1.get_text(" ", strip=True) if title_h1 else ""
    apply_links = [
        anchor.get("href", "")
        for anchor in soup.select("a[href]")
        if anchor.get_text(" ", strip=True).casefold() == "apply now"
    ]
    issues = []
    if normalized_title(h1) != normalized_title(expected_row["title"]):
        issues.append("checkpoint_detail_h1_title_mismatch")
    if expected_row["applyUrl"] not in apply_links:
        issues.append("checkpoint_detail_apply_link_missing")
    if changed:
        issues.append("checkpoint_detail_listing_fields_changed")
    return {
        "row": detail,
        "canonical": canonical,
        "h1": h1,
        "changed_listing_fields": changed,
        "sections": sections,
        "description_html": body_html,
        "description_text": body_text,
        "description_chars": len(body_text),
        "description_fnv1a64": fnv1a64(body_text),
        "apply_links": apply_links,
        "issues": issues,
    }


def parse_sitemap(xml: str) -> dict:
    """Extract the company's own canonical job URLs from its sitemap."""
    locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml or "")
    jobs, issues, seen = [], [], set()
    for loc in locs:
        parsed = urlsplit(loc)
        if parsed.scheme != "https" or parsed.netloc != "careers.checkpoint.com":
            issues.append(f"checkpoint_sitemap_foreign_url:{loc}")
            continue
        match = JOB_PATH.fullmatch(parsed.path)
        if match is None:
            continue
        identity, slug = match.groups()
        if identity in seen:
            issues.append(f"checkpoint_sitemap_duplicate_id:{identity}")
            continue
        seen.add(identity)
        jobs.append({"id": identity, "slug": slug, "url": loc})
    if not jobs:
        issues.append("checkpoint_sitemap_has_no_job_urls")
    return {
        "jobs": jobs,
        "ids": [job["id"] for job in jobs],
        "count": len(jobs),
        "loc_count": len(locs),
        "issues": issues,
    }


def _response_record(response, seconds: float) -> dict:
    return {
        "request_url": str(response.request.url),
        "final_url": str(response.url),
        "status": response.status_code,
        "bytes": len(response.content),
        "sha256": sha256(response.content).hexdigest(),
        "redirects": [str(item.url) for item in response.history],
        "seconds": round(max(0.0, seconds), 6),
    }


class CheckPoint(Adapter):
    """Collect only Check Point's own listing, exact job pages and sitemap."""

    VERIFICATION_REVISION = "checkpoint.nextjs-listing-sitemap-and-exact-details.1"

    async def _get(self, url: str, *, referer: str | None = None):
        return await self.http.get(url, headers=request_headers(referer))

    async def _entry(self):
        started = time.perf_counter()
        response = await self._get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        record = _response_record(response, seconds)
        if str(response.url) != LISTING_URL:
            raise ValueError("Check Point official entry no longer redirects to /jobs")
        if not response.history or str(response.history[0].url) != OFFICIAL_ENTRY:
            raise ValueError("Check Point official entry redirect chain changed")
        record["redirect_status"] = response.history[0].status_code
        return record

    async def _listing_page(self, page: int) -> dict:
        url = LISTING_URL if page == 1 else f"{LISTING_URL}?page={page}"
        started = time.perf_counter()
        response = await self._get(url, referer=LISTING_URL)
        seconds = max(0.0, time.perf_counter() - started)
        record = _response_record(response, seconds)
        parsed = parse_listing_page(response.text, page, str(response.url))
        parsed["request"] = record
        return parsed

    async def _sitemap(self) -> dict:
        started = time.perf_counter()
        response = await self._get(SITEMAP_URL, referer=LISTING_URL)
        seconds = max(0.0, time.perf_counter() - started)
        record = _response_record(response, seconds)
        parsed = parse_sitemap(response.text)
        parsed["request"] = record
        return parsed

    async def _collect_listing(self, label: str) -> dict:
        started = time.perf_counter()
        issues: list[str] = []
        pages: list[dict] = []
        rows: list[dict] = []
        try:
            first = await self._listing_page(1)
        except Exception as exc:
            issues.append(f"checkpoint_listing_page_failed:{label}:1:{type(exc).__name__}:{exc}")
            return {
                "rows": [], "ids": [], "count": None, "pages": None,
                "page_records": [], "issues": issues,
                "seconds": round(max(0.0, time.perf_counter() - started), 6),
            }
        expected_total, expected_pages = first["count"], first["pages"]
        pages.append(first)
        rows.extend(first["rows"])
        issues.extend(first["issues"])
        for page in range(2, expected_pages + 1):
            if not self.page_guard():
                issues.append("checkpoint_listing_page_budget_exhausted")
                break
            try:
                value = await self._listing_page(page)
            except Exception as exc:
                issues.append(
                    f"checkpoint_listing_page_failed:{label}:{page}:"
                    f"{type(exc).__name__}:{exc}"
                )
                break
            if (value["count"], value["pages"]) != (expected_total, expected_pages):
                issues.append(
                    f"checkpoint_listing_metadata_changed:{label}:{page}:"
                    f"{value['count']}/{value['pages']}"
                )
            pages.append(value)
            rows.extend(value["rows"])
            issues.extend(value["issues"])
        unique: dict[str, dict] = {}
        for row in rows:
            identity = row["id"]
            if identity in unique:
                issues.append(f"checkpoint_duplicate_listing_id:{label}:{identity}")
                continue
            unique[identity] = row
        if expected_total is not None and len(unique) != expected_total:
            issues.append(
                f"checkpoint_listing_total_mismatch:{label}:"
                f"{len(unique)}!={expected_total}"
            )
        return {
            "rows": list(unique.values()),
            "ids": list(unique),
            "count": expected_total,
            "pages": expected_pages,
            "page_records": pages,
            "issues": list(dict.fromkeys(issues)),
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        }

    def _listing_summary(self, value: dict) -> dict:
        return {
            "count": value["count"],
            "pages": value["pages"],
            "unique_ids": len(value["ids"]),
            "ids": value["ids"],
            "page_records": value["page_records"],
            "issues": value["issues"],
            "seconds": value["seconds"],
        }

    def _make_job(self, row: dict, sitemap_row: dict | None):
        sections = description_sections(row)
        body_html = description_html(row, sections)
        body_text = normalized_body_text(body_html)
        responsibilities = [s for s in sections if s["field"] == "responsibility"]
        qualifications = [s for s in sections if s["field"] == "description"]
        job = self.job(
            {
                "listing": dict(row),
                "sitemap": dict(sitemap_row) if sitemap_row else None,
                "sections": sections,
                "description_normalized_chars": len(body_text),
                "description_fnv1a64": fnv1a64(body_text),
            },
            title=normalized_title(row["title"]),
            url=job_url(row),
            job_id=row["id"],
            requisition_id=row["refNumber"],
            apply_url=row["applyUrl"],
            description=body_html,
            method="checkpoint.next_listing.information_responsibility_description",
            description_url=job_url(row),
            locations=[{
                "city": row["city"],
                "cityState": row["cityState"],
                "country": row["country"],
                "region": row["region"],
            }],
            department=row["department"],
            employment_type=row["jobOrderTypeName"],
            date_posted=normalize_date(row["datePosted"]),
            date_posted_raw=row["datePosted"],
            date_updated=None,
            valid_through=None,
            salary=None,
            employer="Check Point Software Technologies",
            language="en",
            responsibilities=responsibilities or None,
            qualifications=qualifications or None,
        )
        job.description_text = body_text
        return job

    async def _detail(self, job):
        started = time.perf_counter()
        response = await self._get(job.url, referer=LISTING_URL)
        seconds = max(0.0, time.perf_counter() - started)
        listing_row = job.raw["listing"]
        parsed = parse_detail_page(response.text, listing_row, job.url)
        detail = parsed["row"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "checkpoint.detail.next_data.information_responsibility_description"
        job.description_source_url = job.url
        job.apply_url = detail["applyUrl"]
        job.raw["detail"] = {
            "source_row": detail,
            "canonical": parsed["canonical"],
            "h1": parsed["h1"],
            "sections": parsed["sections"],
            "description_normalized_chars": parsed["description_chars"],
            "description_fnv1a64": parsed["description_fnv1a64"],
            "apply_links": parsed["apply_links"],
            "changed_listing_fields": parsed["changed_listing_fields"],
            "issues": parsed["issues"],
            "response": _response_record(response, seconds),
        }
        for issue in parsed["issues"]:
            self.result.errors.append(f"{job.job_id}:{issue}")
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "sitemap_url": SITEMAP_URL,
            "company_scope_verified": self.company.scope == "company",
            "desktop_user_agent": DESKTOP_USER_AGENT,
            "listing_request": {
                "method": "GET",
                "urls": [LISTING_URL, f"{LISTING_URL}?page=2..N"],
                "page_parameter": "one-based ?page=N; props.pageProps.queryState.page is zero-based",
                "page_data_path": "script#__NEXT_DATA__ -> props.pageProps.result",
                "page_size": PAGE_SIZE,
                "filters_applied": False,
            },
            "detail_request": {
                "method": "GET",
                "url_shape": "https://careers.checkpoint.com/jobs/{id}/{slug}",
                "page_data_path": "script#__NEXT_DATA__ -> props.pageProps.job",
                "per_job": True,
            },
            "apply_url_policy": "Retain each row's exact applyUrl; never enumerate the SmartRecruiters board.",
            "description_method": (
                "information + responsibility + description employer HTML, each "
                "behind the detail page's rendered heading; empty sections omit "
                "their heading. Detail page is re-read for every job."
            ),
        })
        try:
            entry = await self._entry()
            self.result.metadata["official_entry"] = entry
            self.result.timings["entry_seconds"] = entry["seconds"]
        except Exception as exc:
            self.result.errors.append(
                f"checkpoint_official_entry_failed:{type(exc).__name__}:{exc}"
            )
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6
            )
            return self.result

        initial = await self._collect_listing("initial")
        self.result.metadata["initial_listing"] = self._listing_summary(initial)
        self.result.timings["listing_phase_seconds"] = initial["seconds"]
        if initial["count"] is None:
            self.result.errors.extend(initial["issues"])
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = round(
                max(0.0, time.perf_counter() - source_started), 6
            )
            return self.result
        self.result.expected_total = initial["count"]
        self.result.errors.extend(initial["issues"])

        try:
            sitemap = await self._sitemap()
            self.result.metadata["initial_sitemap"] = {
                key: value for key, value in sitemap.items() if key != "jobs"
            }
            self.result.timings["sitemap_seconds"] = sitemap["request"]["seconds"]
        except Exception as exc:
            sitemap = None
            self.result.errors.append(
                f"checkpoint_sitemap_failed:{type(exc).__name__}:{exc}"
            )
            self.result.metadata["initial_sitemap"] = None

        sitemap_by_id = {row["id"]: row for row in (sitemap or {}).get("jobs", [])}
        listing_ids = set(initial["ids"])
        sitemap_ids = set(sitemap_by_id)
        sitemap_matches_listing = (
            sitemap is not None and sitemap_ids == listing_ids
            and all(sitemap_by_id[row["id"]]["slug"] == row["slug"]
                    for row in initial["rows"])
        )
        if sitemap is not None:
            self.result.errors.extend(sitemap["issues"])
            if sitemap_ids != listing_ids:
                self.result.errors.append(
                    "checkpoint_sitemap_and_listing_membership_mismatch:"
                    f"listing_only={sorted(listing_ids - sitemap_ids)}:"
                    f"sitemap_only={sorted(sitemap_ids - listing_ids)}"
                )
        else:
            sitemap_matches_listing = False

        jobs = []
        for row in initial["rows"]:
            job = self._make_job(row, sitemap_by_id.get(row["id"]))
            self.add(job)
            jobs.append(job)
            if self.limit():
                break
        if len(jobs) != initial["count"]:
            self.result.errors.append(
                f"checkpoint_retained_rows_do_not_match_listing:{len(jobs)}!={initial['count']}"
            )

        self.result.jobs = await self.hydrate(jobs, self._detail)
        details = dict(self.result.metadata.get("details") or {})

        final_listing = await self._collect_listing("final")
        self.result.metadata["final_listing"] = self._listing_summary(final_listing)
        try:
            final_sitemap = await self._sitemap()
            self.result.metadata["final_sitemap"] = {
                key: value for key, value in final_sitemap.items() if key != "jobs"
            }
            self.result.timings["final_sitemap_seconds"] = final_sitemap["request"]["seconds"]
        except Exception as exc:
            final_sitemap = None
            self.result.errors.append(
                f"checkpoint_final_sitemap_failed:{type(exc).__name__}:{exc}"
            )
            self.result.metadata["final_sitemap"] = None
        self.result.timings["final_recheck_seconds"] = round(
            final_listing["seconds"]
            + ((final_sitemap or {}).get("request", {}).get("seconds") or 0.0), 6
        )

        initial_by_id = {row["id"]: row for row in initial["rows"]}
        final_by_id = {row["id"]: row for row in final_listing["rows"]}
        changed_fields = {}
        for identity in sorted(set(initial_by_id) & set(final_by_id)):
            changed = [
                field for field in SOURCE_FIELDS
                if initial_by_id[identity].get(field) != final_by_id[identity].get(field)
            ]
            if changed:
                changed_fields[identity] = changed
        new_ids = [identity for identity in final_listing["ids"] if identity not in initial_by_id]
        removed_ids = [identity for identity in initial["ids"] if identity not in final_by_id]
        same_ids = initial["ids"] == final_listing["ids"]
        counts_match = (initial["count"], initial["pages"]) == (
            final_listing["count"], final_listing["pages"]
        )
        final_sitemap_by_id = {
            row["id"]: row for row in (final_sitemap or {}).get("jobs", [])
        }
        final_sitemap_matches = (
            final_sitemap is not None
            and set(final_sitemap_by_id) == set(final_listing["ids"])
        )
        sitemap_stable = (
            sitemap is not None and final_sitemap is not None
            and sitemap_ids == set(final_sitemap_by_id)
        )
        final_issues = list(final_listing["issues"])
        if final_sitemap is not None:
            final_issues.extend(final_sitemap["issues"])
        if not same_ids:
            self.result.errors.append("checkpoint_listing_membership_changed_during_collection")
        if not counts_match:
            self.result.errors.append("checkpoint_listing_counts_changed_during_collection")
        if changed_fields:
            self.result.errors.append("checkpoint_listing_source_fields_changed_during_collection")
        if new_ids:
            self.result.errors.append(f"checkpoint_new_job_ids_during_collection:{new_ids}")
        if removed_ids:
            self.result.errors.append(f"checkpoint_removed_job_ids_during_collection:{removed_ids}")
        if not final_sitemap_matches:
            self.result.errors.append("checkpoint_final_sitemap_membership_mismatch")
        if not sitemap_stable:
            self.result.errors.append("checkpoint_sitemap_changed_during_collection")
        self.result.errors.extend(final_issues)

        final_matches = bool(
            same_ids and counts_match and not changed_fields and not new_ids
            and not removed_ids and not final_issues and final_sitemap_matches
            and sitemap_stable and sitemap_matches_listing
        )
        self.result.metadata["membership_reconciliation"] = {
            "initial_ids": initial["ids"],
            "final_ids": final_listing["ids"],
            "initial_count": initial["count"],
            "final_count": final_listing["count"],
            "initial_pages": initial["pages"],
            "final_pages": final_listing["pages"],
            "initial_unique_ids": len(initial["ids"]),
            "final_unique_ids": len(final_listing["ids"]),
            "sitemap_ids": sorted(sitemap_ids),
            "final_sitemap_ids": sorted(final_sitemap_by_id),
            "sitemap_matches_listing": sitemap_matches_listing,
            "final_sitemap_matches_listing": final_sitemap_matches,
            "sitemap_stable": sitemap_stable,
            "same_ids_and_order": same_ids,
            "counts_match": counts_match,
            "changed_fields_by_job_id": changed_fields,
            "new_job_ids": new_ids,
            "removed_job_ids": removed_ids,
            "complete": final_matches,
        }
        self.result.metadata["job_ids"] = [job.job_id for job in self.result.jobs]
        self.result.metadata["detail_hydration"] = details
        self.result.metadata["failed_job_ids"] = [
            job.job_id for job in self.result.jobs
            if not job.description_verified
            or "detail_fetch_failed" in job.warnings
        ]
        self.result.metadata["description_stats"] = {
            "jobs": len(self.result.jobs),
            "non_empty_descriptions": sum(bool(job.description_text.strip()) for job in self.result.jobs),
            "verified_descriptions": sum(job.description_verified for job in self.result.jobs),
            "empty_information_jobs": [
                job.job_id for job in self.result.jobs
                if not (job.raw["listing"].get("information") or "").strip()
            ],
            "min_chars": min((len(job.description_text) for job in self.result.jobs), default=0),
            "max_chars": max((len(job.description_text) for job in self.result.jobs), default=0),
        }
        self.result.listing_complete = bool(
            final_matches and len(self.result.jobs) == initial["count"]
        )
        self.result.timings["source_collection_wall_seconds"] = round(
            max(0.0, time.perf_counter() - source_started), 6
        )
        return self.result
