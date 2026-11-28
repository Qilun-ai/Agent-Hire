"""Spotify's own job search API and complete descriptions from its own detail pages."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from html import escape, unescape
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


ENTRY = "https://www.lifeatspotify.com/jobs"
API = "https://api.lifeatspotify.com/wp-json/animal/v1/job/search"
DETAIL_BASE = "https://www.lifeatspotify.com/jobs/"
VERIFICATION_REVISION = "spotify.lifeatspotify-search-and-details.2"

TITLE_SPLIT = re.compile(r"[^a-z0-9]+")
RESPONSIBILITY_HEADING = re.compile(
    r"\b(?:what you(?:'|’)ll do|what you will do|what you(?:'|’)ll be doing|"
    r"responsibilities|your role|the role)\b", re.I)
QUALIFICATION_HEADING = re.compile(
    r"\b(?:who you are|who you(?:'|’)ll be|what you(?:'|’)ll need|what you will need|"
    r"qualifications|requirements|about you|your experience|skills)\b", re.I)
SALARY_WORD = re.compile(r"\b(?:base|salary|compensation|pay)\b", re.I)
CURRENCY = re.compile(r"\b(USD|CAD|EUR|GBP|SEK)\b")
AMOUNT_RANGE = re.compile(
    r"\$?\s*(?P<low>\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*"
    r"(?:-|–|—|to)\s*\$?\s*(?P<high>\d{1,3}(?:,\d{3})*(?:\.\d+)?)", re.I)


def clean_text(value) -> str:
    """Normalize source text without changing its visible wording."""
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def title_key(value) -> str:
    normalized = unicodedata.normalize("NFKD", clean_text(value)).casefold()
    return re.sub(r"\s+", " ", TITLE_SPLIT.sub(" ", normalized)).strip()


def _string(value):
    if not isinstance(value, str):
        return None
    value = clean_text(value)
    return value or None


def _names(values):
    names = []
    if not isinstance(values, list):
        return names
    for item in values:
        if isinstance(item, str):
            name = _string(item)
        elif isinstance(item, dict):
            name = _string(item.get("location") or item.get("name"))
        else:
            name = None
        if name and name not in names:
            names.append(name)
    return names


def listing_employment_type(row: dict):
    job_type = row.get("job_type")
    if isinstance(job_type, dict):
        return _string(job_type.get("name"))
    return _string(job_type)


def parse_listing(payload: dict) -> tuple[list[dict], dict]:
    """Validate the exact unfiltered response used by lifeatspotify.com/jobs."""
    if not isinstance(payload, dict):
        raise ValueError("Spotify job search response is not an object")
    rows = payload.get("result")
    if not isinstance(rows, list):
        raise ValueError("Spotify company search schema changed")
    parsed, slugs = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Spotify listing row is not an object")
        source_id = row.get("id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("Spotify listing has no posting slug")
        if source_id in slugs:
            raise ValueError(f"Spotify listing repeats posting slug: {source_id}")
        title = clean_text(row.get("text"))
        if not title:
            raise ValueError(f"Spotify listing has no title: {source_id}")
        main_category = row.get("main_category")
        if main_category is not None and not isinstance(main_category, dict):
            raise ValueError(f"Spotify listing category is malformed: {source_id}")
        locations = row.get("locations")
        if locations is not None and not isinstance(locations, list):
            raise ValueError(f"Spotify listing locations are malformed: {source_id}")
        slugs.add(source_id)
        parsed.append(row)
    return parsed, {
        "api": API,
        "time": payload.get("time"),
        "main_categories": payload.get("main_categories") or [],
        "count": len(parsed),
        "row_fields": sorted({key for row in parsed for key in row}),
    }


def parse_entry(html: str) -> dict:
    """Read the official /jobs page's own filter totals for a membership cross-check."""
    soup = BeautifulSoup(html or "", "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        raise ValueError("Spotify official jobs page data is missing")
    try:
        page = json.loads(script.string or script.get_text())["props"]["pageProps"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Spotify official jobs page data is malformed") from exc
    if not isinstance(page, dict) or page.get("statusCode") != 200:
        raise ValueError("Spotify official jobs page is unavailable")
    filters = page.get("allFilters")
    if not isinstance(filters, dict):
        raise ValueError("Spotify official jobs page has no filter totals")
    for key in ("locations", "categories", "jobtypes"):
        values = filters.get(key)
        if not isinstance(values, list):
            raise ValueError(f"Spotify official jobs page {key} filters are missing")
        if any(not isinstance(item, dict) or not isinstance(item.get("positions"), int) for item in values):
            raise ValueError(f"Spotify official jobs page {key} filters are malformed")
    meta = (page.get("data") or {}).get("meta") or {}
    return {
        "title": clean_text(soup.title.get_text() if soup.title else ""),
        "meta": meta,
        "filters": filters,
        "location_total": sum(item["positions"] for item in filters["locations"]),
        "category_total": sum(item["positions"] for item in filters["categories"]),
        "jobtype_total": sum(item["positions"] for item in filters["jobtypes"]),
    }


def parse_detail(html: str) -> dict:
    """Read one exact Spotify detail page's embedded published-job object."""
    soup = BeautifulSoup(html or "", "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        raise ValueError("Spotify detail page data is missing")
    try:
        page = json.loads(script.string or script.get_text())["props"]["pageProps"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Spotify detail page data is malformed") from exc
    if not isinstance(page, dict) or page.get("statusCode") != 200:
        raise ValueError("Spotify posting is unavailable or no longer public")
    info = page.get("job")
    if not isinstance(info, dict) or info.get("state") != "published":
        raise ValueError("Spotify posting is unavailable or no longer public")
    if not isinstance(info.get("id"), str) or not info["id"].strip():
        raise ValueError("Spotify detail has no posting id")
    if not clean_text(info.get("text")):
        raise ValueError("Spotify detail has no title")
    content = info.get("content")
    if not isinstance(content, dict):
        raise ValueError("Spotify detail description fields are missing")
    sections = content.get("lists", [])
    if not isinstance(sections, list) or any(not isinstance(section, dict) for section in sections):
        raise ValueError("Spotify detail description sections are malformed")
    return info


def description_from_detail(info: dict) -> tuple[str, list[dict]]:
    """Rebuild the employer-authored description in its published section order."""
    content = info.get("content") or {}
    sections = content.get("lists") or []
    parts = []
    intro_html = content.get("descriptionHtml") or ""
    if intro_html:
        parts.append(intro_html)
    elif _string(content.get("description")):
        parts.append(escape(content["description"]).replace("\n", "<br>"))
    for section in sections:
        heading = clean_text(section.get("text"))
        body = section.get("content") or ""
        if heading:
            parts.append(f"<h2>{escape(heading)}</h2>")
        if body:
            parts.append(body)
    closing_html = content.get("closingHtml") or ""
    closing_text = content.get("closing") or ""
    closing_body = closing_html if text_html(closing_html) else closing_text
    # The source uses a lone dash as a placeholder close on some postings.
    if closing_body and re.search(r"[A-Za-z0-9]", text_html(closing_body)):
        parts.append(closing_html or escape(closing_text).replace("\n", "<br>"))
    description = "".join(parts)
    if not text_html(description):
        raise ValueError("Spotify full description fields missing")
    return description, sections


def _section_records(sections, pattern):
    records = []
    for section in sections:
        heading = clean_text(section.get("text"))
        if heading and pattern.search(heading):
            body = section.get("content") or ""
            records.append({"heading": heading, "html": body, "text": text_html(body)})
    return records or None


def extract_salary(html: str):
    """Return the shortest exact block that publishes a salary range, if any."""
    soup = BeautifulSoup(html or "", "lxml")
    candidates = []
    for block in soup.find_all(["p", "div", "li"]):
        text = clean_text(block.get_text(" ", strip=True))
        if not text or "$" not in text or not SALARY_WORD.search(text):
            continue
        match = AMOUNT_RANGE.search(text)
        if match is None:
            continue
        candidates.append((len(text), text, match))
    if not candidates:
        return None
    _, text, match = min(candidates, key=lambda item: item[0])
    low, high = (float(value.replace(",", "")) for value in match.groups())
    low = int(low) if low.is_integer() else low
    high = int(high) if high.is_integer() else high
    currency = CURRENCY.search(text)
    if currency is None:
        if re.search(r"\b(?:United States|US|U\.S\.)\b", text):
            currency_value = "USD"
        elif re.search(r"\bCanada\b", text):
            currency_value = "CAD"
        else:
            currency_value = None
    else:
        currency_value = currency.group(1).upper()
    return {
        "text": text,
        "min": low,
        "max": high,
        "currency": currency_value,
        "source": "Spotify closingHtml",
    }


class Spotify(Adapter):
    API = API

    async def run(self):
        started = time.perf_counter()
        payload = await self.http.json(self.API)
        self.result.timings["listing_phase_seconds"] = max(0.0, time.perf_counter() - started)
        rows, listing = parse_listing(payload)
        self.result.pages, self.result.expected_total = 1, len(rows)
        self.result.metadata.update({
            "verification_revision": VERIFICATION_REVISION,
            "official_entry_url": ENTRY,
            "listing_url": ENTRY,
            "listing_api_url": self.API,
            "listing_api_method": "GET",
            "listing_api_query": None,
            "listing_api_time": listing["time"],
            "listing_api_count": listing["count"],
            "listing_api_row_fields": listing["row_fields"],
            "listing_main_categories": listing["main_categories"],
            "listing_filters_applied": False,
            "listing_pagination": (
                "The company's /jobs UI loads this full unfiltered list once and "
                "paginates it client-side with a 'Load more jobs' control (5 per click)."
            ),
            "detail_method": (
                "Exact /jobs/{slug} page's __NEXT_DATA__.props.pageProps.job:"
                " descriptionHtml + ordered lists + closingHtml."
            ),
            "detail_url_template": DETAIL_BASE + "{listing_slug}",
        })
        initial_ids = []
        for row in rows:
            source_id = row["id"]
            initial_ids.append(source_id)
            self.add(self.job(
                dict(row),
                title=clean_text(row["text"]),
                job_id=source_id,
                url=DETAIL_BASE + quote(source_id, safe=""),
                locations=_names(row.get("locations")),
                department=(row.get("main_category") or {}).get("name"),
                employment_type=listing_employment_type(row),
            ))
            if self.limit():
                break
        self.result.metadata["listing_ids"] = initial_ids
        self.result.listing_complete = (
            "sample_limit" not in self.result.warnings and len(self.seen) == len(rows)
        )

        async def detail(job):
            response = await self.http.get(job.url)
            parsed = urlsplit(str(response.url))
            requested = urlsplit(job.url)
            if (parsed.scheme != "https" or parsed.netloc != "www.lifeatspotify.com"
                    or parsed.path.rstrip("/") != requested.path.rstrip("/")):
                raise ValueError("Spotify detail page redirected away from its exact job route")
            info = parse_detail(response.text)
            listing_title = job.raw.get("text")
            if title_key(info.get("text")) != title_key(listing_title):
                raise ValueError(
                    f"Spotify detail title mismatch: {clean_text(listing_title)!r} vs {clean_text(info.get('text'))!r}"
                )
            description, sections = description_from_detail(info)
            categories = info.get("categories") or {}
            urls = info.get("urls") or {}
            job.raw["detail"] = info
            job.raw["detail_response_url"] = str(response.url)
            job.job_id = str(info["id"])
            job.title = clean_text(info["text"])
            job.description_html = description
            job.description_method = "spotify.page_data.full_sections"
            job.description_source_url = str(response.url)
            job.locations = (
                _names(categories.get("allLocations"))
                or _names(categories.get("locations"))
                or job.locations
            )
            job.department = (
                _string(categories.get("department"))
                or _string((categories.get("main_category") or {}).get("name"))
                or job.department
            )
            job.team = _string(categories.get("team"))
            job.employment_type = _string(categories.get("commitment")) or job.employment_type
            job.apply_url = _string(urls.get("apply"))
            job.qualifications = _section_records(sections, QUALIFICATION_HEADING)
            job.responsibilities = _section_records(sections, RESPONSIBILITY_HEADING)
            job.salary = extract_salary(description)
            if job.salary:
                job.raw["compensation_text"] = job.salary["text"]
            # createdAt is an ATS creation timestamp, not an independently
            # identified first-publication date; preserve it raw and keep the
            # normalized posting date null.
            job.date_posted_raw = {"createdAt": info.get("createdAt")}
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        if self.result.listing_complete:
            final_started = time.perf_counter()
            final = await self.http.json(self.API)
            self.result.timings["final_listing_recheck_seconds"] = max(
                0.0, time.perf_counter() - final_started
            )
            self.result.pages += 1
            try:
                final_rows, final_listing = parse_listing(final)
            except ValueError as exc:
                self.result.listing_complete = False
                self.result.errors.append(f"spotify_final_listing_invalid: {exc}")
                return self.result
            final_ids = [row["id"] for row in final_rows]
            self.result.metadata.update({
                "final_listing_api_time": final_listing["time"],
                "final_listing_ids": final_ids,
                "initial_final_id_order_matches": final_ids == initial_ids,
                "initial_final_full_rows_match": final_rows == rows,
                "new_ids": sorted(set(final_ids) - set(initial_ids)),
                "removed_ids": sorted(set(initial_ids) - set(final_ids)),
            })
            if set(final_ids) != set(initial_ids):
                self.result.listing_complete = False
                self.result.errors.append("spotify_listings_changed_during_run")
            elif final_rows != rows:
                self.result.warnings.append("spotify_listing_rows_changed_during_run")
        return self.result
