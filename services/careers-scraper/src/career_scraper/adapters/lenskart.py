"""Lenskart's own careers page, its exact AInterview board embed, and that board's API.

The ledger entry https://www.lenskart.com/careers redirects to the company's
Next.js careers page, which publishes one iframe titled "Career at Lenskart":
https://ainterviews.com/job_board/lenskart_ho/.  The board is Lenskart's own
organization ("Lenskart"/"lenskart_ho"), not a parent-wide or guessed ATS board.
The board page is client-rendered, so its visible membership is observed with
headless Chromium; every membership and detail field is then read from the
exact same board-scoped public JSON endpoints over plain HTTP.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html

OFFICIAL_ENTRY = "https://www.lenskart.com/careers"
OFFICIAL_FINAL = "https://www.lenskart.com/careers-at-lenskart"
IFRAME_TITLE = "Career at Lenskart"
BOARD_ORIGIN = "https://ainterviews.com"
BOARD_URL = BOARD_ORIGIN + "/job_board/lenskart_ho/"
BOARD_SLUG = "lenskart_ho"
ORGANIZATION = "Lenskart"
BOARD_API = BOARD_ORIGIN + "/api/job_board/lenskart_ho/"
JOBS_API = BOARD_ORIGIN + "/api/job_board/lenskart_ho/jobs/"
DETAIL_API = BOARD_ORIGIN + "/api/job_board/lenskart_ho/job/{identity}/"
DETAIL_PAGE = BOARD_ORIGIN + "/job_board/lenskart_ho/job/{identity}/"
JOB_PATH = re.compile(r"^/job_board/lenskart_ho/job/(\d+)/$")
DETAIL_API_PATH = re.compile(r"^/api/job_board/lenskart_ho/job/(\d+)/$")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
LISTING_FIELDS = (
    "id", "title", "description", "location", "salary_min", "salary_max",
    "category", "job_type", "experience_level", "posted_date",
    "application_deadline", "link", "apply_url",
)
DETAIL_FIELDS = LISTING_FIELDS + (
    "company_description", "apply_mode", "form_id", "enable_quick_apply",
    "enable_resume_autofill", "has_default_parser", "assessment_display_mode",
)


def normalized_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return normalized_space(soup.get_text(" ", strip=True))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def source_date(value) -> str | None:
    """Return the source's own ISO timestamp only when it is a real timestamp."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"Lenskart source date is not a timestamp: {value!r}")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError(f"Lenskart source date is missing its timezone: {value!r}")
    return stamp.isoformat()


def exact_detail_url(identity: str | int) -> str:
    return DETAIL_PAGE.format(identity=identity)


def exact_detail_api(identity: str | int) -> str:
    return DETAIL_API.format(identity=identity)


def _is_board_job_link(url: str) -> re.Match | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.netloc != "ainterviews.com":
        return None
    return JOB_PATH.fullmatch(parts.path)


def parse_official_entry(html: str, response_url: str, redirects: list[dict] | None = None) -> dict:
    """Validate the exact Lenskart careers page and its single careers iframe."""
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "www.lenskart.com":
        raise ValueError("Lenskart careers entry no longer ends on www.lenskart.com")
    if parsed.path.rstrip("/") != "/careers-at-lenskart":
        raise ValueError("Lenskart careers entry no longer reaches /careers-at-lenskart")
    if re.search(r"Sorry, you have been blocked|cf-error-details", html or "", re.I):
        raise ValueError("Lenskart official entry returned a Cloudflare block page")
    soup = BeautifulSoup(html, "lxml")
    iframe_matches = []
    for iframe in soup.select("iframe[src]"):
        src = iframe.get("src", "")
        parts = urlsplit(src)
        if parts.netloc == "ainterviews.com" and parts.path.rstrip("/") == "/job_board/lenskart_ho":
            iframe_matches.append(iframe)
    if len(iframe_matches) != 1:
        raise ValueError("Lenskart official careers page no longer exposes one Ainterviews board iframe")
    iframe = iframe_matches[0]
    if iframe.get("title") != IFRAME_TITLE:
        raise ValueError("Lenskart careers iframe title changed")
    if iframe.get("src") != BOARD_URL:
        raise ValueError("Lenskart careers iframe URL is not the inspected board URL")
    chain = [dict(hop) for hop in (redirects or [])]
    if chain:
        locations = [str(hop.get("location") or "") for hop in chain]
        if not any(urlsplit(location).path.rstrip("/") == "/careers-at-lenskart" for location in locations):
            raise ValueError("Lenskart careers redirect chain no longer targets /careers-at-lenskart")
    return {
        "request_url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "iframe_url": iframe.get("src"),
        "iframe_title": iframe.get("title"),
        "iframe_allow": iframe.get("allow"),
        "redirects": chain,
        "page_title": normalized_space(soup.title.get_text(" ", strip=True)) if soup.title else "",
    }


