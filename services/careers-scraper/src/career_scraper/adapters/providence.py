"""Providence's official careers site (providence.jobs) and its DirectEmployers feed.

The official marketing site publishes a jobs page whose widget is rendered from
an origin-scoped DirectEmployers (JobSyn) search API.  Each listed role has a
canonical detail route on providence.jobs and a public detail document on the
DirectEmployers microsite.  The listing API's ``description`` value is a
truncated preview; the microsite detail's ``html_description`` is the full
rendered body used by the job page.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import slug, text_html


OFFICIAL_ENTRY = "https://www.providence.jobs/"
LISTING_URL = "https://providence.jobs/jobs/"
LISTING_API = "https://prod-search-api.jobsyn.org/api/v1/solr/search"
DETAIL_API = "https://microsites.dejobs.org/ALL_JOBS/{guid}.json"
ORIGIN = "providence.jobs"
PAGE_SIZE = 40
DETAIL_CONCURRENCY = 2
GUID_RE = re.compile(r"^[0-9A-F]{32}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
PAGINATION_KEYS = ("has_more_pages", "offset", "page", "page_size", "total", "total_pages")
SECTION_QUALIFICATION = re.compile(
    r"qualific|requirement|education|licens|certif|credential|experience", re.I
)
SECTION_RESPONSIBILITY = re.compile(
    r"responsibilit|essential functions|job summary|position summary|what you.ll do|about the role|duties",
    re.I,
)
FIX_HTML_REMOVALS = (
    "<p><strong>Description</strong></p>",
    "<p>Check out our benefits page for more information about our Benefits and Rewards.</p>",
    "<p>The amounts listed are the base pay range; additional compensation may be available for "
    "this role, such as shift differentials, standby/on-call, overtime, premiums, extra shift "
    "incentives, or bonus opportunities.</p>",
)


def compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_visible(value: str | None) -> str:
    """Approximate rendered innerText normalisation used by the site pages."""
    value = compact(value)
    return re.sub(r"\s+([,.;:!?])", r"\1", value)


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def parse_json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def detail_url(row: dict) -> str:
    """Reconstruct the exact official detail route for a listing row."""
    location = compact(row.get("location_exact") or row.get("city_exact") or "")
    title_slug = row.get("title_slug") or slug(row.get("title_exact") or "")
    return f"https://providence.jobs/{slug(location)}/{title_slug}/{row['guid']}/job/"


def listing_card_url(row: dict) -> str:
    """Exact href the rendered listing card uses.

    The site's AppJobsList links Provider Medical Group (buid 59189) cards
    straight to the apply redirect, and every other card to the official job
    detail route.  The detail route is still the canonical posting page.
    """
    if str(row.get("buid")) == "59189":
        return f"https://rr.jobsyn.org/{row['guid']}10"
    return detail_url(row)


def apply_url(guid: str, buid: Any) -> str:
    """The apply URL built by the official site's AppJobApplyButton component.

    ``guidViewSource`` defaults to 10; for Provider Solutions & Development
    (buid 53254) the official component passes 5824.  Both are on the company's
    published redirect host and resolve to the employer's application.
    """
    suffix = "5824" if str(buid) == "53254" else "10"
    return f"https://rr.jobsyn.org/{guid}{suffix}"


def fix_html(value: str, buid: Any) -> str:
    """Replicate the official job page's description cleanup (see page bundle)."""
    value = value or ""
    if str(buid) == "53254":
        marker = value.find("Job ID Number")
        if marker != -1:
            value = value[:marker]
    marker = value.find("<p><strong>Requsition ID:")
    if marker != -1:
        value = value[:marker]
    for removal in FIX_HTML_REMOVALS:
        value = value.replace(removal, "")
    return value


