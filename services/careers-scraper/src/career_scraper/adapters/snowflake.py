"""Snowflake's own Phenom careers search and native job-detail widgets.

The official entry (``careers.snowflake.com/us/en/search-results``) embeds a
global, unfiltered ``refineSearch`` listing.  Its HTML pagination exposes ten
roles per page, while the same public widget request accepts a larger page for
membership reconciliation.  The official ``us/en/sitemap.xml`` publishes the
exact canonical detail URLs for the same ``jobSeqNo`` set.

Full descriptions come from the site's own ``jobDetail`` widget request for
each exact listed ``jobSeqNo``.  Net-new Snowflake roles are applied for on
Ashby, but this adapter never enumerates the Ashby board: the Snowflake
listing is the membership authority and the Ashby URL is only the exposed
apply destination for one listed role.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import text_html
from ..structured import embedded_page_object


OFFICIAL_ENTRY = "https://careers.snowflake.com/us/en/search-results"
OFFICIAL_REDIRECT = "https://www.snowflake.com/careers/"
BASE_URL = "https://careers.snowflake.com"
SITEMAP_URL = BASE_URL + "/us/en/sitemap.xml"
WIDGET_ENDPOINT = BASE_URL + "/widgets"
REF_NUM = "SNCOUS"
SITE_TYPE = "external"
SEARCH_PAGE_ID = "page11-ds"
JOB_PAGE_ID = "page407-prod"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
LISTING_FIELDS = frozenset(
    """
    applyUrl category city cityState cityStateCountry companyName country
    dateCreated department descriptionTeaser externalApply grade industry
    isMultiCategory isMultiLocation jobId jobSeqNo jobVisibility locale location
    multi_category multi_category_array multi_location multi_location_array
    postedDate refNum remote reqId siteType state type visibilityType
    """.split()
)
# Fields that are generated summaries, duplicate description/test switches,
# internal personnel, hashes or implementation bookkeeping.  They are not
# published role fields and are omitted from retained evidence.
DENY_KEYS = frozenset(
    """
    ai_summary descriptionTeaser fesHash gradeValue hashKey hiringManagerName
    ifelseresult jobLevelValue ml_Description ml_country ml_highlight
    ml_job_overview ml_job_parser ml_language_fullform ml_skills ml_title sample
    substringField switch2result switch3 switch4result tetssts topic uniqueValue
    workerSubTypeValue
    """.split()
)
TITLE_WS = re.compile(r"\s+")


def normalized_title(value: str | None) -> str:
    return TITLE_WS.sub(" ", (value or "").strip())


def clean_row(row: dict) -> dict:
    """Retain public role fields, dropping generated/test/internal fields."""
    return {
        key: value
        for key, value in row.items()
        if key not in DENY_KEYS and not key.startswith("ml_") and not key.startswith("emb_")
    }


def parse_entry(html: str) -> dict:
    """Read the inspected Phenom entry config, session and eager first page."""
    config = embedded_page_object(html, "var phApp = phApp || ")
    session = embedded_page_object(html, "phApp.sessionParams = ")
    ddo = embedded_page_object(html, "phApp.ddo = ")
    if (
        config.get("widgetApiEndpoint") != WIDGET_ENDPOINT
        or config.get("refNum") != REF_NUM
        or config.get("siteType") != SITE_TYPE
        or config.get("baseUrl") != BASE_URL + "/us/en/"
        or config.get("pageName") != "search-results"
        or config.get("locale") != "en_us"
        or config.get("country") != "us"
        or config.get("deviceType") != "desktop"
    ):
        raise ValueError("Snowflake official careers configuration changed")
    token = session.get("csrfToken")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Snowflake official careers CSRF session token is missing")
    eager = ddo.get("eagerLoadRefineSearch")
    if not isinstance(eager, dict):
        raise ValueError("Snowflake official entry eager listing is missing")
    return {"config": config, "session": session, "ddo": ddo, "eager": eager}


def parse_sitemap(xml: str) -> list[dict]:
    """Parse the official job URLs from Snowflake's own sitemap."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ValueError("Snowflake official sitemap is not valid XML") from exc
    rows: list[dict] = []
    seen: set[str] = set()
    for node in root.iter():
        if not node.tag.endswith("loc") or not node.text:
            continue
        url = node.text.strip()
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "careers.snowflake.com":
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 5 or parts[:3] != ["us", "en", "job"]:
            continue
        sequence, slug = parts[3], parts[4]
        if not sequence or not slug or sequence in seen:
            raise ValueError("Snowflake official sitemap repeats or omits a jobSeqNo")
        seen.add(sequence)
        rows.append({"jobSeqNo": sequence, "slug": slug, "url": url})
    if not rows:
        raise ValueError("Snowflake official sitemap contains no job URLs")
    return rows


