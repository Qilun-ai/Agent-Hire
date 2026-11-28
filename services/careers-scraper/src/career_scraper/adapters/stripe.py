"""Stripe's own careers search page and its native listing-detail pages.

Journey (observed 2026-11-08, see companies/stripe/journey-research.md):
`https://stripe.com/jobs/search` 307-redirects to `https://stripe.com/careers/search`.
That page is a Next.js SSR page whose ``__NEXT_DATA__`` payload carries the whole
company listing (``jobIndexData.filters`` + ``jobIndexData.listings``); the visible
UI filters and paginates that embedded array client-side (20 cards per page,
one card per location). Each row is a Stripe-hosted detail at
``/careers/listing/<slug>/<greenhouseId>`` whose payload carries the full
``contentMarkdown`` description, resolved locations/teams/tags, ``postedAt`` and
salary range. Apply is the Stripe-hosted ``/careers/apply/<slug>/<id>`` page.
No external ATS board is linked or embedded.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from hashlib import sha256
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..http import FetchError
from ..models import text_html


OFFICIAL_ENTRY = "https://stripe.com/jobs/search"
LISTING_URL = "https://stripe.com/careers/search"
DETAIL_TEMPLATE = "https://stripe.com/careers/listing/{slug}/{job_id}"
APPLY_TEMPLATE = "https://stripe.com/careers/apply/{slug}/{job_id}"
SITEMAP_URL = "https://stripe.com/sitemap/partition-0.xml"
HOST = "stripe.com"
LISTING_PATH = "/careers/search"
DETAIL_PAGE_ID = "/careers/listing/[slug]/[id]"
UI_PAGE_SIZE = 20
MIN_DESCRIPTION_CHARS = 200
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DETAIL_PATH_RE = re.compile(r"^/careers/listing/([a-z0-9]+(?:-[a-z0-9]+)*)/(\d+)/?$")


def parse_next_data(html: str) -> dict:
    """Return the Next.js ``__NEXT_DATA__`` payload of a Stripe page."""
    soup = BeautifulSoup(html or "", "lxml")
    node = soup.find("script", id="__NEXT_DATA__")
    if node is None or not node.string:
        raise ValueError("Stripe page is missing its __NEXT_DATA__ payload")
    try:
        data = json.loads(node.string)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Stripe __NEXT_DATA__ is not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("props"), dict):
        raise ValueError("Stripe __NEXT_DATA__ has an unexpected shape")
    return data


def _check_host(url: str, path: str | None = None, *, what: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != HOST:
        raise ValueError(f"Stripe {what} came from an unexpected origin: {url}")
    if path is not None and parsed.path.rstrip("/") != path:
        raise ValueError(f"Stripe {what} came from an unexpected path: {url}")


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_body_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _filter_names(rows, label: str) -> list[str]:
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"Stripe listing is missing its {label} filter options")
    names = []
    for row in rows:
        if not isinstance(row, dict) or not normalized_title(row.get("name")):
            raise ValueError(f"Stripe {label} filter option has no name")
        names.append(normalized_title(row["name"]))
    return names


def _indices(value, size: int, field: str, identity: str, issues: list[str]) -> list[int]:
    if not isinstance(value, list):
        issues.append(f"stripe_listing_{field}_not_a_list:{identity}")
        return []
    result = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool) or not 0 <= item < size:
            issues.append(f"stripe_listing_{field}_index_out_of_range:{identity}:{item}")
            continue
        result.append(item)
    return result


def parse_listing(html: str, response_url: str = LISTING_URL) -> dict:
    """Parse Stripe's company listing payload and its filter vocabulary."""
    _check_host(response_url, LISTING_PATH, what="listing")
    data = parse_next_data(html)
    if data.get("page") != LISTING_PATH:
        raise ValueError(f"Stripe listing page id changed: {data.get('page')!r}")
    try:
        index = data["props"]["pageProps"]["jobIndexData"]
        filters = index["filters"]
        listings = index["listings"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Stripe listing payload is missing jobIndexData filters/listings") from exc
    if not isinstance(listings, list) or not listings:
        raise ValueError("Stripe listing payload has no listings")
    if not isinstance(filters, dict):
        raise ValueError("Stripe listing payload has no filter vocabulary")

    teams = _filter_names(filters.get("teams"), "team")
    locations = _filter_names(filters.get("locations"), "location")
    tags = _filter_names(filters.get("tags"), "tag")
    employment_types = _filter_names(filters.get("employmentTypes"), "employment type")

    rows, seen, issues = [], set(), []
    for raw in listings:
        if not isinstance(raw, dict):
            issues.append("stripe_listing_row_not_an_object")
            continue
        identity = raw.get("greenhouseId")
        if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
            issues.append(f"stripe_listing_row_missing_numeric_id:{identity!r}")
            continue
        identity = str(identity)
        title = normalized_title(raw.get("title"))
        slug = raw.get("slug")
        if not title:
            issues.append(f"stripe_listing_row_missing_title:{identity}")
            continue
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
            issues.append(f"stripe_listing_row_invalid_slug:{identity}")
            continue
        location_indices = _indices(raw.get("locationIndices"), len(locations), "location", identity, issues)
        if not location_indices:
            issues.append(f"stripe_listing_row_without_locations:{identity}")
        team_indices = _indices(raw.get("teamIndices"), len(teams), "team", identity, issues)
        tag_indices = _indices(raw.get("tagIndices"), len(tags), "tag", identity, issues)
        employment_type = normalized_title(raw.get("employmentType"))
        if employment_type not in employment_types:
            issues.append(f"stripe_listing_row_unknown_employment_type:{identity}")
        if identity in seen:
            issues.append(f"stripe_listing_duplicate_id:{identity}")
            continue
        seen.add(identity)
        rows.append({
            "id": identity,
            "title": title,
            "slug": slug,
            "location_indices": location_indices,
            "team_indices": team_indices,
            "tag_indices": tag_indices,
            "employment_type": employment_type,
            "employment_type_index": raw.get("employmentTypeIndex"),
            "listing_row": raw,
        })
    if not rows:
        raise ValueError("Stripe listing payload has no usable rows")

    location_rows = sum(len(row["location_indices"]) for row in rows)
    return {
        "url": response_url,
        "page": data.get("page"),
        "build_id": data.get("buildId"),
        "listings": rows,
        "ids": [row["id"] for row in rows],
        "count": len(rows),
        "location_rows": location_rows,
        "ui_pages": -(-location_rows // UI_PAGE_SIZE),
        "filters": {
            "teams": teams,
            "locations": locations,
            "tags": tags,
            "employment_types": employment_types,
        },
        "issues": issues,
    }


def parse_detail(
    html: str,
    response_url: str,
    *,
    expected_id: str,
    expected_slug: str,
    expected_title: str | None = None,
) -> dict:
    """Parse one Stripe listing detail and prove it is the requested posting."""
    _check_host(response_url, what="detail")
    match = DETAIL_PATH_RE.fullmatch(urlsplit(response_url).path.rstrip("/"))
    if match is None:
        raise ValueError(f"Stripe detail URL is not a listing route: {response_url}")
    url_slug, url_id = match.groups()
    if url_id != str(expected_id) or url_slug != expected_slug:
        raise ValueError(f"Stripe detail URL does not match the requested posting: {response_url}")
    data = parse_next_data(html)
    if data.get("page") != DETAIL_PAGE_ID:
        raise ValueError(f"Stripe detail page id changed: {data.get('page')!r}")
    if data.get("query") != {"slug": expected_slug, "id": str(expected_id)}:
        raise ValueError(f"Stripe detail query does not match the posting: {data.get('query')!r}")
    try:
        listing = data["props"]["pageProps"]["listing"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Stripe detail payload is missing its listing record") from exc
    identity = listing.get("greenhouseId")
    if not isinstance(identity, int) or isinstance(identity, bool) or str(identity) != str(expected_id):
        raise ValueError(f"Stripe detail payload id changed: {identity!r}")
    title = normalized_title(listing.get("title"))
    if not title:
        raise ValueError(f"Stripe detail payload has no title for {expected_id}")
    if expected_title is not None and title != normalized_title(expected_title):
        raise ValueError(f"Stripe detail title changed for {expected_id}: {title!r}")
    if listing.get("slug") != expected_slug:
        raise ValueError(f"Stripe detail slug changed for {expected_id}: {listing.get('slug')!r}")

    markdown = listing.get("contentMarkdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError(f"Stripe detail payload has no description for {expected_id}")
    description_html = markdown_to_html(markdown)
    description_text = text_html(description_html)
    if len(normalized_body_text(description_text)) < MIN_DESCRIPTION_CHARS:
        raise ValueError(f"Stripe detail description is empty or truncated for {expected_id}")

    issues = []
    locations = listing.get("locations")
    if not isinstance(locations, list) or not locations:
        raise ValueError(f"Stripe detail payload has no locations for {expected_id}")
    location_names, remote_flags = [], []
    for location in locations:
        if not isinstance(location, dict) or not normalized_title(location.get("name")):
            issues.append(f"stripe_detail_location_without_name:{expected_id}")
            continue
        location_names.append(normalized_title(location["name"]))
        remote_flags.append(bool(location.get("remote")))
    if not location_names:
        raise ValueError(f"Stripe detail payload has no usable locations for {expected_id}")

    teams = listing.get("teams")
    team_names = [normalized_title(team.get("name")) for team in teams or []
                  if isinstance(team, dict) and normalized_title(team.get("name"))]
    tags = [normalized_title(tag.get("name")) for tag in listing.get("tags") or []
            if isinstance(tag, dict) and normalized_title(tag.get("name"))]

    posted_at = listing.get("postedAt")
    date_posted = None
    if posted_at is not None:
        date_posted = _iso_date(posted_at)
        if date_posted is None:
            issues.append(f"stripe_detail_posted_at_not_iso:{expected_id}")

    salary = None
    currency = listing.get("salaryCurrency")
    low, high = listing.get("salaryMin"), listing.get("salaryMax")
    if any(value is not None for value in (currency, low, high)):
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            issues.append(f"stripe_detail_salary_currency_invalid:{expected_id}")
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (low, high)):
            issues.append(f"stripe_detail_salary_range_invalid:{expected_id}")
        elif low > high:
            issues.append(f"stripe_detail_salary_range_reversed:{expected_id}")
        else:
            salary = {"currency": currency, "min": low, "max": high}

    employment_type = normalized_title(listing.get("employmentType"))
    if not employment_type:
        issues.append(f"stripe_detail_employment_type_missing:{expected_id}")

    primary = listing.get("location") if isinstance(listing.get("location"), dict) else {}
    body = normalized_body_text(description_text)
    return {
        "id": str(identity),
        "slug": expected_slug,
        "title": title,
        "description_html": description_html,
        "description_text": description_text,
        "description_chars": len(body),
        "content_markdown_chars": len(markdown),
        "description_sha256": sha256(body.encode()).hexdigest(),
        "description_fnv1a64": fnv1a64(body),
        "location_names": location_names,
        "remote_flags": remote_flags,
        "team_names": team_names,
        "tags": tags,
        "employment_type": employment_type,
        "workplace_type": "Remote" if primary.get("remote") is True else None,
        "date_posted": date_posted,
        "date_posted_raw": posted_at,
        "salary": salary,
        "apply_url": APPLY_TEMPLATE.format(slug=expected_slug, job_id=expected_id),
        "record": listing,
        "issues": issues,
    }


def _iso_date(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.isoformat()


def _inline_markdown(value: str) -> str:
    text = escape(value, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![*\w])\*(?!\s)([^*]+?)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return text


def markdown_to_html(markdown: str) -> str:
    """Render the description markdown subset Stripe publishes into HTML."""
    lines = (markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    lists: list[tuple[str, int]] = []

    def flush_paragraph():
        if paragraph:
            out.append("<p>" + " ".join(_inline_markdown(line.strip()) for line in paragraph) + "</p>")
            paragraph.clear()

    def close_lists(depth: int = 0):
        while len(lists) > depth:
            out.append(f"</{lists.pop()[0]}>")

    for line in lines:
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        bullet = re.match(r"^(\s*)[*-]\s+(.*)$", line)
        ordered = re.match(r"^(\s*)\d+[.)]\s+(.*)$", line)
        if not stripped:
            flush_paragraph()
            close_lists()
            continue
        if heading:
            flush_paragraph()
            close_lists()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline_markdown(heading.group(2).strip())}</h{level}>")
            continue
        if re.fullmatch(r"-{3,}|\*{3,}", stripped):
            flush_paragraph()
            close_lists()
            out.append("<hr/>")
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            close_lists()
            out.append(f"<blockquote><p>{_inline_markdown(stripped.lstrip('>').strip())}</p></blockquote>")
            continue
        if bullet or ordered:
            flush_paragraph()
            tag = "ul" if bullet else "ol"
            indent = len((bullet or ordered).group(1).expandtabs(4))
            depth = indent // 2 + 1
            close_lists(depth)
            if len(lists) < depth:
                out.append(f"<{tag}>")
                lists.append((tag, depth))
            elif lists[-1][0] != tag:
                out.append(f"</{lists.pop()[0]}>")
                out.append(f"<{tag}>")
                lists.append((tag, depth))
            out.append(f"<li>{_inline_markdown((bullet or ordered).group(2).strip())}</li>")
            continue
        close_lists()
        paragraph.append(stripped)
    flush_paragraph()
    close_lists()
    return "\n".join(out)


class Stripe(Adapter):
    blocked = False
    _blocked_reported = False

    async def run(self):
        started = time.perf_counter()
        self.blocked = False
        self._blocked_reported = False
        self.result.metadata.update({
            "official_entry_url": OFFICIAL_ENTRY,
            "expected_listing_url": LISTING_URL,
            "sitemap_cross_check_url": SITEMAP_URL,
            "ui_page_size": UI_PAGE_SIZE,
        })
        if not self.page_guard():
            return self.result
        listing_started = time.perf_counter()
        try:
            response = await self.http.request(
                "GET", OFFICIAL_ENTRY,
                headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        except FetchError as exc:
            self.result.errors.append(f"stripe_listing_fetch_failed:{exc}")
            self.result.listing_complete = False
            return self.result
        final_url = str(response.url)
        _check_host(final_url, LISTING_PATH, what="listing")
        listing = parse_listing(response.text, final_url)
        self.result.timings["company_listing_seconds"] = self.result.timings.get(
            "company_listing_seconds", 0.0) + max(0.0, time.perf_counter() - listing_started)
        self.result.expected_total = listing["count"]
        self.result.metadata.update({
            "listing_url": final_url,
            "listing_status": response.status_code,
            "listing_build_id": listing["build_id"],
            "listing_rows": listing["count"],
            "listing_location_rows": listing["location_rows"],
            "listing_ui_pages": listing["ui_pages"],
            "filters": {
                "teams": len(listing["filters"]["teams"]),
                "locations": len(listing["filters"]["locations"]),
                "tags": listing["filters"]["tags"],
                "employment_types": listing["filters"]["employment_types"],
            },
            "listing_issues": listing["issues"],
        })
        if listing["issues"]:
            self.result.errors.extend(listing["issues"])

        filters = listing["filters"]
        for row in listing["listings"]:
            detail_url = DETAIL_TEMPLATE.format(slug=row["slug"], job_id=row["id"])
            job = self.job(
                {
                    "listing": row["listing_row"],
                    "listing_row": row,
                    "location_names": [filters["locations"][i] for i in row["location_indices"]],
                    "team_names": [filters["teams"][i] for i in row["team_indices"]],
                    "tag_names": [filters["tags"][i] for i in row["tag_indices"]],
                },
                title=row["title"],
                url=detail_url,
                job_id=row["id"],
                apply_url=APPLY_TEMPLATE.format(slug=row["slug"], job_id=row["id"]),
                employment_type=row["employment_type"],
                locations=[filters["locations"][i] for i in row["location_indices"]],
            )
            self.add(job)
        if self.limit():
            return self.result

        await self.hydrate(self.result.jobs, self._detail)
        listing_complete = (not self.result.errors
                            and len(self.result.jobs) == self.result.expected_total
                            and all(job.description_verified for job in self.result.jobs))
        self.result.listing_complete = listing_complete
        self.result.timings["run_wall_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        return self.result

    async def _detail(self, job):
        if self.blocked:
            if not self._blocked_reported:
                self.result.errors.append("stripe_collection_aborted_after_source_block")
                self._blocked_reported = True
            job.warnings.append("detail_skipped_after_source_block")
            return job
        slug = job.raw["listing_row"]["slug"]
        try:
            response = await self.http.request(
                "GET", job.url,
                headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        except FetchError as exc:
            message = str(exc)
            if re.search(r"HTTP (?:403|429)\b", message):
                self.blocked = True
                self.result.errors.append(f"stripe_source_blocked:{message}")
                job.warnings.append("detail_blocked_by_source")
                return job
            raise
        parsed = parse_detail(
            response.text, str(response.url),
            expected_id=job.job_id, expected_slug=slug, expected_title=job.title)
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "company_detail_page_contentMarkdown"
        job.description_source_url = str(response.url)
        job.apply_url = parsed["apply_url"]
        job.locations = parsed["location_names"]
        job.department = parsed["team_names"] or None
        job.team = parsed["team_names"] or None
        job.employment_type = parsed["employment_type"] or job.employment_type
        job.workplace_type = parsed["workplace_type"]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.salary = parsed["salary"]
        job.employer = "Stripe"
        job.raw["detail"] = parsed["record"]
        job.raw["detail_resolved"] = {
            "locations": parsed["location_names"],
            "remote_flags": parsed["remote_flags"],
            "teams": parsed["team_names"],
            "tags": parsed["tags"],
            "description_chars": parsed["description_chars"],
            "content_markdown_chars": parsed["content_markdown_chars"],
            "description_sha256": parsed["description_sha256"],
            "detail_response_status": response.status_code,
            "detail_final_url": str(response.url),
        }
        if parsed["issues"]:
            job.warnings.extend(parsed["issues"])
        if not parsed["team_names"]:
            job.warnings.append("stripe_detail_team_missing")
        return job
