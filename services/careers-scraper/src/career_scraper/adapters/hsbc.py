"""HSBC's official careers journey (reviewed 2026-11-07).

The ledger URL ``https://www.hsbc.com/careers/find-a-job`` is HSBC's own
careers page.  It publishes a **Search jobs** call to action and a country
picker whose 48 portal entries point at HSBC's Eightfold candidate portal
``https://portal.careers.hsbc.com/careers`` for ``domain=hsbc.com``.  That
portal's rendered listing says "1284 open jobs." and loads 10 cards per
"Show more opportunities" click; its own ``/api/apply`` endpoint returns the
same count and rows, and each posting has a company-published detail endpoint
with the full description and posting dates.

Only that company-scoped portal listing is enumerated.  The per-posting
application handoff (``career2.successfactors.eu``, company ``hsbcholdin``) is
recorded as ``apply_url`` but never used as a board.  The legacy SuccessFactors
site the country picker still links for Maldives now publishes zero jobs and
points visitors at the new portal.

The portal sitemap is a stale index (1330 URLs while 1284 postings are live);
it is kept as a reconciliation note, not as the membership authority.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now

OFFICIAL_ENTRY = "https://www.hsbc.com/careers/find-a-job"
PORTAL_ORIGIN = "https://portal.careers.hsbc.com"
LISTING_URL = "https://portal.careers.hsbc.com/careers?location=any"
LISTING_API = PORTAL_ORIGIN + "/api/apply/v2/jobs"
DETAIL_API = PORTAL_ORIGIN + "/api/apply/v2/jobs/{job_id}"
SITEMAP_URL = PORTAL_ORIGIN + "/careers/sitemap.xml?domain=hsbc.com"
LEGACY_SITE = "https://mycareer.hsbc.com/en_GB/external/SearchJobs/"
LEGACY_HOST = "mycareer.hsbc.com"

DOMAIN = "hsbc.com"
LOCATION_FILTER = "any"
SORT_BY = "relevance"
PAGE_SIZE = 10
MAX_LISTING_PAGES = 500
ENTRY_HOST = "www.hsbc.com"
PORTAL_HOST = "portal.careers.hsbc.com"
ENTRY_PATH = "/careers/find-a-job"
APPLY_HOST = "career2.successfactors.eu"
APPLY_COMPANY = "hsbcholdin"


def normalized_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()


def parse_source_date(value) -> str | None:
    """Convert an HSBC published date such as ``29 October 2026`` to ISO.

    Returns ``None`` for an absent value.  An unparseable value raises so the
    caller records it instead of silently inventing a date.
    """
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    text = normalized_text(value)
    if not text:
        return None
    for pattern in ("%d %B %Y", "%d %b %Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised HSBC source date: {text!r}")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _host_is(url: str, expected: str) -> bool:
    host = _host(url)
    return host == expected or host.endswith("." + expected)


def is_bot_challenge(exc: BaseException) -> bool:
    """True when the portal answered a posting request with a WAF/throttle status.

    HSBC's Eightfold portal fronts its API with AWS WAF.  A 405 or 429 there is
    a human-verification challenge, not a missing posting, so the adapter stops
    instead of retrying or solving it.
    """
    message = str(exc)
    return "HTTP 405" in message or "HTTP 429" in message


def _json_arrays(script_text: str, marker: str):
    """Yield every JSON array passed to a JS constructor such as BankingRoute."""
    decoder = json.JSONDecoder()
    position = 0
    while True:
        index = script_text.find(marker, position)
        if index < 0:
            return
        start = script_text.find("[", index)
        if start < 0:
            return
        try:
            value, _ = decoder.raw_decode(script_text[start:])
        except ValueError:
            position = index + len(marker)
            continue
        if isinstance(value, list):
            yield value
        position = start + 1


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    """Validate HSBC's careers page and extract its published listing journey."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or _host(response_url) not in {ENTRY_HOST, "hsbc.com"}
            or parts.path.rstrip("/") != ENTRY_PATH):
        raise ValueError(f"HSBC careers entry redirected to an unexpected URL: {response_url}")

    soup = BeautifulSoup(html, "lxml")
    title = normalized_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "find a job" not in title.lower():
        raise ValueError(f"HSBC careers page title changed: {title!r}")

    portal_links = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        parsed = urlsplit(href)
        if _host(href) == PORTAL_HOST and parsed.path.rstrip("/") == "/careers":
            portal_links.append({
                "label": normalized_text(anchor.get_text(" ", strip=True)),
                "url": href,
                "location": (parse_qs(parsed.query).get("location") or [None])[0],
            })
    search_cta = next((link for link in portal_links if link["location"] == LOCATION_FILTER), None)
    if search_cta is None:
        raise ValueError("HSBC careers page no longer links its Search jobs CTA to the portal listing")

    country_targets: dict[str, str] = {}
    legacy_targets: list[str] = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        for group in _json_arrays(text, "new BankingRoute("):
            for item in group:
                if not isinstance(item, dict) or not isinstance(item.get("countries"), list):
                    continue
                for country in item["countries"]:
                    action = str((country or {}).get("action") or "").strip()
                    if not action.startswith(("http://", "https://")):
                        continue
                    host = _host(action)
                    if host == PORTAL_HOST:
                        country_targets[normalized_text(country.get("title"))] = action
                    elif host == "mycareer.hsbc.com":
                        legacy_targets.append(action)

    if len(country_targets) < 40:
        raise ValueError(
            "HSBC careers country picker no longer exposes the expected portal entries"
            f" (found {len(country_targets)})")
    if not legacy_targets:
        raise ValueError("HSBC careers country picker no longer exposes its legacy Maldives site link")

    return {
        "requested_url": OFFICIAL_ENTRY,
        "url": response_url,
        "status": 200,
        "title": title,
        "chars": len(html),
        "search_jobs_cta": search_cta["url"],
        "portal_links": portal_links,
        "portal_country_targets": country_targets,
        "portal_country_count": len(country_targets),
        "legacy_targets": list(dict.fromkeys(legacy_targets)),
        "listing_url": LISTING_URL,
    }