def listing_body(config: dict, *, size: int, start: int) -> dict:
    return {
        "lang": config["locale"],
        "deviceType": config["deviceType"],
        "country": config["country"],
        "pageName": "search-results",
        "ddoKey": "refineSearch",
        "sortBy": "",
        "subsearch": "",
        "from": start,
        "irs": False,
        "jobs": True,
        "counts": True,
        "all_fields": ["category", "region", "location", "remote", "jobLevel"],
        "size": size,
        "clearAll": False,
        "jdsource": "facets",
        "isSliderEnable": False,
        "pageId": SEARCH_PAGE_ID,
        "siteType": config["siteType"],
        "keywords": "",
        "global": True,
        "selected_fields": {},
        "locationData": {},
    }


def detail_body(config: dict, job_seq_no: str) -> dict:
    return {
        "lang": config["locale"],
        "deviceType": config["deviceType"],
        "country": config["country"],
        "pageName": "job",
        "ddoKey": "jobDetail",
        "jobSeqNo": job_seq_no,
        "siteType": config["siteType"],
        "pageId": JOB_PAGE_ID,
    }


def parse_listing(value: dict, *, requested_size: int, requested_from: int) -> tuple[list[dict], int]:
    """Validate one observed ``refineSearch`` widget page."""
    if not isinstance(value, dict):
        raise ValueError("Snowflake refineSearch response is not an object")
    search = value.get("refineSearch")
    if not isinstance(search, dict):
        raise ValueError("Snowflake refineSearch response has no refineSearch object")
    data = search.get("data")
    rows = data.get("jobs") if isinstance(data, dict) else None
    total = search.get("totalHits")
    hits = search.get("hits")
    if (
        search.get("status") != 200
        or not isinstance(total, int)
        or total < 0
        or not isinstance(hits, int)
        or not isinstance(rows, list)
        or hits != len(rows)
        or len(rows) > requested_size
        or (requested_from and requested_from >= total)
        or (requested_from == 0 and total < len(rows))
    ):
        raise ValueError("Snowflake refineSearch pagination schema changed")
    if data.get("ui_selections") not in (None, {}) or data.get("locationData") not in (None, {}):
        raise ValueError("Snowflake refineSearch returned an unexpected filter state")
    seen_sequences: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Snowflake refineSearch row is not an object")
        sequence = row.get("jobSeqNo")
        if not sequence or not row.get("jobId") or sequence in seen_sequences:
            raise ValueError("Snowflake refineSearch posting identity missing or repeated")
        if not normalized_title(row.get("title")) or not row.get("reqId"):
            raise ValueError("Snowflake refineSearch posting title/requisition missing")
        seen_sequences.add(sequence)
    return [clean_row(row) for row in rows], total


def parse_detail(value: dict, listing_row: dict) -> dict:
    """Validate one observed ``jobDetail`` widget response against its listing row."""
    if not isinstance(value, dict):
        raise ValueError("Snowflake jobDetail response is not an object")
    detail = value.get("jobDetail")
    data = detail.get("data", {}).get("job") if isinstance(detail, dict) else None
    if (
        not isinstance(detail, dict)
        or detail.get("status") != 200
        or detail.get("hits") != 1
        or detail.get("totalHits") != 1
        or not isinstance(data, dict)
    ):
        raise ValueError("Snowflake jobDetail response is missing its single job")
    if (
        data.get("jobSeqNo") != listing_row.get("jobSeqNo")
        or data.get("jobId") != listing_row.get("jobId")
        or data.get("refNum") != REF_NUM
        or data.get("siteType") != SITE_TYPE
        or str(data.get("companyName", "")).casefold() != "snowflake"
        or normalized_title(data.get("title")) != normalized_title(listing_row.get("title"))
    ):
        raise ValueError("Snowflake jobDetail identity does not match its listing row")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Snowflake full native description field is missing")
    return clean_row(data)