def parse_board_meta(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Lenskart board identity response is not an object")
    if value.get("slug") != BOARD_SLUG or value.get("organization") != ORGANIZATION or value.get("name") != ORGANIZATION:
        raise ValueError("Lenskart Ainterviews board identity changed")
    return {
        "name": value.get("name"),
        "slug": value.get("slug"),
        "organization": value.get("organization"),
        "logo": value.get("logo"),
        "banner_image": value.get("banner_image"),
        "description": value.get("description"),
    }


def parse_board_listing(html: str, response_url: str = BOARD_URL) -> dict:
    """Read the rendered board cards, advertised count, filters and controls."""
    if str(response_url).rstrip("/") != BOARD_URL.rstrip("/"):
        raise ValueError("Lenskart rendered listing came from an unexpected board URL")
    soup = BeautifulSoup(html, "lxml")
    cards, issues, seen = [], [], set()
    for anchor in soup.select('a[href^="/job_board/lenskart_ho/job/"]'):
        match = JOB_PATH.fullmatch(anchor.get("href", ""))
        if match is None:
            continue
        identity = match.group(1)
        if identity in seen:
            issues.append(f"lenskart_rendered_duplicate_job_link:{identity}")
            continue
        title_node = anchor.select_one("h3")
        tags = [normalized_space(tag.get_text(" ", strip=True)) for tag in anchor.select(".job-tag")]
        section = anchor.find_parent("section")
        category_node = section.select_one("h2") if section else None
        title = normalized_space(title_node.get_text(" ", strip=True)) if title_node else ""
        if not title:
            issues.append(f"lenskart_rendered_card_title_missing:{identity}")
        seen.add(identity)
        cards.append({
            "id": identity,
            "url": exact_detail_url(identity),
            "title": title,
            "tags": [tag for tag in tags if tag],
            "location": tags[0] if tags else None,
            "employment_type": tags[1] if len(tags) > 1 else None,
            "category": normalized_space(category_node.get_text(" ", strip=True)) if category_node else None,
        })
    if not cards:
        raise ValueError("Lenskart rendered board no longer contains company job cards")
    page_text = soup.get_text(" ", strip=True)
    advertised_match = re.search(r"(\d[\d,]*)\s+jobs?\s+available", page_text, re.I)
    if advertised_match is None:
        raise ValueError("Lenskart rendered board no longer shows its advertised job count")
    advertised = int(advertised_match.group(1).replace(",", ""))
    if advertised != len(cards):
        issues.append("lenskart_rendered_advertised_count_does_not_match_cards")
    pagination_controls = []
    for node in soup.select("button,a"):
        if node.name == "a" and node.get("href", "").startswith("/job_board/lenskart_ho/job/"):
            continue
        label = normalized_space(node.get_text(" ", strip=True))
        if re.fullmatch(r"(?:next|previous|load\s*more|page\s*\d+|next\s+page|previous\s+page)", label, re.I):
            pagination_controls.append(label)
    filters = [
        normalized_space(node.find("option").get_text(" ", strip=True))
        for node in soup.select("select") if node.find("option")
    ]
    return {
        "url": response_url,
        "page_title": normalized_space(soup.title.get_text(" ", strip=True)) if soup.title else "",
        "advertised_count": advertised,
        "count": len(cards),
        "job_ids": [card["id"] for card in cards],
        "cards": cards,
        "first_card": cards[0],
        "last_card": cards[-1],
        "pagination_controls": list(dict.fromkeys(pagination_controls)),
        "filters_visible": filters,
        "issues": issues,
    }


def parse_jobs_api(value: dict) -> dict:
    """Validate the complete unfiltered membership response for lenskart_ho."""
    if not isinstance(value, dict):
        raise ValueError("Lenskart board jobs response is not an object")
    rows, filters = value.get("jobs"), value.get("filters")
    if not isinstance(rows, list) or not isinstance(filters, dict):
        raise ValueError("Lenskart board jobs response is missing jobs or filters")
    issues, seen, retained, empty_ids = [], set(), [], []
    for row in rows:
        if not isinstance(row, dict):
            issues.append("lenskart_jobs_api_row_not_object")
            continue
        identity = row.get("id")
        if isinstance(identity, bool) or not isinstance(identity, int):
            issues.append("lenskart_jobs_api_row_missing_numeric_id")
            continue
        identity = str(identity)
        if identity in seen:
            issues.append(f"lenskart_jobs_api_duplicate_id:{identity}")
            continue
        missing = [field for field in LISTING_FIELDS if field not in row]
        if missing:
            issues.append(f"lenskart_jobs_api_row_missing_fields:{identity}:{','.join(missing)}")
            continue
        if not isinstance(row.get("title"), str) or not normalized_space(row["title"]):
            issues.append(f"lenskart_jobs_api_title_missing:{identity}")
            continue
        if not isinstance(row.get("description"), str):
            issues.append(f"lenskart_jobs_api_description_not_text:{identity}")
            continue
        for field in ("location", "category", "job_type", "experience_level"):
            if not isinstance(row.get(field), str):
                issues.append(f"lenskart_jobs_api_{field}_not_text:{identity}")
        apply_url = row.get("apply_url")
        if not isinstance(apply_url, str) or not re.fullmatch(rf"/job_board/lenskart_ho/job/{identity}/", apply_url):
            issues.append(f"lenskart_jobs_api_apply_url_changed:{identity}")
        try:
            source_date(row.get("posted_date"))
        except ValueError:
            issues.append(f"lenskart_jobs_api_posted_date_invalid:{identity}")
        try:
            source_date(row.get("application_deadline"))
        except ValueError:
            issues.append(f"lenskart_jobs_api_deadline_invalid:{identity}")
        for field in ("salary_min", "salary_max"):
            amount = row.get(field)
            if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (int, float))):
                issues.append(f"lenskart_jobs_api_{field}_not_number:{identity}")
        if not normalized_body_text(row.get("description")):
            empty_ids.append(identity)
        seen.add(identity)
        retained.append(dict(row))
    if not retained:
        raise ValueError("Lenskart board jobs response no longer contains company rows")
    categories = filters.get("categories")
    locations = filters.get("locations")
    job_types = filters.get("job_types")
    experience_levels = filters.get("experience_levels")
    if not all(isinstance(items, list) for items in (categories, locations, job_types, experience_levels)):
        raise ValueError("Lenskart board jobs response filter lists changed")
    return {
        "rows": retained,
        "count": len(retained),
        "job_ids": [str(row["id"]) for row in retained],
        "filters": {
            "categories": categories,
            "locations": locations,
            "job_types": job_types,
            "experience_levels": experience_levels,
            "salary_ranges": filters.get("salary_ranges"),
        },
        "publisher_empty_job_ids": empty_ids,
        "issues": issues,
    }


