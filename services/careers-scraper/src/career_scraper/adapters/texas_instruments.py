"""Texas Instruments' own careers journey: careers.ti.com -> its Oracle site CX.

The ledger entry https://careers.ti.com/ redirects to TI's Oracle Candidate
Experience (Oracle Recruiting Cloud) site at
https://careers.ti.com/en/sites/CX (tenant edbz.fa.us2.oraclecloud.com, site
number CX).  The rendered "Search Jobs" page
(https://careers.ti.com/en/sites/CX/jobs) shows ``Jobs 905`` and loads 25 cards
per infinite-scroll batch through the same public findReqs request it issues
itself, with ``offset`` 0, 25, ... 900 and no filters applied.  That request's
``requisitionList`` is the only membership authority used here, always scoped to
``siteNumber=CX``.

Listing rows do not contain descriptions, so every listed id is hydrated from
the exact ``ById;Id="<id>",siteNumber=CX`` detail response the job page itself
requests.  The detail page renders these response fields, in this order, as the
role body:

  JOB DESCRIPTION      -> ExternalDescriptionStr
  RESPONSIBILITIES     -> ExternalResponsibilitiesStr (only when non-empty)
  QUALIFICATIONS       -> ExternalQualificationsStr
  ABOUT US             -> CorporateDescriptionStr
  (headerless notice)  -> OrganizationDescriptionStr

The ``JOB INFO`` block and ``Similar Jobs`` widget below the body are job
metadata/widgets, not description; the site-wide EEO/footer is not role text.

No keyword search, facet filter, guessed slug, other Oracle site/tenant or
aggregator is ever queried.  TI's Apply Now button opens
``.../job/<id>/apply/email`` (observed in the rendered page), stored as
apply_url next to the canonical ``.../job/<id>`` job URL.

The JOB INFO block also renders a ``Base Range`` requisition flex field (for
example ``$130,000 - $240,500``, ``€ 1.500 per month`` or ``$33.00 –
$47.50/hr``) and its explanatory ``Base Range Info`` context.  ``salary`` keeps
that published value exactly as written -- no currency, period, amount or
format conversion -- and stays null when no Base Range is published.
"""
from __future__ import annotations

import hashlib
import re
import time
from hashlib import sha256
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter

from ..models import text_html

OFFICIAL_ENTRY = "https://careers.ti.com/"
ENTRY_FINAL_PATH = "/en/sites/CX"
VANITY_HOST = "careers.ti.com"
BOARD_URL = f"https://{VANITY_HOST}{ENTRY_FINAL_PATH}"
JOBS_PAGE_URL = f"{BOARD_URL}/jobs"
ORACLE_HOST = "edbz.fa.us2.oraclecloud.com"
ORACLE_ORIGIN = f"https://{ORACLE_HOST}"
SITE_NUMBER = "CX"
LISTING_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAILS_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
PAGE_SIZE = 25  # the rendered page's own batch size
SORT_BY = "POSTING_DATES_DESC"
FACETS = (
    "WORK_LOCATIONS;WORKPLACE_TYPES;TITLES;CATEGORIES;ORGANIZATIONS;"
    "POSTING_DATES;FLEX_FIELDS;LOCATIONS"
)
EXPAND = (
    "requisitionList.workLocation,requisitionList.otherWorkLocations,"
    "requisitionList.secondaryLocations,flexFieldsFacet.values,"
    "requisitionList.requisitionFlexFields"
)
# Rendered body order of the exact detail fields (label None = no heading).
BODY_FIELDS = (
    ("ExternalDescriptionStr", "JOB DESCRIPTION"),
    ("ExternalResponsibilitiesStr", "RESPONSIBILITIES"),
    ("ExternalQualificationsStr", "QUALIFICATIONS"),
    ("CorporateDescriptionStr", "ABOUT US"),
    ("OrganizationDescriptionStr", None),
)
ROLE_FIELDS = (
    "ExternalDescriptionStr",
    "ExternalResponsibilitiesStr",
    "ExternalQualificationsStr",
)
SALARY_WORDS = ("salary", "compensation", "pay range", "pay rate", "hourly rate")
BASE_RANGE_PROMPT = "Base Range"
BASE_RANGE_INFO_PROMPT = "Base Range Info"
# Listing response fields that the public UI itself changes per request/session.
# They are recorded when they differ but never treated as a membership change.
VOLATILE_LISTING_FIELDS = frozenset({"BeFirstToApplyFlag", "Relevancy"})