def parse_listing_page(data: dict, *, expected_total: int | None = None) -> dict:
    """Parse one Eightfold listing page and validate its company scope."""
    if not isinstance(data, dict):
        raise ValueError("HSBC listing response is not a JSON object")
    if data.get("domain") not in (None, DOMAIN):
        raise ValueError(f"HSBC listing response domain changed: {data.get('domain')!r}")
    rows = data.get("positions")
    count = data.get("count")
    if not isinstance(rows, list) or not isinstance(count, int):
        raise ValueError("HSBC listing response is missing positions/count")
    if expected_total is not None and count != expected_total:
        raise ValueError(f"HSBC listing count changed from {expected_total} to {count}")
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or not row.get("name"):
            raise ValueError("HSBC listing row is missing id/name")
        url = row.get("canonicalPositionUrl") or ""
        if _host(url) != PORTAL_HOST or "/careers/job/" not in url:
            raise ValueError(f"HSBC listing row has an unexpected detail URL: {url!r}")
    return {
        "count": count,
        "rows": rows,
        "row_count": len(rows),
        "query": data.get("query"),
        "facets": data.get("facets") or {},
        "job_card_config": data.get("jobCardConfig") or {},
    }


def _first(values):
    if isinstance(values, list):
        return values[0] if values else None
    return values


def _clean_list(values) -> list[str]:
    if not isinstance(values, list):
        values = [values] if values else []
    return [normalized_text(value) for value in values if normalized_text(value)]