def parse_detail_api(value: dict, identity: str | int) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"Lenskart detail response for {identity} is not an object")
    if value.get("id") != int(identity):
        raise ValueError(f"Lenskart detail response id changed for {identity}")
    missing = [field for field in DETAIL_FIELDS if field not in value]
    if missing:
        raise ValueError(f"Lenskart detail response for {identity} is missing fields: {','.join(missing)}")
    for field in ("title", "description", "location", "category",
                  "job_type", "experience_level", "apply_mode"):
        if not isinstance(value.get(field), str):
            raise ValueError(f"Lenskart detail response field {field} is not text for {identity}")
    if value.get("company_description") is not None and not isinstance(value["company_description"], str):
        raise ValueError(f"Lenskart detail company_description is not text for {identity}")
    if not normalized_space(value["title"]):
        raise ValueError(f"Lenskart detail response title is empty for {identity}")
    source_date(value.get("posted_date"))
    source_date(value.get("application_deadline"))
    if not isinstance(value.get("apply_url"), str):
        raise ValueError(f"Lenskart detail apply_url is not text for {identity}")
    return dict(value)


RESPONSIBILITY_TOKENS = (
    "responsibilit", "what you'll do", "what you will do", "what you'll own",
    "what you will own", "your role",
)
QUALIFICATION_TOKENS = (
    "qualif", "requirement", "what we're looking for", "what we are looking for",
    "what you'll need", "who you are", "who we're looking for",
    "who we are looking for", "required skills", "skills",
)
TOPIC_STOP_PREFIXES = (
    "about ", "what we", "what you'll do", "what you will do", "what you'll own",
    "what you will own", "what you can expect", "why ", "benefits", "requirements",
    "qualifications", "who you are", "who we", "how to apply", "compensation", "perks",
    "equal opportunity", "the role", "role overview", "location", "experience",
    "function",
)


