"""HighRadius' own careers page and the Greenhouse board it publishes.

Observed journey (see companies/highradius/journey-research.md):
- The ledger entry https://www.highradius.com/about/careers/ redirects to the
  company's current page https://www.highradius.com/about/career/.
- That page contains 68 server-rendered HighRadius job cards and publishes the
  exact Greenhouse board embed script
  https://boards.greenhouse.io/embed/job_board/js?for=highradius.
- Every card links to https://www.highradius.com/about/careers-list/?gh_jid=<id>.
  The company page's own embed script mounts the matching Greenhouse application
  iframe (job_app?for=highradius&token=<id>), whose job description is the same
  content as the company-scoped board API.
- Membership is the full unfiltered company board (one page, 68 rows); the page's
  location/department/search controls are display filters only and are not
  applied. No other Greenhouse board, slug, aggregator, or ATS tenant is queried.

Descriptions are taken from the same company-scoped board request with
``content=true``. Two representative per-job board endpoints and the rendered
company iframes are checked independently; dates are source-only (Greenhouse
first_published/updated_at) and unknown values stay null.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from html import unescape
import re
import time
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Comment, Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://www.highradius.com/about/careers/"
OFFICIAL_FINAL = "https://www.highradius.com/about/career/"
LISTING_PAGE = OFFICIAL_FINAL
BOARD_API = "https://boards-api.greenhouse.io/v1/boards/highradius/jobs?content=true"
BOARD_DETAIL_BASE = "https://boards-api.greenhouse.io/v1/boards/highradius/jobs"
JOB_APP_BASE = "https://job-boards.greenhouse.io/embed/job_app"
BOARD_EMBED_SCRIPT = "https://boards.greenhouse.io/embed/job_board/js?for=highradius"
COMPANY_NAME = "HighRadius"
BOARD_SLUG = "highradius"
JOB_LINK_PATH = re.compile(r"^/about/careers-list/$")
GREENHOUSE_ID = re.compile(r"^\d{7,13}$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_description(html: str | None) -> str:
    """Normalised visible description text; used for rendered-body comparisons."""
    value = compact(text_html(html))
    # text_html separates block/inline tags with newlines; the browser keeps
    # adjacent inline punctuation attached (for example "</strong>.").
    return re.sub(r"\s+([,.;:!?])", r"\1", value)


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _decode_wrapper(html: str | None) -> str:
    """Decode only a wholly HTML-escaped Greenhouse content wrapper."""
    value = html or ""
    for _ in range(2):
        start = r'^\s*&(?:amp;)*lt;(?:p|div|section|article|ul|ol|h[1-6])(?:\s|&(?:amp;)*gt;)'
        end = r'&(?:amp;)*lt;/(?:p|div|section|article|ul|ol|h[1-6])&(?:amp;)*gt;\s*$'
        if not (re.search(start, value, re.I) and re.search(end, value, re.I)):
            break
        value = unescape(value)
    return value


def _valid_iso(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def description_sections(html: str | None) -> list[dict]:
    """Keep employer-authored headings and the exact body below each heading."""
    soup = BeautifulSoup(f'<div data-highradius-body="true">{_decode_wrapper(html)}</div>', "lxml")
    root = soup.select_one("[data-highradius-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections: list[dict] = []
    current: dict | None = None
    for block in blocks:
        heading = ""
        if block.name in HEADING_TAGS:
            heading = compact(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find("strong", recursive=False)
            full_text = compact(block.get_text(" ", strip=True))
            strong_text = compact(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text.rstrip(":")
        if heading:
            current = {"heading": heading, "html": "", "text": ""}
            sections.append(current)
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
            sections.append(current)
        current["html"] += str(block)
    for section in sections:
        section["text"] = text_html(section["html"])
    return sections or [{"heading": None, "html": "", "text": text_html(html)}]


def _section_matches(sections: list[dict], tokens: tuple[str, ...]) -> list[dict] | None:
    values = [section for section in sections
              if section.get("heading") and section.get("text")
              and any(token in section["heading"].lower() for token in tokens)]
    return values or None


def _links(html: str | None) -> list[dict]:
    soup = BeautifulSoup(_decode_wrapper(html), "lxml")
    return [{"text": compact(a.get_text(" ", strip=True)), "url": a.get("href")}
            for a in soup.select("a[href]")]


def _attachments(links: list[dict]) -> list[dict]:
    return [link for link in links if link.get("url") and ATTACHMENT_SUFFIX.search(link["url"])]


def job_url(identity: str) -> str:
    if not GREENHOUSE_ID.fullmatch(str(identity)):
        raise ValueError(f"HighRadius job ID is not a Greenhouse ID: {identity!r}")
    return f"https://www.highradius.com/about/careers-list/?gh_jid={identity}"


def board_detail_url(identity: str) -> str:
    if not GREENHOUSE_ID.fullmatch(str(identity)):
        raise ValueError(f"HighRadius job ID is not a Greenhouse ID: {identity!r}")
    return f"{BOARD_DETAIL_BASE}/{identity}"


def job_app_url(identity: str) -> str:
    if not GREENHOUSE_ID.fullmatch(str(identity)):
        raise ValueError(f"HighRadius job ID is not a Greenhouse ID: {identity!r}")
    return f"{JOB_APP_BASE}?for={BOARD_SLUG}&token={identity}"


def _parse_company_job_url(url: str, identity: str) -> None:
    parts = urlsplit(str(url))
    if (parts.scheme != "https" or parts.hostname != "www.highradius.com"
            or not JOB_LINK_PATH.fullmatch(parts.path)
            or parse_qs(parts.query) != {"gh_jid": [str(identity)]}):
        raise ValueError(f"HighRadius job URL is not the exact company-published card target: {url}")


def parse_official_entry(html: str, response_url: str = OFFICIAL_FINAL) -> dict:
    """Read the company page's cards and its company-scoped Greenhouse embed."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname != "www.highradius.com"
            or parts.path.rstrip("/") != "/about/career"):
        raise ValueError("HighRadius official careers entry redirected away from its current page")
    soup = BeautifulSoup(html, "lxml")
    title = compact(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "Careers at HighRadius" not in title:
        raise ValueError("HighRadius official careers page title changed")

    scripts = [script.get("src", "") for script in soup.select("script[src]")]
    commented_embed = any(BOARD_EMBED_SCRIPT in str(node) for node in soup.find_all(string=lambda text: isinstance(text, Comment)))
    container = soup.select_one("#job_display")
    if container is None:
        raise ValueError("HighRadius official page no longer has the job display container")

    cards, issues, seen = [], [], set()
    for card in container.select(".gh-job-card"):
        anchor = card.select_one("a[href]")
        title_node = card.select_one(".job-title")
        location_node = card.select_one(".job_loc_icon")
        date_node = card.select_one(".job-date")
        if not anchor or not title_node or not location_node or not date_node:
            issues.append("company_job_card_fields_missing")
            continue
        href = anchor.get("href", "")
        target = urlsplit(href)
        query = parse_qs(target.query)
        identity = (query.get("gh_jid") or [""])[0]
        if not GREENHOUSE_ID.fullmatch(identity):
            issues.append(f"company_job_card_invalid_gh_jid:{href}")
            continue
        if identity in seen:
            issues.append(f"company_job_card_duplicate_id:{identity}")
            continue
        try:
            _parse_company_job_url(href, identity)
        except ValueError as exc:
            issues.append(str(exc))
            continue
        card_title = compact(title_node.get_text(" ", strip=True))
        location = compact(location_node.get_text(" ", strip=True))
        date_display = compact(date_node.get_text(" ", strip=True))
        if not card_title or not location or not date_display:
            issues.append(f"company_job_card_empty_field:{identity}")
        try:
            datetime.strptime(date_display, "%d-%b-%Y")
        except ValueError:
            issues.append(f"company_job_card_date_format_changed:{identity}:{date_display}")
        classes = list(card.get("class") or [])
        cards.append({
            "id": identity,
            "title": card_title,
            "url": href,
            "location": location,
            "date_display": date_display,
            "location_slug": classes[2] if len(classes) > 2 else None,
            "department_slug": classes[3] if len(classes) > 3 else None,
            "card_html": str(card),
        })
        seen.add(identity)
    if not cards:
        issues.append("highradius_company_page_has_no_exposed_job_cards")

    def options(selector: str) -> list[dict]:
        node = soup.select_one(selector)
        if node is None:
            return []
        return [{"value": option.get("value"), "label": compact(option.get_text(" ", strip=True))}
                for option in node.select("option")]

    return {
        "url": response_url,
        "page_title": title,
        "cards": cards,
        "job_ids": [card["id"] for card in cards],
        "count": len(cards),
        "greenhouse_embed_script": BOARD_EMBED_SCRIPT if BOARD_EMBED_SCRIPT in scripts else None,
        "greenhouse_embed_script_commented_out": commented_embed,
        "search_available": soup.select_one("#search_job") is not None,
        "location_filter": options("#gh-location"),
        "department_filter": options("#gh-department"),
        "show_more_control": compact(soup.select_one("#show_more").get_text(" ", strip=True))
                             if soup.select_one("#show_more") else None,
        "issues": list(dict.fromkeys(issues)),
    }


def parse_detail_entry(html: str, response_url: str, identity: str) -> dict:
    """Validate a company-published exact job page and its active Greenhouse embed."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.hostname != "www.highradius.com"
            or not JOB_LINK_PATH.fullmatch(parts.path)
            or parse_qs(parts.query) != {"gh_jid": [str(identity)]}):
        raise ValueError("HighRadius exact job page did not stay on its company-published detail route")
    soup = BeautifulSoup(html, "lxml")
    scripts = [script.get("src", "") for script in soup.select("script[src]")]
    if BOARD_EMBED_SCRIPT not in scripts:
        raise ValueError(f"HighRadius exact job page no longer publishes its Greenhouse embed: {identity}")
    if soup.select_one("#grnhse_app") is None:
        raise ValueError(f"HighRadius exact job page no longer has the Greenhouse iframe container: {identity}")
    title = compact(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "HighRadius" not in title:
        raise ValueError(f"HighRadius exact job page title changed: {identity}")
    return {
        "job_id": str(identity),
        "url": response_url,
        "page_title": title,
        "greenhouse_embed_script": BOARD_EMBED_SCRIPT,
        "iframe_container_id": "grnhse_app",
        "job_iframe_url": job_app_url(identity),
    }


def parse_board_listing(payload: dict, request_url: str = BOARD_API) -> dict:
    """Validate the exact unfiltered company Greenhouse board and its rows."""
    if request_url != BOARD_API:
        raise ValueError("HighRadius board request did not use its inspected company-scoped URL")
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError("HighRadius company board response has no jobs array")
    meta = payload.get("meta")
    rows = payload["jobs"]
    total = meta.get("total") if isinstance(meta, dict) else None
    if not isinstance(total, int) or total < 0:
        raise ValueError("HighRadius company board response has no integer meta.total")
    if total != len(rows):
        raise ValueError(f"HighRadius board meta.total ({total}) differs from row count ({len(rows)})")

    parsed, issues, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("board_row_not_object")
            continue
        identity = row.get("id")
        if not isinstance(identity, int) or not GREENHOUSE_ID.fullmatch(str(identity)):
            issues.append("board_row_missing_or_invalid_greenhouse_id")
            continue
        identity = str(identity)
        if identity in seen:
            issues.append(f"board_row_duplicate_id:{identity}")
            continue
        title = compact(row.get("title"))
        if not title:
            issues.append(f"board_row_missing_title:{identity}")
            continue
        try:
            _parse_company_job_url(row.get("absolute_url", ""), identity)
        except ValueError as exc:
            issues.append(f"{identity}:{exc}")
        if row.get("company_name") != COMPANY_NAME:
            issues.append(f"board_row_wrong_employer:{identity}:{row.get('company_name')}")
        if row.get("language") != "en":
            issues.append(f"board_row_unexpected_language:{identity}:{row.get('language')}")
        content = row.get("content")
        if not isinstance(content, str) or len(normalized_description(content)) < 100:
            issues.append(f"board_row_full_description_missing_or_short:{identity}")
        if not _valid_iso(row.get("first_published")):
            issues.append(f"board_row_first_published_invalid:{identity}")
        if not _valid_iso(row.get("updated_at")):
            issues.append(f"board_row_updated_at_invalid:{identity}")
        location = row.get("location")
        if not isinstance(location, dict) or not compact(location.get("name")):
            issues.append(f"board_row_location_missing:{identity}")
        if not isinstance(row.get("departments"), list):
            issues.append(f"board_row_departments_missing:{identity}")
        if not isinstance(row.get("offices"), list):
            issues.append(f"board_row_offices_missing:{identity}")
        if not isinstance(row.get("metadata"), list):
            issues.append(f"board_row_metadata_missing:{identity}")
        if row.get("requisition_id") is not None and not isinstance(row.get("requisition_id"), str):
            issues.append(f"board_row_requisition_id_invalid:{identity}")
        seen.add(identity)
        parsed.append(dict(row))
    if len(parsed) != total:
        issues.append("board_retained_rows_do_not_match_meta_total")
    return {
        "rows": parsed,
        "job_ids": [str(row["id"]) for row in parsed],
        "total": total,
        "issues": list(dict.fromkeys(issues)),
    }


def parse_board_detail(payload: dict, expected_row: dict) -> dict:
    """Check an exact per-job board endpoint against its listing row."""
    identity = str(expected_row["id"])
    if not isinstance(payload, dict) or str(payload.get("id")) != identity:
        raise ValueError(f"HighRadius exact detail endpoint returned a different job: {identity}")
    checks = {
        "title_match": compact(payload.get("title")) == compact(expected_row.get("title")),
        "content_match": payload.get("content") == expected_row.get("content"),
        "absolute_url_match": payload.get("absolute_url") == expected_row.get("absolute_url"),
        "first_published_match": payload.get("first_published") == expected_row.get("first_published"),
        "updated_at_match": payload.get("updated_at") == expected_row.get("updated_at"),
        "requisition_id_match": payload.get("requisition_id") == expected_row.get("requisition_id"),
    }
    if not all(checks.values()):
        raise ValueError(f"HighRadius exact detail endpoint differs from its listing row: {identity}")
    return {"job_id": identity, "url": board_detail_url(identity), **checks}


def parse_job_app_reference(html: str, expected_row: dict, expected_url: str) -> dict:
    """Compare the company-embedded job application iframe's visible body."""
    identity = str(expected_row["id"])
    parts = urlsplit(expected_url)
    if (parts.scheme != "https" or parts.hostname != "job-boards.greenhouse.io"
            or parts.path != "/embed/job_app"
            or parse_qs(parts.query) != {"for": [BOARD_SLUG], "token": [identity]}):
        raise ValueError("HighRadius rendered reference is not its exact company job iframe URL")
    soup = BeautifulSoup(html, "lxml")
    title = compact(soup.select_one("h1").get_text(" ", strip=True)) if soup.select_one("h1") else ""
    body = soup.select_one(".job__description")
    if not body:
        raise ValueError(f"HighRadius job iframe has no rendered description: {identity}")
    rendered = normalized_description(str(body))
    expected = normalized_description(expected_row.get("content"))
    location = compact(soup.select_one(".job__location").get_text(" ", strip=True)) if soup.select_one(".job__location") else ""
    return {
        "job_id": identity,
        "url": expected_url,
        "title": title,
        "location": location,
        "description_text": rendered,
        "description_normalized_chars": len(rendered),
        "description_fnv1a64": fnv1a64(rendered),
        "description_sha256": sha256(rendered.encode()).hexdigest(),
        "matches_stored_description": rendered == expected,
        "apply_control": bool(soup.select_one(".job__header button")),
        "application_form_present": soup.select_one("form") is not None,
        "application_form_excluded_from_description": body.select_one("form") is None,
    }


def _metadata_map(row: dict) -> dict:
    values = {}
    for item in row.get("metadata") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            values[item["name"]] = item.get("value")
    return values


class HighRadius(Adapter):
    """Read HighRadius' current careers page and the Greenhouse board it publishes."""

    VERIFICATION_REVISION = "highradius.official-careers.company-published-greenhouse-board.1"

    async def _official_snapshot(self, label: str) -> dict:
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update({
            "label": label,
            "status": response.status_code,
            "request_url": OFFICIAL_ENTRY,
            "final_url": str(response.url),
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _board_snapshot(self, label: str) -> dict:
        started = time.perf_counter()
        response = await self.http.get(BOARD_API)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != BOARD_API:
            raise ValueError("HighRadius company board request redirected away from its inspected URL")
        parsed = parse_board_listing(response.json(), str(response.url))
        parsed.update({
            "label": label,
            "status": response.status_code,
            "request_url": BOARD_API,
            "final_url": str(response.url),
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        })
        return parsed

    async def _detail_entry(self, identity: str) -> dict:
        url = job_url(identity)
        started = time.perf_counter()
        response = await self.http.get(url)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_detail_entry(response.text, str(response.url), identity)
        parsed.update({"status": response.status_code, "seconds": seconds,
                       "response_bytes": len(response.content),
                       "response_sha256": sha256(response.content).hexdigest()})
        return parsed

    async def _representative_detail(self, identity: str, row: dict) -> dict:
        started = time.perf_counter()
        url = board_detail_url(identity)
        response = await self.http.get(url)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != url:
            raise ValueError(f"HighRadius exact detail request redirected: {identity}")
        parsed = parse_board_detail(response.json(), row)
        parsed.update({"status": response.status_code, "seconds": seconds,
                       "response_bytes": len(response.content),
                       "response_sha256": sha256(response.content).hexdigest()})
        return parsed

    def _make_job(self, row: dict, card: dict):
        identity = str(row["id"])
        sections = description_sections(row.get("content"))
        metadata = _metadata_map(row)
        links = _links(row.get("content"))
        attachments = _attachments(links)
        offices = [office for office in row.get("offices") or [] if isinstance(office, dict)]
        locations = [{"name": office.get("name"), "location": office.get("location")} for office in offices]
        if not locations and isinstance(row.get("location"), dict):
            locations = [{"name": row["location"].get("name"), "location": row["location"].get("name")}]
        departments = [dept.get("name") for dept in row.get("departments") or []
                       if isinstance(dept, dict) and dept.get("name")]
        experience_ranges = metadata.get("Experience Range")
        if isinstance(experience_ranges, str):
            experience = [experience_ranges]
        elif isinstance(experience_ranges, list):
            experience = experience_ranges
        else:
            experience = None
        remote = metadata.get("Remote Position")
        workplace = {True: "Remote", False: "On-Site"}.get(remote) if isinstance(remote, bool) else None
        talent_pipeline = bool(re.search(r"talent (?:community|pipeline|pool)", row["title"], re.I))
        job = self.job(
            {
                "listing": row,
                "company_listing_card": card,
                "board_api_url": BOARD_API,
                "board_detail_url": board_detail_url(identity),
                "company_job_iframe_url": job_app_url(identity),
                "metadata_fields": metadata,
                "description_sections": sections,
                "description_links": links,
                "role_attachments": attachments,
                "posting_kind": "talent_pipeline" if talent_pipeline else "job_posting",
                "date_display_on_company_card": card.get("date_display"),
            },
            title=compact(row.get("title")),
            url=row["absolute_url"],
            job_id=identity,
            requisition_id=row.get("requisition_id"),
            apply_url=job_app_url(identity),
            description=row.get("content") or "",
            method="highradius.official_careers.company_published_greenhouse_board.content",
            description_url=BOARD_API,
            locations=locations,
            department="; ".join(departments) if departments else None,
            team=metadata.get("Sub Department"),
            employment_type=None,
            workplace_type=workplace,
            date_posted=row.get("first_published"),
            date_posted_raw=row.get("first_published"),
            date_updated=row.get("updated_at"),
            valid_through=row.get("application_deadline"),
            salary=None,
            qualifications=_section_matches(sections, ("qualif", "requirement", "desired", "what you will need",
                                                       "what you'll need", "who you are")),
            responsibilities=_section_matches(sections, ("responsibilit", "your day-to-day", "what you will do",
                                                         "what you'll do", "key duties")),
            experience=experience,
            education=row.get("education"),
            employer=row.get("company_name"),
            language=row.get("language"),
        )
        job.description_text = normalized_description(row.get("content"))
        job.record_identity = f"highradius:{identity}"
        job.company_scope_verified = self.company.scope == "company"
        if talent_pipeline:
            job.warnings.append("talent_pipeline_listing")
        if attachments:
            job.warnings.append("role_attachment_links_present_without_full_attachment_text")
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_final_url": OFFICIAL_FINAL,
            "listing_url": LISTING_PAGE,
            "board_api_url": BOARD_API,
            "listing_authority": "HighRadius' current official careers page and the exact Greenhouse embed it publishes for highradius.",
            "membership_request": {
                "method": "GET",
                "url": BOARD_API,
                "query": {"content": "true"},
                "filters_applied": False,
                "scope": "Only the company-scoped Greenhouse board for HighRadius; no other board/slug/tenant is requested.",
            },
            "detail_method": "Full employer content is returned inline by the same unfiltered company-scoped board request; two exact per-job endpoints and the rendered company iframe bodies are checked independently.",
            "request_scope": "Only highradius.com's own careers pages and the Greenhouse highradius board it publishes.",
        })
        try:
            if not self.page_guard():
                return self.result
            initial_entry = await self._official_snapshot("initial")
            initial_board = await self._board_snapshot("initial")
        except Exception as exc:
            self.result.errors.append(f"HighRadius source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.expected_total = initial_board["total"]
        self.result.metadata["initial_official_entry"] = {k: v for k, v in initial_entry.items() if k != "cards"}
        self.result.metadata["initial_board_listing"] = {k: v for k, v in initial_board.items() if k != "rows"}
        if initial_entry["issues"]:
            self.result.errors.extend(initial_entry["issues"])
        if initial_board["issues"]:
            self.result.errors.extend(initial_board["issues"])
        if set(initial_entry["job_ids"]) != set(initial_board["job_ids"]):
            self.result.errors.append("highradius_official_cards_and_company_board_membership_mismatch")
        if initial_entry["count"] != initial_board["total"]:
            self.result.errors.append("highradius_official_card_count_and_board_total_mismatch")
        if initial_entry["job_ids"] != initial_board["job_ids"]:
            self.result.warnings.append("highradius_company_card_order_differs_from_board_response_order_for_tied_titles")

        # Inspect representative exact detail endpoints before trusting the inline bodies.
        detail_started = time.perf_counter()
        representative = {}
        initial_by_id = {str(row["id"]): row for row in initial_board["rows"]}
        card_by_id = {card["id"]: card for card in initial_entry["cards"]}
        representative_ids = list(dict.fromkeys(
            [identity for identity in (initial_entry["job_ids"][:1] + initial_entry["job_ids"][-1:])
             if identity in initial_by_id]
        ))
        for identity in representative_ids:
            try:
                representative[identity] = await self._representative_detail(identity, initial_by_id[identity])
            except Exception as exc:
                self.result.errors.append(
                    f"highradius_representative_detail_failed:{identity}:{type(exc).__name__}:{exc}"
                )
        detail_entries = {}
        for identity in representative_ids:
            try:
                detail_entries[identity] = await self._detail_entry(identity)
            except Exception as exc:
                self.result.errors.append(
                    f"highradius_representative_company_detail_failed:{identity}:{type(exc).__name__}:{exc}"
                )
        detail_seconds = max(0.0, time.perf_counter() - detail_started)
        self.result.timings["representative_detail_seconds"] = detail_seconds
        self.result.metadata["representative_details"] = representative
        self.result.metadata["representative_company_detail_pages"] = detail_entries

        # Final company-page and board snapshots must close over the same membership.
        final_started = time.perf_counter()
        try:
            final_entry = await self._official_snapshot("final")
            final_board = await self._board_snapshot("final")
        except Exception as exc:
            final_entry = None
            final_board = None
            self.result.errors.append(f"HighRadius final source recheck failed: {type(exc).__name__}: {exc}")
        final_seconds = max(0.0, time.perf_counter() - final_started)
        self.result.timings["final_recheck_seconds"] = final_seconds
        if final_entry is not None and final_board is not None:
            self.result.metadata["final_official_entry"] = {k: v for k, v in final_entry.items() if k != "cards"}
            self.result.metadata["final_board_listing"] = {k: v for k, v in final_board.items() if k != "rows"}
            if final_entry["issues"]:
                self.result.errors.extend(f"final:{issue}" for issue in final_entry["issues"])
            if final_board["issues"]:
                self.result.errors.extend(f"final:{issue}" for issue in final_board["issues"])
            if set(final_entry["job_ids"]) != set(initial_entry["job_ids"]):
                self.result.errors.append("highradius_official_card_membership_changed_between_snapshots")
            if set(final_board["job_ids"]) != set(initial_board["job_ids"]):
                self.result.errors.append("highradius_company_board_membership_changed_between_snapshots")
            if set(final_entry["job_ids"]) != set(final_board["job_ids"]):
                self.result.errors.append("highradius_final_official_cards_and_board_membership_mismatch")
            rows = final_board["rows"]
            cards = {card["id"]: card for card in final_entry["cards"]}
        else:
            rows = initial_board["rows"]
            cards = card_by_id

        # Cross-check the company page's card fields against the board row.
        rows_by_id = {str(row["id"]): row for row in rows}
        for identity, card in cards.items():
            row = rows_by_id.get(identity)
            if row is None:
                self.result.errors.append(f"highradius_company_card_missing_board_row:{identity}")
                continue
            if compact(card.get("title")) != compact(row.get("title")):
                self.result.errors.append(f"highradius_company_card_title_mismatch:{identity}")
            locations = [office.get("location") or office.get("name") for office in row.get("offices") or []
                         if isinstance(office, dict)]
            if card.get("location") not in locations and compact(card.get("location")) != compact(row.get("location", {}).get("name")):
                self.result.errors.append(f"highradius_company_card_location_mismatch:{identity}")
            try:
                shown = datetime.strptime(card["date_display"], "%d-%b-%Y").date()
                updated = datetime.fromisoformat(row["updated_at"]).date()
                if shown != updated:
                    self.result.errors.append(f"highradius_company_card_date_mismatch:{identity}")
            except (ValueError, KeyError):
                self.result.errors.append(f"highradius_company_card_date_unreadable:{identity}")

        jobs, parse_started = [], time.perf_counter()
        listing_order = (final_entry["job_ids"] if final_entry is not None else initial_entry["job_ids"])
        for identity in listing_order:
            row = rows_by_id.get(identity)
            card = cards.get(identity)
            if row is None:
                self.result.errors.append(f"highradius_company_card_missing_board_row:{identity}")
                continue
            if card is None:
                self.result.errors.append(f"highradius_board_row_missing_company_card:{identity}")
                continue
            job = self._make_job(row, card)
            jobs.append(job)
            self.add(job)
        self.result.timings["inline_detail_parse_seconds_sum"] = max(0.0, time.perf_counter() - parse_started)
        if len(jobs) != self.result.expected_total:
            self.result.errors.append("highradius_retained_rows_do_not_match_company_board_total")
        self.result.listing_complete = bool(
            not self.result.errors and len(jobs) == self.result.expected_total
            and final_entry is not None and final_board is not None
            and set(initial_entry["job_ids"]) == set(final_entry["job_ids"])
            == set(initial_board["job_ids"]) == set(final_board["job_ids"])
        )
        self.result.metadata["membership_reconciliation"] = {
            "initial_card_ids": initial_entry["job_ids"],
            "initial_board_ids": initial_board["job_ids"],
            "final_card_ids": final_entry["job_ids"] if final_entry else None,
            "final_board_ids": final_board["job_ids"] if final_board else None,
            "initial_card_count": initial_entry["count"],
            "initial_board_total": initial_board["total"],
            "final_card_count": final_entry["count"] if final_entry else None,
            "final_board_total": final_board["total"] if final_board else None,
            "retained_count": len(jobs),
            "initial_order_matches_exactly": initial_entry["job_ids"] == initial_board["job_ids"],
            "final_order_matches_exactly": (final_entry["job_ids"] == final_board["job_ids"]) if final_entry and final_board else None,
            "retained_order": [job.job_id for job in jobs],
            "complete": self.result.listing_complete,
        }
        self.result.pages = 1
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