def parse_detail(data: dict, listing_row: dict | None = None) -> dict:
    """Parse one HSBC posting detail and cross-check it against its listing row."""
    if not isinstance(data, dict):
        raise ValueError("HSBC detail response is not a JSON object")
    listing_id = str((listing_row or {}).get("id") or "")
    detail_id = str(data.get("id") or "")
    if not detail_id or (listing_id and detail_id != listing_id):
        raise ValueError(f"HSBC detail returned id {detail_id!r} for listing id {listing_id!r}")

    title = normalized_text(data.get("name") or data.get("posting_name"))
    if not title:
        raise ValueError("HSBC detail is missing its title")
    description = data.get("job_description") or ""
    if not isinstance(description, str) or not description.strip():
        raise ValueError("HSBC detail is missing its full job_description")

    fields = ((data.get("custom_JD") or {}).get("data_fields") or {})
    posted_raw = _first(fields.get("postingStartDate"))
    end_raw = _first(fields.get("postingEndDate"))
    salary_min = _first(fields.get("minSalFormatted"))
    salary_max = _first(fields.get("maxSalFormatted"))
    salary = None
    if salary_min or salary_max:
        salary = {"min": salary_min, "max": salary_max,
                  "min_raw": _first(fields.get("minSalUSA")),
                  "max_raw": _first(fields.get("maxSalUSA"))}

    apply_url = data.get("apply_redirect_url") or None
    if apply_url:
        parsed_apply = urlsplit(apply_url)
        query = parse_qs(parsed_apply.query)
        if _host(apply_url) != APPLY_HOST:
            raise ValueError(f"HSBC apply route changed host: {apply_url!r}")
        if (query.get("company") or [None])[0] != APPLY_COMPANY:
            raise ValueError(f"HSBC apply route changed employer: {apply_url!r}")

    display_job_id = data.get("display_job_id") or _first(fields.get("jobid"))

    return {
        "id": detail_id,
        "title": title,
        "display_job_id": str(display_job_id) if display_job_id is not None else None,
        "description_html": description,
        "locations": _clean_list(data.get("locations") or data.get("location")),
        "department": normalized_text(data.get("department"))
        or _first(_clean_list(fields.get("department"))) or None,
        "business_unit": normalized_text(data.get("business_unit")) or None,
        "brand": _first(_clean_list(fields.get("brand"))) or None,
        "job_type": _first(_clean_list(fields.get("jobType"))) or None,
        "work_style": _first(_clean_list(fields.get("workStyle"))) or None,
        "work_location_option": data.get("work_location_option") or None,
        "date_posted_raw": posted_raw,
        "date_posted": parse_source_date(posted_raw),
        "valid_through_raw": end_raw,
        "valid_through": parse_source_date(end_raw),
        "salary": salary,
        "apply_url": apply_url,
        "canonical_url": data.get("canonicalPositionUrl") or LISTING_URL,
        "t_create": data.get("t_create"),
        "t_update": data.get("t_update"),
        "custom_fields": fields,
    }


def _legacy_fields(container) -> dict:
    fields = {}
    for field in container.select(".article__content__view__field"):
        label = normalized_text(field.select_one(".article__content__view__field__label").get_text(" ", strip=True)
                               if field.select_one(".article__content__view__field__label") else "")
        value_node = field.select_one(".article__content__view__field__value")
        if label and value_node is not None:
            fields[label] = value_node
    return fields


def _legacy_text(fields: dict, label: str) -> str | None:
    node = fields.get(label)
    if node is None:
        return None
    value = normalized_text(node.get_text(" ", strip=True))
    return value or None


