"""Notion's own careers page, its exact Ashby job links, and the board's public data.

Membership authority is the server-rendered ``#open-positions`` list on
https://www.notion.com/careers: 154 unique cards, grouped by department, each
linking to the exact Ashby job URL published by Notion.  The official page does
not embed Ashby's board API in a widget; it publishes the Ashby links directly.
The public, company-scoped Ashby board API (tenant ``notion``) is therefore used
only to read the full source fields of those exact linked IDs, never to discover
additional jobs.  Every linked detail page is also opened and its embedded
``window.__appData.posting`` object is compared byte-for-byte with the board row.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
import unicodedata
from json import JSONDecoder
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.notion.com/careers"
ASHBY_BOARD = "notion"
ASHBY_ORIGIN = "https://jobs.ashbyhq.com"
ASHBY_JOB_BASE = f"{ASHBY_ORIGIN}/{ASHBY_BOARD}/"
ASHBY_BOARD_API = (
    f"https://api.ashbyhq.com/posting-api/job-board/{ASHBY_BOARD}?includeCompensation=true"
)
ASHBY_API_HOST = "api.ashbyhq.com"
ASHBY_API_PATH = f"/posting-api/job-board/{ASHBY_BOARD}"
EMPLOYER = "Notion Labs, Inc."
UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
JOB_LINK = re.compile(r"^https://jobs\.ashbyhq\.com/notion/([0-9a-f-]{36})$", re.I)
PAGER = re.compile(r"^(?:next|previous|load more|show more|more jobs|page\s*\d+)$", re.I)
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
EMPLOYMENT_LABEL = {
    "FullTime": "Full-time",
    "PartTime": "Part-time",
    "Intern": "Internship",
    "Contract": "Contract",
    "Temporary": "Temporary",
}


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    """Whitespace-collapsed visible text, with inline-tag punctuation spacing repaired.

    ``text_html`` joins block boundaries with newlines; collapsing those to
    spaces can create "together ." where the rendered page shows "together.".
    The repair is applied symmetrically to the rendered browser reference and
    the stored source HTML so the comparison is about content, not extraction
    artefacts.
    """
    value = text_html(html or "")
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", value)


def display_text(html: str | None) -> str:
    """Readable full text with paragraph breaks and inline-tag punctuation repaired."""
    value = text_html(html or "").replace("\xa0", " ")
    return re.sub(r"[ \t]+([,.;:!?])", r"\1", value)


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _assert_official_url(url: str) -> None:
    parsed = urlsplit(str(url))
    if (parsed.scheme != "https" or parsed.hostname not in {"notion.com", "www.notion.com"}
            or parsed.path.rstrip("/") != "/careers" or parsed.query or parsed.fragment):
        raise ValueError(f"Notion careers entry left its inspected official URL: {url}")


def _assert_job_url(url: str, identity: str) -> None:
    match = JOB_LINK.fullmatch(str(url))
    if match is None or match.group(1).lower() != identity.lower():
        raise ValueError(f"Notion job link is not the exact company-published Ashby URL: {url}")


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    _assert_official_url(response_url)
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "Careers at Notion" not in title:
        raise ValueError("Notion careers page title changed")
    if not any(normalized_title(h.get_text(" ", strip=True)) == "Careers at Notion"
               for h in soup.select("h1")):
        raise ValueError("Notion careers page heading is missing")
    section = soup.select_one("section#open-positions")
    if section is None:
        raise ValueError("Notion careers page no longer exposes #open-positions")

    rows, seen, issues = [], set(), []
    for department_section in section.select("section.g07cPq_departmentSection"):
        heading = department_section.find(["h2", "h3", "h4"])
        department = normalized_title(heading.get_text(" ", strip=True)) if heading else None
        for item in department_section.select("li.g07cPq_jobsListItem"):
            anchor = item.select_one('a[href^="https://jobs.ashbyhq.com/notion/"]')
            if anchor is None:
                issues.append("notion_job_card_without_ashby_link")
                continue
            href = anchor.get("href", "").strip()
            match = JOB_LINK.fullmatch(href)
            if match is None:
                issues.append(f"notion_job_card_with_unexpected_link:{href}")
                continue
            identity = match.group(1).lower()
            if identity in seen:
                issues.append(f"notion_duplicate_company_job_link:{identity}")
                continue
            title_node = item.select_one(".g07cPq_jobTitle")
            location_node = item.select_one(".g07cPq_jobLocation")
            if title_node is None or location_node is None:
                issues.append(f"notion_job_card_fields_missing:{identity}")
                continue
            rows.append({
                "id": identity,
                "url": href,
                "title": normalized_title(title_node.get_text(" ", strip=True)),
                "location": normalized_title(location_node.get_text(" ", strip=True)),
                "department": department,
                "card_html": str(item),
            })
            seen.add(identity)
    if not rows:
        issues.append("notion_official_listing_has_no_exposed_job_rows")

    all_page_ids = []
    for anchor in soup.select('a[href^="https://jobs.ashbyhq.com/notion/"]'):
        match = JOB_LINK.fullmatch(anchor.get("href", "").strip())
        if match:
            all_page_ids.append(match.group(1).lower())
    outside = sorted(set(all_page_ids) - seen)
    if outside:
        issues.append(f"notion_ashby_job_links_outside_open_positions:{','.join(outside[:10])}")

    filters = _parse_filters(section)
    pager_controls = [
        node.get_text(" ", strip=True)
        for node in section.select("button,a")
        if PAGER.fullmatch(normalized_title(node.get_text(" ", strip=True)))
    ]
    if pager_controls:
        issues.append("notion_unobserved_pagination_control")

    return {
        "url": response_url,
        "title": title,
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "count": len(rows),
        "departments": [row["department"] for row in rows if row["department"]],
        "filter_controls": filters["controls"],
        "filter_options": filters["options"],
        "pagination_controls": pager_controls,
        "all_page_job_link_ids": list(dict.fromkeys(all_page_ids)),
        "issues": list(dict.fromkeys(issues)),
    }


def _parse_filters(section: Tag) -> dict:
    controls, options = [], {}
    for root in section.select("div._7KcoPq_root"):
        button = root.find("button")
        if button is None:
            continue
        label = normalized_title(button.get_text(" ", strip=True))
        if label not in {"All locations", "All departments"}:
            continue
        controls.append(label)
        listbox = root.select_one('div[role="listbox"]')
        if listbox is None:
            continue
        aria = normalized_title(listbox.get("aria-label"))
        values = [
            normalized_title(option.get_text(" ", strip=True))
            for option in listbox.select('button[role="option"]')
            if normalized_title(option.get_text(" ", strip=True))
        ]
        key = "locations" if "location" in (aria or label).lower() else "departments"
        options[key] = values
    return {"controls": controls, "options": options}


def parse_board_api(value: dict, response_url: str = ASHBY_BOARD_API) -> dict:
    parsed_url = urlsplit(response_url)
    if (parsed_url.scheme != "https" or parsed_url.hostname != ASHBY_API_HOST
            or parsed_url.path != ASHBY_API_PATH):
        raise ValueError("Notion Ashby board response came from an unexpected URL")
    if not isinstance(value, dict):
        raise ValueError("Notion Ashby board response is not an object")
    if str(value.get("apiVersion")) != "1":
        raise ValueError("Notion Ashby board apiVersion changed")
    jobs = value.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("Notion Ashby board response has no jobs array")

    rows, seen, issues = [], set(), []
    for row in jobs:
        if not isinstance(row, dict):
            issues.append("notion_ashby_row_not_object")
            continue
        identity = str(row.get("id", "")).lower()
        if not UUID.fullmatch(identity):
            issues.append("notion_ashby_row_missing_uuid")
            continue
        if identity in seen:
            issues.append(f"notion_ashby_duplicate_id:{identity}")
            continue
        seen.add(identity)
        if row.get("isListed") is not True:
            issues.append(f"notion_ashby_row_not_listed:{identity}")
        title = normalized_title(row.get("title"))
        if not title:
            issues.append(f"notion_ashby_row_missing_title:{identity}")
        job_url = str(row.get("jobUrl") or "")
        try:
            _assert_job_url(job_url, identity)
        except ValueError:
            issues.append(f"notion_ashby_row_unexpected_job_url:{identity}")
        expected_apply = job_url.rstrip("/") + "/application"
        if str(row.get("applyUrl") or "") != expected_apply:
            issues.append(f"notion_ashby_row_unexpected_apply_url:{identity}")
        description = row.get("descriptionHtml")
        if not isinstance(description, str):
            issues.append(f"notion_ashby_row_missing_descriptionHtml:{identity}")
            description = ""
        elif len(normalized_body_text(description)) < 100:
            issues.append(f"notion_ashby_description_empty_or_short:{identity}")
        if not isinstance(row.get("descriptionPlain"), str):
            issues.append(f"notion_ashby_row_missing_descriptionPlain:{identity}")
        if not isinstance(row.get("secondaryLocations"), list):
            issues.append(f"notion_ashby_row_missing_secondaryLocations:{identity}")
        if not isinstance(row.get("publishedAt"), str) or not row["publishedAt"].strip():
            issues.append(f"notion_ashby_row_missing_publishedAt:{identity}")
        rows.append({**row, "id": identity, "title": title, "jobUrl": job_url,
                     "applyUrl": expected_apply, "descriptionHtml": description})
    return {
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "count": len(rows),
        "api_version": str(value.get("apiVersion")),
        "issues": list(dict.fromkeys(issues)),
    }


def parse_detail_page(html: str, expected_id: str, response_url: str) -> dict:
    identity = str(expected_id).lower()
    _assert_job_url(response_url, identity)
    marker = html.find("window.__appData")
    if marker < 0:
        raise ValueError("Notion Ashby detail page has no window.__appData")
    start = html.find("{", marker)
    if start < 0:
        raise ValueError("Notion Ashby detail page app data has no JSON object")
    try:
        data, _end = JSONDecoder().raw_decode(html[start:])
    except ValueError as exc:
        raise ValueError(f"Notion Ashby detail app data is not valid JSON: {exc}") from exc
    posting = data.get("posting") if isinstance(data, dict) else None
    organization = data.get("organization") if isinstance(data, dict) else None
    if not isinstance(posting, dict):
        raise ValueError("Notion Ashby detail page has no posting object")
    if str(posting.get("id", "")).lower() != identity:
        raise ValueError("Notion Ashby detail page embeds a different job ID than its URL")
    description = posting.get("descriptionHtml")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Notion Ashby detail page has no full descriptionHtml")
    if posting.get("isListed") is not True:
        raise ValueError("Notion Ashby detail posting is not listed")
    if posting.get("isConfidential") is not False:
        raise ValueError("Notion Ashby detail posting is confidential or has an unexpected flag")
    if not isinstance(organization, dict) or organization.get("hostedJobsPageSlug") != ASHBY_BOARD:
        raise ValueError("Notion Ashby detail page organization is not the inspected notion tenant")
    soup = BeautifulSoup(description, "lxml")
    links = [{"text": a.get_text(" ", strip=True), "url": a.get("href")}
             for a in soup.select("a[href]")]
    return {
        "posting": posting,
        "organization": organization,
        "description_html": description,
        "description_text": display_text(description),
        "normalized_chars": len(normalized_body_text(description)),
        "normalized_fnv1a64": fnv1a64(normalized_body_text(description)),
        "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
        "description_sections": description_sections(description),
        "description_links": links,
        "role_attachments": [link for link in links
                             if ATTACHMENT_SUFFIX.search(link["url"] or "")],
        "apply_url": (posting.get("jobUrl") or response_url).rstrip("/") + "/application",
    }


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-notion-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-notion-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = normalized_title(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    else:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def _section_roles(sections: list[dict]) -> tuple[list[dict] | None, list[dict] | None]:
    responsibility_tokens = ("responsibilit", "what you'll achieve", "what you will achieve",
                             "what you'll do", "what you will do", "your impact", "day to day")
    qualification_tokens = ("skills you'll need", "skills you will need", "requirement",
                            "qualification", "nice to have", "who you are", "what we're looking for",
                            "what we are looking for")
    responsibilities = [section for section in sections if section["heading"] and any(
        token in section["heading"].lower() for token in responsibility_tokens)]
    qualifications = [section for section in sections if section["heading"] and any(
        token in section["heading"].lower() for token in qualification_tokens)]
    return responsibilities or None, qualifications or None


def _locations(row: dict) -> list[dict]:
    primary = normalized_title(row.get("location"))
    values = []
    if primary:
        values.append({"location": primary, "address": row.get("address")})
    for secondary in row.get("secondaryLocations") or []:
        if isinstance(secondary, dict) and normalized_title(secondary.get("location")):
            values.append({"location": normalized_title(secondary.get("location")),
                           "address": secondary.get("address")})
    return values


def _location_label(row: dict) -> str:
    return "; ".join(value["location"] for value in _locations(row))


def _salary(row: dict, posting: dict) -> dict | None:
    compensation = row.get("compensation") if isinstance(row.get("compensation"), dict) else {}
    summary = compensation.get("compensationTierSummary") or posting.get("compensationTierSummary")
    tiers = compensation.get("compensationTiers") or posting.get("compensationTiers") or []
    scrapeable = compensation.get("scrapeableCompensationSalarySummary") or posting.get(
        "scrapeableCompensationSalarySummary")
    if not summary and not tiers and not scrapeable:
        return None
    return {
        "compensationTierSummary": summary,
        "compensationTiers": tiers,
        "scrapeableCompensationSalarySummary": scrapeable,
        "source": "ashby.public_board_and_detail",
    }


class Notion(Adapter):
    """Read Notion's official careers list and only its exact linked Ashby jobs."""

    VERIFICATION_REVISION = "notion.official-careers-list-and-exact-ashby-links.1"

    async def _official_snapshot(self, label: str) -> dict:
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        _assert_official_url(str(response.url))
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update({
            "status": response.status_code,
            "seconds": seconds,
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "label": label,
        })
        return parsed

    async def _board_snapshot(self, label: str) -> dict:
        started = time.perf_counter()
        response = await self.http.get(ASHBY_BOARD_API)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_board_api(response.json(), str(response.url))
        parsed.update({
            "url": str(response.url),
            "status": response.status_code,
            "seconds": seconds,
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "label": label,
        })
        return parsed

    def _make_job(self, row: dict, card: dict) -> object:
        identity = row["id"]
        description_html = row["descriptionHtml"]
        sections = description_sections(description_html)
        responsibilities, qualifications = _section_roles(sections)
        locations = _locations(row)
        job = self.job(
            {
                "listing": row,
                "company_listing_card": card,
                "source_ui": {
                    "official_entry_url": OFFICIAL_ENTRY,
                    "listing_page_url": OFFICIAL_ENTRY,
                    "board_api_url": ASHBY_BOARD_API,
                    "native_job_url": row["jobUrl"],
                    "rendered_apply_url": row["applyUrl"],
                    "card_title": card.get("title"),
                    "card_location": card.get("location"),
                    "card_department": card.get("department"),
                },
                "description_sections": sections,
                "description_links": [],
                "role_attachments": [],
                "dates": {
                    "publishedAt": row.get("publishedAt"),
                    "applicationDeadline": None,
                    "date_posted_mapping": "ashby.public_board.publishedAt",
                    "date_updated_mapping": None,
                    "valid_through_mapping": "ashby.detail.posting.applicationDeadline",
                },
                "compensation": row.get("compensation"),
            },
            title=row["title"],
            url=row["jobUrl"],
            job_id=identity,
            requisition_id=None,
            apply_url=row["applyUrl"],
            description=description_html,
            method="notion.ashby_board.descriptionHtml",
            description_url=ASHBY_BOARD_API,
            locations=locations,
            department=normalized_title(row.get("department")),
            team=normalized_title(row.get("team")),
            employment_type=EMPLOYMENT_LABEL.get(row.get("employmentType"), row.get("employmentType")),
            workplace_type=row.get("workplaceType"),
            date_posted=row.get("publishedAt") if isinstance(row.get("publishedAt"), str) else None,
            date_posted_raw=row.get("publishedAt"),
            date_updated=None,
            valid_through=None,
            salary=_salary(row, {}),
            employer=EMPLOYER,
            language="en",
            responsibilities=responsibilities,
            qualifications=qualifications,
        )
        job.description_text = display_text(description_html)
        job.record_identity = f"notion:{identity}"
        return job

    async def _detail(self, job) -> object:
        response = await self.http.get(job.url)
        if str(response.url) != job.url:
            raise ValueError(f"Notion exact Ashby detail URL redirected: {response.url}")
        parsed = parse_detail_page(response.text, job.job_id, str(response.url))
        posting = parsed["posting"]
        listing = job.raw["listing"]
        card = job.raw["company_listing_card"]
        if posting["descriptionHtml"] != listing["descriptionHtml"]:
            raise ValueError("Notion exact detail descriptionHtml differs from the board row")
        if normalized_title(posting.get("title")) != normalized_title(listing.get("title")):
            raise ValueError("Notion exact detail title differs from the board row")
        if normalized_title(posting.get("departmentName")) != normalized_title(listing.get("department")):
            raise ValueError("Notion exact detail department differs from the board row")
        if normalized_title(posting.get("locationName")) != normalized_title(listing.get("location")):
            raise ValueError("Notion exact detail location differs from the board row")
        detail_secondary = [normalized_title(x) for x in posting.get("secondaryLocationNames") or []]
        listing_secondary = [normalized_title(x.get("location")) for x in listing.get("secondaryLocations") or []
                             if isinstance(x, dict)]
        if detail_secondary != listing_secondary:
            raise ValueError("Notion exact detail secondary locations differ from the board row")
        if posting.get("employmentType") != listing.get("employmentType"):
            raise ValueError("Notion exact detail employment type differs from the board row")
        if posting.get("workplaceType") != listing.get("workplaceType"):
            raise ValueError("Notion exact detail workplace type differs from the board row")
        if normalized_title(card.get("location")) != _location_label(listing):
            raise ValueError("Notion official card location differs from the board row")
        if normalized_title(card.get("department")) != normalized_title(listing.get("department")):
            raise ValueError("Notion official card department differs from the board row")

        job.raw["detail_posting"] = posting
        job.raw["detail_organization"] = {
            "organizationId": parsed["organization"].get("organizationId"),
            "name": parsed["organization"].get("name"),
            "hostedJobsPageSlug": parsed["organization"].get("hostedJobsPageSlug"),
            "publicWebsite": parsed["organization"].get("publicWebsite"),
        }
        job.raw["detail_response"] = {
            "url": job.url,
            "final_url": str(response.url),
            "status": response.status_code,
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
            "description_sha256": parsed["description_sha256"],
            "normalized_chars": parsed["normalized_chars"],
            "normalized_fnv1a64": parsed["normalized_fnv1a64"],
            "description_sections": parsed["description_sections"],
            "description_links": parsed["description_links"],
            "role_attachments": parsed["role_attachments"],
        }
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_source_url = job.url
        job.description_method = "notion.ashby_detail.posting.descriptionHtml"
        job.valid_through = posting.get("applicationDeadline") or None
        job.salary = _salary(listing, posting)
        job.raw["description_sections"] = parsed["description_sections"]
        job.raw["description_links"] = parsed["description_links"]
        job.raw["role_attachments"] = parsed["role_attachments"]
        job.raw["dates"] = {
            "publishedAt": listing.get("publishedAt"),
            "applicationDeadline": posting.get("applicationDeadline"),
            "date_posted_mapping": "ashby.public_board.publishedAt",
            "date_updated_mapping": None,
            "valid_through_mapping": "ashby.detail.posting.applicationDeadline",
        }
        job.raw["compensation"] = listing.get("compensation")
        responsibilities, qualifications = _section_roles(parsed["description_sections"])
        job.responsibilities = responsibilities
        job.qualifications = qualifications
        return job

    async def _hydrate_details(self, jobs: list) -> dict:
        started = time.perf_counter()
        sem = asyncio.Semaphore(self.options.detail_concurrency)
        stats = {"total": len(jobs), "checked": 0, "fetched_jobs": 0, "failed": 0}
        failures = []

        async def one(job):
            async with sem:
                try:
                    await self._detail(job)
                    stats["fetched_jobs"] += 1
                except Exception as exc:
                    job.warnings.append("detail_fetch_failed")
                    reason = f"{type(exc).__name__}: {exc}"
                    self.result.errors.append(f"Detail {job.url}: {reason}")
                    failures.append({"id": job.job_id, "url": job.url, "reason": reason})
                    stats["failed"] += 1
                self.result.validate_job(job)
                self.result.dirty_jobs.append(job)
                stats["checked"] += 1

        await asyncio.gather(*(one(job) for job in jobs))
        stats["duration_seconds"] = round(max(0.0, time.perf_counter() - started), 6)
        self.result.timings["detail_phase_seconds"] = stats["duration_seconds"]
        self.result.metadata["details"] = stats
        self.result.metadata["failed_details"] = failures
        return stats

    async def run(self):
        run_started = time.perf_counter()
        source_wall = 0.0
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_ENTRY,
            "listing_authority": "The server-rendered #open-positions list on Notion's own careers page.",
            "listing_membership": {
                "method": "GET",
                "url": OFFICIAL_ENTRY,
                "scope": "All 154 unique Ashby job links published by the official Notion careers page; no country or seniority filter.",
                "filters": "Location and department controls are client-side over the same unfiltered server-rendered list.",
                "pagination": "No pagination; all cards are present in the initial HTML and rendered DOM.",
            },
            "detail_method": "Each exact linked Ashby detail page is opened; its window.__appData.posting.descriptionHtml is compared with the company-scoped board row.",
            "request_scope": "Only the official Notion careers page, the public Ashby board API for tenant notion, and the exact job URLs linked from the official page.",
        })
        try:
            initial_source_started = time.perf_counter()
            initial_entry = await self._official_snapshot("initial")
            initial_board = await self._board_snapshot("initial")
            initial_source_seconds = max(0.0, time.perf_counter() - initial_source_started)
            source_wall += initial_source_seconds
        except Exception as exc:
            self.result.errors.append(f"Notion source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - run_started)
            return self.result

        self.result.expected_total = initial_entry["count"]
        self.result.metadata["initial_official_entry"] = {
            k: v for k, v in initial_entry.items() if k != "rows"}
        self.result.metadata["initial_board_api"] = {
            k: v for k, v in initial_board.items() if k != "rows"}
        self.result.errors.extend(initial_entry["issues"])
        self.result.errors.extend(initial_board["issues"])

        page_by_id = {row["id"]: row for row in initial_entry["rows"]}
        board_by_id = {row["id"]: row for row in initial_board["rows"]}
        page_ids = initial_entry["job_ids"]
        board_ids = initial_board["job_ids"]
        missing_from_board = [identity for identity in page_ids if identity not in board_by_id]
        extra_on_board = [identity for identity in board_ids if identity not in page_by_id]
        if missing_from_board:
            self.result.errors.append(
                "notion_official_job_ids_missing_from_board_api:" + ",".join(missing_from_board[:20]))
        if extra_on_board:
            self.result.errors.append(
                "notion_board_api_ids_not_published_on_official_page:" + ",".join(extra_on_board[:20]))
        if set(page_ids) != set(board_ids):
            self.result.errors.append("notion_official_and_board_membership_mismatch")

        jobs = []
        for identity in page_ids:
            row = board_by_id.get(identity)
            card = page_by_id.get(identity)
            if row is None or card is None:
                self.result.errors.append(f"notion_linked_job_missing_source_row:{identity}")
                continue
            if normalized_title(row.get("title")) != card["title"]:
                self.result.errors.append(f"notion_board_card_title_mismatch:{identity}")
            if normalized_title(row.get("department")) != card["department"]:
                self.result.errors.append(f"notion_board_card_department_mismatch:{identity}")
            if _location_label(row) != card["location"]:
                self.result.errors.append(f"notion_board_card_location_mismatch:{identity}")
            jobs.append(self._make_job(row, card))
        if len(jobs) != initial_entry["count"]:
            self.result.errors.append("notion_retained_rows_do_not_match_official_listing_count")

        self.result.jobs = jobs
        detail_stats = await self._hydrate_details(jobs)

        final_entry = final_board = None
        final_source_seconds = 0.0
        try:
            final_source_started = time.perf_counter()
            final_entry = await self._official_snapshot("final")
            final_board = await self._board_snapshot("final")
            final_source_seconds = max(0.0, time.perf_counter() - final_source_started)
            source_wall += final_source_seconds
        except Exception as exc:
            self.result.errors.append(f"Notion final source recheck failed: {type(exc).__name__}: {exc}")

        final_matches = False
        if final_entry is not None and final_board is not None:
            self.result.errors.extend(final_entry["issues"])
            self.result.errors.extend(final_board["issues"])
            final_page_by_id = {row["id"]: row for row in final_entry["rows"]}
            final_board_by_id = {row["id"]: row for row in final_board["rows"]}
            changed_fields = {}
            for identity in set(page_by_id) & set(final_page_by_id):
                if page_by_id[identity] != final_page_by_id[identity]:
                    changed_fields[identity] = sorted(
                        key for key in set(page_by_id[identity]) | set(final_page_by_id[identity])
                        if page_by_id[identity].get(key) != final_page_by_id[identity].get(key)
                    )
            changed_board_fields = {}
            for identity in set(board_by_id) & set(final_board_by_id):
                if board_by_id[identity] != final_board_by_id[identity]:
                    changed_board_fields[identity] = sorted(
                        key for key in set(board_by_id[identity]) | set(final_board_by_id[identity])
                        if board_by_id[identity].get(key) != final_board_by_id[identity].get(key)
                    )
            same_page_ids = initial_entry["job_ids"] == final_entry["job_ids"]
            same_board_ids = initial_board["job_ids"] == final_board["job_ids"]
            same_counts = initial_entry["count"] == final_entry["count"] == initial_board["count"] == final_board["count"]
            same_filters = initial_entry["filter_options"] == final_entry["filter_options"]
            same_pagination = initial_entry["pagination_controls"] == final_entry["pagination_controls"]
            final_matches = (same_page_ids and same_board_ids and same_counts and not changed_fields
                             and not changed_board_fields and same_filters and same_pagination
                             and set(final_entry["job_ids"]) == set(final_board["job_ids"])
                             and not final_entry["issues"] and not final_board["issues"])
            self.result.metadata["final_official_entry"] = {
                k: v for k, v in final_entry.items() if k != "rows"}
            self.result.metadata["final_board_api"] = {
                k: v for k, v in final_board.items() if k != "rows"}
            self.result.metadata["membership_reconciliation"] = {
                "initial_page_ids": initial_entry["job_ids"],
                "final_page_ids": final_entry["job_ids"],
                "initial_board_ids": initial_board["job_ids"],
                "final_board_ids": final_board["job_ids"],
                "initial_count": initial_entry["count"],
                "final_count": final_entry["count"],
                "initial_board_count": initial_board["count"],
                "final_board_count": final_board["count"],
                "page_and_board_sets_equal": set(initial_entry["job_ids"]) == set(initial_board["job_ids"]),
                "same_page_ids_and_order": same_page_ids,
                "same_board_ids_and_order": same_board_ids,
                "same_counts": same_counts,
                "changed_card_fields_by_id": changed_fields,
                "changed_board_fields_by_id": changed_board_fields,
                "new_page_ids": [i for i in final_entry["job_ids"] if i not in page_by_id],
                "removed_page_ids": [i for i in page_ids if i not in final_page_by_id],
                "new_board_ids": [i for i in final_board["job_ids"] if i not in board_by_id],
                "removed_board_ids": [i for i in board_ids if i not in final_board_by_id],
                "filter_options_unchanged": same_filters,
                "pagination_controls_unchanged": same_pagination,
                "complete": final_matches,
            }
            if not same_page_ids or not same_board_ids or not same_counts:
                self.result.errors.append("notion_listing_membership_changed_during_collection")
            if changed_fields:
                self.result.errors.append("notion_official_card_fields_changed_during_collection")
            if changed_board_fields:
                self.result.errors.append("notion_board_fields_changed_during_collection")
            if not same_filters:
                self.result.errors.append("notion_filter_options_changed_during_collection")
        else:
            self.result.metadata["membership_reconciliation"] = {
                "initial_page_ids": initial_entry["job_ids"],
                "initial_board_ids": initial_board["job_ids"],
                "complete": False,
                "reason": "final source recheck unavailable",
            }

        self.result.metadata["detail_hydration_mode"] = (
            "Every exact linked Ashby detail page was opened; full posting.descriptionHtml "
            "and all public posting fields were retained."
        )
        self.result.metadata["job_ids"] = [job.job_id for job in jobs]
        self.result.metadata["official_listing_count"] = initial_entry["count"]
        self.result.metadata["board_api_count"] = initial_board["count"]
        self.result.metadata["filter_controls"] = initial_entry["filter_controls"]
        self.result.metadata["filter_options"] = initial_entry["filter_options"]
        self.result.metadata["pagination_controls"] = initial_entry["pagination_controls"]
        self.result.metadata["departments"] = initial_entry["departments"]
        self.result.metadata["current_native_membership_reconciled"] = final_matches
        self.result.metadata["detail_failures"] = detail_stats["failed"]
        self.result.listing_complete = bool(
            final_matches and len(jobs) == initial_entry["count"] and not self.result.errors)
        self.result.timings["source_collection_wall_seconds"] = source_wall
        self.result.timings["initial_source_seconds"] = initial_source_seconds
        self.result.timings["final_source_seconds"] = final_source_seconds
        self.result.metadata["source_collection_breakdown"] = {
            "initial_official_plus_board_seconds": initial_source_seconds,
            "final_official_plus_board_seconds": final_source_seconds,
            "detail_phase_seconds": self.result.timings.get("detail_phase_seconds"),
            "adapter_wall_seconds": max(0.0, time.perf_counter() - run_started),
        }
        return self.result
