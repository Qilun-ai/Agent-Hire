"""Supabase's own careers page and the exact company-scoped Ashby roles it lists.

Membership comes only from `https://supabase.com/careers`: its Next.js page data
and its visible `#positions` cards expose the same 70 company roles. Each role
links to an exact `https://jobs.ashbyhq.com/supabase/<uuid>` detail page. Those
exact detail pages are used to verify the full employer description; no Ashby
board discovery, guessed slug, parent board or aggregator is enumerated.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html

OFFICIAL_ENTRY = "https://supabase.com/careers"
LISTING_URL = OFFICIAL_ENTRY + "#positions"
ASHBY_HOST = "jobs.ashbyhq.com"
ASHBY_ORGANIZATION = "Supabase"
ASHBY_BOARD_SLUG = "supabase"
ASHBY_DETAIL_PATH = re.compile(
    r"^/supabase/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.I,
)
TALENT_COMMUNITY_URL = "https://jobs.ashbyhq.com/supabase/form/talent-community-form"
OPEN_POSITIONS_COUNT = re.compile(r"Open positions\s*\(\s*(\d+)\s*\)", re.I)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
PAGINATION_WORDS = re.compile(r"\b(?:next|previous|load more|page\s+\d+)\b", re.I)


def normalized(value: str | None) -> str:
    value = unicodedata.normalize("NFC", value or "")
    return re.sub(r"\s+", " ", value).strip()


def normalized_description(html: str | None) -> str:
    return normalized(text_html(html))


def description_sha256(html: str | None) -> str:
    return sha256(normalized_description(html).encode()).hexdigest()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def parse_next_data(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one('script#__NEXT_DATA__[type="application/json"]')
    if node is None or not node.string:
        raise ValueError("Supabase careers page is missing its Next.js page data")
    try:
        value = json.loads(node.string)
    except ValueError as exc:
        raise ValueError("Supabase careers page Next.js data is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Supabase careers page Next.js data is not an object")
    return value


def _validate_row(row: dict, department: str, seen: set[str]) -> dict:
    if not isinstance(row, dict):
        raise ValueError(f"Supabase careers job in {department!r} is not an object")
    identity = str(row.get("id", ""))
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", identity, re.I):
        raise ValueError(f"Supabase careers job id is not an Ashby UUID: {identity!r}")
    if identity in seen:
        raise ValueError(f"Supabase careers page repeats job id {identity}")
    seen.add(identity)
    title = normalized(row.get("title"))
    if not title:
        raise ValueError(f"Supabase careers job {identity} has no title")
    if normalized(row.get("department")) != normalized(department):
        raise ValueError(f"Supabase careers job {identity} department does not match its group")
    if not normalized(row.get("team")):
        raise ValueError(f"Supabase careers job {identity} has no team")
    if not normalized(row.get("location")):
        raise ValueError(f"Supabase careers job {identity} has no location")
    if not isinstance(row.get("secondaryLocations"), list):
        raise ValueError(f"Supabase careers job {identity} has malformed secondary locations")
    for secondary in row["secondaryLocations"]:
        if not isinstance(secondary, dict) or not normalized(secondary.get("location")):
            raise ValueError(f"Supabase careers job {identity} has an unidentified secondary location")
        if secondary.get("address") is not None and not isinstance(secondary["address"], dict):
            raise ValueError(f"Supabase careers job {identity} has a malformed secondary address")
    if row.get("address") is not None and not isinstance(row["address"], dict):
        raise ValueError(f"Supabase careers job {identity} has a malformed address")
    if not normalized(row.get("employmentType")):
        raise ValueError(f"Supabase careers job {identity} has no employment type")
    if row.get("isListed") is not True:
        raise ValueError(f"Supabase careers page contains a non-listed job {identity}")
    job_url = str(row.get("jobUrl", ""))
    parsed_url = urlsplit(job_url)
    match = ASHBY_DETAIL_PATH.fullmatch(parsed_url.path)
    if (parsed_url.scheme != "https" or parsed_url.netloc != ASHBY_HOST
            or match is None or match.group(1).lower() != identity.lower()):
        raise ValueError(f"Supabase careers job {identity} does not link to its exact Ashby detail")
    if str(row.get("applyUrl", "")) != job_url + "/application":
        raise ValueError(f"Supabase careers job {identity} apply URL changed")
    published = str(row.get("publishedAt", ""))
    try:
        published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Supabase careers job {identity} has no valid publishedAt") from exc
    if published_at.tzinfo is None:
        raise ValueError(f"Supabase careers job {identity} publishedAt has no timezone")
    description = row.get("descriptionHtml")
    if not isinstance(description, str) or len(normalized_description(description)) < 100:
        raise ValueError(f"Supabase careers job {identity} has no full description")
    if not isinstance(row.get("descriptionPlain"), str) or len(normalized(row["descriptionPlain"])) < 100:
        raise ValueError(f"Supabase careers job {identity} has no full plain description")
    return row


def parse_careers_page(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed_url = urlsplit(response_url)
    if (parsed_url.scheme != "https" or parsed_url.netloc != "supabase.com"
            or parsed_url.path.rstrip("/") != "/careers"):
        raise ValueError("Supabase official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    application_name = soup.select_one('meta[name="application-name"]')
    if title != "Careers | Supabase":
        raise ValueError(f"Supabase careers page title changed: {title!r}")
    if application_name is None or application_name.get("content") != "Supabase":
        raise ValueError("Supabase careers page identity changed")
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical is None or canonical.get("href") != OFFICIAL_ENTRY:
        raise ValueError("Supabase careers page canonical URL changed")

    data = parse_next_data(html)
    page_props = data.get("props", {}).get("pageProps", {})
    jobs_by_department = page_props.get("jobs")
    if not isinstance(jobs_by_department, dict) or not jobs_by_department:
        raise ValueError("Supabase careers page no longer exposes its department job groups")
    if page_props.get("placeholderJob") is not None:
        raise ValueError("Supabase careers page unexpectedly exposes a placeholder job")

    rows: list[dict] = []
    departments: list[dict] = []
    seen: set[str] = set()
    for department, group in jobs_by_department.items():
        if not isinstance(department, str) or not department.strip() or not isinstance(group, list):
            raise ValueError("Supabase careers department grouping is malformed")
        for row in group:
            rows.append(_validate_row(row, department, seen))
        departments.append({"name": department, "count": len(group)})
    expected_count = len(rows)
    if expected_count == 0:
        raise ValueError("Supabase careers page exposes no jobs")

    count_link = soup.select_one('a[href="/careers#positions"]')
    count_text = normalized(count_link.get_text(" ", strip=True)) if count_link else ""
    count_match = OPEN_POSITIONS_COUNT.search(count_text)
    if count_match is None:
        raise ValueError("Supabase careers page no longer advertises its open-position count")
    advertised = int(count_match.group(1))
    if advertised != expected_count:
        raise ValueError(
            f"Supabase advertised count {advertised} does not match its {expected_count} page jobs"
        )

    positions = soup.select_one("#positions")
    if positions is None:
        raise ValueError("Supabase careers page no longer exposes its #positions listing")
    visible: list[dict] = []
    visible_ids: list[str] = []
    talent_links: list[str] = []
    unexpected_links: list[str] = []
    for anchor in positions.select("a[href]"):
        href = str(anchor.get("href", ""))
        if href == TALENT_COMMUNITY_URL:
            talent_links.append(href)
            continue
        target = urlsplit(href)
        if target.scheme != "https" or target.netloc != ASHBY_HOST or not target.path.startswith("/supabase/"):
            continue
        match = ASHBY_DETAIL_PATH.fullmatch(target.path)
        if match is None:
            unexpected_links.append(href)
            continue
        identity = match.group(1)
        if identity in visible_ids:
            raise ValueError(f"Supabase careers listing repeats visible job link {identity}")
        heading = anchor.find("h4")
        card_title = normalized(heading.get_text(" ", strip=True)) if heading else ""
        card_text = normalized(anchor.get_text(" ", strip=True))
        group_heading = anchor.find_previous("h3")
        card_department = normalized(group_heading.get_text(" ", strip=True)) if group_heading else ""
        row = next((value for value in rows if value["id"].lower() == identity.lower()), None)
        if row is None:
            raise ValueError(f"Supabase careers listing exposes unlisted job {identity}")
        if card_title != normalized(row["title"]):
            raise ValueError(f"Supabase careers visible card title changed for {identity}")
        if card_department != normalized(row["department"]):
            raise ValueError(f"Supabase careers visible card department changed for {identity}")
        if normalized(row["location"]) not in card_text:
            raise ValueError(f"Supabase careers visible card location changed for {identity}")
        visible_ids.append(identity)
        visible.append({
            "id": identity,
            "title": card_title,
            "department": card_department,
            "location": normalized(row["location"]),
            "url": href,
            "apply_label_present": "apply for position" in card_text.lower(),
        })
    if unexpected_links:
        raise ValueError(f"Supabase careers listing has unexpected Ashby links: {unexpected_links[:3]}")
    if visible_ids != [row["id"] for row in rows]:
        raise ValueError("Supabase careers visible job links do not match its page-data membership")
    if len(talent_links) > 1:
        raise ValueError("Supabase careers page repeats its talent-community link")

    department_headings = [
        normalized(node.get_text(" ", strip=True)) for node in positions.select("h3")
    ]
    department_positions = []
    for department in jobs_by_department:
        name = normalized(department)
        if name not in department_headings:
            raise ValueError(f"Supabase careers listing has no heading for {name!r}")
        department_positions.append(department_headings.index(name))
    if department_positions != sorted(department_positions):
        raise ValueError("Supabase careers department headings are out of page-data order")

    filters = [
        normalized(node.get_text(" ", strip=True))
        for node in positions.select("input, select, button")
    ]
    pagination = [
        normalized(node.get_text(" ", strip=True))
        for node in positions.select("button, a")
        if normalized(node.get_text(" ", strip=True))
        and PAGINATION_WORDS.search(node.get_text(" ", strip=True))
    ]
    if pagination:
        raise ValueError(f"Supabase careers listing now exposes pagination: {pagination[:3]}")
    page_text = normalized(soup.get_text(" ", strip=True))
    if "Open positions" not in page_text:
        raise ValueError("Supabase careers page no longer names its open positions section")

    return {
        "url": response_url,
        "title": title,
        "count": expected_count,
        "advertised_count": advertised,
        "departments": departments,
        "department_headings": department_headings,
        "rows": rows,
        "visible_rows": visible,
        "visible_ids": visible_ids,
        "visible_count": len(visible),
        "talent_community_urls": talent_links,
        "talent_community_is_posting": False,
        "filter_controls": [value for value in filters if value],
        "pagination_controls": pagination,
        "jobs_payload_sha256": sha256(
            json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "visible_listing_sha256": sha256(
            json.dumps(visible, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    }


def description_sections(html: str) -> list[dict]:
    """Split the employer description on its own headings without dropping text."""
    soup = BeautifulSoup(f'<div data-supabase-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-supabase-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections: list[dict] = []
    current: dict | None = None
    for block in blocks:
        heading = ""
        if block.name in HEADING_TAGS:
            heading = normalized(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized(block.get_text(" ", strip=True))
            strong_text = normalized(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text
        if heading:
            if current is not None:
                current["text"] = normalized(text_html(current["html"]))
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = normalized(text_html(current["html"]))
        sections.append(current)
    return [section for section in sections if section["heading"] or section["text"]]


def parse_detail_page(html: str, row: dict, response_url: str) -> dict:
    identity = str(row.get("id", ""))
    expected_url = str(row.get("jobUrl", ""))
    if response_url != expected_url:
        raise ValueError(f"Supabase exact detail {identity} redirected to {response_url}")
    parsed_url = urlsplit(response_url)
    match = ASHBY_DETAIL_PATH.fullmatch(parsed_url.path)
    if (parsed_url.scheme != "https" or parsed_url.netloc != ASHBY_HOST
            or match is None or match.group(1).lower() != identity.lower()):
        raise ValueError(f"Supabase exact detail URL changed for {identity}")

    soup = BeautifulSoup(html, "lxml")
    script = next(
        (node for node in soup.find_all("script") if node.string and "window.__appData" in node.string),
        None,
    )
    if script is None:
        raise ValueError(f"Supabase exact detail {identity} has no Ashby app data")
    text = script.string
    start = text.find("{")
    if start < 0:
        raise ValueError(f"Supabase exact detail {identity} app data is malformed")
    try:
        app_data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as exc:
        raise ValueError(f"Supabase exact detail {identity} app data is not valid JSON") from exc
    if not isinstance(app_data, dict):
        raise ValueError(f"Supabase exact detail {identity} app data is not an object")
    organization = app_data.get("organization")
    posting = app_data.get("posting")
    if not isinstance(organization, dict) or not isinstance(posting, dict):
        raise ValueError(f"Supabase exact detail {identity} has no organization/posting data")
    if organization.get("name") != ASHBY_ORGANIZATION or organization.get("hostedJobsPageSlug") != ASHBY_BOARD_SLUG:
        raise ValueError(f"Supabase exact detail {identity} belongs to another board")
    if str(posting.get("id", "")).lower() != identity.lower():
        raise ValueError(f"Supabase exact detail {identity} posting id changed")
    if normalized(posting.get("title")) != normalized(row.get("title")):
        raise ValueError(f"Supabase exact detail {identity} title changed")
    if normalized(posting.get("departmentName")) != normalized(row.get("department")):
        raise ValueError(f"Supabase exact detail {identity} department changed")
    if normalized(posting.get("locationName")) != normalized(row.get("location")):
        raise ValueError(f"Supabase exact detail {identity} location changed")
    if normalized(posting.get("employmentType")) != normalized(row.get("employmentType")):
        raise ValueError(f"Supabase exact detail {identity} employment type changed")
    if posting.get("secondaryLocationNames") != [
        secondary["location"] for secondary in row.get("secondaryLocations", [])
    ]:
        raise ValueError(f"Supabase exact detail {identity} secondary locations changed")
    if posting.get("isListed") is not True or posting.get("isConfidential") is not False:
        raise ValueError(f"Supabase exact detail {identity} is no longer a public listed posting")
    description = posting.get("descriptionHtml")
    if not isinstance(description, str) or len(normalized_description(description)) < 100:
        raise ValueError(f"Supabase exact detail {identity} has no full description")
    if description != row.get("descriptionHtml"):
        raise ValueError(f"Supabase exact detail {identity} description differs from its listed role")

    body = normalized_description(description)
    sections = description_sections(description)
    page_title = normalized(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if page_title != f"{normalized(row.get('title'))} @ Supabase":
        raise ValueError(f"Supabase exact detail {identity} page title changed")
    return {
        "url": response_url,
        "page_title": page_title,
        "organization": organization,
        "posting": posting,
        "description_html": description,
        "description_text": text_html(description),
        "description_normalized": body,
        "description_normalized_chars": len(body),
        "description_sha256": sha256(body.encode()).hexdigest(),
        "description_fnv1a64": fnv1a64(body),
        "description_sections": sections,
        "is_public_listed_posting": True,
    }


def _listing_summary(parsed: dict) -> dict:
    return {
        "url": parsed["url"],
        "title": parsed["title"],
        "advertised_count": parsed["advertised_count"],
        "count": parsed["count"],
        "departments": parsed["departments"],
        "department_headings": parsed["department_headings"],
        "visible_count": parsed["visible_count"],
        "visible_ids": parsed["visible_ids"],
        "talent_community_urls": parsed["talent_community_urls"],
        "talent_community_is_posting": parsed["talent_community_is_posting"],
        "filter_controls": parsed["filter_controls"],
        "pagination_controls": parsed["pagination_controls"],
        "jobs_payload_sha256": parsed["jobs_payload_sha256"],
        "visible_listing_sha256": parsed["visible_listing_sha256"],
        "rows": [
            {
                "id": row["id"],
                "title": normalized(row["title"]),
                "department": normalized(row["department"]),
                "team": normalized(row.get("team")),
                "location": normalized(row["location"]),
                "secondary_locations": row.get("secondaryLocations", []),
                "employment_type": normalized(row["employmentType"]),
                "published_at": row["publishedAt"],
                "description_normalized_chars": len(normalized_description(row["descriptionHtml"])),
                "description_sha256": description_sha256(row["descriptionHtml"]),
            }
            for row in parsed["rows"]
        ],
    }


def _row_changes(initial: list[dict], final: list[dict]) -> dict:
    initial_by_id = {row["id"]: row for row in initial}
    final_by_id = {row["id"]: row for row in final}
    changed = {}
    for identity in initial_by_id:
        if identity not in final_by_id:
            continue
        fields = sorted(
            key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
            if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
        )
        if fields:
            changed[identity] = fields
    return changed


class Supabase(Adapter):
    """Read only Supabase's own careers listing and the exact Ashby jobs it links."""

    VERIFICATION_REVISION = "supabase.careers.next-data-plus-exact-ashby-details.1"

    def __init__(self, http, company, source, options, *, initial_capture: dict | None = None):
        super().__init__(http, company, source, options)
        self.initial_capture = initial_capture

    async def _careers_page(self, label: str) -> dict:
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY, headers={"Accept": "text/html"})
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_careers_page(response.text, str(response.url))
        parsed.update({
            "label": label,
            "status": response.status_code,
            "final_url": str(response.url),
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    def _make_job(self, row: dict, index: int, department: str):
        secondary = [value for value in row.get("secondaryLocations", []) if isinstance(value, dict)]
        locations = [{
            "location": normalized(row["location"]),
            "address": row.get("address"),
            "is_remote": row.get("isRemote"),
            "workplace_type": row.get("workplaceType"),
        }] + [
            {"location": normalized(value["location"]), "address": value.get("address")}
            for value in secondary
        ]
        return self.job(
            {
                "company_careers_row": row,
                "department_group": department,
                "listing_position": index,
                "careers_listing_url": LISTING_URL,
                "official_entry_url": OFFICIAL_ENTRY,
            },
            title=normalized(row["title"]),
            url=row["jobUrl"],
            job_id=row["id"],
            apply_url=row["applyUrl"],
            description=row["descriptionHtml"],
            description_text=text_html(row["descriptionHtml"]),
            method="supabase.careers.next_data.jobs.descriptionHtml",
            description_url=OFFICIAL_ENTRY,
            locations=locations,
            department=normalized(row["department"]),
            team=normalized(row.get("team")),
            employment_type=normalized(row["employmentType"]),
            workplace_type=row.get("workplaceType"),
            date_posted=row["publishedAt"],
            date_posted_raw=row["publishedAt"],
            employer=ASHBY_ORGANIZATION,
            requisition_id=None,
        )

    async def _detail(self, job):
        response = await self.http.get(job.url, headers={"Accept": "text/html"})
        parsed = parse_detail_page(response.text, job.raw["company_careers_row"], str(response.url))
        posting = parsed["posting"]
        job.raw["detail"] = posting
        job.raw["detail_verification"] = {
            "detail_url": parsed["url"],
            "page_title": parsed["page_title"],
            "organization_name": parsed["organization"].get("name"),
            "hosted_jobs_page_slug": parsed["organization"].get("hostedJobsPageSlug"),
            "is_public_listed_posting": parsed["is_public_listed_posting"],
            "description_exactly_matches_listing": True,
            "description_normalized_chars": parsed["description_normalized_chars"],
            "description_sha256": parsed["description_sha256"],
            "description_fnv1a64": parsed["description_fnv1a64"],
            "description_section_headings": [
                section["heading"] for section in parsed["description_sections"] if section["heading"]
            ],
        }
        job.raw["detail_description_sections"] = [
            {"heading": section["heading"], "text": section["text"]}
            for section in parsed["description_sections"]
        ]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "ashby.posting.exact-company-job.__appData.descriptionHtml"
        job.description_source_url = parsed["url"]
        job.apply_url = job.raw["company_careers_row"]["applyUrl"]
        job.employer = ASHBY_ORGANIZATION
        compensation = {
            "compensationTierSummary": posting.get("compensationTierSummary"),
            "compensationTiers": posting.get("compensationTiers"),
            "scrapeableCompensationSalarySummary": posting.get("scrapeableCompensationSalarySummary"),
            "compensationTierGuideUrl": posting.get("compensationTierGuideUrl"),
        }
        job.salary = compensation if any(
            value not in (None, [], "") for value in compensation.values()
        ) else None
        job.valid_through = posting.get("applicationDeadline")
        job.raw["detail_compensation"] = compensation
        job.raw["detail_application_deadline"] = posting.get("applicationDeadline")
        job.raw["detail_application_limit_callout_html"] = posting.get("applicationLimitCalloutHtml")
        job.raw["detail_legal_entity_name_for_texting_consent"] = posting.get(
            "legalEntityNameForTextingConsent"
        )
        return job

    async def run(self):
        if self.company.name != "Supabase" or str(self.source.url).rstrip("/") != OFFICIAL_ENTRY:
            raise ValueError("Supabase must start at its official careers URL")
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_authority": (
                "The #positions listing on Supabase's own careers page, cross-checked against "
                "the page's Next.js jobs data and its visible department cards."
            ),
            "detail_method": (
                "Each exact company-linked Ashby detail page's public window.__appData.posting "
                "descriptionHtml must exactly equal the listed role's full description."
            ),
            "request_scope": (
                "supabase.com/careers plus only the exact jobs.ashbyhq.com/supabase/<uuid> links "
                "exposed by that page; no board discovery, guessed slug, parent or aggregator."
            ),
        })
        try:
            if self.initial_capture is None:
                initial = await self._careers_page("initial")
            else:
                capture_started = time.perf_counter()
                capture = self.initial_capture
                initial = parse_careers_page(capture["html"], capture["metadata"]["final_url"])
                initial.update(capture["metadata"])
                self.result.timings["reused_initial_listing_parse_seconds"] = max(
                    0.0, time.perf_counter() - capture_started
                )
        except Exception as exc:
            self.result.errors.append(f"Supabase careers listing failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started
            )
            return self.result

        self.result.expected_total = initial["count"]
        self.result.pages = 1
        self.result.metadata["initial_careers_listing"] = _listing_summary(initial)
        self.result.metadata["initial_page_status"] = initial["status"]
        self.result.metadata["initial_page_final_url"] = initial["final_url"]
        self.result.metadata["initial_page_bytes"] = initial["response_bytes"]
        self.result.metadata["initial_page_sha256"] = initial["response_sha256"]
        self.result.metadata["initial_page_request_seconds"] = initial["seconds"]
        self.result.metadata["initial_jobs_payload_sha256"] = initial["jobs_payload_sha256"]
        self.result.metadata["initial_visible_listing_sha256"] = initial["visible_listing_sha256"]
        if initial["count"] != initial["visible_count"]:
            self.result.errors.append("supabase_page_data_and_visible_listing_count_mismatch")
        if initial["count"] != initial["advertised_count"]:
            self.result.errors.append("supabase_advertised_and_page_data_count_mismatch")

        jobs = [
            self._make_job(row, index, row["department"])
            for index, row in enumerate(initial["rows"])
        ]
        jobs = await self.hydrate(jobs, self._detail)
        self.result.jobs = jobs
        self.result.metadata["details"] = {
            **self.result.metadata.get("details", {}),
            "detail_url_count": len(jobs),
            "detail_urls": [job.url for job in jobs],
            "detail_source": "exact company-linked Ashby detail pages",
        }

        final = None
        final_error = None
        try:
            final = await self._careers_page("final")
        except Exception as exc:
            final_error = f"{type(exc).__name__}: {exc}"
            self.result.errors.append(f"Supabase final careers recheck failed: {final_error}")
        if final is not None:
            self.result.metadata["final_careers_listing"] = _listing_summary(final)
            self.result.metadata["final_page_status"] = final["status"]
            self.result.metadata["final_page_final_url"] = final["final_url"]
            self.result.metadata["final_page_bytes"] = final["response_bytes"]
            self.result.metadata["final_page_sha256"] = final["response_sha256"]
            self.result.metadata["final_page_request_seconds"] = final["seconds"]
            self.result.metadata["final_jobs_payload_sha256"] = final["jobs_payload_sha256"]
            self.result.metadata["final_visible_listing_sha256"] = final["visible_listing_sha256"]
            initial_ids = [row["id"] for row in initial["rows"]]
            final_ids = [row["id"] for row in final["rows"]]
            same_ids = initial_ids == final_ids
            same_departments = initial["departments"] == final["departments"]
            same_visible_ids = initial["visible_ids"] == final["visible_ids"]
            same_rows = initial["rows"] == final["rows"]
            changed_fields = _row_changes(initial["rows"], final["rows"])
            complete = (
                same_ids and same_departments and same_visible_ids and same_rows
                and initial["count"] == final["count"] == self.result.expected_total
                and initial["jobs_payload_sha256"] == final["jobs_payload_sha256"]
                and initial["visible_listing_sha256"] == final["visible_listing_sha256"]
            )
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial_ids,
                "final_ids": final_ids,
                "initial_count": initial["count"],
                "final_count": final["count"],
                "initial_pages": 1,
                "final_pages": 1,
                "same_ids_and_order": same_ids,
                "same_departments": same_departments,
                "same_visible_ids": same_visible_ids,
                "same_all_row_fields": same_rows,
                "changed_fields_by_job_id": changed_fields,
                "new_job_ids": [identity for identity in final_ids if identity not in initial_ids],
                "removed_job_ids": [identity for identity in initial_ids if identity not in final_ids],
                "initial_jobs_payload_sha256": initial["jobs_payload_sha256"],
                "final_jobs_payload_sha256": final["jobs_payload_sha256"],
                "initial_visible_listing_sha256": initial["visible_listing_sha256"],
                "final_visible_listing_sha256": final["visible_listing_sha256"],
                "complete": complete,
            }
            if not same_ids or not same_visible_ids:
                self.result.errors.append("supabase_listing_membership_changed_during_collection")
            if not same_departments:
                self.result.errors.append("supabase_department_grouping_changed_during_collection")
            if changed_fields:
                self.result.errors.append("supabase_source_fields_changed_during_collection")
            if not complete and not any(
                marker in error for error in self.result.errors
                for marker in ("changed_during_collection", "count_mismatch")
            ):
                self.result.errors.append("supabase_listing_reconciliation_incomplete")
        else:
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": [row["id"] for row in initial["rows"]],
                "final_ids": None,
                "initial_count": initial["count"],
                "final_count": None,
                "complete": False,
                "error": final_error,
            }

        if len(jobs) != initial["count"]:
            self.result.errors.append("supabase_retained_jobs_do_not_match_company_listing_count")
        self.result.metadata["job_ids"] = [job.job_id for job in jobs]
        self.result.metadata["current_native_membership_reconciled"] = bool(
            self.result.metadata.get("membership_reconciliation", {}).get("complete")
        )
        self.result.listing_complete = bool(
            self.result.metadata["current_native_membership_reconciled"]
            and len(jobs) == initial["count"]
        )
        self.result.timings["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        return self.result