def parse_legacy_listing(html: str, response_url: str = LEGACY_SITE) -> dict:
    """Parse the legacy SuccessFactors career site's own unfiltered job list."""
    if _host(response_url) != LEGACY_HOST:
        raise ValueError(f"HSBC legacy careers response is not on {LEGACY_HOST}: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for article in soup.select("article.article--result"):
        title_anchor = article.select_one(".article__header__text__title a") or article.select_one("h3 a")
        if title_anchor is None or not title_anchor.get("href"):
            continue
        href = title_anchor["href"].strip()
        match = re.search(r"/(\d+)(?:[?#]|$)", href)
        if not match:
            raise ValueError(f"HSBC legacy listing row has no numeric id: {href!r}")
        subtitle = article.select_one(".article__header__text__subtitle")
        location = (normalized_text(subtitle.select_one(".item--location").get_text(" ", strip=True))
                    if subtitle and subtitle.select_one(".item--location") else None)
        items = [normalized_text(span.get_text(" ", strip=True))
                 for span in subtitle.select("span.article--item")] if subtitle else []
        if location:
            items = [item for item in items if item != location]
        dates = []
        for item in items:
            found = re.findall(r"\d{1,2}-[A-Za-z]{3}-\d{4}", item)
            if found:
                dates = found
                items.remove(item)
                break
        details = [item for item in items if item]
        rows.append({
            "origin": "mycareer",
            "id": match.group(1),
            "title": normalized_text(title_anchor.get_text(" ", strip=True)),
            "url": href,
            "location": location,
            "area_of_interest": details[0] if len(details) > 0 else None,
            "job_type": details[1] if len(details) > 1 else None,
            "work_style": details[2] if len(details) > 2 else None,
            "opening_date": dates[0] if dates else None,
            "closing_date": dates[1] if len(dates) > 1 else None,
        })
    for row in rows:
        if _host(row["url"]) != LEGACY_HOST:
            raise ValueError(f"HSBC legacy listing row points off-site: {row['url']!r}")
    return {"rows": rows, "count": len(rows), "url": response_url}


def parse_legacy_detail(html: str, response_url: str, listing_row: dict) -> dict:
    """Parse one legacy SuccessFactors posting detail and its full advert body."""
    if _host(response_url) != LEGACY_HOST:
        raise ValueError(f"HSBC legacy detail response is not on {LEGACY_HOST}: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    fields = _legacy_fields(soup)
    job_id = _legacy_text(fields, "Job ID")
    expected = str((listing_row or {}).get("id") or "")
    if not job_id or (expected and job_id != expected):
        raise ValueError(f"HSBC legacy detail returned Job ID {job_id!r} for listing id {expected!r}")
    title_node = soup.select_one(".banner__text__title")
    title = normalized_text(title_node.get_text(" ", strip=True) if title_node else "")
    if not title and soup.title:
        title = re.sub(r"\s+-\s+\d+\s*$", "", normalized_text(soup.title.get_text(" ", strip=True)))
    if not title:
        raise ValueError("HSBC legacy detail is missing its title")
    advert = next(((label, node) for label, node in fields.items()
                   if "job advert" in label.lower()), None)
    if advert is None:
        raise ValueError("HSBC legacy detail is missing its Job Advert Details section")
    description = advert[1].decode_contents().strip()
    if len(normalized_text(advert[1].get_text(" ", strip=True))) < 100:
        raise ValueError("HSBC legacy detail has a short or empty advert body")
    apply_anchor = soup.select_one('a[href*="ExternalApply"]')
    return {
        "id": job_id,
        "title": title,
        "description_html": description,
        "location": _legacy_text(fields, "Location"),
        "area_of_interest": _legacy_text(fields, "Area of interest"),
        "job_type": _legacy_text(fields, "Job type"),
        "work_style": _legacy_text(fields, "Work style"),
        "opening_date_raw": _legacy_text(fields, "Opening date"),
        "closing_date_raw": _legacy_text(fields, "Closing date"),
        "date_posted": parse_source_date(_legacy_text(fields, "Opening date")),
        "valid_through": parse_source_date(_legacy_text(fields, "Closing date")),
        "apply_url": apply_anchor["href"].strip() if apply_anchor else None,
    }


def apply_legacy_detail(job, info: dict, *, source_url: str) -> None:
    """Copy a parsed legacy SuccessFactors posting onto a Job."""
    job.title = info["title"]
    job.description_html = info["description_html"]
    job.description_method = "hsbc.successfactors.job_advert"
    job.description_source_url = source_url
    job.apply_url = info["apply_url"]
    job.locations = [info["location"]] if info["location"] else job.locations
    job.department = info["area_of_interest"] or job.department
    job.employment_type = info["job_type"]
    job.workplace_type = info["work_style"]
    job.date_posted = info["date_posted"]
    job.date_posted_raw = info["opening_date_raw"]
    job.valid_through = info["valid_through"]
    job.employer = "HSBC"
    job.raw["source_dates"] = {
        "openingDate": info["opening_date_raw"],
        "closingDate": info["closing_date_raw"],
    }
    if info["opening_date_raw"] and not info["date_posted"]:
        job.warnings.append("unparsed_opening_date")
    if info["closing_date_raw"] and not info["valid_through"]:
        job.warnings.append("unparsed_closing_date")


def apply_detail(job, info: dict, *, source_url: str) -> None:
    """Copy a parsed HSBC detail onto a Job, preserving source dates and raw rows."""
    job.title = info["title"]
    job.requisition_id = info["display_job_id"] or job.requisition_id
    job.description_html = info["description_html"]
    job.description_method = "hsbc.eightfold.job_detail"
    job.description_source_url = source_url
    job.apply_url = info["apply_url"]
    job.locations = info["locations"] or job.locations
    job.department = info["department"] or job.department
    job.team = info["business_unit"]
    job.employment_type = info["job_type"]
    job.workplace_type = info["work_style"] or info["work_location_option"] or job.workplace_type
    job.employer = info["brand"] or job.employer
    job.date_posted = info["date_posted"]
    job.date_posted_raw = info["date_posted_raw"]
    job.valid_through = info["valid_through"]
    job.salary = info["salary"]
    job.raw["source_dates"] = {
        "postingStartDate": info["date_posted_raw"],
        "postingEndDate": info["valid_through_raw"],
        "t_create": info["t_create"],
        "t_update": info["t_update"],
    }
    if info["date_posted_raw"] and not info["date_posted"]:
        job.warnings.append("unparsed_posting_start_date")


class Hsbc(Adapter):
    async def _crawl_listing(self, *, counted: bool) -> dict:
        offset = 0
        rows: list[dict] = []
        counts: list[int] = []
        pages = 0
        while True:
            if counted:
                if not self.page_guard():
                    break
            elif pages >= MAX_LISTING_PAGES:
                break
            data = await self.http.json(LISTING_API, params={
                "domain": DOMAIN, "start": offset, "num": PAGE_SIZE,
                "location": LOCATION_FILTER, "sort_by": SORT_BY, "sort_order": "desc",
            })
            page = parse_listing_page(data)
            counts.append(page["count"])
            rows.extend(page["rows"])
            pages += 1
            offset += page["row_count"]
            if not page["row_count"] or offset >= page["count"]:
                break
        unique = list(dict.fromkeys(str(row["id"]) for row in rows))
        return {
            "rows": rows, "pages": pages, "counts": sorted(set(counts)),
            "count": counts[-1] if counts else None, "unique_ids": unique,
            "duplicate_rows": len(rows) - len(unique),
        }

    def _row_job(self, row: dict):
        return self.job(
            dict(row, origin="eightfold"),
            title=row.get("name") or row.get("posting_name"),
            url=row.get("canonicalPositionUrl") or f"{PORTAL_ORIGIN}/careers/job/{row['id']}",
            job_id=str(row["id"]),
            requisition_id=row.get("display_job_id"),
            locations=row.get("locations") or [row.get("location")],
            department=row.get("department"),
            employer=(row.get("custom_data") or {}).get("brand"),
            workplace_type=row.get("work_location_option"),
        )

    def _legacy_job(self, row: dict):
        return self.job(
            dict(row),
            title=row.get("title"),
            url=row.get("url"),
            job_id=str(row.get("id")),
            requisition_id=str(row.get("id")),
            locations=[row["location"]] if row.get("location") else [],
            department=row.get("area_of_interest"),
            employment_type=row.get("job_type"),
            workplace_type=row.get("work_style"),
            employer="HSBC",
        )

    async def _crawl_legacy(self) -> dict:
        started = time.perf_counter()
        response = await self.http.get(LEGACY_SITE)
        parsed = parse_legacy_listing(response.text, str(response.url))
        parsed.update(status=response.status_code,
                      seconds=round(max(0.0, time.perf_counter() - started), 6))
        return parsed

    def _membership(self, portal: dict, legacy: dict) -> dict:
        portal_count = portal.get("count") if portal else None
        legacy_count = legacy.get("count") if legacy else None
        ids = ({f"eightfold:{row_id}" for row_id in portal.get("unique_ids", [])}
               | {f"mycareer:{row['id']}" for row in legacy.get("rows", [])})
        return {
            "portal_count": portal_count,
            "legacy_count": legacy_count,
            "total": (portal_count or 0) + (legacy_count or 0),
            "ids": ids,
        }

    async def run(self):
        self.result.metadata["official_entry_url"] = OFFICIAL_ENTRY
        self.result.metadata["listing_url"] = LISTING_URL
        self.result.metadata["legacy_listing_url"] = LEGACY_SITE
        collection_started = time.perf_counter()

        try:
            entry_response = await self.http.get(OFFICIAL_ENTRY)
            entry = parse_official_entry(entry_response.text, str(entry_response.url))
            entry.update({
                "status": entry_response.status_code,
                "bytes": len(entry_response.content),
                "html_sha256": sha256(entry_response.content).hexdigest(),
                "redirects": [
                    {"status": item.status_code, "url": str(item.url),
                     "location": item.headers.get("location")}
                    for item in entry_response.history
                ],
                "fetched_at": now(),
            })
            self.result.metadata["official_entry"] = entry
        except Exception as exc:
            self.result.errors.append(f"hsbc_official_entry_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["hsbc_source_collection_seconds"] = max(
                0.0, time.perf_counter() - collection_started)
            return self.result

        listing_started = time.perf_counter()
        try:
            snapshot = await self._crawl_listing(counted=True)
        except Exception as exc:
            self.result.errors.append(f"hsbc_listing_failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["hsbc_source_collection_seconds"] = max(
                0.0, time.perf_counter() - collection_started)
            return self.result
        finally:
            self.result.timings["hsbc_listing_seconds"] = max(
                0.0, time.perf_counter() - listing_started)

        if not snapshot["counts"]:
            self.result.errors.append("hsbc_listing_no_pages")
        if len(snapshot["counts"]) > 1:
            self.result.errors.append(f"hsbc_listing_count_changed: {snapshot['counts']}")
        if snapshot["duplicate_rows"]:
            self.result.errors.append(f"hsbc_listing_duplicate_rows: {snapshot['duplicate_rows']}")

        legacy_snapshot = None
        try:
            legacy_snapshot = await self._crawl_legacy()
        except Exception as exc:
            self.result.errors.append(f"hsbc_legacy_listing_failed: {type(exc).__name__}: {exc}")
        membership = self._membership(snapshot, legacy_snapshot or {})
        self.result.expected_total = membership["total"]
        self.result.metadata["listing"] = {
            "portal": {
                "requests": snapshot["pages"],
                "rows": len(snapshot["rows"]),
                "unique_ids": len(snapshot["unique_ids"]),
                "counts_seen": snapshot["counts"],
                "duplicate_rows": snapshot["duplicate_rows"],
                "page_size": PAGE_SIZE,
                "location_filter": LOCATION_FILTER,
                "sort_by": SORT_BY,
            },
            "legacy": {
                "url": LEGACY_SITE,
                "rows": legacy_snapshot["count"] if legacy_snapshot else None,
                "status": legacy_snapshot["status"] if legacy_snapshot else None,
                "ids": [row["id"] for row in legacy_snapshot["rows"]] if legacy_snapshot else None,
            },
            "total_expected": membership["total"],
        }
        for row in snapshot["rows"]:
            self.add(self._row_job(row))
        if legacy_snapshot:
            for row in legacy_snapshot["rows"]:
                self.add(self._legacy_job(row))
        self.result.metadata["listing"]["jobs_added"] = len(self.result.jobs)
        if len(self.result.jobs) != self.result.expected_total:
            self.result.errors.append(
                f"hsbc_listing_total_mismatch: {len(self.result.jobs)}/{self.result.expected_total}")
        self.result.listing_complete = len(self.result.jobs) == self.result.expected_total

        blocked = {"active": False}

        async def detail(job):
            origin = job.raw.get("origin")
            if blocked["active"] and origin != "mycareer":
                job.description_unavailable = True
                job.warnings.append("detail_skipped_after_bot_challenge")
                return job
            if origin == "mycareer":
                response = await self.http.get(job.url)
                info = parse_legacy_detail(response.text, str(response.url), job.raw)
                job.raw["detail_html_sha256"] = sha256(response.content).hexdigest()
                apply_legacy_detail(job, info, source_url=str(response.url))
                return job
            api = DETAIL_API.format(job_id=job.job_id)
            try:
                data = await self.http.json(api, params={"domain": DOMAIN})
            except Exception as exc:
                if is_bot_challenge(exc):
                    if not blocked["active"]:
                        blocked["active"] = True
                        self.result.metadata["bot_challenge"] = {
                            "status": 405 if "HTTP 405" in str(exc) else 429,
                            "detected_at": now(),
                            "first_url": api,
                            "action": "Stopped posting requests after the AWS WAF human-verification "
                                      "response; no CAPTCHA was solved and no protection was bypassed.",
                        }
                        self.result.warnings.append("waf_human_verification_challenge")
                    job.description_unavailable = True
                    job.warnings.append("bot_challenge_blocked_detail")
                raise
            info = parse_detail(data, job.raw)
            job.raw["detail"] = data
            apply_detail(job, info, source_url=api)
            return job

        self.result.jobs = await self.hydrate(self.result.jobs, detail)
        portal_ids = set(snapshot["unique_ids"])
        for job in self.result.jobs:
            job.raw.setdefault("origin", "eightfold" if job.job_id in portal_ids else "mycareer")
        skipped = sum(1 for job in self.result.jobs
                      if "detail_skipped_after_bot_challenge" in job.warnings)
        if blocked["active"] or skipped:
            self.result.metadata["bot_challenge"]["skipped_details_after_block"] = skipped
            self.result.metadata["bot_challenge"]["verified_before_block"] = sum(
                1 for job in self.result.jobs if job.description_verified)
            self.result.errors.append(
                "hsbc_bot_challenge_stopped_collection: unverified="
                f"{len(self.result.jobs) - sum(1 for job in self.result.jobs if job.description_verified)}")
        self.result.timings["hsbc_description_seconds"] = self.result.timings.get(
            "detail_phase_seconds", 0.0)

        # Closing reconciliation: both published listings are live, so re-read them.
        if blocked["active"]:
            self.result.listing_complete = False
            self.result.metadata["membership_recheck"] = {
                "skipped": True,
                "reason": "AWS WAF human-verification challenge stopped further requests",
            }
            self.result.timings["hsbc_source_collection_seconds"] = max(
                0.0, time.perf_counter() - collection_started)
            return self.result
        recheck_started = time.perf_counter()
        try:
            recheck = await self._crawl_listing(counted=False)
        except Exception as exc:
            self.result.errors.append(f"hsbc_listing_recheck_failed: {type(exc).__name__}: {exc}")
            self.result.timings["hsbc_membership_recheck_seconds"] = max(
                0.0, time.perf_counter() - recheck_started)
            self.result.timings["hsbc_source_collection_seconds"] = max(
                0.0, time.perf_counter() - collection_started)
            return self.result
        legacy_recheck = None
        try:
            legacy_recheck = await self._crawl_legacy()
        except Exception as exc:
            self.result.errors.append(
                f"hsbc_legacy_listing_recheck_failed: {type(exc).__name__}: {exc}")
        final_membership = self._membership(recheck, legacy_recheck or {})
        first_ids = membership["ids"]
        final_ids = final_membership["ids"]
        new_ids = sorted(final_ids - first_ids)
        removed_ids = sorted(first_ids - final_ids)
        self.result.metadata["membership_recheck"] = {
            "portal_requests": recheck["pages"],
            "count_at_start": membership["total"],
            "count_at_finish": final_membership["total"],
            "portal_ids_at_start": snapshot["count"],
            "portal_ids_at_finish": recheck["count"],
            "legacy_ids_at_start": legacy_snapshot["count"] if legacy_snapshot else None,
            "legacy_ids_at_finish": legacy_recheck["count"] if legacy_recheck else None,
            "ids_at_start": len(first_ids),
            "ids_at_finish": len(final_ids),
            "new_ids": new_ids,
            "removed_ids": removed_ids,
        }
        if new_ids:
            portal_rows = {f"eightfold:{row['id']}": row for row in recheck["rows"]}
            legacy_rows = {f"mycareer:{row['id']}": row for row in (legacy_recheck or {}).get("rows", [])}
            additions = []
            for key in new_ids:
                if key in portal_rows:
                    additions.append(self._row_job(portal_rows[key]))
                elif key in legacy_rows:
                    additions.append(self._legacy_job(legacy_rows[key]))
            if additions:
                existing = self.result.jobs
                added = await self.hydrate(additions, detail)
                self.result.jobs = existing + added
        if final_membership["total"]:
            self.result.expected_total = final_membership["total"]
        if new_ids or removed_ids:
            self.result.listing_complete = False
            self.result.errors.append(
                "hsbc_listing_membership_changed_during_run: "
                f"new={len(new_ids)} removed={len(removed_ids)}")
        else:
            self.result.listing_complete = len(self.result.jobs) == self.result.expected_total
        self.result.timings["hsbc_membership_recheck_seconds"] = max(
            0.0, time.perf_counter() - recheck_started)
        self.result.timings["hsbc_source_collection_seconds"] = max(
            0.0, time.perf_counter() - collection_started)
        return self.result
