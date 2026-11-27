"""Meta's own metacareers.com global listing and exact job-detail pages.

The public listing is a React/Relay page. Its unfiltered
``CareersJobSearchResultsV2DataQuery`` response is the company-scoped listing
membership (all rows are returned by one request; the UI has no page controls).
Each exact ``/profile/job_details/<id>/`` page server-renders the complete
``xcp_requisition_job_description`` object plus a schema.org JobPosting.
"""
from __future__ import annotations

from hashlib import sha256
from html import escape
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter

OFFICIAL_ENTRY = "https://www.metacareers.com/jobs/"
LISTING_URL = "https://www.metacareers.com/jobsearch/"
GRAPHQL_URL = "https://www.metacareers.com/graphql"
DETAIL_URL = "https://www.metacareers.com/profile/job_details/{job_id}/"
APPLY_URL = "https://www.metacareers.com/profile/create_application/{job_id}/"
LISTING_DOC_ID = "27129360303422352"
LISTING_FRIENDLY_NAME = "CareersJobSearchResultsV2DataQuery"
JOB_COUNT_FRIENDLY_NAME = "CareersJobSearchHideFiltersBarV2Query"
JOB_COUNT_DOC_ID = "26210170368675892"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="128", "Not;A=Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

COUNTRY_NAMES = {
    "US": "United States of America",
    "CA": "Canada",
    "GB": "United Kingdom",
    "IE": "Ireland",
    "DE": "Germany",
    "FR": "France",
    "IL": "Israel",
    "IN": "India",
    "SG": "Singapore",
    "AU": "Australia",
    "BR": "Brazil",
    "MX": "Mexico",
    "PL": "Poland",
    "ES": "Spain",
    "IT": "Italy",
    "NL": "Netherlands",
    "SE": "Sweden",
    "CH": "Switzerland",
    "AE": "United Arab Emirates",
    "JP": "Japan",
    "KR": "South Korea",
    "TW": "Taiwan",
    "HK": "Hong Kong",
}

COMPENSATION_DISCLAIMER = (
    "Individual compensation is determined by skills, qualifications, experience, "
    "and location. Compensation details listed in this posting reflect the base "
    "hourly rate, monthly rate, or annual salary only, and do not include bonus, "
    "equity or sales incentives, if applicable. In addition to base compensation, "
    "Meta offers benefits. Learn more about benefits at Meta."
)

PUBLIC_COMPENSATION_FIELD = "xcp_requisition_job_description.public_compensation"
DETAIL_FIELD = "xcp_requisition_job_description"


def detail_url(job_id: str) -> str:
    return DETAIL_URL.format(job_id=job_id)


def apply_url(job_id: str) -> str:
    """Route reached by the job page's own ``Apply now`` button (observed in Chromium)."""
    return APPLY_URL.format(job_id=job_id)


def normalized_body_text(value: str | None) -> str:
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_visible_listing_count(text: str) -> int | None:
    match = re.search(r"([\d,]+)\s+Items\b", text or "")
    return int(match.group(1).replace(",", "")) if match else None


def _job_id_from_url(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    match = re.fullmatch(r"/profile/job_details/(\d+)/?", parsed.path)
    if parsed.scheme != "https" or parsed.hostname != "www.metacareers.com" or match is None:
        raise ValueError("Meta job-detail response came from an unexpected URL")
    return match.group(1), parsed.path


def _find_preloader(html: str) -> tuple[dict, str]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select('script[type="application/json"]'):
        raw = tag.string or tag.get_text()
        if DETAIL_FIELD not in raw:
            continue
        try:
            outer = json.loads(raw)
        except ValueError:
            continue
        found = _find_key(outer, DETAIL_FIELD)
        if isinstance(found, dict):
            return found, raw
    raise ValueError("Meta detail page has no xcp_requisition_job_description preloader")


def _find_key(value, key: str):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _find_key(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, key)
            if found is not None:
                return found
    return None


def _find_jobposting(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select('script[type="application/ld+json"]'):
        raw = tag.string or tag.get_text()
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        for candidate in value if isinstance(value, list) else [value]:
            if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                return candidate
    raise ValueError("Meta detail page has no schema.org JobPosting")


def _inner_html(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("__html") or "")
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, str)):
            return _inner_html(parsed)
    if stripped.startswith("<"):
        return value
    return f"<p>{escape(value)}</p>"