def description_body(value: str, buid: Any) -> tuple[str, str]:
    """Return the page-visible body HTML/text after the site's fixHtml transform."""
    body_html = fix_html(value, buid)
    soup = BeautifulSoup(body_html, "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return body_html, normalize_visible(soup.get_text(" "))


def parse_salary(other_value: Any, buid: Any) -> dict | None:
    """Parse the public compensation fields without inventing undisclosed values."""
    other = parse_json_object(other_value)
    if not other:
        return None
    if str(buid) == "53254":
        low, high, frequency = other.get("lowrange"), other.get("highrange"), other.get("payfreq")
        source = "other.lowrange/other.highrange"
    else:
        low, high, frequency = other.get("paylow"), other.get("payhigh"), None
        source = "other.paylow/other.payhigh"

    def number(value: Any) -> float | None:
        if value is None:
            return None
        text = str(value).strip().replace(",", "")
        if not text or text.casefold() in {"see posting", "n/a", "na", "not disclosed"}:
            return None
        if text in {"0", "0.0", "0.00"}:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    minimum, maximum = number(low), number(high)
    unit = {
        "per year": "YEAR",
        "per hour": "HOUR",
        "biweekly": "BIWEEKLY",
        "per month": "MONTH",
    }.get(compact(str(frequency)).casefold())
    if unit is None and str(buid) != "53254":
        # The site's JobPosting schema hard-codes HOUR for paylow/payhigh.
        unit = "HOUR"
    note = None
    if minimum is None and maximum is None and compact(str(low)).casefold() == "see posting":
        note = "see posting"
    if minimum is None and maximum is None and note is None:
        return None
    return {
        "min": minimum,
        "max": maximum,
        "currency": "USD",
        "unit": unit,
        "note": note,
        "source": source,
        "raw": {"low": low, "high": high, "frequency": frequency},
    }


def _heading_text(node) -> str | None:
    """A section heading is an h1-h6 or a paragraph consisting only of bold text."""
    if getattr(node, "name", None) in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        value = compact(node.get_text(" ", strip=True))
        return value or None
    strong = node.find("strong", recursive=False) or node.find("b", recursive=False)
    if strong is None:
        return None
    heading = compact(strong.get_text(" ", strip=True))
    if not heading or len(heading) > 120:
        return None
    remainder = compact(node.get_text(" ", strip=True)).replace(heading, "", 1).strip()
    return heading if not remainder else None


def parse_sections(value: str) -> list[dict]:
    """Collect labelled description sections, preserving their public text."""
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    sections: list[dict] = []
    headings = [node for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p"])
                if _heading_text(node)]
    heading_ids = {id(node) for node in headings}
    for heading in headings:
        label = _heading_text(heading)
        chunks = []
        for sibling in heading.next_siblings:
            if getattr(sibling, "name", None) is None:
                continue
            if id(sibling) in heading_ids or _heading_text(sibling):
                break
            text = normalize_visible(sibling.get_text(" "))
            if text:
                chunks.append(text)
        if chunks:
            sections.append({"label": label, "text": "\n\n".join(chunks)})
    return sections


def labelled_sections(sections: list[dict], pattern: re.Pattern) -> list[dict] | None:
    matched = [section for section in sections if pattern.search(section["label"] or "")]
    return matched or None


def parse_listing_page(payload: Any, requested_page: int) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Providence search API response is not an object")
    errors = payload.get("errors")
    if errors:
        raise ValueError(f"Providence search API returned errors: {errors}")
    pagination = payload.get("pagination")
    jobs = payload.get("jobs")
    if not isinstance(pagination, dict) or not isinstance(jobs, list):
        raise ValueError("Providence search API response is missing pagination or jobs")
    missing = [key for key in PAGINATION_KEYS if key not in pagination]
    if missing:
        raise ValueError(f"Providence search API pagination is missing {missing}")
    total, total_pages, page_size = (int(pagination["total"]), int(pagination["total_pages"]),
                                     int(pagination["page_size"]))
    page, offset = int(pagination["page"]), int(pagination["offset"])
    has_more = bool(pagination["has_more_pages"])
    if page != requested_page or page_size != PAGE_SIZE or total < 0 or total_pages < 1:
        raise ValueError("Providence search API pagination schema changed")
    if has_more != (page < total_pages):
        raise ValueError("Providence search API has_more_pages disagrees with total_pages")
    expected_rows = min(page_size, max(0, total - offset))
    issues = []
    if len(jobs) > page_size:
        raise ValueError(f"Providence search page {page} returned more than {page_size} rows")
    if len(jobs) != expected_rows:
        issues.append(f"page_row_count_mismatch:{page}:{len(jobs)}!={expected_rows}")

    rows, seen = [], set()
    for item in jobs:
        if not isinstance(item, dict):
            issues.append("listing_row_not_object")
            continue
        guid = str(item.get("guid") or "").upper()
        if not GUID_RE.fullmatch(guid):
            issues.append("listing_row_missing_or_invalid_guid")
            continue
        if guid in seen:
            issues.append(f"duplicate_guid:{guid}")
            continue
        reqid = item.get("reqid")
        title = compact(item.get("title_exact") or item.get("title"))
        location = compact(item.get("location_exact") or item.get("location"))
        company = compact(item.get("company_exact") or item.get("company"))
        title_slug = item.get("title_slug")
        if not reqid or not title or not location or not company or not title_slug:
            issues.append(f"listing_row_missing_public_field:{guid}")
        if item.get("id") and str(item["id"]) != f"seo.joblisting.{guid}":
            issues.append(f"listing_row_unexpected_id:{guid}")
        row = {
            **item,
            "guid": guid,
            "reqid": str(reqid) if reqid is not None else None,
            "title_exact": title,
            "location_exact": location,
            "company_exact": company,
            "title_slug": title_slug,
            "buid": item.get("buid"),
            "detail_url": detail_url({**item, "guid": guid, "location_exact": location,
                                      "title_exact": title, "title_slug": title_slug}),
            "card_url": listing_card_url({**item, "guid": guid, "buid": item.get("buid")}),
            "card_url_is_external": str(item.get("buid")) == "59189",
        }
        rows.append(row)
        seen.add(guid)
    return {
        "page": page,
        "offset": offset,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_more_pages": has_more,
        "filters": payload.get("filters") if isinstance(payload.get("filters"), dict) else {},
        "meta": payload.get("meta") if isinstance(payload.get("meta"), dict) else {},
        "rows": rows,
        "issues": issues,
    }


def _iso(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if ISO_DATE_RE.fullmatch(text) else None


def apply_detail(job, payload: Any, response_url: str) -> Any:
    """Hydrate a listing job from the exact microsite detail document."""
    if not isinstance(payload, dict):
        raise ValueError("Providence detail document is not an object")
    listing = job.raw.get("listing") or {}
    guid = str(payload.get("guid") or "").upper()
    if guid != job.job_id:
        raise ValueError(f"Providence detail guid {guid!r} does not match listing {job.job_id!r}")
    reqid = payload.get("reqid") or listing.get("reqid")
    title = compact(payload.get("title_exact") or payload.get("title"))
    company = compact(payload.get("company_exact") or payload.get("company"))
    if not title or not company or reqid is None:
        raise ValueError(f"Providence detail {guid} is missing title, company or requisition id")
    if title.casefold() != compact(listing.get("title_exact")).casefold():
        job.warnings.append("title_listing_detail_mismatch")
    if company.casefold() != compact(listing.get("company_exact")).casefold():
        job.warnings.append("company_listing_detail_mismatch")

    html_value = payload.get("html_description") or ""
    if not isinstance(html_value, str) or not html_value.strip():
        raise ValueError(f"Providence detail {guid} has no html_description")
    full_text = text_html(html_value)
    body_html, body_text = description_body(html_value, payload.get("buid", listing.get("buid")))
    if len(body_text) < 100:
        raise ValueError(f"Providence detail {guid} body is too short ({len(body_text)} characters)")

    other = parse_json_object(payload.get("other"))
    sections = parse_sections(html_value)
    buid = payload.get("buid", listing.get("buid"))
    date_new = _iso(payload.get("date_new"))
    date_updated = _iso(payload.get("date_updated"))

    job.title = title
    job.requisition_id = str(reqid)
    job.apply_url = apply_url(guid, buid)
    job.description_html = html_value
    job.description_text = full_text
    job.description_method = "providence.microsites.html_description"
    job.description_source_url = response_url
    job.description_unavailable = False
    job.employer = company
    job.locations = [compact(payload.get("location_exact") or payload.get("location")
                             or listing.get("location_exact"))]
    job.department = other.get("department") or None
    job.team = payload.get("job_category") or None
    job.employment_type = payload.get("job_type") or None
    job.workplace_type = other.get("workplacetype") or None
    job.date_posted = date_new
    job.date_posted_raw = {
        "date_new": payload.get("date_new"),
        "date_updated": payload.get("date_updated"),
        "date_added": payload.get("date_added"),
        "salted_date": payload.get("salted_date"),
        "policy": "date_posted uses the source posting date_new; date_added is the feed/index "
                  "insertion time (also published as JobPosting datePosted by the site) and is "
                  "retained raw, not substituted for date_new.",
    }
    job.date_updated = date_updated
    job.valid_through = None
    job.salary = parse_salary(payload.get("other"), buid)
    job.qualifications = labelled_sections(sections, SECTION_QUALIFICATION)
    job.responsibilities = labelled_sections(sections, SECTION_RESPONSIBILITY)
    job.language = "en"

    detail_fields = {key: value for key, value in payload.items()
                     if key not in {"html_description", "text", "description", "assets"}}
    detail_fields["assets_keys"] = sorted((payload.get("assets") or {}).keys())
    job.raw = {
        "listing": listing,
        "detail": detail_fields,
        "description": {
            "full_html_chars": len(html_value),
            "full_text_chars": len(full_text),
            "body_html_chars": len(body_html),
            "body_text_chars": len(body_text),
            "body_text_sha256": sha256(body_text.encode()).hexdigest(),
            "body_text_fnv1a64": fnv1a64(body_text),
            "fix_html_buid": str(buid),
            "source_field": "html_description",
        },
        "application": {
            "site_apply_url": job.apply_url,
            "microsite_link": payload.get("link"),
            "guid_view_source": "5824" if str(buid) == "53254" else "10",
            "rule": "AppJobApplyButton guidViewSource default 10; Provider Solutions & Development "
                    "(buid 53254) passes 5824.",
        },
        "sections": [
            {"label": section["label"], "text_chars": len(section["text"])}
            for section in sections
        ],
    }
    return job


def parse_entry(body: str, final_url: str) -> dict:
    soup = BeautifulSoup(body or "", "lxml")
    title = compact(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if urlsplit(final_url).netloc not in {"providence.jobs", "www.providence.jobs"}:
        raise ValueError(f"Providence official entry redirected off-site: {final_url}")
    if "Providence" not in title:
        raise ValueError(f"Providence official entry title changed: {title!r}")
    if not soup.select('a[href="/jobs/"]'):
        raise ValueError("Providence official entry no longer links to its jobs listing")
    footer = " ".join(compact(node.get_text(" ", strip=True)) for node in soup.find_all("footer"))
    if "Providence Health" not in footer:
        raise ValueError("Providence official entry footer identity changed")
    return {
        "url": OFFICIAL_ENTRY,
        "final_url": final_url,
        "title": title,
        "jobs_link": "https://providence.jobs/jobs/",
        "footer_identity": "Providence Health & Services",
        "footer_text": footer,
    }


class Providence(Adapter):
    async def verify_entry(self) -> dict:
        started = time.monotonic()
        response = await self.http.get(OFFICIAL_ENTRY)
        record = parse_entry(response.text, str(response.url))
        record.update({
            "status": response.status_code,
            "redirects": [str(hop.url) for hop in response.history],
            "seconds": round(max(0.0, time.monotonic() - started), 6),
        })
        self.result.metadata["official_entry"] = record
        self.result.timings["official_entry_seconds"] = record["seconds"]
        return record

    async def fetch_listing_page(self, page: int) -> dict:
        payload = await self.http.json(
            LISTING_API,
            params={"page": page, "num_items": PAGE_SIZE},
            headers={"x-origin": ORIGIN},
        )
        return parse_listing_page(payload, page)

    async def collect_listing(self, *, label: str) -> tuple[list[dict], dict]:
        started = time.monotonic()
        first = await self.fetch_listing_page(1)
        total, total_pages = first["total"], first["total_pages"]
        pages = {1: first}
        for page in range(2, total_pages + 1):
            if not self.page_guard():
                break
            pages[page] = await self.fetch_listing_page(page)
        rows, issues = [], []
        seen = set()
        for page in sorted(pages):
            record = pages[page]
            rows.extend(record["rows"])
            issues.extend(record["issues"])
            for row in record["rows"]:
                if row["guid"] in seen:
                    issues.append(f"duplicate_guid_across_pages:{row['guid']}")
                seen.add(row["guid"])
            if record["total"] != total or record["total_pages"] != total_pages:
                issues.append(f"listing_meta_changed_on_page_{page}")
        seconds = max(0.0, time.monotonic() - started)
        meta = {
            "label": label,
            "api_url": LISTING_API,
            "origin_header": ORIGIN,
            "page_size": PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "pages_fetched": sorted(pages),
            "rows": len(rows),
            "unique_guids": len(seen),
            "last_page_rows": len(pages[total_pages]["rows"]) if total_pages in pages else None,
            "issues": issues,
            "seconds": round(seconds, 6),
            "page_guids": {str(page): [row["guid"] for row in pages[page]["rows"]]
                           for page in sorted(pages)},
        }
        self.result.metadata.setdefault("listing_snapshots", {})[label] = meta
        self.result.timings[f"listing_{label}_seconds"] = round(seconds, 6)
        return rows, meta

    async def fetch_detail(self, job):
        url = DETAIL_API.format(guid=job.job_id)
        payload = await self.http.json(url)
        return apply_detail(job, payload, url)

    async def run(self):
        started = time.monotonic()
        await self.verify_entry()

        rows, initial = await self.collect_listing(label="initial")
        total = initial["total"]
        self.result.expected_total = total
        self.result.pages = len(initial["pages_fetched"])

        jobs = []
        for row in rows:
            job = self.job(
                {"listing": row},
                title=row["title_exact"],
                url=row["detail_url"],
                job_id=row["guid"],
                requisition_id=row["reqid"],
                employer=row.get("company_exact"),
                locations=[row.get("location_exact")],
                team=row.get("job_category"),
                employment_type=row.get("job_type"),
            )
            self.add(job)
            jobs.append(job)

        initial_complete = (
            initial["unique_guids"] == total
            and len(rows) == total
            and not initial["issues"]
        )
        if not initial_complete:
            self.result.errors.append("providence_initial_listing_membership_not_complete")
            self.result.listing_complete = False

        # Details are public, immutable GUID documents; each one carries the full body.
        await self.hydrate(self.result.jobs, self.fetch_detail)

        final_recheck_failed = None
        try:
            final_rows, final = await self.collect_listing(label="final")
        except Exception as exc:  # keep the complete initial membership but do not certify closure
            final_recheck_failed = f"{type(exc).__name__}: {exc}"
            final_rows, final = rows, initial
            self.result.errors.append(f"final_listing_recheck_failed: {final_recheck_failed}")
        final_by_guid = {row["guid"]: row for row in final_rows}
        current = {job.job_id: job for job in self.result.jobs}
        removed = sorted(set(current) - set(final_by_guid))
        added = sorted(set(final_by_guid) - set(current))
        new_jobs = []
        for guid in added:
            row = final_by_guid[guid]
            job = self.job(
                {"listing": row},
                title=row["title_exact"],
                url=row["detail_url"],
                job_id=guid,
                requisition_id=row["reqid"],
                employer=row.get("company_exact"),
                locations=[row.get("location_exact")],
                team=row.get("job_category"),
                employment_type=row.get("job_type"),
            )
            new_jobs.append(job)
        if new_jobs:
            await self.hydrate(new_jobs, self.fetch_detail)
            for job in new_jobs:
                current[job.job_id] = job

        order = [row["guid"] for row in final_rows]
        self.result.jobs[:] = [current[guid] for guid in order if guid in current]
        self.result.expected_total = final["total"]
        final_complete = (
            final_recheck_failed is None
            and final["unique_guids"] == final["total"]
            and len(final_rows) == final["total"]
            and not final["issues"]
        )
        self.result.listing_complete = bool(initial_complete and final_complete)
        self.result.metadata["membership_reconciliation"] = {
            "initial_total": initial["total"],
            "final_total": final["total"],
            "initial_unique_guids": initial["unique_guids"],
            "final_unique_guids": final["unique_guids"],
            "initial_pages": initial["total_pages"],
            "final_pages": final["total_pages"],
            "initial_rows": len(rows),
            "final_rows": len(final_rows),
            "removed_guids": removed,
            "added_guids": added,
            "final_order_matches_initial": [row["guid"] for row in rows] == order,
            "final_recheck_failed": final_recheck_failed,
            "complete": self.result.listing_complete,
        }
        if not final_complete:
            self.result.errors.append("providence_final_listing_membership_not_complete")
        if removed or added:
            self.result.warnings.append("providence_membership_changed_during_collection")

        employers: dict[str, int] = {}
        buids: dict[str, int] = {}
        for job in self.result.jobs:
            employers[str(job.employer)] = employers.get(str(job.employer), 0) + 1
            buid = str((job.raw.get("detail") or {}).get("buid"))
            buids[buid] = buids.get(buid, 0) + 1
        page_one = initial.get("page_guids", {}).get("1", [])
        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_api": LISTING_API,
            "detail_api": DETAIL_API,
            "detail_concurrency": DETAIL_CONCURRENCY,
            "employer_counts": dict(sorted(employers.items())),
            "buid_counts": dict(sorted(buids.items())),
            "filters_visible": {
                key: [
                    {"display": item.get("display"), "value": item.get("value"),
                     "link": item.get("link")}
                    for item in (initial.get("filters", {}).get(key) or [])
                ]
                for key in ("employer", "workplacetype", "jobfunction", "jobcategory", "state")
            },
            "first_page_guids": page_one,
            "request_country_observed": (initial.get("meta") or {}).get("request_country"),
            "date_policy": "date_posted = source date_new; date_added retained raw as feed/index "
                           "insertion (site JSON-LD datePosted) and not substituted; unknown dates null.",
            "salary_policy": "other.paylow/payhigh (site schema unitText HOUR) or Provider "
                             "Solutions & Development lowrange/highrange with payfreq; "
                             "'see posting' retained as a note, never guessed.",
        })
        self.result.timings["collection_wall_seconds"] = round(max(0.0, time.monotonic() - started), 6)
        return self.result

