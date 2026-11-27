"""MakeMyTrip's own careers SPA and its same-origin jobs API.

The official entry ``https://careers.makemytrip.com/`` is a small React shell.
Its shipped bundle requests ``GET /api/jobs`` for the complete company careers
listing and ``GET /api/jobDetails?jobId=<id>`` for one exact role.  The rendered
``/prod/jobs`` page loads five cards at a time with a client-side ``LOAD MORE``
control; the API itself returns the complete unfiltered membership in one
response.  No ATS board is enumerated: the Darwinbox link on each role is only
the application destination published by MakeMyTrip.
"""

from __future__ import annotations

import hashlib
import html
from datetime import datetime
import re
import time
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html


ORIGIN = "https://careers.makemytrip.com"
OFFICIAL_ENTRY = ORIGIN + "/"
LISTING_URL = ORIGIN + "/prod/jobs"
LISTING_API_URL = ORIGIN + "/api/jobs"
DETAIL_API_URL = ORIGIN + "/api/jobDetails"
DETAIL_API_TEMPLATE = DETAIL_API_URL + "?jobId={job_id}"
JOB_PATH_TEMPLATE = "/prod/opportunity/{job_id}/{slug}"
EXPECTED_TITLE = "MakeMyTrip"
EXPECTED_HOST = "careers.makemytrip.com"
JOB_ID_RE = re.compile(r"^[0-9a-f]{14}$")
JOB_CODE_RE = re.compile(r"^JOB_\d+$")
TIMESTAMP_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4}) (\d{2}):(\d{2}):(\d{2})$")
WRAPPER_RE = re.compile(
    r"^\s*&(?:amp;)*lt;(?:p|div|section|article|ul|ol|h[1-6])(?:\s|&(?:amp;)*gt;)",
    re.I,
)
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": DESKTOP_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Referer": LISTING_URL,
    "Origin": ORIGIN,
}


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def normalized_body_text(value: str | None) -> str:
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+([,;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def decode_escaped_html(value: str | None) -> str:
    """Decode the whole escaped HTML wrapper the MakeMyTrip API returns.

    The source field is escaped once in JSON (and occasionally twice).  Only a
    wrapper that starts with ``&lt;p>``/``&lt;div>``/... is decoded, so literal
    comparison text such as ``&lt;T&gt;`` inside an already-HTML body is kept.
    """
    text = (value or "").strip()
    for _ in range(2):
        if not WRAPPER_RE.match(text):
            break
        text = html.unescape(text)
    return text


def slug_title(value: str | None) -> str:
    """Match the SPA's exact href slug: lowercase and spaces become hyphens."""
    return (value or "").strip().lower().replace(" ", "-")


def job_url(row: dict) -> str:
    identity = str(row.get("job_id") or "")
    if not JOB_ID_RE.fullmatch(identity):
        raise ValueError(f"MakeMyTrip listing row has an unexpected job id: {identity!r}")
    slug = slug_title(row.get("job_title"))
    if not slug:
        raise ValueError(f"MakeMyTrip listing row {identity} has no title")
    return ORIGIN + JOB_PATH_TEMPLATE.format(job_id=identity, slug=quote(slug, safe="()-&"))


def detail_api_url(identity: str) -> str:
    if not JOB_ID_RE.fullmatch(identity or ""):
        raise ValueError(f"MakeMyTrip job id is not the observed 14-hex form: {identity!r}")
    return DETAIL_API_TEMPLATE.format(job_id=identity)


def source_date(value: str | None) -> str | None:
    """DD-MM-YYYY HH:MM:SS employer timestamp -> ISO date, retaining raw separately."""
    match = TIMESTAMP_RE.fullmatch((value or "").strip())
    if not match:
        return None
    day, month, year, hour, minute, second = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second).date().isoformat()
    except ValueError:
        return None


def parse_official_entry(html_text: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.hostname != EXPECTED_HOST or parts.path.rstrip("/") != "":
        raise ValueError("MakeMyTrip official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html_text, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != EXPECTED_TITLE:
        raise ValueError(f"MakeMyTrip official careers title changed: {title!r}")
    root = soup.select_one("#root")
    scripts = [script.get("src", "") for script in soup.select("script[src]")]
    if root is None or not any(src.endswith("/bundle.js") for src in scripts):
        raise ValueError("MakeMyTrip official careers React shell no longer exposes #root and bundle.js")
    return {
        "url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "title": title,
        "root_present": True,
        "bundle_scripts": scripts,
        "company_identity": "MakeMyTrip",
    }


def parse_listing_shell(html_text: str, response_url: str = LISTING_URL) -> dict:
    parsed = parse_official_entry(html_text, ORIGIN + "/")
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.hostname != EXPECTED_HOST or parts.path.rstrip("/") != "/prod/jobs":
        raise ValueError("MakeMyTrip listing route redirected away from /prod/jobs")
    return {**parsed, "url": LISTING_URL, "final_url": response_url}


def parse_listing_payload(value: dict, response_url: str = LISTING_API_URL) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.hostname != EXPECTED_HOST or parts.path != "/api/jobs":
        raise ValueError("MakeMyTrip listing API redirected outside its same-origin /api/jobs route")
    if not isinstance(value, dict):
        raise ValueError("MakeMyTrip listing API response is not an object")
    all_jobs = value.get("allJobs")
    business_units = value.get("businessUnits")
    locations = value.get("locations")
    if not isinstance(all_jobs, list) or not isinstance(business_units, list) or not isinstance(locations, list):
        raise ValueError("MakeMyTrip listing API is missing allJobs, businessUnits or locations")
    if any(not isinstance(item, dict) for item in business_units):
        raise ValueError("MakeMyTrip businessUnits contains a non-object row")
    if any(not isinstance(item, str) or not item.strip() for item in locations):
        raise ValueError("MakeMyTrip location filter contains an empty or non-string value")

    rows, excluded, issues = [], [], []
    seen_ids, seen_codes = set(), set()
    for row in all_jobs:
        if not isinstance(row, dict):
            issues.append("listing_row_not_object")
            continue
        identity = str(row.get("job_id") or "")
        code = str(row.get("job_code") or "")
        if not JOB_ID_RE.fullmatch(identity):
            issues.append(f"listing_row_unexpected_job_id:{identity or '<empty>'}")
            continue
        if not JOB_CODE_RE.fullmatch(code):
            issues.append(f"listing_row_unexpected_job_code:{identity}:{code or '<empty>'}")
        if not normalized_title(row.get("job_title")):
            issues.append(f"listing_row_missing_title:{identity}")
            continue
        if identity in seen_ids:
            issues.append(f"listing_row_duplicate_job_id:{identity}")
            continue
        if code and code in seen_codes:
            issues.append(f"listing_row_duplicate_job_code:{code}")
        if row.get("post_on_careers_page") != 1:
            excluded.append(dict(row))
            continue
        if not isinstance(row.get("location"), list) or not row["location"]:
            issues.append(f"listing_row_missing_location:{identity}")
        if not isinstance(row.get("location_city"), list) or not row["location_city"]:
            issues.append(f"listing_row_missing_location_city:{identity}")
        if not normalized_title(row.get("group_company")):
            issues.append(f"listing_row_missing_group_company:{identity}")
        seen_ids.add(identity)
        if code:
            seen_codes.add(code)
        rows.append(dict(row))

    counts = []
    for unit in business_units:
        name = normalized_title(unit.get("name"))
        count = unit.get("count")
        if not name or not isinstance(count, int) or count < 0:
            issues.append("business_unit_row_invalid")
            continue
        counts.append((name, count))
    advertised_total = sum(count for _, count in counts)
    if advertised_total != len(rows):
        issues.append(f"business_unit_count_sum_mismatch:{advertised_total}!={len(rows)}")
    if len({name for name, _ in counts}) != len(counts):
        issues.append("business_unit_names_not_unique")

    city_values = {city for row in rows for city in row.get("location_city", []) if isinstance(city, str)}
    unknown_cities = sorted(city_values - set(locations))
    if unknown_cities:
        issues.append("listing_city_not_in_location_filter:" + ",".join(unknown_cities))

    return {
        "rows": rows,
        "job_ids": [str(row["job_id"]) for row in rows],
        "count": len(rows),
        "advertised_total": advertised_total,
        "business_units": [{"name": name, "count": count} for name, count in counts],
        "locations": list(locations),
        "excluded_non_careers_rows": excluded,
        "issues": issues,
        "request_url": LISTING_API_URL,
        "final_url": response_url,
    }


def parse_detail_payload(value: dict, expected_row: dict, response_url: str) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.hostname != EXPECTED_HOST or parts.path != "/api/jobDetails":
        raise ValueError("MakeMyTrip detail API redirected outside its /api/jobDetails route")
    identity = str(expected_row.get("job_id") or "")
    if parts.query not in ("", f"jobId={identity}"):
        raise ValueError("MakeMyTrip detail API response does not match the requested job id")
    if not isinstance(value, dict) or value.get("status") != 1 or not isinstance(value.get("data"), dict):
        raise ValueError(f"MakeMyTrip detail API failed for {identity}: {value!r}"[:500])
    data = value["data"]
    required = [
        "applyUrl", "department", "department_code", "designation", "employee_type",
        "experience_from", "experience_to", "group_company", "job_created_timestamp",
        "job_decription", "job_status", "job_title", "job_updated_timestamp",
        "location", "location_city", "location_country", "open_positions", "unit_experience",
    ]
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"MakeMyTrip detail {identity} is missing fields: {missing}")
    if normalized_title(data["job_title"]) != normalized_title(expected_row.get("job_title")):
        raise ValueError(f"MakeMyTrip detail {identity} title changed: {data['job_title']!r}")
    apply_url = str(data.get("applyUrl") or "")
    apply_parts = urlsplit(apply_url)
    if (apply_parts.scheme != "https" or apply_parts.hostname != "gommt.darwinbox.in"
            or f"/jobDetails/{identity}" not in apply_parts.path):
        raise ValueError(f"MakeMyTrip detail {identity} has an unexpected apply URL: {apply_url!r}")
    if data.get("job_status") != "OPEN":
        raise ValueError(f"MakeMyTrip detail {identity} is not an open posting: {data.get('job_status')!r}")
    if not isinstance(data.get("job_decription"), str) or not data["job_decription"].strip():
        raise ValueError(f"MakeMyTrip detail {identity} has an empty description field")
    if not isinstance(data.get("location"), list) or not data["location"]:
        raise ValueError(f"MakeMyTrip detail {identity} has no location records")

    description_html = decode_escaped_html(data["job_decription"])
    description_text = text_html(description_html)
    normalized = normalized_body_text(description_html)
    if len(description_text.strip()) < 100:
        raise ValueError(f"MakeMyTrip detail {identity} description is implausibly short")
    return {
        "data": data,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(normalized),
        "description_fnv1a64": fnv1a64(normalized),
        "description_sha256": hashlib.sha256(description_html.encode()).hexdigest(),
        "source_dates": {
            "job_created_timestamp": data.get("job_created_timestamp"),
            "job_updated_timestamp": data.get("job_updated_timestamp"),
            "date_posted": source_date(data.get("job_created_timestamp")),
            "date_updated": source_date(data.get("job_updated_timestamp")),
        },
    }


class MakeMyTrip(Adapter):
    """Collect only the exact roles returned by MakeMyTrip's own careers API."""

    VERIFICATION_REVISION = "makemytrip.careers.api.1"

    def _make_job(self, row: dict, detail: dict | None = None):
        identity = str(row["job_id"])
        data = detail["data"] if detail else {}
        description_html = detail["description_html"] if detail else ""
        description_text = detail["description_text"] if detail else ""
        locations = [
            {
                "location": location,
                "city": data.get("location_city", [])[index] if index < len(data.get("location_city", [])) else None,
                "country": data.get("location_country"),
            }
            for index, location in enumerate(data.get("location") or [])
        ]
        experience = None
        if data:
            experience = {
                "from": data.get("experience_from"),
                "to": data.get("experience_to"),
                "unit": data.get("unit_experience"),
            }
        dates = detail["source_dates"] if detail else {}
        job = self.job(
            {
                "listing": dict(row),
                "detail": dict(data) if data else None,
                "listing_api_url": LISTING_API_URL,
                "detail_api_url": detail_api_url(identity) if detail else None,
                "detail_page_url": job_url(row),
                "source_dates": dates,
                "description_decoded_sha256": detail["description_sha256"] if detail else None,
                "description_normalized_chars": detail["description_normalized_chars"] if detail else None,
                "description_fnv1a64": detail["description_fnv1a64"] if detail else None,
            },
            title=normalized_title(data.get("job_title") or row.get("job_title")),
            url=job_url(row),
            job_id=identity,
            requisition_id=row.get("job_code") or None,
            apply_url=data.get("applyUrl"),
            description=description_html,
            method="makemytrip.careers.api.jobDetails.job_decription" if detail else None,
            description_url=detail_api_url(identity) if detail else None,
            locations=locations,
            department=data.get("department") or row.get("department"),
            team=None,
            employment_type=data.get("employee_type") or row.get("employee_type"),
            workplace_type=None,
            date_posted=dates.get("date_posted"),
            date_posted_raw=dates.get("job_created_timestamp"),
            date_updated=dates.get("date_updated"),
            valid_through=None,
            salary=None,
            qualifications=None,
            responsibilities=None,
            experience=experience,
            education=None,
            employer=data.get("group_company") or row.get("group_company"),
            language="en",
        )
        job.description_text = description_text
        job.record_identity = f"makemytrip:{identity}"
        job.company_scope_verified = self.company.scope == "company"
        return job

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY, headers=BROWSER_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        return {**parsed, "status": response.status_code, "seconds": seconds,
                "response_bytes": len(response.content),
                "response_sha256": hashlib.sha256(response.content).hexdigest()}

    async def _listing_shell(self):
        started = time.perf_counter()
        response = await self.http.get(LISTING_URL, headers=BROWSER_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_listing_shell(response.text, str(response.url))
        return {**parsed, "status": response.status_code, "seconds": seconds,
                "response_bytes": len(response.content),
                "response_sha256": hashlib.sha256(response.content).hexdigest()}

    async def _listing(self, label: str):
        started = time.perf_counter()
        response = await self.http.get(LISTING_API_URL, headers=BROWSER_HEADERS)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_listing_payload(response.json(), str(response.url))
        return {**parsed, "label": label, "status": response.status_code, "seconds": seconds,
                "response_bytes": len(response.content),
                "response_sha256": hashlib.sha256(response.content).hexdigest()}

    @staticmethod
    def _stable_fields(row: dict) -> dict:
        return {
            "job_code": row.get("job_code"),
            "job_title": normalized_title(row.get("job_title")),
            "group_company": normalized_title(row.get("group_company")),
            "department": row.get("department"),
            "parent_department": row.get("parent_department"),
            "business_unit": row.get("business_unit"),
            "division": row.get("division"),
            "employee_type": row.get("employee_type"),
            "post_on_careers_page": row.get("post_on_careers_page"),
            "location": row.get("location"),
            "location_city": row.get("location_city"),
            "location_country": row.get("location_country"),
            "job_created_timestamp": row.get("job_created_timestamp"),
        }

    async def run(self):
        source_started = time.perf_counter()
        entry = await self._official_entry()
        shell = await self._listing_shell()
        listing = await self._listing("initial")
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_api_url": LISTING_API_URL,
            "detail_api_url_template": DETAIL_API_TEMPLATE,
            "official_entry": entry,
            "listing_shell": shell,
            "listing_authority": (
                "MakeMyTrip's own React careers app requests its same-origin /api/jobs route; "
                "the rendered /prod/jobs page shows the same ordered membership with client-side LOAD MORE."
            ),
            "rendered_listing_observation": {
                "initial_cards": 5,
                "load_more_step": 5,
                "load_more_clicks_to_complete": 6,
                "final_visible_cards": 35,
                "filters_not_applied": True,
                "filter_controls": {
                    "departments": listing["business_units"],
                    "locations": listing["locations"],
                    "keyword_search": True,
                },
                "pagination": "Client-side LOAD MORE only; /api/jobs returns the complete membership once.",
            },
            "listing_membership_request": {
                "method": "GET",
                "url": LISTING_API_URL,
                "query": None,
                "filters_applied": False,
                "scope": "Same-origin careers API only; no Darwinbox or other company board is enumerated.",
            },
            "detail_method": (
                "GET /api/jobDetails?jobId=<id> for every exact listing id; job_decription is the full "
                "employer-published body. The Darwinbox URL is retained only as the apply destination."
            ),
            "dates": {
                "date_posted": "Source job_created_timestamp (DD-MM-YYYY HH:MM:SS) mapped to ISO date; raw retained.",
                "date_updated": "Source job_updated_timestamp mapped to ISO date; raw retained.",
                "valid_through": None,
                "salary": None,
                "requisition_id": "Source job_code (e.g. JOB_2141).",
            },
        })
        if listing["issues"]:
            self.result.errors.extend(f"initial_listing:{issue}" for issue in listing["issues"])
        if listing["excluded_non_careers_rows"]:
            self.result.warnings.append(
                f"excluded_non_careers_rows:{len(listing['excluded_non_careers_rows'])}"
            )
        if not self.page_guard():
            return self.result
        self.result.expected_total = listing["count"]
        self.result.metadata["initial_listing"] = listing

        for row in listing["rows"]:
            self.add(self._make_job(row))

        async def detail(job):
            identity = str(job.job_id)
            request_url = detail_api_url(identity)
            started = time.perf_counter()
            response = await self.http.get(request_url, headers=BROWSER_HEADERS)
            seconds = max(0.0, time.perf_counter() - started)
            expected_row = job.raw["listing"]
            parsed = parse_detail_payload(response.json(), expected_row, str(response.url))
            job = self._make_job(expected_row, parsed)
            job.raw["detail"]["api_response_bytes"] = len(response.content)
            job.raw["detail"]["api_response_sha256"] = hashlib.sha256(
                response.content
            ).hexdigest()
            job.raw["detail"]["request_seconds"] = seconds
            job.raw["detail"]["response_status"] = response.status_code
            return job

        self.result.jobs[:] = await self.hydrate(self.result.jobs, detail)

        final_started = time.perf_counter()
        final = await self._listing("final")
        final_seconds = max(0.0, time.perf_counter() - final_started)
        self.result.metadata["final_listing"] = final

        initial_by_id = {str(row["job_id"]): row for row in listing["rows"]}
        final_by_id = {str(row["job_id"]): row for row in final["rows"]}
        initial_ids = [str(row["job_id"]) for row in listing["rows"]]
        final_ids = [str(row["job_id"]) for row in final["rows"]]
        new_ids = [identity for identity in final_ids if identity not in initial_by_id]
        removed_ids = [identity for identity in initial_ids if identity not in final_by_id]
        changed_fields = {}
        for identity in sorted(set(initial_by_id) & set(final_by_id)):
            before = self._stable_fields(initial_by_id[identity])
            after = self._stable_fields(final_by_id[identity])
            if before != after:
                changed_fields[identity] = {
                    key: {"initial": before[key], "final": after[key]}
                    for key in before if before[key] != after[key]
                }
        order_stable = initial_ids == final_ids
        complete = (
            not listing["issues"]
            and not final["issues"]
            and not new_ids
            and not removed_ids
            and not changed_fields
            and listing["count"] == final["count"] == listing["advertised_total"]
            and len(self.result.jobs) == listing["count"]
        )
        if new_ids:
            self.result.errors.append("listing_new_ids_during_collection:" + ",".join(new_ids))
        if removed_ids:
            self.result.errors.append("listing_removed_ids_during_collection:" + ",".join(removed_ids))
        if changed_fields:
            self.result.errors.append(
                "listing_source_fields_changed_during_collection:" + ",".join(sorted(changed_fields))
            )
        if not order_stable:
            self.result.warnings.append("listing_order_changed_during_collection")
        if final["issues"]:
            self.result.errors.extend(f"final_listing:{issue}" for issue in final["issues"])
        if final["count"] != listing["count"]:
            self.result.errors.append(
                f"listing_count_changed_during_collection:{listing['count']}!={final['count']}"
            )
        self.result.listing_complete = complete
        self.result.metadata["membership_reconciliation"] = {
            "initial_ids": initial_ids,
            "final_ids": final_ids,
            "initial_count": listing["count"],
            "final_count": final["count"],
            "advertised_business_unit_total": listing["advertised_total"],
            "new_ids": new_ids,
            "removed_ids": removed_ids,
            "order_stable": order_stable,
            "changed_fields_by_id": changed_fields,
            "complete": complete,
        }
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        self.result.timings["official_entry_seconds"] = entry["seconds"]
        self.result.timings["listing_shell_seconds"] = shell["seconds"]
        self.result.timings["listing_api_seconds"] = listing["seconds"]
        self.result.timings["final_recheck_seconds"] = final_seconds
        self.result.timings["detail_phase_seconds"] = self.result.timings.get("detail_phase_seconds", 0.0)
        return self.result


def rendered_detail_body(html_text: str) -> dict:
    """Extract the rendered description container used by the browser reference."""
    soup = BeautifulSoup(html_text or "", "lxml")
    container = soup.select_one(".jobDescContainer")
    if container is None:
        raise ValueError("MakeMyTrip rendered detail page has no .jobDescContainer")
    title = soup.select_one("h1, h2.title")
    text = normalized_body_text(str(container))
    if not text:
        raise ValueError("MakeMyTrip rendered detail .jobDescContainer is empty")
    return {
        "title": normalized_title(title.get_text(" ", strip=True)) if title else None,
        "description_text": text,
        "description_normalized_chars": len(text),
        "description_fnv1a64": fnv1a64(text),
    }