def attachment_links(description_html: str) -> list[dict]:
    """Return role-bearing attachment URLs published in a description."""
    soup = BeautifulSoup(description_html or "", "lxml")
    links = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if re.search(r"\.(?:pdf|docx?|rtf)(?:[?#]|$)", href, re.I):
            links.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return links


def native_job_url(sequence: str, title: str) -> str:
    """Fallback canonical route; the sitemap supplies the exact published slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", normalized_title(title).casefold()).strip("-")
    if not sequence:
        raise ValueError("Snowflake native job URL requires a jobSeqNo")
    return f"{BASE_URL}/us/en/job/{sequence}/{slug}" if slug else f"{BASE_URL}/us/en/job/{sequence}"


def salary_fields(data: dict) -> dict | None:
    ranges = ("displayRangeBase", "displayRangeOte", "minBase", "maxBase", "minOte", "maxOte")
    if not any(data.get(key) not in (None, "") for key in ranges):
        return None
    keys = ranges + ("currencyConstant", "period", "paySplit")
    return {key: data.get(key) for key in keys if data.get(key) not in (None, "")}


class Snowflake(Adapter):
    VERIFICATION_REVISION = "snowflake.native-phenom-refine-and-jobdetail.1"

    async def listing(self, *, size: int, start: int, csrf: str) -> tuple[list[dict], int]:
        if not self.page_guard():
            raise ValueError("Snowflake listing pagination budget exhausted")
        body = listing_body(self.config, size=size, start=start)
        response = await self.http.json(
            WIDGET_ENDPOINT,
            method="POST",
            json=body,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Origin": BASE_URL,
                "Referer": self.search_entry,
                "x-csrf-token": csrf,
            },
        )
        rows, total = parse_listing(response, requested_size=size, requested_from=start)
        return rows, total

    async def detail(self, job, csrf: str):
        body = detail_body(self.config, job.job_id)
        response = await self.http.json(
            WIDGET_ENDPOINT,
            method="POST",
            json=body,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Origin": BASE_URL,
                "Referer": self.search_entry,
                "x-csrf-token": csrf,
            },
        )
        data = parse_detail(response, job.raw["listing"])
        description = data["description"]
        body_text = text_html(description)
        links = attachment_links(description)
        job.raw["detail"] = data
        job.raw["native_detail_url"] = job.url
        job.raw["detail_request_url"] = WIDGET_ENDPOINT
        job.raw["attachment_links"] = links
        if links:
            job.warnings.append("role_attachment_links_present")
        job.description_html = description
        job.description_text = body_text.strip()
        job.description_method = "snowflake.native_widgets.jobDetail.description"
        job.description_source_url = job.url
        job.requisition_id = data.get("reqId") or data.get("customJobReqId") or job.requisition_id
        multi = data.get("multi_location")
        if isinstance(multi, list) and multi:
            job.locations = multi
        elif data.get("location"):
            job.locations = [{"location": data.get("location"), "country": data.get("country")}]
        else:
            job.locations = job.locations or []
        job.department = data.get("category") or data.get("department") or job.department
        job.employment_type = data.get("type") or data.get("hiringType") or job.employment_type
        job.workplace_type = data.get("workerType") or data.get("remote") or job.workplace_type
        job.experience = data.get("jobLevel")
        job.date_posted = data.get("postedDate") or job.date_posted
        job.date_posted_raw = data.get("postedDate") or job.date_posted_raw
        job.date_updated = data.get("jobUpdatedDate") or None
        job.salary = salary_fields(data)
        job.employer = data.get("companyName") or job.employer
        job.language = data.get("locale") or job.language
        job.apply_url = data.get("applyUrl") or job.apply_url
        if len(body_text.strip()) < 100:
            job.description_unavailable = True
            job.warnings.append("upstream_short_description")
        listing_posted = job.raw["listing"].get("postedDate")
        if listing_posted and data.get("postedDate") and listing_posted != data.get("postedDate"):
            job.warnings.append("posting_date_changed_between_listing_and_detail")
        listing_apply = job.raw["listing"].get("applyUrl")
        if listing_apply and data.get("applyUrl") and listing_apply.casefold() != data["applyUrl"].casefold():
            job.warnings.append("apply_url_changed_between_listing_and_detail")
        return job

    def add_listing_job(self, row: dict, sitemap_row: dict | None):
        url = sitemap_row["url"] if sitemap_row else native_job_url(row["jobSeqNo"], row["title"])
        job = self.job(
            {"listing": row, "sitemap_url": sitemap_row["url"] if sitemap_row else None},
            title=normalized_title(row["title"]),
            job_id=row["jobSeqNo"],
            url=url,
            date_posted=row.get("postedDate"),
        )
        job.requisition_id = row.get("reqId")
        job.apply_url = row.get("applyUrl")
        job.department = row.get("category")
        job.employment_type = row.get("type")
        job.locations = row.get("multi_location") or (
            [{"location": row.get("location"), "country": row.get("country")}]
            if row.get("location") else []
        )
        job.employer = row.get("companyName")
        job.language = row.get("locale")
        job.date_posted_raw = row.get("postedDate")
        return job

    async def paginate(self, *, size: int, csrf: str, label: str):
        """Read every page at the observed UI size, reconciling by jobSeqNo."""
        rows_by_sequence: dict[str, dict] = {}
        pages: list[dict] = []
        total = None
        start = 0
        while total is None or start < total:
            rows, page_total = await self.listing(size=size, start=start, csrf=csrf)
            if total is None:
                total = page_total
            elif page_total != total:
                self.result.errors.append(f"snowflake_listing_total_changed_during_{label}")
            page = {
                "label": label, "from": start, "hits": len(rows),
                "totalHits": page_total,
                "job_ids": [row["jobSeqNo"] for row in rows],
            }
            pages.append(page)
            for row in rows:
                sequence = row["jobSeqNo"]
                if sequence in rows_by_sequence:
                    self.result.errors.append(
                        f"snowflake_listing_repeated_jobSeqNo:{sequence}:{label}"
                    )
                    continue
                rows_by_sequence[sequence] = row
            if not rows and len(rows_by_sequence) < (total or 0):
                self.result.errors.append(f"snowflake_listing_ended_early_{label}")
                break
            start += len(rows)
        return rows_by_sequence, total or 0, pages

    async def run(self):
        started = time.monotonic()
        entry = await self.http.get(
            self.company.careers_url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )
        self.search_entry = str(entry.url)
        parsed = urlsplit(self.search_entry)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "careers.snowflake.com"
            or parsed.path.rstrip("/") != "/us/en/search-results"
        ):
            raise ValueError("Snowflake flow must start from its official HTTPS /us/en/search-results entry")
        entry_data = parse_entry(entry.text)
        self.config = entry_data["config"]
        csrf = entry_data["session"]["csrfToken"]
        eager = entry_data["eager"]
        eager_rows = eager.get("data", {}).get("jobs", []) if isinstance(eager.get("data"), dict) else []
        eager_ids = [row.get("jobSeqNo") for row in eager_rows if isinstance(row, dict)]
        if (
            eager.get("status") != 200
            or not isinstance(eager.get("totalHits"), int)
            or eager.get("hits") != len(eager_rows)
            or eager.get("totalHits") < len(eager_rows)
        ):
            raise ValueError("Snowflake official entry eager listing schema changed")

        sitemap_response = await self.http.get(
            SITEMAP_URL,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )
        sitemap_rows = parse_sitemap(sitemap_response.text)
        sitemap_by_sequence = {row["jobSeqNo"]: row for row in sitemap_rows}

        initial_rows, total, initial_pages = await self.paginate(
            size=400, csrf=csrf, label="initial-large"
        )
        self.result.expected_total = total
        if not initial_rows:
            self.result.errors.append("snowflake_official_listing_returned_zero_jobs")
            return self.result
        ui_rows, ui_total, ui_pages = await self.paginate(
            size=10, csrf=csrf, label="initial-ui-pagination"
        )
        self.result.metadata.update(
            verification_revision=self.VERIFICATION_REVISION,
            official_entry=str(entry.url),
            official_redirect_source=OFFICIAL_REDIRECT,
            listing_url=self.search_entry,
            listing_authority=(
                "Snowflake's own unfiltered global refineSearch listing on "
                "careers.snowflake.com (keywords empty, global true, no filters)"
            ),
            listing_request={
                "method": "POST", "url": WIDGET_ENDPOINT,
                "page_sizes": [400, 10],
                "observed_ui_page_size": 10,
                "observed_ui_pages": len(ui_pages),
                "filters": "none (keywords='', global=true, selected_fields={})",
            },
            sitemap_url=SITEMAP_URL,
            sitemap_jobs=len(sitemap_rows),
            entry_eager_total=eager.get("totalHits"),
            entry_eager_first_page_ids=eager_ids,
            initial_total=total,
            initial_large_pages=initial_pages,
            initial_ui_pages=ui_pages,
            ui_pagination_total=ui_total,
            detail_method=(
                "Snowflake's own Phenom jobDetail widget request for each exact "
                "jobSeqNo; the full description field is the same body rendered "
                "in the public detail page's jobdescription-text container"
            ),
            ats_note=(
                "Ashby apply URLs are retained as apply_url only; the Ashby "
                "board is never enumerated for membership"
            ),
            university_recruiting_note=(
                "The university-recruiting page was inspected separately; all "
                "of its sampled intern jobSeqNos were present in this same "
                "global listing, so no disjoint membership was added"
            ),
            date_semantics=(
                "postedDate/jobUpdatedDate are copied from the source job object; "
                "unknown dates remain null, never request time"
            ),
        )
        if ui_total != total:
            self.result.errors.append("snowflake_ui_pagination_total_differs_from_large_listing")
        if set(ui_rows) != set(initial_rows):
            self.result.errors.append("snowflake_ui_pagination_id_set_differs_from_large_listing")
        if eager_ids and set(eager_ids) != set(ui_pages[0]["job_ids"]):
            self.result.warnings.append(
                "snowflake_entry_eager_first_page_order_or_subset_changed"
            )
        if set(sitemap_by_sequence) != set(initial_rows):
            self.result.errors.append("snowflake_sitemap_job_set_differs_from_listing")
        missing_sitemap = sorted(set(initial_rows) - set(sitemap_by_sequence))
        extra_sitemap = sorted(set(sitemap_by_sequence) - set(initial_rows))
        if missing_sitemap:
            for sequence in missing_sitemap:
                self.result.errors.append(f"snowflake_listing_job_missing_from_sitemap:{sequence}")
        if extra_sitemap:
            for sequence in extra_sitemap:
                self.result.errors.append(f"snowflake_sitemap_job_missing_from_listing:{sequence}")

        self.result.timings["source_collection_seconds"] = round(max(0.0, time.monotonic() - started), 6)
        for sequence in initial_rows:
            self.add(self.add_listing_job(initial_rows[sequence], sitemap_by_sequence.get(sequence)))
            if self.limit():
                break
        self.result.metadata["company_published_job_ids"] = sorted(initial_rows)
        self.result.metadata["sitemap_job_ids"] = sorted(sitemap_by_sequence)
        if "sample_limit" in self.result.warnings:
            self.result.listing_complete = False
            return self.result

        self.result.jobs = await self.hydrate(
            self.result.jobs, lambda job: self.detail(job, csrf)
        )
        final_started = time.monotonic()
        final_rows, final_total, final_pages = await self.paginate(
            size=400, csrf=csrf, label="final-large"
        )
        self.result.timings["final_reconciliation_seconds"] = round(
            max(0.0, time.monotonic() - final_started), 6
        )
        self.result.metadata["final_total"] = final_total
        self.result.metadata["final_pages"] = final_pages
        if final_total != total or set(final_rows) != set(initial_rows):
            self.result.listing_complete = False
            self.result.errors.append("snowflake_listing_id_set_changed_during_run")
        self.result.listing_complete = (
            not self.result.errors
            and len(initial_rows) == total
            and final_total == total
            and set(final_rows) == set(initial_rows)
            and "sample_limit" not in self.result.warnings
        )
        return self.result
