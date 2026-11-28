"""Whatfix's own careers page and the exact Trakstar Hire listing it publishes.

The reviewed journey on 2026-11-08 is:

* ``https://whatfix.com/careers/`` (official entry, HTTP 200);
* its ``See All Openings`` button points at
  ``https://whatfix101.recruiterbox.com/#content``, which 301-redirects to
  ``https://whatfix101.hire.trakstar.com/``;
* that Trakstar Hire board publishes 33 jobs over two sorted pages, 25 + 8,
  with the visible paginator text ``1 - 25 of 33 Jobs`` / ``26 - 33 of 33 Jobs``;
* every exact ``/jobs/<id>/`` detail page carries a full JobPosting JSON-LD
  description and the same body in its ``.jobdesciption`` container.

Only the company's own published board is enumerated.  No guessed ATS slug,
aggregator, parent-company board or search-engine result is used.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
from html import unescape
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://whatfix.com/careers/"
PUBLISHED_LISTING_LINK = "https://whatfix101.recruiterbox.com/#content"
LISTING_URL = "https://whatfix101.hire.trakstar.com/"
LISTING_HOST = "whatfix101.hire.trakstar.com"
RSS_URL = "https://whatfix101.hire.trakstar.com/jobfeeds/whatfix101"
JSAPI_URL = "https://jsapi.recruiterbox.com/v1/openings?client_name=whatfix101&limit=100"
JSAPI_CLIENT = "whatfix101"
EMPLOYER_NAME = "Whatfix"
PAGE_SIZE = 25
JOB_PATH = re.compile(r"^/jobs/([a-z0-9]+)/?$", re.I)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
}


def clean(value) -> str:
    """Normalise a published visible string without inventing content."""
    value = unescape(unicodedata.normalize("NFC", str(value or "")))
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s+([,.;:!?%)\]])", r"\1", value)
    value = re.sub(r"([(\[])\s+", r"\1", value)
    return value


def normalized_body_text(value: str) -> str:
    """Match the browser's whitespace-normalised innerText for a body block."""
    return clean(value)


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _listing_page_url(response_url: str) -> str:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc.lower() != LISTING_HOST:
        raise ValueError(f"Whatfix listing redirected to an unexpected host: {response_url}")
    query = parts.query
    path = parts.path or "/"
    return f"https://{LISTING_HOST}{path}" + (f"?{query}" if query else "")


def _job_id(url: str) -> str | None:
    match = JOB_PATH.fullmatch(urlsplit(url).path)
    return match.group(1) if match else None


def _json_ld_job_posting(soup: BeautifulSoup) -> dict:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw, strict=False)
        except (TypeError, ValueError):
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            types = node.get("@type")
            if types == "JobPosting" or isinstance(types, list) and "JobPosting" in types:
                return node
    return {}


def _parse_init_data(html: str) -> dict:
    filters_match = re.search(r"filters:\s*(\{.*?\})\s*,\s*radius_search_parameters", html, re.S)
    if not filters_match:
        raise ValueError("Whatfix Trakstar listing init data has no filters object")
    try:
        filters = json.loads(filters_match.group(1))
    except ValueError as exc:
        raise ValueError("Whatfix Trakstar listing filters object is not JSON") from exc

    paginator_match = re.search(r"paginator:\s*\{(.*?)\}\s*,\s*\}", html, re.S)
    if not paginator_match:
        raise ValueError("Whatfix Trakstar listing init data has no paginator object")
    paginator = paginator_match.group(1)

    def number(name):
        match = re.search(rf"{name}:\s*'?(\d+)'?", paginator)
        return int(match.group(1)) if match else None

    def link(name):
        match = re.search(rf"{name}:\s*(?:(['\"])(.*?)\1|null)", paginator, re.S)
        if not match or match.group(1) is None:
            return None
        return match.group(2) or None

    total_jobs_match = re.search(r"total_jobs:\s*(\d+)", html)
    selection_match = re.search(r"jobs_for_selection:\s*(\d+)", html)
    sort_match = re.search(r"sort_by:\s*(['\"])(.*?)\1", html)
    return {
        "filters": filters,
        "total_jobs": int(total_jobs_match.group(1)) if total_jobs_match else None,
        "jobs_for_selection": int(selection_match.group(1)) if selection_match else None,
        "sort_by": sort_match.group(2) if sort_match else None,
        "page": number("page"),
        "start_index": number("start_index"),
        "end_index": number("end_index"),
        "total_results": number("total_results"),
        "next_page_url": link("next_page_url"),
        "prev_page_url": link("prev_page_url"),
    }


