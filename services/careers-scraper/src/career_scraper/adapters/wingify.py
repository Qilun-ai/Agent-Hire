"""Wingify's own careers board, its linked Keka portal and its exact AB Tasty job pages.

Wingify publishes its listing on its own WordPress site
(``https://wingify.com/company/careers/``): the board element carries
``data-jobs-endpoint="https://wingify.com/wp-json/api/get-active-jobs"`` and the
page's own JavaScript renders one card per returned row.  Each row points either
at the company's Keka career portal (``wingify.keka.com``) or, for the postings
shared with the merged AB Tasty organisation, at the exact AB Tasty job page the
careers page links to.  The same section also links the Keka portal board
("View all open roles"), which exposes one additional live role.

Membership is therefore the union of those two company-published listing
surfaces, and descriptions come only from the exact job routes they expose.
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html


ENTRY_URL = "https://wingify.com/careers"
CAREERS_URL = "https://wingify.com/company/careers/"
JOBS_ENDPOINT = "https://wingify.com/wp-json/api/get-active-jobs"
KEKA_ORIGIN = "https://wingify.keka.com"
KEKA_BOARD_URL = KEKA_ORIGIN + "/careers/"
KEKA_ACTIVE_API = KEKA_ORIGIN + "/careers/api/jobs/default/active"
ABTASTY_ORIGIN = "https://careers.abtasty.com"
APPLY_LABEL = "Apply now"
KEKA_JOB_PATH = re.compile(r"^/careers/jobdetails/(\d+)$")
ABTASTY_JOB_PATH = re.compile(r"^/jobs/(\d+)-([a-z0-9-]+)$")
KEKA_UNAVAILABLE_TEXT = "The Job posting is not available anymore"
# Keka's own careers bundle maps these ids: {1: "Part Time", 2: "Full Time"}.
KEKA_JOB_TYPES = {1: "Part Time", 2: "Full Time"}
ABTASTY_EMPLOYMENT_TYPES = {
    "FULL_TIME": "Full Time",
    "PART_TIME": "Part Time",
    "CONTRACTOR": "Contractor",
    "TEMPORARY": "Temporary",
    "INTERN": "Intern",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
RESPONSIBILITY_TOKENS = ("responsibilit", "what you will do", "what you'll do", "your role",
                         "role overview")
QUALIFICATION_TOKENS = ("qualif", "requirement", "what we are looking for", "what we're looking for",
                        "what you bring", "what you'll need", "what you will need", "who you are",
                        "skills", "about you")


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return normalized_text(soup.get_text(" ", strip=True))


def job_url_identity(url: str) -> tuple[str, str] | None:
    """Return (provider, source id) for an exact job route exposed by the careers page."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https":
        return None
    if parts.netloc == "wingify.keka.com":
        match = KEKA_JOB_PATH.fullmatch(parts.path)
        return ("keka", match.group(1)) if match else None
    if parts.netloc == "careers.abtasty.com":
        match = ABTASTY_JOB_PATH.fullmatch(parts.path)
        return ("abtasty", match.group(1)) if match else None
    return None