def _items(values) -> list[str]:
    items = []
    for value in values or []:
        if isinstance(value, dict):
            text = value.get("item") or value.get("text") or value.get("label")
        else:
            text = value
        if isinstance(text, str) and text.strip():
            items.append(text.strip())
    return items


def _list_html(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _country_name(html: str, code: str | None) -> str:
    if not code:
        return ""
    if code in COUNTRY_NAMES:
        return COUNTRY_NAMES[code]
    match = re.search(rf'"{re.escape(code)}":"([^"]+)"', html)
    return match.group(1) if match else code


def _compensation_html(html: str, compensation) -> tuple[str, list[dict]]:
    rows = [row for row in (compensation or []) if isinstance(row, dict)]
    if not rows:
        return "", []
    parts = []
    for row in rows:
        label = _country_name(html, row.get("country_code"))
        minimum = row.get("compensation_amount_minimum")
        maximum = row.get("compensation_amount_maximum")
        if not minimum and not maximum:
            continue
        text = f"{label}: {minimum or ''}"
        if maximum and maximum != minimum:
            text += f" to {maximum}"
        extras = []
        if row.get("has_bonus"):
            extras.append("bonus")
        if row.get("has_equity"):
            extras.append("equity")
        extras.append("benefits")
        text += " + " + " + ".join(extras)
        parts.append(f"<p>{escape(text)}</p>")
        note = row.get("error_apology_note")
        if isinstance(note, str) and note.strip():
            parts.append(f"<p>{escape(note.strip())}</p>")
    if parts:
        parts.append(f"<p>{escape(COMPENSATION_DISCLAIMER)}</p>")
    return "".join(parts), rows


def _description_html(html: str, detail: dict) -> tuple[str, dict]:
    title = str(detail.get("title") or "").strip()
    responsibilities = _items(detail.get("responsibilities"))
    minimum = _items(detail.get("minimum_qualifications"))
    preferred = _items(detail.get("preferred_qualifications"))
    compensation_html, compensation_rows = _compensation_html(
        html, detail.get("public_compensation")
    )

    parts = []
    intro = _inner_html(detail.get("description"))
    if intro:
        parts.append(intro)
    if responsibilities:
        parts.append(f"<h2>{escape(title)} Responsibilities</h2>" + _list_html(responsibilities))
    if minimum:
        parts.append("<h2>Minimum Qualifications</h2>" + _list_html(minimum))
    if preferred:
        parts.append("<h2>Preferred Qualifications</h2>" + _list_html(preferred))
    boiler = _inner_html(detail.get("boiler_plate_intro"))
    if boiler:
        parts.append("<h2>About Meta</h2>" + boiler)
    california = _inner_html(detail.get("california_disclaimer"))
    if california:
        parts.append(california)
    international = _inner_html(detail.get("intl_disclaimer"))
    if international:
        parts.append(international)
    if compensation_html:
        parts.append(compensation_html)
    eeo = _inner_html(detail.get("equal_opportunity_message"))
    if eeo:
        parts.append("<h2>Equal Employment Opportunity</h2>" + eeo)
    accommodations = _inner_html(detail.get("accommodations_message"))
    if accommodations:
        parts.append(accommodations)
    ownership = _inner_html(detail.get("ownership_information"))
    if ownership:
        parts.append(ownership)

    sections = {
        "intro_html_chars": len(intro),
        "responsibility_items": len(responsibilities),
        "minimum_qualification_items": len(minimum),
        "preferred_qualification_items": len(preferred),
        "has_about_meta": bool(boiler),
        "has_california_disclaimer": bool(california),
        "has_international_disclaimer": bool(international),
        "compensation_rows": len(compensation_rows),
        "has_equal_opportunity": bool(eeo),
        "has_accommodations": bool(accommodations),
        "has_ownership_information": bool(ownership),
    }
    return "".join(parts), sections


def parse_listing_payload(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Meta listing GraphQL response is not an object")
    if value.get("errors"):
        raise ValueError(f"Meta listing GraphQL returned errors: {value['errors']}")
    data = (value.get("data") or {}).get("job_search_with_featured_jobs_v2")
    if not isinstance(data, dict):
        raise ValueError("Meta listing GraphQL response is missing job_search_with_featured_jobs_v2")
    all_jobs = data.get("all_jobs")
    featured_jobs = data.get("featured_jobs") or []
    if not isinstance(all_jobs, list) or not isinstance(featured_jobs, list):
        raise ValueError("Meta listing GraphQL response has non-list job rows")

    rows, seen, issues = [], set(), []
    all_ids, featured_ids = [], []
    for source_name, source_rows in (("all_jobs", all_jobs), ("featured_jobs", featured_jobs)):
        for row in source_rows:
            if not isinstance(row, dict):
                issues.append(f"meta_{source_name}_row_not_object")
                continue
            job_id = str(row.get("id") or "")
            title = row.get("title")
            if not re.fullmatch(r"\d+", job_id):
                issues.append(f"meta_{source_name}_row_missing_numeric_id")
                continue
            if not isinstance(title, str) or not title.strip():
                issues.append(f"meta_{source_name}_row_missing_title:{job_id}")
                continue
            row["title"] = title.strip()
            if not isinstance(row.get("locations"), list) or not isinstance(row.get("teams"), list):
                issues.append(f"meta_{source_name}_row_missing_list_fields:{job_id}")
                continue
            if not isinstance(row.get("sub_teams"), list):
                issues.append(f"meta_{source_name}_row_missing_sub_teams:{job_id}")
            if source_name == "all_jobs":
                all_ids.append(job_id)
            else:
                featured_ids.append(job_id)
            if job_id in seen:
                if source_name == "featured_jobs":
                    issues.append(f"meta_featured_duplicate_of_all_jobs:{job_id}")
                else:
                    issues.append(f"meta_listing_duplicate_id:{job_id}")
                continue
            seen.add(job_id)
            rows.append(dict(row))
    if not rows:
        issues.append("meta_listing_has_no_jobs")
    return {
        "rows": rows,
        "job_ids": [str(row["id"]) for row in rows],
        "all_job_ids": all_ids,
        "featured_ids": featured_ids,
        "featured_only_ids": [job_id for job_id in featured_ids if job_id not in set(all_ids)],
        "all_jobs_count": len(all_jobs),
        "featured_jobs_count": len(featured_jobs),
        "unique_count": len(rows),
        "issues": issues,
    }


def parse_detail_page(html: str, response_url: str) -> dict:
    job_id, _ = _job_id_from_url(response_url)
    preloader, preloader_raw = _find_preloader(html)
    try:
        posting = _find_jobposting(html)
    except ValueError:
        posting = {}
    if str(preloader.get("id") or "") != job_id:
        raise ValueError("Meta detail preloader ID differs from the URL job ID")
    title = preloader.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Meta detail preloader has no title")
    locations = preloader.get("locations")
    if not isinstance(locations, list) or not all(isinstance(x, str) for x in locations):
        raise ValueError("Meta detail preloader has invalid locations")

    description_html, sections = _description_html(html, preloader)
    description_text = normalized_body_text(description_html)
    if len(description_text) < 100:
        raise ValueError("Meta detail description is empty or too short")

    ld_date = posting.get("datePosted")
    date_posted = None
    warnings = []
    if not posting:
        warnings.append("source_schema_org_jobposting_absent")
    if isinstance(ld_date, str) and ld_date.strip():
        try:
            from datetime import datetime
            datetime.fromisoformat(ld_date.replace("Z", "+00:00"))
            date_posted = ld_date
        except ValueError:
            warnings.append("source_date_posted_not_iso8601")
    else:
        warnings.append("source_date_posted_absent")

    identifier = posting.get("identifier")
    requisition_id = None
    if isinstance(identifier, dict):
        value = identifier.get("value")
        if isinstance(value, str) and value.strip():
            requisition_id = value.strip()
    employer = posting.get("hiringOrganization")
    if not isinstance(employer, dict) or not employer.get("name"):
        employer = {"name": "Meta"}

    qualifications = {
        "minimum": _items(preloader.get("minimum_qualifications")),
        "preferred": _items(preloader.get("preferred_qualifications")),
        "source_schema_org": posting.get("qualifications"),
    }
    responsibilities = _items(preloader.get("responsibilities"))
    compensation = preloader.get("public_compensation")
    if not isinstance(compensation, list):
        compensation = []
    salary = {
        "public_compensation": compensation,
        "disclaimer": COMPENSATION_DISCLAIMER if compensation else None,
        "show_partial_public_comp_disclaimer": bool(
            preloader.get("show_partial_public_comp_disclaimer")
        ),
    }
    employment_type = posting.get("employmentType")
    if isinstance(employment_type, list):
        employment_type = employment_type[0] if employment_type else None

    raw = {
        "job_id": job_id,
        "preloader": preloader,
        "schema_org_jobposting": posting,
        "schema_org_jobposting_absent": not bool(posting),
        "preloader_sha256": sha256(preloader_raw.encode()).hexdigest(),
        "description_sections": sections,
        "source_field_names": sorted(preloader.keys()),
        "schema_org_field_names": sorted(posting.keys()),
    }
    return {
        "id": job_id,
        "title": title.strip(),
        "locations": locations,
        "departments": [
            str(x) for x in preloader.get("departments") or [] if isinstance(x, str)
        ],
        "internal_departments": [
            str(x) for x in preloader.get("internal_departments") or [] if isinstance(x, str)
        ],
        "description_html": description_html,
        "description_text": description_text,
        "description_method": "metacareers.detail_page.xcp_requisition_job_description",
        "description_source_url": response_url,
        "apply_url": apply_url(job_id),
        "requisition_id": requisition_id,
        "date_posted": date_posted,
        "date_posted_raw": ld_date if isinstance(ld_date, str) else None,
        "employment_type": employment_type,
        "employer": employer,
        "qualifications": qualifications,
        "responsibilities": responsibilities,
        "salary": salary,
        "language": "en",
        "warnings": warnings,
        "raw": raw,
    }


async def capture_listing_browser(*, timeout_ms: int = 90_000, settle_ms: int = 15_000) -> dict:
    """Render Meta's own listing once and capture its unfiltered Relay response."""
    from playwright.async_api import async_playwright

    started = time.perf_counter()
    records = []
    navigations = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        try:
            page = await browser.new_page(viewport={"width": 1400, "height": 1000}, locale="en-US")
            page.on("framenavigated", lambda frame: navigations.append(frame.url)
                    if frame == page.main_frame else None)

            def on_response(response):
                if "/graphql" not in response.url:
                    return
                records.append({
                    "url": response.url,
                    "status": response.status,
                    "content_type": response.headers.get("content-type"),
                    "request_post_data": response.request.post_data,
                })

            page.on("response", on_response)
            def predicate(response):
                return (
                    "/graphql" in response.url
                    and LISTING_FRIENDLY_NAME in (response.request.post_data or "")
                )
            async with page.expect_response(predicate, timeout=timeout_ms) as response_info:
                response = await page.goto(
                    OFFICIAL_ENTRY, wait_until="domcontentloaded", timeout=timeout_ms
                )
            main = await response_info.value
            body = await main.body()
            await page.wait_for_timeout(1_500)
            visible_text = await page.locator("body").inner_text(timeout=timeout_ms)
            rendered_html = await page.content()
            if not body or b"all_jobs" not in body:
                raise RuntimeError(
                    "Meta listing Relay response was captured but did not contain all_jobs"
                )
            return {
                "requested_url": OFFICIAL_ENTRY,
                "final_url": page.url,
                "http_status": response.status if response else None,
                "navigation_urls": navigations,
                "seconds": round(time.perf_counter() - started, 6),
                "graphql_url": main.url,
                "graphql_status": main.status,
                "graphql_content_type": main.headers.get("content-type"),
                "graphql_request_post_data": main.request.post_data,
                "graphql_body": body,
                "graphql_body_sha256": sha256(body).hexdigest(),
                "rendered_html": rendered_html,
                "rendered_html_sha256": sha256(rendered_html.encode()).hexdigest(),
                "visible_text": visible_text,
                "visible_text_sha256": sha256(visible_text.encode()).hexdigest(),
                "visible_item_count": parse_visible_listing_count(visible_text),
                "page_title": await page.title(),
                "graphql_responses": [
                    {
                        "url": row["url"],
                        "status": row["status"],
                        "friendly_name": _form_value(
                            row["request_post_data"], "fb_api_req_friendly_name"
                        ),
                    }
                    for row in records
                ],
            }
        finally:
            await browser.close()


def _form_value(post_data: str | None, key: str) -> str | None:
    from urllib.parse import parse_qs
    values = parse_qs(post_data or "").get(key)
    return values[0] if values else None


class Meta(Adapter):
    VERIFICATION_REVISION = "meta.metacareers-relay-listing+ssr-detail.1"

    def _archive(self, *, kind: str, url: str, body: bytes, request_body=None,
                 status: int = 200, content_type: str | None = None,
                 final_url: str | None = None, extra: dict | None = None):
        output = getattr(self.http, "output", None)
        if not output:
            return None
        import gzip
        from ..models import now

        digest = sha256(body).hexdigest()
        target = output / "responses" / f"{digest}.gz"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(gzip.compress(body))
        record = {
            "url": url,
            "final_url": final_url or url,
            "method": "POST" if request_body is not None else "GET",
            "request_body": request_body,
            "status": status,
            "sha256": digest,
            "content_type": content_type,
            "redirects": [],
            "fetched_at": now(),
            "source_kind": kind,
        }
        record.update(extra or {})
        with (output / "requests.jsonl").open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    async def detail(self, job):
        response = await self.http.get(job.url)
        final_url = str(response.url)
        parsed = parse_detail_page(response.text, final_url)
        if parsed["id"] != job.job_id:
            raise ValueError(
                f"Meta detail ID {parsed['id']} differs from listing ID {job.job_id}"
            )
        if parsed["title"] != job.title:
            job.warnings.append("listing_title_differs_from_detail")
        if set(parsed["locations"]) != set(job.locations or []):
            job.warnings.append("listing_locations_differ_from_detail")
        job.title = parsed["title"]
        job.requisition_id = parsed["requisition_id"]
        job.apply_url = parsed["apply_url"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = parsed["description_method"]
        job.description_source_url = parsed["description_source_url"]
        if parsed["locations"]:
            job.locations = parsed["locations"]
        elif job.locations:
            job.warnings.append("detail_locations_empty_using_listing")
        job.department = parsed["departments"]
        job.team = parsed["internal_departments"]
        job.employment_type = parsed["employment_type"]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.employer = parsed["employer"]
        job.qualifications = parsed["qualifications"]
        job.responsibilities = parsed["responsibilities"]
        job.salary = parsed["salary"]
        job.language = parsed["language"]
        job.warnings.extend(parsed["warnings"])
        job.raw["detail"] = parsed["raw"]
        job.raw["detail_http"] = {
            "request_url": job.url,
            "final_url": final_url,
            "status": response.status_code,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "content_type": response.headers.get("content-type"),
            "fetched_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
        }
        return job

    async def run(self):
        result = self.result
        result.pages = 1
        listing_started = time.perf_counter()
        try:
            capture = await capture_listing_browser()
        except Exception as exc:
            result.errors.append(f"Meta listing capture failed: {type(exc).__name__}: {exc}")
            result.listing_complete = False
            result.metadata["initial_listing"] = {
                "requested_url": OFFICIAL_ENTRY,
                "error": f"{type(exc).__name__}: {exc}",
            }
            return result
        listing_seconds = max(0.0, time.perf_counter() - listing_started)

        listing_body = capture.pop("graphql_body")
        rendered_html = capture.pop("rendered_html")
        self._archive(
            kind="company_listing_graphql",
            url=capture["graphql_url"],
            body=listing_body,
            request_body=capture.get("graphql_request_post_data"),
            status=capture.get("graphql_status") or 200,
            content_type=capture.get("graphql_content_type"),
            extra={"listing_doc_id": LISTING_DOC_ID},
        )
        self._archive(
            kind="company_listing_rendered",
            url=capture["requested_url"],
            body=rendered_html.encode(),
            status=capture.get("http_status") or 200,
            content_type="text/html; charset=utf-8",
            final_url=capture["final_url"],
            extra={"visible_item_count": capture.get("visible_item_count")},
        )
        try:
            payload = json.loads(listing_body)
            listing = parse_listing_payload(payload)
        except Exception as exc:
            result.errors.append(f"Meta listing parse failed: {type(exc).__name__}: {exc}")
            result.listing_complete = False
            result.metadata["initial_listing"] = capture
            return result

        result.metadata["initial_listing"] = {
            key: value for key, value in capture.items()
            if key not in {"graphql_request_post_data"}
        }
        result.metadata["initial_listing"]["visible_item_count"] = capture.get("visible_item_count")
        result.metadata["listing_payload"] = {
            "all_jobs_count": listing["all_jobs_count"],
            "featured_jobs_count": listing["featured_jobs_count"],
            "unique_count": listing["unique_count"],
            "featured_only_ids": listing["featured_only_ids"],
            "issues": listing["issues"],
        }
        if listing["issues"]:
            result.errors.extend(listing["issues"])
        if not listing["rows"]:
            result.errors.append("meta_listing_has_no_jobs")
            result.listing_complete = False
            return result
        visible_count = capture.get("visible_item_count")
        if visible_count is not None and visible_count != listing["all_jobs_count"]:
            result.errors.append(
                f"meta_ui_item_count_{visible_count}_differs_from_all_jobs_{listing['all_jobs_count']}"
            )

        for row in listing["rows"]:
            job_id = str(row["id"])
            job = self.job(
                {"listing": row},
                title=row["title"],
                job_id=job_id,
                requisition_id=None,
                url=detail_url(job_id),
                apply_url=apply_url(job_id),
                locations=list(row.get("locations") or []),
                department=list(row.get("teams") or []),
                team=list(row.get("sub_teams") or []),
            )
            self.add(job)
        result.expected_total = listing["unique_count"]

        detail_started = time.perf_counter()
        result.jobs = await self.hydrate(result.jobs, self.detail)
        detail_seconds = max(0.0, time.perf_counter() - detail_started)

        listing_by_id = {str(row["id"]): row for row in listing["rows"]}
        for job in result.jobs:
            row = listing_by_id.get(str(job.job_id)) or {}
            job.raw["listing"] = row
            listing_title = str(row.get("title") or "").strip()
            listing_locations = list(row.get("locations") or [])
            if listing_title and job.title == listing_title:
                job.warnings = [
                    warning for warning in job.warnings
                    if warning != "listing_title_differs_from_detail"
                ]
            if listing_locations and set(job.locations or []) == set(listing_locations):
                job.warnings = [
                    warning for warning in job.warnings
                    if warning != "listing_locations_differ_from_detail"
                ]
            self.result.validate_job(job)

        detail_ids = {str(job.job_id) for job in result.jobs}
        if detail_ids != set(listing["job_ids"]):
            result.errors.append(
                "meta_collected_job_ids_differ_from_listing "
                f"missing={sorted(set(listing['job_ids']) - detail_ids)} "
                f"extra={sorted(detail_ids - set(listing['job_ids']))}"
            )
        title_mismatches = [
            job.job_id for job in result.jobs
            if "listing_title_differs_from_detail" in job.warnings
        ]
        location_mismatches = [
            job.job_id for job in result.jobs
            if "listing_locations_differ_from_detail" in job.warnings
        ]
        if title_mismatches:
            result.errors.append(
                f"meta_listing_detail_title_mismatches:{len(title_mismatches)}:"
                + ",".join(title_mismatches[:20])
            )
        if location_mismatches:
            result.warnings.append(
                f"meta_listing_detail_location_mismatches:{len(location_mismatches)}:"
                + ",".join(location_mismatches[:20])
            )

        final_started = time.perf_counter()
        try:
            final_capture = await capture_listing_browser()
            final_body = final_capture.pop("graphql_body")
            final_html = final_capture.pop("rendered_html")
            self._archive(
                kind="company_listing_graphql_final",
                url=final_capture["graphql_url"],
                body=final_body,
                request_body=final_capture.get("graphql_request_post_data"),
                status=final_capture.get("graphql_status") or 200,
                content_type=final_capture.get("graphql_content_type"),
                extra={"listing_doc_id": LISTING_DOC_ID, "final_recheck": True},
            )
            self._archive(
                kind="company_listing_rendered_final",
                url=final_capture["requested_url"],
                body=final_html.encode(),
                status=final_capture.get("http_status") or 200,
                content_type="text/html; charset=utf-8",
                final_url=final_capture["final_url"],
                extra={"visible_item_count": final_capture.get("visible_item_count"),
                       "final_recheck": True},
            )
            final_payload = json.loads(final_body)
            final_listing = parse_listing_payload(final_payload)
            result.metadata["final_listing"] = {
                key: value for key, value in final_capture.items()
                if key not in {"graphql_request_post_data"}
            }
            result.metadata["final_listing"]["visible_item_count"] = final_capture.get(
                "visible_item_count"
            )
            final_ids = set(final_listing["job_ids"])
            if final_ids != set(listing["job_ids"]):
                result.errors.append(
                    "meta_final_listing_id_set_differs "
                    f"initial_only={sorted(set(listing['job_ids']) - final_ids)} "
                    f"final_only={sorted(final_ids - set(listing['job_ids']))}"
                )
            if final_capture.get("visible_item_count") != listing["all_jobs_count"]:
                result.errors.append(
                    "meta_final_ui_item_count_differs_from_initial_all_jobs"
                )
        except Exception as exc:
            result.errors.append(
                f"Meta final listing recheck failed: {type(exc).__name__}: {exc}"
            )
            result.metadata["final_listing"] = {
                "requested_url": OFFICIAL_ENTRY,
                "error": f"{type(exc).__name__}: {exc}",
            }
        final_seconds = max(0.0, time.perf_counter() - final_started)

        failed = [
            {"id": job.job_id, "url": job.url, "reason": ",".join(job.warnings)}
            for job in result.jobs if not job.description_verified
        ]
        result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_authority": (
                "Meta's own unfiltered metacareers.com Relay search response "
                "(CareersJobSearchResultsV2DataQuery), rendered in Chromium; one response "
                "returns all All Jobs rows and the UI exposes no pagination controls."
            ),
            "listing_membership_request": {
                "method": "POST",
                "url": GRAPHQL_URL,
                "doc_id": LISTING_DOC_ID,
                "friendly_name": LISTING_FRIENDLY_NAME,
                "variables": {
                    "search_input": {
                        "q": None, "divisions": [], "offices": [], "roles": [],
                        "leadership_levels": [], "saved_jobs": [], "saved_searches": [],
                        "sub_teams": [], "teams": [], "is_leadership": False,
                        "is_remote_only": False, "sort_by_new": False,
                        "results_per_page": None,
                    },
                    "viewasUserID": None,
                    "isLoggedIn": False,
                },
                "filters_applied": False,
            },
            "advertised_jobs_ui": visible_count,
            "all_jobs_count": listing["all_jobs_count"],
            "featured_jobs_count": listing["featured_jobs_count"],
            "featured_only_ids": listing["featured_only_ids"],
            "unique_listing_jobs": listing["unique_count"],
            "detail_method": (
                "Each exact /profile/job_details/<id>/ page's server-rendered "
                "xcp_requisition_job_description object; full sections assembled in "
                "the page's visible order."
            ),
            "apply_url_method": (
                "Observed route reached by the job page's own Apply now button in Chromium: "
                "/profile/create_application/<id>/"
            ),
            "date_policy": (
                "datePosted comes from the detail page schema.org JobPosting; no request "
                "time or template date is used."
            ),
            "listing_complete": False,
            "failed_jobs": failed,
            "job_ids": listing["job_ids"],
            "detail_ids": sorted(detail_ids),
            "title_mismatch_ids": title_mismatches,
            "location_mismatch_ids": location_mismatches,
            "collection_wall_seconds": round(time.perf_counter() - result._started_clock, 6),
        })
        result.timings["listing_phase_seconds"] = round(listing_seconds, 6)
        result.timings["detail_wall_seconds"] = round(detail_seconds, 6)
        result.timings["final_listing_recheck_seconds"] = round(final_seconds, 6)
        result.listing_complete = (
            not result.errors
            and len(result.jobs) == listing["unique_count"]
            and set(detail_ids) == set(listing["job_ids"])
            and all(job.description_verified for job in result.jobs)
        )
        result.metadata["listing_complete"] = result.listing_complete
        return result