def job_page_url(job_id: str) -> str:
    return f"{BOARD_URL}/job/{job_id}"


def apply_page_url(job_id: str) -> str:
    """The route the rendered Apply Now button opens for this exact job."""
    return f"{job_page_url(job_id)}/apply/email"


def normalized_title(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalized_visible_text(value: str) -> str:
    """Whitespace-normalised visible text, matching what the page renders.

    ``text_html`` joins every inline text node with a newline, which leaves a
    space before a period that directly follows a link (``culture .``).  The
    rendered page has none, so that join artefact is removed here; nothing else
    about the text is changed.
    """
    text = re.sub(r"\s+", " ", value or "").strip()
    text = re.sub(r"\s+([,.;:!?)\]])", r"\1", text)
    return re.sub(r"([(\[])\s+", r"\1", text)


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def parse_official_entry(html: str, response_url: str) -> dict:
    """Validate TI's own entry and return the Oracle site it publishes."""
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != VANITY_HOST or parsed.path.rstrip("/") != ENTRY_FINAL_PATH:
        raise ValueError(
            "Texas Instruments official entry did not stay on its inspected careers route "
            f"({OFFICIAL_ENTRY} -> {response_url})"
        )
    soup = BeautifulSoup(html, "lxml")
    base = soup.select_one("base[data-sitenumber]")
    if base is None:
        raise ValueError("TI official entry no longer declares its Oracle Candidate Experience base")
    if base.get("data-sitenumber") != SITE_NUMBER:
        raise ValueError(f"TI official entry site number changed: {base.get('data-sitenumber')}")
    if (base.get("href") or "").rstrip("/") != ENTRY_FINAL_PATH:
        raise ValueError(f"TI official entry base href changed: {base.get('href')}")
    api_base = urlsplit(base.get("data-apibaseurl") or "")
    vanity_base = urlsplit(base.get("data-vanitybaseurl") or "")
    if api_base.hostname != ORACLE_HOST or api_base.scheme != "https":
        raise ValueError(f"TI official entry API tenant changed: {base.get('data-apibaseurl')}")
    if vanity_base.hostname != VANITY_HOST:
        raise ValueError(f"TI official entry vanity host changed: {base.get('data-vanitybaseurl')}")
    og_site = soup.select_one('meta[property="og:site_name"]')
    og_title = soup.select_one('meta[property="og:title"]')
    if og_site is None or og_site.get("content") != "Texas Instruments":
        raise ValueError("TI official entry company identity changed")
    if og_title is None or "Texas Instruments" not in (og_title.get("content") or ""):
        raise ValueError("TI official entry careers title changed")
    return {
        "url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "title": normalized_title(soup.title.get_text(" ", strip=True) if soup.title else ""),
        "og_site_name": og_site.get("content"),
        "og_title": og_title.get("content"),
        "base_href": base.get("href"),
        "site_number": base.get("data-sitenumber"),
        "api_base": base.get("data-apibaseurl"),
        "vanity_base": base.get("data-vanitybaseurl"),
        "jobs_page_url": JOBS_PAGE_URL,
    }


def finder_for(offset: int = 0) -> str:
    finder = (
        f"findReqs;siteNumber={SITE_NUMBER},facetsList={FACETS},"
        f"limit={PAGE_SIZE},sortBy={SORT_BY}"
    )
    return f"{finder},offset={offset}" if offset else finder


def detail_finder(job_id: str) -> str:
    return f'ById;Id="{job_id}",siteNumber={SITE_NUMBER}'


def _facet_values(block: dict, key: str):
    value = block.get(key)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return value.get("values")
    return None


def parse_listing_page(payload: dict, requested_offset: int) -> dict:
    """Validate one unfiltered findReqs page and keep its ordered rows."""
    if not isinstance(payload, dict):
        raise ValueError("TI Oracle listing response is not a JSON object")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError(f"TI Oracle listing response shape changed at offset {requested_offset}")
    block = items[0]
    if not isinstance(block, dict):
        raise ValueError("TI Oracle listing block is not an object")
    if str(block.get("SiteNumber")) != SITE_NUMBER:
        raise ValueError(
            f"TI Oracle listing site mismatch at offset {requested_offset}: {block.get('SiteNumber')}"
        )
    total = block.get("TotalJobsCount")
    rows = block.get("requisitionList")
    if not isinstance(total, int) or total < 0 or not isinstance(rows, list):
        raise ValueError(f"TI Oracle listing pagination fields missing at offset {requested_offset}")
    if len(rows) > PAGE_SIZE:
        raise ValueError(f"TI Oracle listing returned more than the UI page size at offset {requested_offset}")
    if requested_offset == 0 and len(rows) != min(PAGE_SIZE, total):
        raise ValueError(
            f"TI Oracle first listing page returned {len(rows)} rows for total {total}; "
            "the rendered page shows min(25, total)"
        )
    issues = []
    kept = []
    for row in rows:
        if not isinstance(row, dict):
            issues.append("row_not_object")
            continue
        identity = str(row.get("Id") or "")
        if not identity or not normalized_title(row.get("Title")):
            issues.append("row_missing_id_or_title")
            continue
        kept.append(row)
    return {
        "offset": requested_offset,
        "total": total,
        "rows": kept,
        "row_count": len(rows),
        "ids": [str(row["Id"]) for row in kept],
        "issues": issues,
        "facets": {
            "work_locations": _facet_values(block, "workLocationsFacet"),
            "categories": _facet_values(block, "categoriesFacet"),
            "posting_dates": _facet_values(block, "postingDatesFacet"),
            "workplace_types": _facet_values(block, "workplaceTypesFacet"),
        },
    }


def parse_detail(payload: dict, job_id: str) -> dict:
    """Validate the exact ById response for one listed posting."""
    if not isinstance(payload, dict):
        raise ValueError(f"TI Oracle detail for {job_id} is not a JSON object")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError(f"TI Oracle detail for {job_id} did not return exactly one posting")
    row = items[0]
    if str(row.get("Id")) != str(job_id):
        raise ValueError(f"TI Oracle detail returned a different posting ({row.get('Id')}) for {job_id}")
    if not normalized_title(row.get("Title")):
        raise ValueError(f"TI Oracle detail for {job_id} is missing its title")
    missing = [field for field, _ in BODY_FIELDS if field not in row]
    if missing:
        raise ValueError(
            f"TI Oracle detail for {job_id} omitted inspected body field(s) {missing}; "
            "schema drift requires manual review, not an unavailable verdict"
        )
    return row


def description_sections(detail: dict) -> list[dict]:
    """Keep each rendered employer section with its exact source HTML and text."""
    sections = []
    for field, heading in BODY_FIELDS:
        html = detail.get(field) or ""
        if not isinstance(html, str) or not html.strip():
            continue
        text = normalized_visible_text(text_html(html))
        if not text:
            continue
        sections.append({
            "field": field,
            "heading": heading,
            "html": html,
            "text": text,
            "normalized_chars": len(text),
        })
    return sections


def build_description_html(sections: list[dict]) -> str:
    parts = []
    for section in sections:
        heading = section["heading"]
        label = f"<h2>{heading}</h2>" if heading else ""
        parts.append(f"<section>{label}{section['html']}</section>")
    return "\n".join(parts)


def _location_name(item) -> str | None:
    if isinstance(item, dict):
        value = item.get("LocationName") or item.get("Name") or item.get("TownOrCity")
        return value if isinstance(value, str) and value.strip() else None
    if isinstance(item, str) and item.strip():
        return item
    return None


def location_values(detail: dict) -> list:
    values: list = []
    primary = detail.get("PrimaryLocation")
    if isinstance(primary, str) and primary.strip():
        values.append(primary.strip())
    for key in ("secondaryLocations", "otherWorkLocations"):
        for item in detail.get(key) or []:
            value = _location_name(item)
            if value:
                values.append(value)
    if not values:
        for item in detail.get("workLocation") or []:
            value = _location_name(item)
            if value:
                values.append(value)
    return list(dict.fromkeys(values))


def location_addresses(detail: dict) -> list:
    """Exact published address records from the detail row (never invented)."""
    rows = []
    for item in detail.get("workLocation") or []:
        if isinstance(item, dict):
            rows.append({
                key: item.get(key)
                for key in ("LocationId", "LocationName", "AddressLine1", "AddressLine2",
                            "TownOrCity", "Region1", "Region2", "CountryCode", "PostalCode")
            })
    return rows


def flex_fields(detail: dict) -> list:
    values = []
    for field in detail.get("requisitionFlexFields") or []:
        if not isinstance(field, dict):
            continue
        prompt = str(field.get("Prompt") or field.get("Name") or "").strip()
        if not prompt:
            continue
        values.append({"prompt": prompt, "value": field.get("Value"),
                       "control_type": field.get("ControlType")})
    return values


def _normalized_prompt(field: dict) -> str:
    return re.sub(r"\s+", " ", str(field.get("prompt") or "")).strip().casefold()


def salary_fields(detail: dict) -> str | None:
    """Return the exact published ``Base Range`` string, or None.

    The JOB INFO block renders this requisition flex field (USD annual ranges,
    EUR monthly intern ranges, hourly rates and ranges without a currency
    prefix have all been observed).  The value is returned exactly as
    published: no currency, period, amount or whitespace conversion.
    ``Base Range Info`` is the publisher's explanatory context and is kept by
    :func:`salary_context` and in ``raw.detail_derived``; it is never mixed
    into the salary value.  A generic salary/compensation/pay prompt remains a
    fallback if TI ever publishes one without ``Base Range``.
    """
    fallback = None
    for field in flex_fields(detail):
        prompt = _normalized_prompt(field)
        value = field["value"]
        if not isinstance(value, str) or not value.strip():
            continue
        if prompt == BASE_RANGE_PROMPT.casefold():
            return value
        if prompt != BASE_RANGE_INFO_PROMPT.casefold() and fallback is None and any(
            word in prompt for word in SALARY_WORDS
        ):
            fallback = value
    return fallback


def salary_context(detail: dict) -> str | None:
    """Return the published ``Base Range Info`` explanation, if any."""
    for field in flex_fields(detail):
        if _normalized_prompt(field) == BASE_RANGE_INFO_PROMPT.casefold():
            value = field["value"]
            if isinstance(value, str) and value.strip():
                return value
    return None


def salary_evidence(detail: dict) -> list | None:
    """Keep the exact Base Range/Info flex-field records as audit evidence."""
    wanted = {BASE_RANGE_PROMPT.casefold(), BASE_RANGE_INFO_PROMPT.casefold()}
    values = [field for field in flex_fields(detail) if _normalized_prompt(field) in wanted]
    return values or None


def _api_params(offset: int = 0) -> dict:
    return {"onlyData": "true", "expand": EXPAND, "finder": finder_for(offset)}


def _detail_params(job_id: str) -> dict:
    return {"onlyData": "true", "expand": "all", "finder": detail_finder(job_id)}


class TexasInstruments(Adapter):
    """Collect only the membership TI's own Oracle site CX publishes."""

    VERIFICATION_REVISION = "texas-instruments.official-careers-oracle-cx.2"
    PAGE_SIZE = PAGE_SIZE

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._rate_limited = False
        self._rate_limit_logged = False

    # ---------------------------------------------------------------- entry
    async def _entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        entry = parse_official_entry(response.text, str(response.url))
        entry.update(
            status=response.status_code,
            seconds=seconds,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
            redirect_chain=[str(item.url) for item in response.history],
        )
        return entry

    # --------------------------------------------------------------- listing
    async def _listing_page(self, offset: int):
        started = time.perf_counter()
        response = await self.http.request("GET", f"{ORACLE_ORIGIN}{LISTING_PATH}", params=_api_params(offset))
        seconds = max(0.0, time.perf_counter() - started)
        parts = urlsplit(str(response.url))
        if parts.scheme != "https" or parts.netloc != ORACLE_HOST or parts.path != LISTING_PATH:
            raise ValueError("TI Oracle listing request redirected away from TI's tenant route")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"TI Oracle listing page at offset {offset} was not JSON") from exc
        page = parse_listing_page(payload, offset)
        page.update(
            request_url=str(response.request.url),
            final_url=str(response.url),
            status=response.status_code,
            seconds=seconds,
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
        )
        return page

    async def _listing_snapshot(self, *, label: str):
        started = time.perf_counter()
        pages, rows, ids, issues = [], [], [], []
        total = None
        offset = 0
        while self.page_guard():
            page = await self._listing_page(offset)
            if total is None:
                total = page["total"]
            elif page["total"] != total:
                issues.append(f"total_changed:{total}->{page['total']}")
            issues.extend(f"{issue}@{offset}" for issue in page["issues"])
            if not page["rows"]:
                issues.append(f"empty_page_at_offset:{offset}")
                pages.append({key: value for key, value in page.items() if key != "facets"})
                break
            if len(ids) and page["ids"][0] and ids[-1] == page["ids"][0]:
                issues.append("page_overlap")
            for row in page["rows"]:
                identity = str(row["Id"])
                if identity in set(ids):
                    issues.append(f"duplicate_id:{identity}")
                    continue
                ids.append(identity)
                rows.append(row)
            pages.append({key: value for key, value in page.items() if key != "facets"})
            offset += page["row_count"]
            if total is not None and len(ids) >= total:
                break
            if page["row_count"] < PAGE_SIZE:
                issues.append("short_page_before_reported_total")
                break
        seconds = max(0.0, time.perf_counter() - started)
        return {
            "label": label,
            "total": total,
            "ids": ids,
            "rows": rows,
            "pages": pages,
            "issues": list(dict.fromkeys(issues)),
            "page_count": len(pages),
            "seconds": seconds,
            "facets": pages[0].get("facets") if pages else None,
        }

    # ---------------------------------------------------------------- detail
    async def _fetch_detail(self, job_id: str):
        if self._rate_limited:
            raise _RateLimited("TI detail requests stopped after HTTP 429")
        started = time.perf_counter()
        response = await self.http.request("GET", f"{ORACLE_ORIGIN}{DETAILS_PATH}", params=_detail_params(job_id))
        seconds = max(0.0, time.perf_counter() - started)
        parts = urlsplit(str(response.url))
        if parts.scheme != "https" or parts.netloc != ORACLE_HOST or parts.path != DETAILS_PATH:
            raise ValueError(f"TI Oracle detail for {job_id} redirected away from TI's tenant route")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"TI Oracle detail for {job_id} was not JSON") from exc
        row = parse_detail(payload, job_id)
        return {
            "row": row,
            "request_url": str(response.request.url),
            "final_url": str(response.url),
            "status": response.status_code,
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        }

    def _job_from_row(self, row: dict):
        job_id = str(row["Id"])
        listing = dict(row)
        job = self.job(
            {"listing": listing, "listing_observed_at": None},
            title=normalized_title(row.get("Title")),
            job_id=job_id,
            url=job_page_url(job_id),
            apply_url=apply_page_url(job_id),
            date_posted=row.get("PostedDate"),
            date_posted_raw={"listing_PostedDate": row.get("PostedDate")},
            valid_through=row.get("PostingEndDate"),
            locations=location_values(row),
            department=row.get("Category"),
            team=row.get("JobFunction") or row.get("JobFamily"),
            employment_type=row.get("RequisitionType") or row.get("JobType"),
            workplace_type=row.get("WorkplaceType") or row.get("WorkplaceTypeCode"),
            salary=None,
            employer=row.get("LegalEmployer"),
            language=row.get("Language"),
            description="",
        )
        job.record_identity = f"texas-instruments:{job_id}"
        return job

    # ---------------------------------------------------------------- run
    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "entry_final_url": f"https://{VANITY_HOST}{ENTRY_FINAL_PATH}",
            "listing_url": JOBS_PAGE_URL,
            "oracle_tenant": ORACLE_ORIGIN,
            "site_number": SITE_NUMBER,
            "listing_endpoint": f"{ORACLE_ORIGIN}{LISTING_PATH}",
            "detail_endpoint": f"{ORACLE_ORIGIN}{DETAILS_PATH}",
            "listing_request_shape": {
                "method": "GET",
                "onlyData": "true",
                "expand": EXPAND,
                "finder": finder_for(0),
                "page_size": PAGE_SIZE,
                "sort": SORT_BY,
                "filters_applied": False,
                "pagination": "offset increments by 25 (the rendered jobs page's own infinite-scroll batch size)",
            },
            "description_source": (
                "Exact ById detail response fields in the order the job page renders them: "
                "ExternalDescriptionStr (JOB DESCRIPTION), ExternalResponsibilitiesStr "
                "(RESPONSIBILITIES), ExternalQualificationsStr (QUALIFICATIONS), "
                "CorporateDescriptionStr (ABOUT US), OrganizationDescriptionStr (headerless notice)."
            ),
            "description_boundary": (
                "JOB INFO metadata block, Similar Jobs widget and the site-wide EEO/footer are excluded."
            ),
            "apply_url_policy": (
                "The rendered Apply Now button opens .../job/<id>/apply/email; that route is stored as "
                "apply_url while the canonical .../job/<id> page stays the job url."
            ),
            "salary_source": (
                "requisitionFlexFields Prompt='Base Range', rendered in the JOB INFO block; the value is "
                "stored exactly as published with no currency/period/amount conversion."
            ),
            "salary_context_source": (
                "requisitionFlexFields Prompt='Base Range Info' is retained as the publisher's context in "
                "raw.detail_derived.salary_context and salary_fields evidence."
            ),
            "request_scope": (
                "Only siteNumber=CX on TI's Oracle tenant, driven by TI's own entry/jobs page; "
                "no keyword search, facet filter, other site or tenant, guessed slug or aggregator."
            ),
        })
        try:
            entry = await self._entry()
        except Exception as exc:
            self.result.errors.append(f"Texas Instruments official entry failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result
        self.result.metadata["official_entry"] = entry

        try:
            initial = await self._listing_snapshot(label="initial")
        except Exception as exc:
            self.result.errors.append(f"Texas Instruments listing failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.expected_total = initial["total"]
        self.result.metadata["initial_listing"] = {key: value for key, value in initial.items() if key != "rows"}
        self.result.metadata["initial_facets"] = initial["facets"]
        self.result.errors.extend(initial["issues"])
        if initial["total"] is None:
            self.result.errors.append("texas_instruments_listing_total_unknown")
        elif len(initial["ids"]) != initial["total"]:
            self.result.errors.append(
                f"texas_instruments_membership_unreconciled:sourcereported={initial['total']}"
                f",enumerated={len(initial['ids'])}"
            )

        jobs = []
        for row in initial["rows"]:
            job = self._job_from_row(row)
            job.company_scope_verified = self.company.scope == "company"
            jobs.append(job)
        self.result.jobs = jobs

        # ------------------------------------------------------------- details
        detail_started = time.perf_counter()
        rate_limit_events = []

        async def detail(job):
            try:
                payload = await self._fetch_detail(job.job_id)
            except _RateLimited:
                job.warnings.append("rate_limited_stop")
                job.description_unavailable = True
                return job
            except Exception as exc:
                if "429" in str(exc):
                    self._rate_limited = True
                    rate_limit_events.append({"job_id": job.job_id, "error": str(exc)[:200]})
                    job.warnings.append("rate_limited_stop")
                    job.description_unavailable = True
                    return job
                raise
            row = payload["row"]
            if normalized_title(row.get("Title")) != job.title:
                self.result.errors.append(f"texas_instruments_detail_title_mismatch:{job.job_id}")
            sections = description_sections(row)
            if not sections:
                job.description_unavailable = True
                job.warnings.extend(["source_description_missing", "description_not_full_field"])
                self.result.errors.append(f"texas_instruments_detail_has_no_body:{job.job_id}")
            job.raw["detail"] = row
            job.raw["detail_response"] = {
                "request_url": payload["request_url"],
                "final_url": payload["final_url"],
                "status": payload["status"],
                "seconds": round(payload["seconds"], 6),
                "bytes": payload["response_bytes"],
                "sha256": payload["response_sha256"],
            }
            job.title = normalized_title(row.get("Title")) or job.title
            job.requisition_id = str(row["RequisitionId"]) if row.get("RequisitionId") else None
            job.date_posted = row.get("ExternalPostedStartDate") or job.date_posted
            job.date_posted_raw = {
                "listing_PostedDate": (job.raw.get("listing") or {}).get("PostedDate"),
                "detail_ExternalPostedStartDate": row.get("ExternalPostedStartDate"),
            }
            job.valid_through = row.get("ExternalPostedEndDate")
            job.locations = location_values(row) or job.locations
            job.department = row.get("Category") or job.department
            job.team = row.get("JobFunction") or row.get("JobFamily") or job.team
            job.employment_type = (
                row.get("RequisitionType") or row.get("JobType") or row.get("WorkerType") or job.employment_type
            )
            job.workplace_type = (
                row.get("WorkplaceType") or row.get("WorkplaceTypeCode") or job.workplace_type
            )
            job.salary = salary_fields(row)
            job.qualifications = [s for s in sections if s["field"] == "ExternalQualificationsStr"] or None
            job.responsibilities = [s for s in sections if s["field"] == "ExternalResponsibilitiesStr"] or None
            job.education = row.get("StudyLevel")
            job.employer = row.get("LegalEmployer")
            job.language = row.get("ContentLocale") or job.language
            job.description_html = build_description_html(sections)
            job.description_text = normalized_visible_text(text_html(job.description_html))
            job.description_source_url = payload["request_url"]
            job.description_method = "texas_instruments.oracle_ce.rendered_external_sections"
            if job.raw["detail"].get("media"):
                self.result.metadata.setdefault("roles_with_media", []).append(job.job_id)
            job.raw["detail_sections"] = [
                {key: value for key, value in section.items() if key != "html"} for section in sections
            ]
            job.raw["detail_derived"] = {
                "work_location_addresses": location_addresses(row),
                "flex_fields": flex_fields(row),
                "salary": salary_fields(row),
                "salary_context": salary_context(row),
                "salary_fields": salary_evidence(row),
                "description_sha256": hashlib.sha256(job.description_html.encode()).hexdigest(),
                "description_normalized_chars": len(job.description_text),
                "description_fnv1a64": fnv1a64(job.description_text),
            }
            return job

        if jobs:
            self.result.jobs = await self.hydrate(jobs, detail)
        self.result.timings["detail_wall_seconds"] = round(max(0.0, time.perf_counter() - detail_started), 6)
        self.result.metadata["detail_rate_limit_events"] = rate_limit_events
        if rate_limit_events:
            self.result.errors.append(f"texas_instruments_detail_rate_limited:{len(rate_limit_events)}")
            self.result.listing_complete = False

        # ------------------------------------------------------------- final check
        final = None
        final_entry = None
        try:
            final_entry = await self._entry()
            final = await self._listing_snapshot(label="final")
        except Exception as exc:
            self.result.errors.append(f"Texas Instruments final source recheck failed: {type(exc).__name__}: {exc}")
        if final is not None and final_entry is not None:
            self.result.metadata["final_listing"] = {key: value for key, value in final.items() if key != "rows"}
            self.result.metadata["final_official_entry"] = final_entry
            initial_rows = {str(row["Id"]): row for row in initial["rows"]}
            final_rows = {str(row["Id"]): row for row in final["rows"]}
            changed, volatile_changed = {}, {}
            for identity in initial["ids"]:
                if identity not in final_rows or initial_rows.get(identity) == final_rows[identity]:
                    continue
                fields = sorted(
                    key for key in set(initial_rows.get(identity, {})) | set(final_rows[identity])
                    if initial_rows.get(identity, {}).get(key) != final_rows[identity].get(key)
                )
                stable = [key for key in fields if key not in VOLATILE_LISTING_FIELDS]
                if stable:
                    changed[identity] = stable
                if len(stable) != len(fields):
                    volatile_changed[identity] = fields
            same_order = initial["ids"] == final["ids"]
            same_total = initial["total"] == final["total"]
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["ids"],
                "final_ids": final["ids"],
                "initial_total": initial["total"],
                "final_total": final["total"],
                "same_ids_and_order": same_order,
                "same_total": same_total,
                "new_ids": [i for i in final["ids"] if i not in set(initial["ids"])],
                "removed_ids": [i for i in initial["ids"] if i not in set(final["ids"])],
                "changed_fields_by_id": changed,
                "volatile_fields_changed_by_id": volatile_changed,
                "volatile_fields_ignored": sorted(VOLATILE_LISTING_FIELDS),
                "entry_still_exact": final_entry.get("site_number") == SITE_NUMBER,
                "final_issues": final["issues"],
            }
            if not same_order or not same_total:
                self.result.errors.append("texas_instruments_listing_membership_changed_during_collection")
            if changed:
                self.result.errors.append("texas_instruments_listing_fields_changed_during_collection")
            self.result.errors.extend(final["issues"])
        else:
            self.result.errors.append("texas_instruments_final_membership_unverified")

        collected = {str(job.job_id) for job in self.result.jobs}
        missing = [identity for identity in initial["ids"] if identity not in collected]
        if missing:
            self.result.errors.append(f"texas_instruments_listed_but_unretained:{missing[:10]}")
        verified = sum(job.description_verified for job in self.result.jobs)
        self.result.metadata["job_ids"] = [str(job.job_id) for job in self.result.jobs]
        self.result.metadata["source_total"] = initial["total"]
        self.result.metadata["enumerated_ids"] = len(initial["ids"])
        self.result.metadata["descriptions_verified"] = verified
        self.result.listing_complete = bool(
            final is not None
            and initial["total"] is not None
            and len(initial["ids"]) == initial["total"]
            and len(self.result.jobs) == initial["total"]
            and verified == len(self.result.jobs)
            and not rate_limit_events
            and not self.result.errors
        )
        self.result.timings["source_collection_wall_seconds"] = round(
            max(0.0, time.perf_counter() - source_started), 6
        )
        return self.result


class _RateLimited(RuntimeError):
    pass