def topic_sections(sections: list[dict], tokens: tuple[str, ...]) -> list[dict] | None:
    """Collect the matching heading plus its body and unlabelled sub-sections."""
    collected, active = [], False
    for section in sections:
        heading = (section.get("heading") or "").strip().lower()
        if heading and any(token in heading for token in tokens):
            active = True
            collected.append(section)
            continue
        if not active:
            continue
        if heading and any(heading.startswith(prefix) for prefix in TOPIC_STOP_PREFIXES):
            active = False
            continue
        collected.append(section)
    return [section for section in collected if (section.get("text") or "").strip()] or None


def description_sections(html: str) -> list[dict]:
    """Preserve employer blocks as heading/body sections without inventing headings."""
    soup = BeautifulSoup(f'<div data-lenskart-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-lenskart-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = block.get_text(" ", strip=True)
        elif block.name in {"p", "div"}:
            strong = block.find("strong", recursive=False)
            full_text = normalized_space(block.get_text(" ", strip=True))
            strong_text = normalized_space(strong.get_text(" ", strip=True)) if strong else ""
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
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


async def capture_official_journey() -> dict:
    """Observe the official page and its client-rendered board in one browser.

    The top-level Lenskart page is a Cloudflare-protected browser journey; its
    hosted board is client-rendered (the raw HTTP shell advertises 0 jobs), so a
    real browser observation is required for the visible membership.  A single
    page is used, media/images/fonts are skipped, and the browser is always
    closed in ``finally``.
    """
    from playwright.async_api import async_playwright

    started = time.monotonic()
    official_responses: list[dict] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(
                locale="en-US", viewport={"width": 1440, "height": 1000},
                user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            )

            async def skip_non_document(route):
                if route.request.resource_type in {"image", "media", "font"}:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", skip_non_document)

            def track(response):
                if "www.lenskart.com/careers" in response.url:
                    official_responses.append({
                        "url": response.url,
                        "status": response.status,
                        "location": response.headers.get("location"),
                    })

            page.on("response", track)
            official_started = time.monotonic()
            official_response = await page.goto(OFFICIAL_ENTRY, wait_until="domcontentloaded", timeout=60000)
            official_nav_seconds = max(0.0, time.monotonic() - official_started)
            iframe_started = time.monotonic()
            await page.wait_for_selector(f'iframe[title="{IFRAME_TITLE}"]', timeout=30000)
            iframe_seconds = max(0.0, time.monotonic() - iframe_started)
            official = {
                "request_url": OFFICIAL_ENTRY,
                "final_url": page.url,
                "status": official_response.status if official_response else None,
                "redirects": [dict(row) for row in official_responses],
                "nav_seconds": official_nav_seconds,
                "iframe_wait_seconds": iframe_seconds,
                "html": await page.content(),
            }
            official["response_sha256"] = sha256(official["html"].encode()).hexdigest()
            official["response_bytes"] = len(official["html"].encode())

            board_started = time.monotonic()
            await page.goto(BOARD_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_function(
                "() => /\\d[\\d,]*\\s+jobs?\\s+available/i.test(document.body.innerText) "
                "&& document.querySelectorAll('a[href^=\"/job_board/lenskart_ho/job/\"]').length > 0",
                timeout=30000,
            )
            board = {
                "url": page.url,
                "render_seconds": max(0.0, time.monotonic() - board_started),
                "html": await page.content(),
                "inner_text": await page.evaluate("document.body.innerText"),
                "cards": await page.evaluate("""() => Array.from(document.querySelectorAll('a[href^="/job_board/lenskart_ho/job/"]')).map(a => {
                    const section = a.closest('section');
                    const heading = section ? section.querySelector('h2') : null;
                    return {
                        href: a.getAttribute('href'),
                        title: a.querySelector('h3') ? a.querySelector('h3').innerText.trim() : '',
                        tags: Array.from(a.querySelectorAll('.job-tag')).map(t => t.innerText.trim()),
                        category: heading ? heading.innerText.trim() : null,
                    };
                })"""),
                "pagination_controls": await page.evaluate("""() => Array.from(document.querySelectorAll('button,a'))
                    .filter(n => !(n.closest && n.closest('a[href*="/job_board/lenskart_ho/job/"]')))
                    .map(n => n.innerText.trim())
                    .filter(t => /^(next|previous|load\\s*more|page\\s*\\d+|next\\s+page|previous\\s+page)$/i.test(t))"""),
                "filters_visible": await page.evaluate("""() => Array.from(document.querySelectorAll('select'))
                    .map(s => s.options.length ? s.options[0].text.trim() : '')"""),
            }
            board["response_sha256"] = sha256(board["html"].encode()).hexdigest()
            board["response_bytes"] = len(board["html"].encode())
            return {
                "official": official,
                "board": board,
                "seconds": max(0.0, time.monotonic() - started),
            }
        finally:
            await browser.close()


class Lenskart(Adapter):
    """Read only Lenskart's own Ainterviews board embedded on its careers page."""

    VERIFICATION_REVISION = "lenskart.ainterviews-company-board.1"

    def __init__(self, http, company, source, options, *, capture_journey=None):
        super().__init__(http, company, source, options)
        self.capture_journey = capture_journey or capture_official_journey
        self.browser_evidence: dict = {}

    async def _journey(self):
        started = time.perf_counter()
        capture = await self.capture_journey()
        self.result.timings["browser_capture_seconds"] = (
            self.result.timings.get("browser_capture_seconds", 0.0) + max(0.0, time.perf_counter() - started)
        )
        return capture

    async def _board_meta(self):
        started = time.perf_counter()
        response = await self.http.get(BOARD_API)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != BOARD_API:
            raise ValueError("Lenskart board identity endpoint redirected away from the company board")
        parsed = parse_board_meta(response.json())
        return parsed | {
            "url": str(response.url),
            "seconds": seconds,
            "status": response.status_code,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        }

    async def _jobs_listing(self):
        started = time.perf_counter()
        response = await self.http.get(JOBS_API)
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != JOBS_API:
            raise ValueError("Lenskart jobs endpoint redirected away from the company board")
        parsed = parse_jobs_api(response.json())
        return parsed | {
            "url": str(response.url),
            "seconds": seconds,
            "status": response.status_code,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        }

    @staticmethod
    def _journey_parsed(capture: dict) -> tuple[dict, dict]:
        official_raw = capture["official"]
        board_raw = capture["board"]
        entry = parse_official_entry(
            official_raw["html"], official_raw["final_url"], official_raw.get("redirects")
        )
        entry.update({
            "request_url": official_raw.get("request_url", OFFICIAL_ENTRY),
            "status": official_raw.get("status"),
            "seconds": official_raw.get("nav_seconds"),
            "iframe_wait_seconds": official_raw.get("iframe_wait_seconds"),
            "response_bytes": official_raw.get("response_bytes"),
            "response_sha256": official_raw.get("response_sha256"),
        })
        board = parse_board_listing(board_raw["html"], board_raw.get("url", BOARD_URL))
        board.update({
            "render_seconds": board_raw.get("render_seconds"),
            "inner_text_present": bool((board_raw.get("inner_text") or "").strip()),
            "response_bytes": board_raw.get("response_bytes"),
            "response_sha256": board_raw.get("response_sha256"),
            "pagination_controls": board_raw.get("pagination_controls", board["pagination_controls"]),
            "filters_visible": board_raw.get("filters_visible", board["filters_visible"]),
        })
        return entry, board

    def _make_job(self, row: dict, card: dict | None):
        identity = str(row["id"])
        description_html = row.get("description") or ""
        description_text = text_html(description_html)
        sections = description_sections(description_html)
        responsibility_sections = topic_sections(sections, RESPONSIBILITY_TOKENS)
        qualification_sections = topic_sections(sections, QUALIFICATION_TOKENS)
        description_soup = BeautifulSoup(description_html, "lxml")
        links = [{"text": a.get_text(" ", strip=True), "url": a.get("href")}
                 for a in description_soup.select("a[href]")]
        attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
        salary = None
        if row.get("salary_min") is not None or row.get("salary_max") is not None:
            salary = {"min": row.get("salary_min"), "max": row.get("salary_max")}
        job = self.job(
            {
                "board": BOARD_SLUG,
                "listing": dict(row),
                "company_card": card,
                "detail": None,
                "description_sections": sections,
                "description_links": links,
                "role_attachments": attachments,
                "company_description": None,
                "dates": {
                    "posted_date": row.get("posted_date"),
                    "application_deadline": row.get("application_deadline"),
                    "posting_date_mapping": "posted_date is the source posting timestamp",
                    "closing_date_mapping": "application_deadline is the source deadline when present",
                },
            },
            title=normalized_space(row["title"]),
            url=exact_detail_url(identity),
            job_id=identity,
            requisition_id=None,
            apply_url=exact_detail_url(identity),
            description=description_html,
            method="ainterviews.job_board.jobs.description",
            description_url=JOBS_API,
            locations=[row["location"]] if row.get("location") else [],
            department=row.get("category") or None,
            employment_type=row.get("job_type") or None,
            workplace_type=None,
            date_posted=source_date(row.get("posted_date")),
            date_posted_raw=row.get("posted_date"),
            date_updated=None,
            valid_through=source_date(row.get("application_deadline")),
            salary=salary,
            experience=row.get("experience_level") or None,
            employer=ORGANIZATION,
            language="en",
            responsibilities=responsibility_sections or None,
            qualifications=qualification_sections or None,
        )
        job.description_text = description_text
        job.company_scope_verified = self.company.scope == "company"
        if not description_text:
            job.description_unavailable = True
            job.warnings.append("publisher_empty_description")
        job.raw["source_apply_url_field"] = row.get("apply_url")
        job.raw["detail_page_url"] = exact_detail_url(identity)
        return job

    async def _detail(self, job):
        identity = str(job.job_id)
        started = time.perf_counter()
        response = await self.http.get(exact_detail_api(identity))
        seconds = max(0.0, time.perf_counter() - started)
        parts = urlsplit(str(response.url))
        match = DETAIL_API_PATH.fullmatch(parts.path)
        if parts.netloc != "ainterviews.com" or match is None or match.group(1) != identity:
            raise ValueError(f"Lenskart detail endpoint redirected outside the company board for {identity}")
        detail = parse_detail_api(response.json(), identity)
        listing = job.raw["listing"]
        compare_fields = ("title", "location", "category", "job_type", "experience_level",
                          "posted_date", "application_deadline", "salary_min", "salary_max")
        mismatches = [field for field in compare_fields if detail.get(field) != listing.get(field)]
        if mismatches:
            self.result.errors.append(f"lenskart_listing_detail_field_mismatch:{identity}:{','.join(mismatches)}")
        if detail.get("description") != listing.get("description"):
            if not normalized_body_text(detail.get("description")) and normalized_body_text(listing.get("description")):
                self.result.errors.append(f"lenskart_detail_description_missing:{identity}")
            else:
                self.result.errors.append(f"lenskart_listing_detail_description_mismatch:{identity}")
        description_html = detail.get("description") or ""
        description_text = text_html(description_html)
        sections = description_sections(description_html)
        responsibility_sections = topic_sections(sections, RESPONSIBILITY_TOKENS)
        qualification_sections = topic_sections(sections, QUALIFICATION_TOKENS)
        description_soup = BeautifulSoup(description_html, "lxml")
        links = [{"text": a.get_text(" ", strip=True), "url": a.get("href")}
                 for a in description_soup.select("a[href]")]
        attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
        salary = None
        if detail.get("salary_min") is not None or detail.get("salary_max") is not None:
            salary = {"min": detail.get("salary_min"), "max": detail.get("salary_max")}
        job.title = normalized_space(detail["title"])
        job.description_html = description_html
        job.description_text = description_text
        job.locations = [detail["location"]] if detail.get("location") else []
        job.department = detail.get("category") or None
        job.employment_type = detail.get("job_type") or None
        job.experience = detail.get("experience_level") or None
        job.date_posted = source_date(detail.get("posted_date"))
        job.date_posted_raw = detail.get("posted_date")
        job.valid_through = source_date(detail.get("application_deadline"))
        job.salary = salary
        job.description_method = "ainterviews.job_board.job.description"
        job.description_source_url = exact_detail_api(identity)
        job.responsibilities = responsibility_sections or None
        job.qualifications = qualification_sections or None
        job.description_unavailable = not description_text
        if job.description_unavailable:
            job.warnings.append("publisher_empty_description")
        job.raw["detail"] = detail
        job.raw["detail_response"] = {
            "url": str(response.url),
            "status": response.status_code,
            "seconds": seconds,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
        }
        job.raw["description_sections"] = sections
        job.raw["description_links"] = links
        job.raw["role_attachments"] = attachments
        job.raw["company_description"] = detail.get("company_description")
        job.raw["application"] = {
            "apply_mode": detail.get("apply_mode"),
            "form_id": detail.get("form_id"),
            "source_apply_url_field": detail.get("apply_url"),
            "source_apply_url_field_status": 404,
            "public_apply_entry": exact_detail_url(identity),
            "application_form_api": BOARD_ORIGIN + f"/api/job_board/lenskart_ho/job/{identity}/application_form/",
            "application_questions_are_not_role_description": True,
        }
        if attachments:
            self.result.errors.append(f"lenskart_role_attachments_require_exact_review:{identity}")
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_final_url": OFFICIAL_FINAL,
            "listing_url": BOARD_URL,
            "listing_authority": (
                "The single Ainterviews iframe published by Lenskart's own careers page: "
                "board lenskart_ho, organization Lenskart."
            ),
            "membership_request": {
                "method": "GET",
                "url": JOBS_API,
                "filters_applied": False,
                "pagination": "No pagination fields or controls; all 92 rows are returned in one response.",
                "scope": "Only Lenskart's lenskart_ho board; no other Ainterviews organization is queried.",
            },
            "detail_method": "One exact board-scoped detail API GET per listed job; no detail ID is guessed.",
            "date_semantics": (
                "posted_date is the publisher's own posting timestamp and is retained as date_posted; "
                "application_deadline is retained as valid_through when present; date_updated stays null."
            ),
            "access_note": (
                "The official www.lenskart.com page returns Cloudflare HTTP 403 to plain httpx clients "
                "(not a CAPTCHA; no challenge was solved). It is observed with headless Chromium, while "
                "the embedded Ainterviews board and JSON APIs use plain HTTP."
            ),
            "source_apply_url_note": (
                "The detail API apply_url field points at /jobboard/<slug>/job/<id>/apply/, which returns "
                "HTTP 404 in every tested form; the working application control is an in-page modal, so the "
                "exact job detail page is retained as the public apply entry and the broken field is preserved raw."
            ),
        })
        try:
            initial_capture = await self._journey()
            self.browser_evidence["initial"] = initial_capture
            initial_entry, initial_board = self._journey_parsed(initial_capture)
            initial_meta = await self._board_meta()
            initial = await self._jobs_listing()
        except Exception as exc:
            self.result.errors.append(f"Lenskart source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.metadata["initial_official_entry"] = initial_entry
        self.result.metadata["initial_board_listing"] = {
            key: value for key, value in initial_board.items() if key != "cards"
        }
        self.result.metadata["initial_board_cards"] = initial_board["cards"]
        self.result.metadata["initial_board_meta"] = initial_meta
        self.result.metadata["initial_jobs_api"] = {
            key: value for key, value in initial.items() if key not in ("rows",)
        }
        self.result.metadata["filters"] = initial["filters"]
        self.result.timings["initial_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started
        )
        self.result.errors.extend(initial_board["issues"])
        self.result.errors.extend(initial["issues"])

        initial_ids = initial["job_ids"]
        rendered_ids = initial_board["job_ids"]
        if initial_board["advertised_count"] != initial["count"]:
            self.result.errors.append("lenskart_advertised_count_and_jobs_api_count_mismatch")
        if set(rendered_ids) != set(initial_ids):
            self.result.errors.append("lenskart_rendered_board_and_jobs_api_membership_mismatch")
        if initial["publisher_empty_job_ids"]:
            self.result.metadata["publisher_empty_job_ids"] = initial["publisher_empty_job_ids"]

        card_by_id = {card["id"]: card for card in initial_board["cards"]}
        jobs = []
        for row in initial["rows"]:
            identity = str(row["id"])
            card = card_by_id.get(identity)
            if card is None:
                self.result.errors.append(f"lenskart_job_missing_from_rendered_board:{identity}")
                continue
            if normalized_space(card.get("title")) != normalized_space(row.get("title")):
                self.result.errors.append(f"lenskart_card_title_mismatch:{identity}")
            if card.get("location") and normalized_space(card["location"]) != normalized_space(row.get("location")):
                self.result.errors.append(f"lenskart_card_location_mismatch:{identity}")
            jobs.append(self._make_job(row, card))
        if len(jobs) != initial["count"]:
            self.result.errors.append("lenskart_retained_rows_do_not_match_company_listing_count")

        self.result.expected_total = initial["count"]
        self.result.pages = 1
        self.result.metadata["membership_pages"] = 1
        hydrated = await self.hydrate(jobs, self._detail)

        final_capture = None
        final_started = time.perf_counter()
        try:
            final_capture = await self._journey()
            self.browser_evidence["final"] = final_capture
            final_entry, final_board = self._journey_parsed(final_capture)
            final_meta = await self._board_meta()
            final = await self._jobs_listing()
        except Exception as exc:
            self.result.errors.append(f"Lenskart final source recheck failed: {type(exc).__name__}: {exc}")
            final_entry = final_board = final_meta = final = None

        self.result.timings["final_recheck_wall_seconds"] = max(
            0.0, time.perf_counter() - final_started
        )
        if final is None or final_board is None or final_entry is None or final_meta is None:
            final_matches = False
        else:
            initial_by_id = {str(row["id"]): row for row in initial["rows"]}
            final_by_id = {str(row["id"]): row for row in final["rows"]}
            changed_fields = {}
            for identity in initial_ids:
                if identity in final_by_id and initial_by_id[identity] != final_by_id[identity]:
                    changed_fields[identity] = sorted(
                        key for key in set(initial_by_id[identity]) | set(final_by_id[identity])
                        if initial_by_id[identity].get(key) != final_by_id[identity].get(key)
                    )
            same_ids = initial_ids == final["job_ids"]
            same_rendered = set(initial_board["job_ids"]) == set(final_board["job_ids"])
            counts_match = (initial["count"], initial_board["advertised_count"]) == (
                final["count"], final_board["advertised_count"]
            )
            entry_embed_same = (
                initial_entry["iframe_url"] == final_entry["iframe_url"] == BOARD_URL
                and initial_entry["iframe_title"] == final_entry["iframe_title"] == IFRAME_TITLE
            )
            meta_same = initial_meta["slug"] == final_meta["slug"] == BOARD_SLUG
            final_matches = (
                same_ids and same_rendered and counts_match and not changed_fields
                and not final["issues"] and not final_board["issues"] and entry_embed_same and meta_same
            )
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_board_listing"] = {
                key: value for key, value in final_board.items() if key != "cards"
            }
            self.result.metadata["final_board_meta"] = final_meta
            self.result.metadata["final_jobs_api"] = {
                key: value for key, value in final.items() if key not in ("rows",)
            }
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial_ids,
                "final_ids": final["job_ids"],
                "initial_rendered_ids": initial_board["job_ids"],
                "final_rendered_ids": final_board["job_ids"],
                "initial_count": initial["count"],
                "final_count": final["count"],
                "initial_advertised_count": initial_board["advertised_count"],
                "final_advertised_count": final_board["advertised_count"],
                "api_and_rendered_membership_match": set(initial_ids) == set(initial_board["job_ids"]),
                "same_ids_and_order": same_ids,
                "same_rendered_ids": same_rendered,
                "all_row_fields_match": not changed_fields,
                "changed_fields_by_job_id": changed_fields,
                "new_job_ids": [i for i in final["job_ids"] if i not in initial_ids],
                "removed_job_ids": [i for i in initial_ids if i not in final["job_ids"]],
                "official_embed_still_exact": entry_embed_same,
                "board_identity_unchanged": meta_same,
                "pagination_controls_initial": initial_board["pagination_controls"],
                "pagination_controls_final": final_board["pagination_controls"],
                "complete": final_matches,
            }
            if not entry_embed_same:
                self.result.errors.append("lenskart_official_embed_changed_during_collection")
            if not same_ids or not same_rendered or not counts_match:
                self.result.errors.append("lenskart_listing_membership_changed_during_collection")
            if changed_fields:
                self.result.errors.append("lenskart_source_fields_changed_during_collection")
            if not meta_same:
                self.result.errors.append("lenskart_board_identity_changed_during_collection")
            self.result.errors.extend(final["issues"])
            self.result.errors.extend(final_board["issues"])

        self.result.jobs = hydrated
        self.result.metadata["job_ids"] = [str(job.job_id) for job in hydrated]
        self.result.metadata["advertised_jobs"] = initial_board["advertised_count"]
        self.result.metadata["api_count"] = initial["count"]
        self.result.metadata["publisher_empty_job_ids"] = sorted(
            str(job.job_id) for job in hydrated if job.description_unavailable
        )
        self.result.metadata["detail_requests"] = len(hydrated)
        self.result.metadata["current_native_membership_reconciled"] = bool(
            final is not None and self.result.metadata.get("membership_reconciliation", {}).get("complete")
        )
        self.result.listing_complete = bool(final_matches and len(hydrated) == initial["count"])
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        self.result.timings["initial_board_page_seconds"] = initial_board.get("render_seconds")
        self.result.timings["initial_board_meta_seconds"] = initial_meta.get("seconds")
        self.result.timings["initial_jobs_api_seconds"] = initial.get("seconds")
        return self.result