def parse_official_entry(html: str, response_url: str) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "wingify.com"
            or parts.path.rstrip("/") != "/company/careers"):
        raise ValueError("Wingify official careers entry no longer resolves to its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    sections = soup.select("section#open-positions")
    if len(sections) != 1:
        raise ValueError("Wingify official careers page no longer exposes one open-positions section")
    board = sections[0].select_one(".js-careers-board")
    if board is None:
        raise ValueError("Wingify open-positions section lost its jobs board element")
    endpoint = board.get("data-jobs-endpoint")
    if endpoint != JOBS_ENDPOINT:
        raise ValueError(f"Wingify careers board jobs endpoint changed: {endpoint!r}")
    fallback = sections[0].select_one(".js-careers-fallback a[href]")
    fallback_url = fallback.get("href") if fallback else None
    if fallback_url != KEKA_BOARD_URL:
        raise ValueError(f"Wingify careers fallback ATS link changed: {fallback_url!r}")
    return {
        "url": response_url,
        "page_title": soup.title.get_text(" ", strip=True) if soup.title else None,
        "section_id": sections[0].get("id"),
        "jobs_endpoint": endpoint,
        "apply_label": board.get("data-apply-label"),
        "fallback_ats_url": fallback_url,
        "team_filter_container": bool(sections[0].select_one(".js-careers-team-filters")),
        "experience_filter": bool(sections[0].select_one(".js-careers-exp-lower")),
        "pagination_controls": [
            node.get_text(" ", strip=True) for node in sections[0].select("button")
            if re.search(r"\b(?:next|previous|load more|page \d)\b", node.get_text(" ", strip=True), re.I)
        ],
        "hiring_contact": sorted({
            anchor.get("href") for anchor in sections[0].select('a[href^="mailto:"]') if anchor.get("href")
        }),
    }


def parse_jobs_feed(value, *, response_url: str = JOBS_ENDPOINT) -> dict:
    if response_url != JOBS_ENDPOINT:
        raise ValueError("Wingify active-jobs feed answered from an unexpected URL")
    if not isinstance(value, list) or not value:
        raise ValueError("Wingify active-jobs feed is no longer a non-empty flat list")
    rows, issues, seen = [], [], set()
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            issues.append(f"row_{index}_not_object")
            continue
        missing = {"title", "jobLocations", "careerPortalUrl", "departmentName", "jobId", "experience"} - set(row)
        if missing:
            issues.append(f"row_{index}_missing_fields:{','.join(sorted(missing))}")
            continue
        url = row.get("careerPortalUrl") or ""
        identity = job_url_identity(url)
        if identity is None:
            issues.append(f"row_{index}_unexpected_job_route:{url}")
            continue
        provider, job_id = identity
        if provider == "abtasty" and str(row.get("jobId") or "") != job_id:
            issues.append(f"row_{index}_job_id_mismatch:{row.get('jobId')}!={job_id}")
        if provider == "keka" and str(row.get("jobId") or ""):
            issues.append(f"row_{index}_unexpected_keka_job_id:{row.get('jobId')}")
        if not normalized_text(row.get("title")):
            issues.append(f"row_{index}_missing_title")
            continue
        key = f"{provider}:{job_id}"
        if key in seen:
            issues.append(f"duplicate_feed_job:{key}")
            continue
        seen.add(key)
        if not isinstance(row.get("jobLocations"), list):
            issues.append(f"row_{index}_locations_not_list")
        rows.append({**row, "provider": provider, "id": job_id})
    if not rows:
        raise ValueError("Wingify active-jobs feed contained no usable company job rows")
    return {
        "rows": rows,
        "count": len(rows),
        "raw_count": len(value),
        "keka_count": sum(row["provider"] == "keka" for row in rows),
        "abtasty_count": sum(row["provider"] == "abtasty" for row in rows),
        "issues": issues,
        "row_fields": sorted({key for row in value if isinstance(row, dict) for key in row}),
    }


def parse_keka_board(value, *, response_url: str = KEKA_ACTIVE_API) -> dict:
    if response_url != KEKA_ACTIVE_API:
        raise ValueError("Wingify Keka board answered from an unexpected URL")
    if not isinstance(value, list) or not value:
        raise ValueError("Wingify Keka board active-job feed is empty or no longer a list")
    rows, issues, seen = [], [], set()
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            issues.append(f"keka_row_{index}_not_object")
            continue
        job_id = row.get("id")
        if not isinstance(job_id, int) or job_id <= 0:
            issues.append(f"keka_row_{index}_missing_numeric_id")
            continue
        identity = str(job_id)
        if identity in seen:
            issues.append(f"duplicate_keka_job:{identity}")
            continue
        if not normalized_text(row.get("title")) or not isinstance(row.get("description"), str):
            issues.append(f"keka_row_{identity}_missing_title_or_description")
            continue
        if len(normalized_body_text(row["description"])) < 100:
            issues.append(f"keka_row_{identity}_description_empty_or_truncated")
        seen.add(identity)
        rows.append(dict(row))
    if not rows:
        raise ValueError("Wingify Keka board contained no usable active postings")
    return {
        "rows": rows,
        "count": len(rows),
        "issues": issues,
        "row_fields": sorted({key for row in value if isinstance(row, dict) for key in row}),
    }


def description_sections(html: str) -> list[dict]:
    """Split an employer-authored body into heading/body blocks without losing text."""
    soup = BeautifulSoup(f'<div data-wingify-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-wingify-body]")
    sections, current = [], None
    for block in (root.contents if root else []):
        if not isinstance(block, Tag):
            continue
        heading = ""
        if block.name in HEADING_TAGS:
            heading = normalized_text(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full = normalized_text(block.get_text(" ", strip=True))
            strong_text = normalized_text(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full and len(full) <= 120:
                heading = full
        if heading:
            if current is not None:
                current["text"] = text_html(current["html"])
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def classified_sections(sections: list[dict]) -> tuple[list[dict], list[dict]]:
    responsibilities, qualifications = [], []
    for section in sections:
        heading = (section.get("heading") or "").lower()
        if not heading or not section.get("text"):
            continue
        if any(token in heading for token in RESPONSIBILITY_TOKENS):
            responsibilities.append({"heading": section["heading"], "text": section["text"]})
        elif any(token in heading for token in QUALIFICATION_TOKENS):
            qualifications.append({"heading": section["heading"], "text": section["text"]})
    return responsibilities, qualifications


def parse_keka_detail(html: str, job_id: str, response_url: str) -> dict:
    parts = urlsplit(response_url)
    expected_path = f"/careers/jobdetails/{job_id}"
    if parts.scheme != "https" or parts.netloc != "wingify.keka.com" or parts.path != expected_path:
        raise ValueError("Wingify Keka detail answered from an unexpected route")
    soup = BeautifulSoup(html, "lxml")
    page_text = normalized_body_text(html)
    removed = KEKA_UNAVAILABLE_TEXT in page_text
    heading = soup.select_one("h1")
    container = soup.select_one(".job-description-container")
    chips = [normalized_text(node.get_text(" ", strip=True))
             for node in soup.select(".job-details-container .banner .row span.text-white")]
    chips = [chip for chip in chips if chip]
    brand = soup.select_one(".navbar-brand span")
    employer = normalized_text(brand.get_text(" ", strip=True)) if brand else None
    all_jobs = soup.select_one("#all-jobs")
    apply_button = soup.select_one("#apply-job")
    return {
        "url": response_url,
        "job_id": job_id,
        "removed": removed,
        "removed_message": KEKA_UNAVAILABLE_TEXT if removed else None,
        "title": normalized_text(heading.get_text(" ", strip=True)) if heading else None,
        "title_attribute": heading.get("title") if heading else None,
        "employer": employer,
        "header_chips": chips,
        "description_html": str(container) if container is not None else None,
        "description_text": normalized_body_text(str(container)) if container is not None else "",
        "apply_control": normalized_text(apply_button.get_text(" ", strip=True)) if apply_button else None,
        "all_jobs_control": normalized_text(all_jobs.get_text(" ", strip=True)) if all_jobs else None,
        "application_route": KEKA_ORIGIN + f"/careers/applyjob/{job_id}",
    }


def parse_abtasty_detail(html: str, job_id: str, response_url: str, *, slug: str | None = None) -> dict:
    parts = urlsplit(response_url)
    match = ABTASTY_JOB_PATH.fullmatch(parts.path)
    if parts.scheme != "https" or parts.netloc != "careers.abtasty.com" or match is None:
        raise ValueError("Wingify careers-page AB Tasty job link answered from an unexpected URL")
    if match.group(1) != str(job_id) or (slug and match.group(2) != slug):
        raise ValueError("Wingify careers-page AB Tasty job link changed identity")
    soup = BeautifulSoup(html, "lxml")
    postings = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or "")
        except ValueError as exc:
            raise ValueError(f"AB Tasty job JSON-LD is not valid JSON: {exc}") from exc
        for item in (value if isinstance(value, list) else [value]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                postings.append(item)
    if len(postings) != 1:
        raise ValueError("AB Tasty job page must expose exactly one JobPosting JSON-LD object")
    posting = postings[0]
    identifier = posting.get("identifier") or {}
    if str(identifier.get("value")) != str(job_id):
        raise ValueError("AB Tasty job JSON-LD identifier does not match the company-linked job URL")
    if not isinstance(posting.get("description"), str):
        raise ValueError("AB Tasty job JSON-LD is missing its full description field")
    expected = normalized_body_text(posting["description"])
    candidates = [node for node in soup.select("div.prose")
                  if normalized_body_text(str(node)) == expected]
    if not candidates:
        raise ValueError("AB Tasty rendered job body does not match its JSON-LD description")
    heading = soup.select_one("h1")
    apply_url = None
    main = soup.select_one("main[data-careersite--jobs--form-overlay-job-application-url-value]")
    if main is not None:
        apply_url = main.get("data-careersite--jobs--form-overlay-job-application-url-value")
    return {
        "url": response_url,
        "job_id": str(job_id),
        "title": normalized_text(posting.get("title") or ""),
        "page_heading": normalized_text(heading.get_text(" ", strip=True)) if heading else None,
        "date_posted": posting.get("datePosted"),
        "date_posted_raw": posting.get("datePosted"),
        "valid_through": posting.get("validThrough"),
        "employment_type": posting.get("employmentType"),
        "direct_apply": posting.get("directApply"),
        "employer": (posting.get("hiringOrganization") or {}).get("name"),
        "employer_same_as": (posting.get("hiringOrganization") or {}).get("sameAs"),
        "job_locations": posting.get("jobLocation") or [],
        "description_html": posting["description"],
        "description_text": expected,
        "description_chars": len(expected),
        "description_fnv1a64": fnv1a64(expected),
        "rendered_body_matches_json_ld": True,
        "apply_url": apply_url or (response_url.rstrip("/") + "/applications/new"),
        "posting_fields": sorted(posting),
    }


def _keka_locations(row: dict) -> list[str]:
    names = []
    for location in row.get("jobLocations") or []:
        if isinstance(location, dict):
            name = normalized_text(location.get("name"))
            if name and name not in names:
                names.append(name)
    return names


def _listing_locations(row: dict) -> list[str]:
    names = []
    for location in row.get("jobLocations") or []:
        if isinstance(location, dict):
            name = normalized_text(location.get("name"))
            if name and name not in names:
                names.append(name)
    return names


def _abtasty_locations(postings) -> list[str]:
    names = []
    for place in postings or []:
        address = place.get("address") if isinstance(place, dict) else None
        if not isinstance(address, dict):
            continue
        parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
        value = ", ".join(str(part).strip() for part in parts if part)
        if value and value not in names:
            names.append(value)
    return names


def _salary(row: dict):
    value = row.get("salaryRange")
    if not isinstance(value, dict):
        return None
    minimum, maximum = value.get("minimum"), value.get("maximum")
    if not any(isinstance(bound, (int, float)) and bound > 0 for bound in (minimum, maximum)):
        return None
    return {"min": minimum, "max": maximum, "currency": value.get("currency"),
            "period": value.get("salaryPeriod"), "format": row.get("salaryRangeFormat") or None}


class Wingify(Adapter):
    """Read only Wingify's own board feed, its linked Keka board and exact linked job pages."""

    VERIFICATION_REVISION = "wingify.careers-board+keka+abtasty-exact-links.1"

    async def _get_timed(self, url: str, **kwargs):
        started = time.perf_counter()
        response = await self.http.get(url, **kwargs)
        return response, max(0.0, time.perf_counter() - started)

    async def _official_entry(self):
        response, seconds = await self._get_timed(ENTRY_URL)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(
            requested_url=ENTRY_URL,
            redirects=[{"status": item.status_code, "url": str(item.url)} for item in response.history],
            final_url=str(response.url),
            status=response.status_code,
            seconds=seconds,
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
        )
        return parsed

    async def _jobs_feed(self):
        attempts, parsed = [], None
        for attempt in (1, 2):
            response, seconds = await self._get_timed(JOBS_ENDPOINT)
            parsed = parse_jobs_feed(response.json(), response_url=str(response.url))
            parsed.update(
                requested_url=JOBS_ENDPOINT,
                final_url=str(response.url),
                status=response.status_code,
                seconds=seconds,
                response_bytes=len(response.content),
                response_sha256=sha256(response.content).hexdigest(),
                attempt=attempt,
            )
            attempts.append({"attempt": attempt, "count": parsed["count"],
                             "issues": list(parsed["issues"]),
                             "response_sha256": parsed["response_sha256"]})
            if not parsed["issues"]:
                break
            # The inspected endpoint once returned a stale variant whose AB Tasty
            # rows still pointed at dead Keka routes. Re-read once before aborting.
        parsed["attempts"] = attempts
        parsed["issues"] = list(parsed["issues"])
        if parsed["issues"] and len(attempts) == 2:
            raise ValueError(f"Wingify active-jobs feed kept unusable rows: {parsed['issues'][:5]}")
        return parsed

    async def _keka_board(self):
        response, seconds = await self._get_timed(KEKA_ACTIVE_API, headers={"Referer": KEKA_BOARD_URL})
        parsed = parse_keka_board(response.json(), response_url=str(response.url))
        parsed.update(
            requested_url=KEKA_ACTIVE_API,
            final_url=str(response.url),
            status=response.status_code,
            seconds=seconds,
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
        )
        return parsed

    def _make_keka_job(self, listing: dict, board_row: dict):
        job_id = str(board_row["id"])
        description_html = board_row.get("description") or ""
        sections = description_sections(description_html)
        responsibilities, qualifications = classified_sections(sections)
        locations = _keka_locations(board_row) or _listing_locations(listing)
        body = normalized_body_text(description_html)
        job = self.job(
            {"provider": "keka", "listing": listing, "keka_board_row": board_row},
            title=normalized_text(board_row.get("title") or listing.get("title")),
            url=listing["careerPortalUrl"],
            job_id=job_id,
            apply_url=KEKA_ORIGIN + f"/careers/applyjob/{job_id}",
            description=description_html,
            method="wingify.keka.active.description",
            description_url=KEKA_ACTIVE_API,
            locations=locations,
            department=board_row.get("departmentName") or listing.get("departmentName"),
            employment_type=KEKA_JOB_TYPES.get(board_row.get("jobType")),
            workplace_type="Remote" if "Remote" in locations else None,
            date_posted=board_row.get("publishedOn"),
            date_posted_raw=board_row.get("publishedOn"),
            salary=_salary(board_row),
            employer="Wingify Software Pvt. Ltd.",
            experience=board_row.get("experience") or listing.get("experience") or None,
            responsibilities=responsibilities or None,
            qualifications=qualifications or None,
            language="en",
        )
        job.raw["detail"] = {
            "description_sha256": sha256(description_html.encode()).hexdigest(),
            "description_normalized_chars": len(body),
            "description_fnv1a64": fnv1a64(body),
            "description_sections": sections,
            "application_route": {
                "listing_cta": APPLY_LABEL,
                "detail_apply_control": "Apply for this job",
                "apply_url": job.apply_url,
                "observed_control": "button#apply-job -> /careers/applyjob/<id>",
            },
            "listing_sources": ["wingify-careers-board", "wingify-keka-board"],
            "skill_names": board_row.get("skillNames") or [],
            "published_since_days": board_row.get("publishedSinceDays"),
        }
        return job

    def _make_abtasty_job(self, listing: dict):
        job_id = str(listing["jobId"])
        url = listing["careerPortalUrl"]
        job = self.job(
            {"provider": "abtasty", "listing": listing},
            title=normalized_text(listing.get("title")),
            url=url,
            job_id=job_id,
            apply_url=url.rstrip("/") + "/applications/new",
            description="",
            method="wingify.careers-page.abtasty.teamtailor.json-ld.description",
            description_url=url,
            locations=_listing_locations(listing),
            department=listing.get("departmentName"),
            experience=listing.get("experience") or None,
            language="en",
        )
        job.raw["detail"] = {
            "listing_sources": ["wingify-careers-board"],
            "application_route": {"listing_cta": APPLY_LABEL, "apply_url": job.apply_url},
        }
        return job

    async def detail(self, job):
        provider = job.raw.get("provider")
        if provider == "keka":
            return await self._keka_detail(job)
        if provider == "abtasty":
            return await self._abtasty_detail(job)
        raise ValueError(f"Wingify job has no inspected provider: {job.url}")

    async def _keka_detail(self, job):
        response, seconds = await self._get_timed(job.url)
        parsed = parse_keka_detail(response.text, job.job_id, str(response.url))
        page_record = {
            "url": parsed["url"],
            "removed": parsed["removed"],
            "title": parsed["title"],
            "title_attribute": parsed["title_attribute"],
            "employer": parsed["employer"],
            "header_chips": parsed["header_chips"],
            "description_normalized_chars": len(parsed["description_text"]),
            "description_fnv1a64": fnv1a64(parsed["description_text"]),
            "apply_control": parsed["apply_control"],
            "all_jobs_control": parsed["all_jobs_control"],
            "status": response.status_code,
            "seconds": seconds,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
        }
        job.raw["detail"]["page"] = page_record
        if parsed["removed"] or not parsed["description_text"]:
            job.description_unavailable = True
            job.warnings.append("posting_removed_on_detail_page")
            self.result.errors.append(f"Wingify Keka posting no longer available: {job.url}")
            return job
        job.raw["detail"]["matches_detail_page_body"] = (
            parsed["description_text"] == normalized_body_text(job.description_html))
        if not job.raw["detail"]["matches_detail_page_body"]:
            job.warnings.append("description_not_full_field")
            self.result.errors.append(f"Wingify Keka API description differs from detail page: {job.url}")
        if parsed["title"] and normalized_text(parsed["title"]) != normalized_text(job.title):
            job.warnings.append("detail_title_mismatch")
            self.result.errors.append(f"Wingify Keka detail title mismatch: {job.url}")
        if parsed["header_chips"]:
            if len(parsed["header_chips"]) > 1:
                page_type = parsed["header_chips"][1]
                job.raw["detail"]["page_employment_label"] = page_type
                if job.employment_type and page_type.replace("-", " ").lower() != job.employment_type.lower():
                    job.warnings.append("employment_type_mismatch")
        return job

    async def _abtasty_detail(self, job):
        slug = ABTASTY_JOB_PATH.fullmatch(urlsplit(job.url).path).group(2)
        response, seconds = await self._get_timed(job.url)
        parsed = parse_abtasty_detail(response.text, job.job_id, str(response.url), slug=slug)
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.valid_through = parsed["valid_through"]
        job.employment_type = ABTASTY_EMPLOYMENT_TYPES.get(
            str(parsed["employment_type"] or "").upper(), parsed["employment_type"])
        job.employer = parsed["employer"]
        job.locations = _abtasty_locations(parsed["job_locations"]) or job.locations
        job.apply_url = parsed["apply_url"]
        sections = description_sections(job.description_html)
        responsibilities, qualifications = classified_sections(sections)
        job.responsibilities = responsibilities or None
        job.qualifications = qualifications or None
        job.raw["detail"].update({
            "page": {
                "url": parsed["url"],
                "title": parsed["title"],
                "page_heading": parsed["page_heading"],
                "description_normalized_chars": parsed["description_chars"],
                "description_fnv1a64": parsed["description_fnv1a64"],
                "rendered_body_matches_json_ld": parsed["rendered_body_matches_json_ld"],
                "direct_apply": parsed["direct_apply"],
                "posting_fields": parsed["posting_fields"],
                "status": response.status_code,
                "seconds": seconds,
                "response_sha256": sha256(response.content).hexdigest(),
                "response_bytes": len(response.content),
            },
            "description_sha256": sha256(job.description_html.encode()).hexdigest(),
            "description_normalized_chars": parsed["description_chars"],
            "description_fnv1a64": parsed["description_fnv1a64"],
            "description_sections": sections,
            "matches_detail_page_body": parsed["rendered_body_matches_json_ld"],
            "job_locations": parsed["job_locations"],
            "employment_type_raw": parsed["employment_type"],
            "employer_same_as": parsed["employer_same_as"],
            "direct_apply": parsed["direct_apply"],
            "application_route": {
                "listing_cta": APPLY_LABEL,
                "apply_url": job.apply_url,
                "observed_form_overlay_url": parsed["apply_url"],
            },
        })
        if normalized_text(parsed["title"]) != normalized_text(job.title):
            job.warnings.append("detail_title_mismatch")
            self.result.errors.append(f"Wingify AB Tasty detail title mismatch: {job.url}")
        return job

    def _union_rows(self, feed: dict, board: dict):
        order, by_key = [], {}
        for row in feed["rows"]:
            key = f"{row['provider']}:{row['id']}"
            by_key[key] = {"provider": row["provider"], "listing": row, "board_row": None,
                           "listing_sources": ["wingify-careers-board"]}
            order.append(key)
        board_index = {str(row["id"]): row for row in board["rows"]}
        for row in feed["rows"]:
            if row["provider"] != "keka":
                continue
            board_row = board_index.get(row["id"])
            if board_row is None:
                raise ValueError(
                    f"Wingify careers page lists Keka job {row['id']} that its own Keka board omits")
            by_key[f"keka:{row['id']}"]["board_row"] = board_row
            by_key[f"keka:{row['id']}"]["listing_sources"] = [
                "wingify-careers-board", "wingify-keka-board"]
        for job_id, row in board_index.items():
            key = f"keka:{job_id}"
            if key in by_key:
                continue
            order.append(key)
            by_key[key] = {
                "provider": "keka",
                "listing": {
                    "title": row["title"],
                    "departmentName": row.get("departmentName"),
                    "experience": row.get("experience"),
                    "jobLocations": row.get("jobLocations") or [],
                    "careerPortalUrl": f"{KEKA_ORIGIN}/careers/jobdetails/{job_id}",
                    "jobId": "",
                    "provider": "keka",
                    "id": job_id,
                },
                "board_row": row,
                "listing_sources": ["wingify-keka-board"],
            }
        return order, by_key

    async def run(self):
        source_started = time.perf_counter()
        entry = await self._official_entry()
        self.result.metadata["official_entry"] = entry
        if not self.page_guard():
            return self.result
        feed = await self._jobs_feed()
        if feed["issues"]:
            self.result.warnings.append("wingify_careers_feed_row_issues")
            self.result.errors.extend(f"careers_feed:{issue}" for issue in feed["issues"])
        if not self.page_guard():
            return self.result
        board = await self._keka_board()
        if board["issues"]:
            self.result.warnings.append("wingify_keka_board_row_issues")
            self.result.errors.extend(f"keka_board:{issue}" for issue in board["issues"])
        collection_seconds = max(0.0, time.perf_counter() - source_started)
        self.result.metadata["initial_listing"] = {
            "careers_page": {key: value for key, value in feed.items() if key != "rows"},
            "keka_board": {key: value for key, value in board.items() if key != "rows"},
        }
        self.result.metadata["careers_page_rows"] = [
            {key: row.get(key) for key in ("title", "jobLocations", "careerPortalUrl",
                                           "departmentName", "jobId", "experience")}
            | {"provider": row["provider"], "id": row["id"]}
            for row in feed["rows"]
        ]
        self.result.metadata["keka_board_rows"] = [
            {key: row.get(key) for key in ("id", "title", "departmentName", "jobType", "experience",
                                           "publishedOn", "publishedSinceDays", "jobLocations",
                                           "salaryRange", "salaryRangeFormat", "skillNames")}
            for row in board["rows"]
        ]

        order, union = self._union_rows(feed, board)
        for key in order:
            item = union[key]
            if item["provider"] == "keka":
                job = self._make_keka_job(item["listing"], item["board_row"])
            else:
                job = self._make_abtasty_job(item["listing"])
            job.raw["detail"]["listing_sources"] = item["listing_sources"]
            self.add(job)
        self.result.expected_total = len(order)
        self.result.listing_complete = len(self.result.jobs) == len(order)

        self.result.jobs = await self.hydrate(self.result.jobs, self.detail)
        detail_seconds = self.result.timings.get("detail_phase_seconds", 0.0)

        final_started = time.perf_counter()
        final_feed = await self._jobs_feed()
        final_board = await self._keka_board()
        final_order, _ = self._union_rows(final_feed, final_board)
        collected_ids = [f"{job.raw['provider']}:{job.job_id}" for job in self.result.jobs]
        feed_ids = [f"{row['provider']}:{row['id']}" for row in feed["rows"]]
        final_feed_ids = [f"{row['provider']}:{row['id']}" for row in final_feed["rows"]]
        board_ids = [f"keka:{row['id']}" for row in board["rows"]]
        final_board_ids = [f"keka:{row['id']}" for row in final_board["rows"]]
        reconciliation = {
            "careers_page_ids": feed_ids,
            "final_careers_page_ids": final_feed_ids,
            "keka_board_ids": board_ids,
            "final_keka_board_ids": final_board_ids,
            "collected_ids": collected_ids,
            "careers_page_count": len(feed_ids),
            "keka_board_count": len(board_ids),
            "union_count": len(order),
            "overlap_count": len(set(feed_ids) & set(board_ids)),
            "careers_page_only": sorted(set(feed_ids) - set(board_ids)),
            "keka_board_only": sorted(set(board_ids) - set(feed_ids)),
            "careers_page_unchanged": feed_ids == final_feed_ids,
            "keka_board_unchanged": board_ids == final_board_ids,
            "union_unchanged": order == final_order,
            "collected_order_matches_union": collected_ids == order,
            "same_membership": collected_ids == order and order == final_order,
        }
        if not reconciliation["same_membership"]:
            self.result.listing_complete = False
            self.result.errors.append("wingify_final_membership_recheck_mismatch")
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": ENTRY_URL,
            "listing_url": CAREERS_URL,
            "listing_authority": (
                "Wingify's own careers board (its WordPress get-active-jobs feed, rendered by the "
                "careers page) plus the Keka portal board that the same section links for all open roles."),
            "listing_membership_requests": {
                "careers_board": {"method": "GET", "url": JOBS_ENDPOINT, "query": None,
                                  "filters_applied": False, "pagination": None,
                                  "row_fields": feed["row_fields"], "count": feed["count"]},
                "keka_board": {"method": "GET", "url": KEKA_ACTIVE_API, "query": None,
                               "filters_applied": False, "pagination": None,
                               "row_fields": board["row_fields"], "count": board["count"]},
            },
            "request_scope": ("Only wingify.com's own board feed, the linked wingify.keka.com active board, "
                              "and the exact Keka/AB Tasty job routes those two surfaces expose."),
            "detail_method": (
                "Keka: full employer description field from the active board feed, checked against the "
                "exact detail page body. AB Tasty: JobPosting JSON-LD on the exact company-linked job "
                "page, checked against that page's rendered body."),
            "membership_reconciliation": reconciliation,
            "provider_counts": {
                "keka_collected": sum(job.raw["provider"] == "keka" for job in self.result.jobs),
                "abtasty_collected": sum(job.raw["provider"] == "abtasty" for job in self.result.jobs),
            },
            "initial_collection_seconds": round(collection_seconds, 6),
            "detail_phase_seconds": round(detail_seconds, 6),
            "final_membership_recheck_seconds": round(max(0.0, time.perf_counter() - final_started), 6),
        })
        self.result.timings["source_collection_seconds"] = round(collection_seconds, 6)
        self.result.timings["final_membership_recheck_seconds"] = round(
            self.result.metadata["final_membership_recheck_seconds"], 6)
        return self.result