def _option_counts(group: dict | None) -> dict:
    result = {}
    if not isinstance(group, dict):
        return result
    for item in group.get("options", []):
        if not isinstance(item, dict) or not isinstance(item.get("option"), dict):
            continue
        option = item["option"]
        name = clean(option.get("name") or option.get("id"))
        count = item.get("count")
        if name and isinstance(count, int):
            result[name] = count
    return result


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc.lower() not in {"whatfix.com", "www.whatfix.com"}
            or parts.path.rstrip("/") != "/careers"):
        raise ValueError("Whatfix official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html or "", "lxml")
    title = clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
    h1 = [clean(node.get_text(" ", strip=True)) for node in soup.find_all("h1")]
    links = []
    for anchor in soup.find_all("a", href=True):
        label = clean(anchor.get_text(" ", strip=True))
        if label.casefold() != "see all openings":
            continue
        target = urljoin(response_url, anchor["href"])
        links.append({"label": label, "href": anchor["href"], "url": target})
    if len(links) != 1:
        raise ValueError(f"Whatfix careers page no longer publishes exactly one See All Openings link ({len(links)})")
    target = urlsplit(links[0]["url"])
    if target.scheme != "https" or target.netloc.lower() != "whatfix101.recruiterbox.com" or target.path not in {"", "/"}:
        raise ValueError(f"Whatfix See All Openings target is not the reviewed Trakstar journey: {links[0]['url']}")
    canonical = soup.select_one('link[rel="canonical"]')
    return {
        "response_url": response_url,
        "title": title,
        "h1": h1,
        "see_all_openings": links[0],
        "canonical": canonical.get("href") if canonical else None,
        "search_form_present": bool(soup.select_one("#searchData")),
        "job_block_present": bool(soup.select_one("#career_job_block")),
        "job_posting_schema_count": sum(
            1 for node in soup.select('script[type="application/ld+json"]')
            if "JobPosting" in (node.string or "")
        ),
    }


def parse_listing_page(html: str, response_url: str = LISTING_URL, requested_page: int = 1) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc.lower() != LISTING_HOST or parts.path not in {"", "/"}:
        raise ValueError(f"Whatfix Trakstar listing came from an unexpected route: {response_url}")
    query = parts.query
    if requested_page == 1 and query not in {"", "?"}:
        raise ValueError(f"Whatfix Trakstar page 1 has unexpected query: {response_url}")
    if requested_page > 1 and query != f"p={requested_page}":
        raise ValueError(f"Whatfix Trakstar page {requested_page} query changed: {response_url}")

    soup = BeautifulSoup(html or "", "lxml")
    init = _parse_init_data(html or "")
    total = init["total_jobs"]
    if total is None:
        raise ValueError("Whatfix Trakstar listing is missing total_jobs")
    if init["total_results"] is not None and init["total_results"] != total:
        raise ValueError("Whatfix Trakstar total_jobs and paginator total_results disagree")
    expected_page = min(PAGE_SIZE, max(0, total - (requested_page - 1) * PAGE_SIZE))
    expected_start = (requested_page - 1) * PAGE_SIZE + 1
    expected_end = expected_start + expected_page - 1 if expected_page else 0
    issues = []
    if init["start_index"] is not None and init["start_index"] != expected_start:
        issues.append(f"start_index_mismatch:{init['start_index']}!={expected_start}")
    if init["end_index"] is not None and init["end_index"] != expected_end:
        issues.append(f"end_index_mismatch:{init['end_index']}!={expected_end}")
    if init["jobs_for_selection"] is not None and init["jobs_for_selection"] != total:
        issues.append("jobs_for_selection_mismatch")

    filters = init["filters"]
    country_counts = _option_counts(filters.get("countries"))
    state_counts = _option_counts(filters.get("states"))
    city_counts = _option_counts(filters.get("cities"))
    other_counts = _option_counts(filters.get("other_locations"))
    team_counts = _option_counts(filters.get("teams"))
    if country_counts and other_counts and sum(country_counts.values()) + sum(other_counts.values()) != total:
        issues.append("location_filter_counts_do_not_reconcile_with_total")

    rows, seen = [], set()
    for position, card in enumerate(soup.select(".js-careers-page-job-list-item"), 1):
        anchor = card.select_one('a[href^="/jobs/"]')
        href = anchor.get("href") if anchor else card.get("data-href")
        if not href:
            issues.append(f"listing_card_without_job_link:{position}")
            continue
        target = urljoin(response_url, href)
        identity = _job_id(target)
        if not identity:
            issues.append(f"listing_card_job_path_invalid:{href}")
            continue
        if identity in seen:
            issues.append(f"duplicate_listing_job:{identity}")
            continue
        title_node = card.select_one(".js-job-list-opening-name")
        title = clean(title_node.get_text(" ", strip=True) if title_node else "")
        if not title:
            issues.append(f"listing_card_missing_title:{identity}")
            continue
        location_node = card.select_one(".js-job-list-opening-loc")
        city_node = card.select_one(".meta-job-location-city")
        state_node = card.select_one(".meta-job-location-state")
        country_node = card.select_one(".meta-job-location-country")
        city = clean(city_node.get_text(" ", strip=True) if city_node else "")
        state = clean(state_node.get_text(" ", strip=True) if state_node else "")
        country = clean(country_node.get_text(" ", strip=True) if country_node else "")
        location_text = clean(location_node.get_text(" ", strip=True) if location_node else "")
        if not location_text:
            location_text = ", ".join(value for value in (city, state, country) if value)
        meta = card.select_one(".js-job-list-opening-meta")
        employment_type = clean(meta.get_text(" ", strip=True) if meta else "")
        department = None
        if meta is not None and meta.parent is not None:
            for node in meta.parent.find_all("div", recursive=False):
                if node is meta or "rb-text-4" not in (node.get("class") or []):
                    continue
                candidate = clean(node.get_text(" ", strip=True))
                if candidate and not candidate.lower().startswith("apply by"):
                    department = candidate
                    break
        apply_by_raw = None
        apply_by_iso = None
        for stamped in card.select("span[data-time]"):
            label = clean(stamped.get("title") or "")
            if label.lower().startswith("apply by:"):
                apply_by_raw = clean(label.split(":", 1)[1])
                seconds = stamped.get("data-time")
                if seconds and seconds.isdigit():
                    apply_by_iso = datetime.fromtimestamp(int(seconds), timezone.utc).date().isoformat()
                break
        rows.append({
            "id": identity,
            "url": f"https://{LISTING_HOST}/jobs/{identity}/",
            "title": title,
            "location": {
                "city": city or None,
                "state": state or None,
                "country": country or None,
                "display": location_text or None,
            },
            "department": department,
            "employment_type": employment_type or None,
            "apply_by_raw": apply_by_raw,
            "apply_by_iso": apply_by_iso,
            "listing_position": expected_start + position - 1,
            "card_text": clean(card.get_text(" ", strip=True)),
        })
        seen.add(identity)

    if len(rows) != expected_page:
        issues.append(f"page_row_count_mismatch:{len(rows)}!={expected_page}")
    if len(seen) != len(rows):
        issues.append("duplicate_ids_in_page")
    if team_counts and sum(team_counts.values()) + sum(1 for row in rows if not row["department"]) != len(rows):
        # Page-level team counts are global, so this is checked by the caller across all rows.
        pass

    next_url = urljoin(response_url, init["next_page_url"]) if init["next_page_url"] else None
    prev_url = urljoin(response_url, init["prev_page_url"]) if init["prev_page_url"] else None
    return {
        "response_url": response_url,
        "page": requested_page,
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "total": total,
        "start_index": init["start_index"],
        "end_index": init["end_index"],
        "page_size": PAGE_SIZE,
        "next_page_url": next_url,
        "prev_page_url": prev_url,
        "sort_by": init["sort_by"],
        "filters": {
            "countries": country_counts,
            "states": state_counts,
            "cities": city_counts,
            "other_locations": other_counts,
            "teams": team_counts,
        },
        "issues": issues,
        "html_sha256": sha256((html or "").encode()).hexdigest(),
    }


def _parse_apply_by(value: str | None) -> str | None:
    value = clean(value)
    if not value or value.casefold() in {"no close date", "none", "n/a"}:
        return None
    match = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(\d{4})", value)
    if not match:
        return None
    month_name, day, year = match.groups()
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
        "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    month = months.get(month_name.casefold())
    if not month:
        return None
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def _location_from_json_ld(address: dict) -> dict:
    def value(key):
        raw = clean(address.get(key))
        return None if not raw or raw.casefold() == "none" else raw

    city = value("addressLocality")
    state = value("addressRegion")
    country = value("addressCountry")
    postal = value("postalCode")
    display = ", ".join(part for part in (city, state, country) if part)
    return {
        "city": city,
        "state": state,
        "country": country,
        "postal_code": postal,
        "display": display or None,
        "source": "JSON-LD JobPosting.jobLocation.address",
    }


def parse_detail_page(html: str, response_url: str, listing_row: dict) -> dict:
    parts = urlsplit(response_url)
    identity = _job_id(response_url)
    if parts.scheme != "https" or parts.netloc.lower() != LISTING_HOST or not identity:
        raise ValueError(f"Whatfix detail page came from an unexpected route: {response_url}")
    if listing_row.get("id") != identity:
        raise ValueError("Whatfix detail identity differs from its listing card")

    soup = BeautifulSoup(html or "", "lxml")
    posting = _json_ld_job_posting(soup)
    if not posting:
        raise ValueError("Whatfix detail page has no JobPosting JSON-LD")
    identifier = posting.get("identifier")
    identifier_value = identifier.get("value") if isinstance(identifier, dict) else identifier
    if clean(identifier_value) != identity:
        raise ValueError(f"Whatfix JSON-LD identifier changed: {identifier_value!r} != {identity!r}")
    organization = posting.get("hiringOrganization")
    employer = clean(organization.get("name")) if isinstance(organization, dict) else None
    if employer != EMPLOYER_NAME:
        raise ValueError(f"Whatfix JSON-LD hiringOrganization changed: {employer!r}")

    body_node = soup.select_one(".jobdesciption")
    if body_node is None:
        raise ValueError("Whatfix detail page is missing the full .jobdesciption block")
    description_html = body_node.decode_contents()
    description_text = normalized_body_text(text_html(description_html))
    if len(description_text) < 300:
        raise ValueError(f"Whatfix full description is too short ({len(description_text)} chars)")
    schema_description = posting.get("description")
    if not isinstance(schema_description, str) or not schema_description.strip():
        raise ValueError("Whatfix JobPosting JSON-LD has no description")
    schema_text = normalized_body_text(text_html(unescape(schema_description)))
    description_matches_schema = description_text == schema_text

    detail_title = None
    title_node = soup.select_one(".js-job-title")
    if title_node:
        detail_title = clean(title_node.get_text(" ", strip=True))
    title_matches_listing = bool(detail_title) and detail_title == listing_row.get("title")
    if detail_title and not title_matches_listing:
        # Keep both titles and let the caller record the conflict; do not replace the listing title.
        pass

    info_node = soup.select_one(".opening-info")
    info_text = clean(info_node.get_text(" ", strip=True) if info_node else "")
    info_parts = [clean(part) for part in info_text.split("|") if clean(part)]
    info_location = info_parts[0] if info_parts else None
    info_team = info_parts[1] if len(info_parts) >= 3 else None
    info_type = info_parts[-1] if info_parts else None

    apply_by_raw = None
    for span in soup.select(".job-header span.rb-text-3"):
        text = clean(span.get_text(" ", strip=True))
        if text.casefold().startswith("apply by:"):
            apply_by_raw = clean(text.split(":", 1)[1])
            break
    schema_valid_through = posting.get("validThrough")
    valid_through = None
    if isinstance(schema_valid_through, str) and ISO_DATE.fullmatch(schema_valid_through):
        valid_through = schema_valid_through
    if not valid_through:
        valid_through = _parse_apply_by(apply_by_raw)

    apply_node = soup.select_one("a.js-apply-for-job[href]")
    apply_url = urljoin(response_url, apply_node["href"]) if apply_node else None
    date_posted = posting.get("datePosted")
    if isinstance(date_posted, str) and not ISO_DATE.fullmatch(date_posted):
        date_posted_raw = date_posted
        date_posted = None
    else:
        date_posted_raw = date_posted

    address = ((posting.get("jobLocation") or {}).get("address") or {}) if isinstance(posting.get("jobLocation"), dict) else {}
    location = _location_from_json_ld(address) if isinstance(address, dict) else {}
    if not location.get("display"):
        location = {
            "city": None, "state": None, "country": None, "postal_code": None,
            "display": listing_row.get("location", {}).get("display"),
            "source": "listing card (JSON-LD address was empty)",
        }
    language_node = soup.find("html")
    language = clean(language_node.get("lang")) if language_node and language_node.get("lang") else None

    return {
        "id": identity,
        "title": clean(posting.get("title")) or detail_title or listing_row.get("title"),
        "detail_title": detail_title,
        "title_matches_listing": title_matches_listing,
        "info_text": info_text,
        "info_location": info_location,
        "info_team": info_team,
        "info_type": info_type,
        "description_html": description_html,
        "description_text": description_text,
        "description_matches_schema": description_matches_schema,
        "schema_description_chars": len(schema_text),
        "date_posted": date_posted,
        "date_posted_raw": date_posted_raw,
        "valid_through": valid_through,
        "apply_by_raw": apply_by_raw,
        "employment_type": info_type or (clean(posting.get("employmentType")).replace("_", " ").title() if posting.get("employmentType") else None),
        "department": listing_row.get("department") or info_team,
        "employer": employer,
        "organization": organization,
        "location": location,
        "apply_url": apply_url,
        "language": language,
        "posting": {key: value for key, value in posting.items() if key != "description"},
        "html_sha256": sha256((html or "").encode()).hexdigest(),
    }


def parse_jsapi_openings(value: dict, listing_rows: list[dict], response_url: str = JSAPI_URL) -> dict:
    """Reconcile the unfiltered JSON used by the official careers-page search form."""
    if not isinstance(value, dict):
        raise ValueError("Whatfix official-page openings API response is not an object")
    meta = value.get("meta")
    objects = value.get("objects")
    if not isinstance(meta, dict) or not isinstance(objects, list):
        raise ValueError("Whatfix official-page openings API has no meta/objects")
    total = meta.get("total")
    offset = meta.get("offset")
    limit = meta.get("limit")
    if not all(isinstance(item, int) for item in (total, offset, limit)):
        raise ValueError("Whatfix official-page openings API meta is incomplete")
    row_by_id = {row["id"]: row for row in listing_rows}
    ids, issues = [], []
    title_mismatches, url_mismatches, location_mismatches = [], [], []
    team_mismatches, close_date_mismatches = [], []
    description_chars = []
    objects_by_id = {}
    for obj in objects:
        if not isinstance(obj, dict):
            issues.append("object_not_dict")
            continue
        identity = clean(obj.get("id"))
        if not identity or identity in ids:
            issues.append(f"missing_or_duplicate_id:{identity!r}")
            continue
        ids.append(identity)
        row = row_by_id.get(identity)
        if row is None:
            issues.append(f"unlisted_id:{identity}")
            continue
        if clean(obj.get("client_name")) != JSAPI_CLIENT:
            issues.append(f"wrong_client:{identity}:{obj.get('client_name')!r}")
        title = clean(obj.get("title"))
        if title != row["title"]:
            title_mismatches.append({"id": identity, "api": title, "listing": row["title"]})
        hosted_url = clean(obj.get("hosted_url"))
        if hosted_url.rstrip("/") != row["url"].rstrip("/"):
            url_mismatches.append({"id": identity, "api": hosted_url, "listing": row["url"]})
        location = obj.get("location") if isinstance(obj.get("location"), dict) else {}
        api_location = ", ".join(
            part for part in (
                clean(location.get("city")),
                clean(location.get("state")),
                clean(location.get("country")),
            ) if part
        )
        listing_location = (row.get("location") or {}).get("display")
        if api_location != (listing_location or ""):
            location_mismatches.append({"id": identity, "api": api_location, "listing": listing_location})
        api_team = clean(obj.get("team")) or None
        listing_team = row.get("department")
        if api_team != listing_team:
            team_mismatches.append({"id": identity, "api": api_team, "listing": listing_team})
        close_date = obj.get("close_date")
        api_close = None
        if isinstance(close_date, int):
            api_close = datetime.fromtimestamp(close_date, timezone.utc).date().isoformat()
        listing_close = row.get("apply_by_iso")
        if api_close != listing_close:
            close_date_mismatches.append({"id": identity, "api": api_close, "listing": listing_close})
        description = obj.get("description")
        description_text = ""
        if isinstance(description, str):
            description_text = normalized_body_text(text_html(description))
            description_chars.append(len(description_text))
        else:
            issues.append(f"missing_description:{identity}")
        objects_by_id[identity] = {
            "id": identity,
            "title": title,
            "location": location,
            "hosted_url": hosted_url,
            "tags": obj.get("tags"),
            "allows_remote": obj.get("allows_remote"),
            "position_type": obj.get("position_type"),
            "team": api_team,
            "close_date": close_date,
            "close_date_iso": api_close,
            "description_chars": len(description_text),
            "description_sha256": sha256(description_text.encode()).hexdigest() if description_text else None,
        }
    missing = sorted(set(row_by_id) - set(ids))
    extra = sorted(set(ids) - set(row_by_id))
    return {
        "url": response_url,
        "meta": {"offset": offset, "limit": limit, "total": total},
        "object_count": len(objects),
        "unique_ids": len(set(ids)),
        "ids": ids,
        "missing_listing_ids": missing,
        "extra_api_ids": extra,
        "ids_match_listing": not missing and not extra and len(ids) == len(objects),
        "count_matches_listing": total == len(listing_rows) == len(objects),
        "title_mismatches": title_mismatches,
        "hosted_url_mismatches": url_mismatches,
        "location_mismatches": location_mismatches,
        "team_mismatches": team_mismatches,
        "close_date_mismatches": close_date_mismatches,
        "description_chars_min": min(description_chars) if description_chars else None,
        "description_chars_max": max(description_chars) if description_chars else None,
        "objects_by_id": objects_by_id,
        "issues": issues,
        "html_sha256": sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
    }


def parse_rss_feed(xml_text: str, listing_rows: list[dict], response_url: str = RSS_URL) -> dict:
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("Whatfix RSS feed has no channel")
    items = channel.findall("item")
    row_by_id = {row["id"]: row for row in listing_rows}
    item_ids, title_mismatches, pub_dates = [], [], {}
    for item in items:
        link = clean(item.findtext("link") or "")
        identity = _job_id(link)
        if not identity:
            continue
        item_ids.append(identity)
        title = clean(item.findtext("title") or "")
        listing_title = row_by_id.get(identity, {}).get("title")
        if listing_title and title != listing_title:
            title_mismatches.append({"id": identity, "rss": title, "listing": listing_title})
        pub_date = clean(item.findtext("pubDate") or "")
        if pub_date:
            pub_dates[identity] = pub_date
    missing = sorted(set(row_by_id) - set(item_ids))
    extra = sorted(set(item_ids) - set(row_by_id))
    return {
        "url": response_url,
        "channel_title": clean(channel.findtext("title") or ""),
        "last_build_date": clean(channel.findtext("lastBuildDate") or ""),
        "item_count": len(items),
        "unique_item_ids": len(set(item_ids)),
        "missing_listing_ids": missing,
        "extra_rss_ids": extra,
        "title_mismatches": title_mismatches,
        "pub_dates": pub_dates,
        "ids_match_listing": not missing and not extra and len(item_ids) == len(items),
        "html_sha256": sha256((xml_text or "").encode()).hexdigest(),
    }


class Whatfix(Adapter):
    """Collect only the exact jobs published on Whatfix's linked Trakstar board."""

    VERIFICATION_REVISION = "2026-11-08.1"

    async def _read_listing(self, first_url: str) -> dict:
        started = time.perf_counter()
        response = await self.http.get(first_url, headers=HEADERS)
        first_final = _listing_page_url(str(response.url))
        first = parse_listing_page(response.text, first_final, 1)
        pages = [first]
        rows = list(first["rows"])
        total = first["total"]
        seen_urls = {first_final}
        while first["next_page_url"]:
            next_url = first["next_page_url"]
            if next_url in seen_urls:
                raise ValueError(f"Whatfix Trakstar pagination loop at {next_url}")
            seen_urls.add(next_url)
            page_number = len(pages) + 1
            response = await self.http.get(next_url, headers=HEADERS)
            final_url = _listing_page_url(str(response.url))
            current = parse_listing_page(response.text, final_url, page_number)
            if current["total"] != total:
                raise ValueError("Whatfix Trakstar total changed between listing pages")
            pages.append(current)
            rows.extend(current["rows"])
            first = current
        return {
            "pages": pages,
            "rows": rows,
            "total": total,
            "start_index": pages[0]["start_index"],
            "end_index": pages[-1]["end_index"],
            "next_page_url": pages[0]["next_page_url"],
            "prev_page_url": pages[-1]["prev_page_url"],
            "elapsed_seconds": max(0.0, time.perf_counter() - started),
        }

    async def run(self):
        if self.company.slug != "whatfix":
            raise ValueError("Whatfix adapter requires the Whatfix company")
        if self.source.url != LISTING_URL:
            raise ValueError(f"Whatfix adapter only starts at {LISTING_URL}")
        if self.source.evidence_url not in {None, OFFICIAL_ENTRY}:
            raise ValueError("Whatfix source evidence must point to the official careers entry")

        collection_started = time.perf_counter()
        entry_response = await self.http.get(OFFICIAL_ENTRY, headers=HEADERS)
        entry = parse_official_entry(entry_response.text, str(entry_response.url))
        entry_sha = sha256(entry_response.content).hexdigest()
        self.result.metadata["official_journey"] = {
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_final_url": str(entry_response.url),
            "official_entry_status": entry_response.status_code,
            "official_entry_sha256": entry_sha,
            "official_entry_title": entry["title"],
            "see_all_openings_href": entry["see_all_openings"]["href"],
            "see_all_openings_url": entry["see_all_openings"]["url"],
            "published_listing_redirect_expected": f"{PUBLISHED_LISTING_LINK} -> {LISTING_URL}",
            "listing_url": LISTING_URL,
            "listing_host": LISTING_HOST,
            "membership_basis": (
                "The exact See All Openings button on whatfix.com/careers/ redirects to the "
                "company's Trakstar Hire board; all 33 unfiltered cards across its two sorted pages are collected."
            ),
        }

        listing_started = time.perf_counter()
        published = await self.http.get(entry["see_all_openings"]["url"], headers=HEADERS)
        published_final = _listing_page_url(str(published.url))
        if published_final != LISTING_URL and not published_final.startswith(LISTING_URL + "?"):
            raise ValueError(f"Whatfix published listing redirected to an unexpected URL: {published_final}")
        initial = await self._read_listing(LISTING_URL)
        listing_seconds = max(0.0, time.perf_counter() - listing_started)
        rows = initial["rows"]
        total = initial["total"]
        self.result.expected_total = total
        self.result.pages = len(initial["pages"])
        if len(rows) != total:
            self.result.errors.append(f"whatfix_listing_membership_count_mismatch:{len(rows)}!={total}")
        ids = [row["id"] for row in rows]
        if len(ids) != len(set(ids)):
            self.result.errors.append("whatfix_listing_duplicate_ids")
        if initial["start_index"] != 1 or initial["end_index"] != total:
            self.result.errors.append(
                f"whatfix_listing_pagination_bounds_mismatch:{initial['start_index']}-{initial['end_index']}of{total}"
            )
        page_issues = [issue for page in initial["pages"] for issue in page["issues"]]
        if page_issues:
            self.result.errors.extend(f"whatfix_listing_page_issue:{issue}" for issue in page_issues)

        filters = initial["pages"][0]["filters"]
        no_department = sum(1 for row in rows if not row["department"])
        location_counts = dict(filters.get("countries") or {})
        location_counts.update(filters.get("other_locations") or {})
        if location_counts and sum(location_counts.values()) != total:
            self.result.errors.append("whatfix_location_filter_counts_do_not_reconcile")
        if filters.get("teams") and sum(filters["teams"].values()) + no_department != total:
            self.result.errors.append("whatfix_team_filter_counts_do_not_reconcile")

        jsapi_started = time.perf_counter()
        jsapi = None
        jsapi_error = None
        try:
            jsapi_response = await self.http.get(
                JSAPI_URL, headers={**HEADERS, "Accept": "application/json,text/plain,*/*"}
            )
            jsapi = parse_jsapi_openings(jsapi_response.json(), rows, str(jsapi_response.url))
            if not jsapi["ids_match_listing"] or not jsapi["count_matches_listing"]:
                self.result.warnings.append("whatfix_official_page_api_membership_differs_from_listing")
            if jsapi["issues"]:
                self.result.warnings.append("whatfix_official_page_api_has_issues")
            mismatch_keys = (
                "title_mismatches", "hosted_url_mismatches", "location_mismatches",
                "team_mismatches", "close_date_mismatches",
            )
            if any(jsapi[key] for key in mismatch_keys):
                self.result.warnings.append("whatfix_official_page_api_field_mismatch")
        except Exception as exc:  # The board remains authoritative; the official-page API is a cross-check.
            jsapi_error = f"{type(exc).__name__}: {exc}"
            self.result.warnings.append("whatfix_official_page_api_unavailable")
        jsapi_seconds = max(0.0, time.perf_counter() - jsapi_started)

        rss = None
        rss_error = None
        try:
            rss_response = await self.http.get(RSS_URL, headers={**HEADERS, "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8"})
            rss = parse_rss_feed(rss_response.text, rows, str(rss_response.url))
            if rss["item_count"] != total:
                self.result.warnings.append(f"whatfix_rss_item_count_differs:{rss['item_count']}!={total}")
            if not rss["ids_match_listing"]:
                self.result.warnings.append("whatfix_rss_membership_differs_from_listing")
            if rss["title_mismatches"]:
                self.result.warnings.append("whatfix_rss_title_mismatches")
        except Exception as exc:  # RSS is secondary evidence, never a replacement for the listing.
            rss_error = f"{type(exc).__name__}: {exc}"
            self.result.warnings.append("whatfix_rss_feed_unavailable")

        self.result.metadata["listing"] = {
            "url": LISTING_URL,
            "published_link": entry["see_all_openings"]["url"],
            "published_final_url": published_final,
            "pages": len(initial["pages"]),
            "page_size": PAGE_SIZE,
            "page_bounds": [[page["start_index"], page["end_index"]] for page in initial["pages"]],
            "total": total,
            "sort_by": initial["pages"][0]["sort_by"],
            "filters": filters,
            "filter_counts_reconcile": {
                "countries_plus_other_locations": sum(location_counts.values()) if location_counts else None,
                "total": total,
                "teams_sum": sum((filters.get("teams") or {}).values()) if filters.get("teams") else None,
                "rows_without_department": no_department,
            },
            "rss": rss,
            "rss_error": rss_error,
            "official_page_api": {
                "url": JSAPI_URL,
                "crosscheck": jsapi,
                "error": jsapi_error,
                "seconds": round(jsapi_seconds, 6),
            },
        }

        api_objects = (jsapi or {}).get("objects_by_id", {})
        for row in rows:
            api_row = api_objects.get(row["id"])
            job = self.job(
                {"listing": row, "official_page_api": api_row},
                title=row["title"],
                url=row["url"],
                job_id=row["id"],
                locations=[row["location"]] if row.get("location", {}).get("display") else [],
                department=row.get("department"),
                employment_type=row.get("employment_type"),
                workplace_type="Remote" if api_row and api_row.get("allows_remote") is True else None,
                apply_url=f"{row['url']}?apply=true",
            )
            self.add(job)
        self.result.listing_complete = len(self.result.jobs) == total and not self.result.errors
        if not self.result.jobs:
            self.result.errors.append("whatfix_listing_has_no_exact_job_rows")
            return self.result

        detail_started = time.perf_counter()

        async def detail(job):
            response = await self.http.get(job.url, headers=HEADERS)
            parsed = parse_detail_page(response.text, str(response.url), job.raw["listing"])
            if not parsed["title_matches_listing"]:
                job.warnings.append("detail_title_differs_from_listing")
            if not parsed["description_matches_schema"]:
                job.warnings.append("detail_description_differs_from_json_ld")
            job.requisition_id = None
            job.apply_url = parsed["apply_url"] or job.apply_url
            job.description_html = parsed["description_html"]
            job.description_text = parsed["description_text"]
            job.description_method = "whatfix.trakstar.jobdesciption"
            job.description_source_url = str(response.url)
            job.locations = [parsed["location"]] if parsed["location"].get("display") else job.locations
            job.department = parsed["department"]
            job.employment_type = parsed["employment_type"]
            job.date_posted = parsed["date_posted"]
            job.date_posted_raw = parsed["date_posted_raw"]
            job.valid_through = parsed["valid_through"]
            job.employer = parsed["employer"]
            job.language = parsed["language"]
            job.raw["detail"] = {
                "url": str(response.url),
                "html_sha256": parsed["html_sha256"],
                "title": parsed["title"],
                "detail_title": parsed["detail_title"],
                "title_matches_listing": parsed["title_matches_listing"],
                "opening_info": parsed["info_text"],
                "apply_by_raw": parsed["apply_by_raw"],
                "valid_through": parsed["valid_through"],
                "apply_url": parsed["apply_url"],
                "description_container": ".jobdesciption",
                "description_matches_json_ld": parsed["description_matches_schema"],
                "json_ld_description_chars": parsed["schema_description_chars"],
                "job_posting_json_ld_without_description": parsed["posting"],
                "organization": parsed["organization"],
                "location": parsed["location"],
            }
            job.validate()
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        detail_seconds = max(0.0, time.perf_counter() - detail_started)

        final_started = time.perf_counter()
        try:
            final = await self._read_listing(LISTING_URL)
        except Exception as exc:
            self.result.errors.append(f"whatfix_final_membership_recheck_failed:{type(exc).__name__}:{exc}")
            final = None
        if final is not None:
            final_rows = final["rows"]
            final_ids = [row["id"] for row in final_rows]
            same_order = ids == final_ids
            same_fields = all(
                (old["id"], old["title"], old["url"], old.get("department"), old.get("location", {}).get("display"))
                == (new["id"], new["title"], new["url"], new.get("department"), new.get("location", {}).get("display"))
                for old, new in zip(rows, final_rows)
            ) and len(rows) == len(final_rows)
            complete = final["total"] == total and same_order and same_fields
            self.result.metadata["final_membership_recheck"] = {
                "status": "complete" if complete else "mismatch",
                "url": LISTING_URL,
                "total": final["total"],
                "pages": len(final["pages"]),
                "row_count": len(final_rows),
                "initial_ids": ids,
                "final_ids": final_ids,
                "exact_order_and_fields_match": same_order and same_fields,
            }
            if not complete:
                self.result.listing_complete = False
                self.result.errors.append("whatfix_final_listing_membership_recheck_mismatch")
        final_seconds = max(0.0, time.perf_counter() - final_started)

        self.result.metadata["membership_reconciliation"] = {
            "initial_ids": ids,
            "final_ids": ([row["id"] for row in final["rows"]] if final else None),
            "initial_total": total,
            "jobs_added": len(self.result.jobs),
            "pages": len(initial["pages"]),
            "page_bounds": [[page["start_index"], page["end_index"]] for page in initial["pages"]],
            "filter_counts": filters,
            "filter_counts_reconcile": self.result.metadata["listing"]["filter_counts_reconcile"],
            "rss_item_count": rss["item_count"] if rss else None,
            "rss_ids_match_listing": rss["ids_match_listing"] if rss else None,
            "official_page_api_ids_match_listing": jsapi["ids_match_listing"] if jsapi else None,
            "official_page_api_count_matches_listing": jsapi["count_matches_listing"] if jsapi else None,
        }
        self.result.timings["whatfix_initial_listing_seconds"] = round(listing_seconds, 6)
        self.result.timings["whatfix_detail_wall_seconds"] = round(detail_seconds, 6)
        self.result.timings["whatfix_official_page_api_seconds"] = round(jsapi_seconds, 6)
        self.result.timings["whatfix_final_recheck_seconds"] = round(final_seconds, 6)
        self.result.timings["whatfix_collection_wall_seconds"] = round(
            max(0.0, time.perf_counter() - collection_started - detail_seconds), 6
        )
        return self.result
