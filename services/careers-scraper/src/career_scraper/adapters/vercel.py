"""Vercel's own careers site: the SSR listing and its first-party job pages.

Vercel serves both surfaces from ``vercel.com/careers``. The listing document
server-renders every open card and embeds the publishing board's ``jobs`` array
in its Next.js flight payload; each posting's detail page is
``vercel.com/careers/<slug>-<id>``. The company-scoped board feed
(``boards-api.greenhouse.io/v1/boards/vercel/jobs``) is read only to check the
exact employer ``content`` field of the ids that the official page renders.
"""
from __future__ import annotations

from hashlib import sha256
from html import unescape
import json
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import Adapter
from ..models import now, text_html


OFFICIAL_ENTRY = "https://vercel.com/careers"
OFFICIAL_ORIGIN = "https://vercel.com"
BOARD_API = "https://boards-api.greenhouse.io/v1/boards/vercel/jobs"
BOARD_CONTENT_API = BOARD_API + "?content=true"
BOARD_TOKEN = "vercel"
EMPLOYER_NAME = "Vercel"

JOB_LINK_PATH = re.compile(r"^/careers/([a-z0-9][a-z0-9-]*)-(\d+)$", re.I)
ABSOLUTE_URL = re.compile(r"^https://job-boards\.greenhouse\.io/vercel/jobs/(\d+)$", re.I)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
DESCRIPTION_BLOCK_CLASS = "border-r-background-200"
APPLY_HEADING = "Apply for Role"
APPLICATION_MARKERS = ("Apply Now.", "First Name", "Last Name", "Upload your resume",
                       "Phone Number", "LinkedIn Profile")
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)

LISTING_JOB_FIELDS = (
    "absolute_url", "data_compliance", "internal_job_id", "location", "metadata",
    "id", "updated_at", "requisition_id", "title", "company_name", "first_published",
    "language", "application_deadline", "content", "departments", "offices",
    "ai_disclaimer", "include_ai_disclaimer", "ai_opt_out_request_url",
)


def normalized_title(value: str | None) -> str:
    value = unicodedata.normalize("NFC", value or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalized_body_text(html: str | None) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True).replace("\xa0", " ")
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def flight_payload(html: str) -> str:
    """Concatenate the decoded Next.js flight rows embedded in the document."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', html or "")
    if not chunks:
        raise ValueError("Vercel careers page no longer embeds a Next.js flight payload")
    decoded = []
    for chunk in chunks:
        try:
            decoded.append(json.loads(chunk))
        except ValueError as exc:
            raise ValueError("Vercel careers page flight chunk is not valid JSON text") from exc
    return "".join(decoded)


def embedded_jobs_array(payload: str) -> list[dict]:
    """Return the ``{"jobs":[...]}`` array serialised into the flight payload."""
    marker = '"jobs":['
    index = payload.find(marker)
    if index < 0:
        raise ValueError("Vercel careers flight payload no longer contains the jobs array")
    start = payload.index("[", index)
    depth = 0
    end = None
    for position in range(start, len(payload)):
        char = payload[position]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = position + 1
                break
    if end is None:
        raise ValueError("Vercel careers jobs array is unterminated")
    try:
        value = json.loads(payload[start:end])
    except ValueError as exc:
        raise ValueError("Vercel careers jobs array is not valid JSON") from exc
    if not isinstance(value, list):
        raise ValueError("Vercel careers jobs payload is not an array")
    return value


def parse_careers_listing(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if parts.scheme != "https" or parts.netloc != "vercel.com" or parts.path.rstrip("/") != "/careers":
        raise ValueError("Vercel careers entry redirected away from https://vercel.com/careers")
    soup = BeautifulSoup(html, "lxml")
    if soup.title is None or normalized_title(soup.title.get_text(" ", strip=True)) != "Careers":
        raise ValueError("Vercel careers document title changed")
    og = {m.get("property"): m.get("content") for m in soup.select("meta[property]") if m.get("property")}
    if og.get("og:url") != OFFICIAL_ENTRY:
        raise ValueError("Vercel careers canonical og:url changed")

    anchors = soup.select('a[data-testid^="careers/position/"]')
    cards, issues = [], []
    seen_ids: set[str] = set()
    for index, anchor in enumerate(anchors):
        testid = anchor.get("data-testid", "")
        suffix = testid.rsplit("/", 1)[-1]
        if suffix != str(index):
            issues.append(f"vercel_position_card_index_out_of_order:{suffix}")
        href = anchor.get("href", "")
        match = JOB_LINK_PATH.fullmatch(urlsplit(href).path if href.startswith("http") else href)
        if match is None:
            issues.append(f"vercel_position_card_href_unexpected:{href}")
            continue
        slug, identity = match.groups()
        title_node = anchor.select_one(f'[data-testid="careers/position-title/{index}"]')
        location_node = anchor.select_one(f'[data-testid="careers/position-locations/{index}"]')
        if title_node is None or location_node is None:
            issues.append(f"vercel_position_card_fields_missing:{identity}")
            continue
        if identity in seen_ids:
            issues.append(f"vercel_position_card_duplicate_id:{identity}")
            continue
        seen_ids.add(identity)
        cards.append({
            "index": index,
            "id": identity,
            "slug": slug,
            "href": href,
            "url": OFFICIAL_ORIGIN + href if href.startswith("/") else href,
            "title": normalized_title(title_node.get_text(" ", strip=True)),
            "location_display": normalized_title(location_node.get_text(" ", strip=True)),
            "cta": anchor.get_text(" ", strip=True),
            "card_html": str(anchor),
        })

    payload = flight_payload(html)
    jobs = embedded_jobs_array(payload)
    rows, by_id, order = [], {}, []
    for job in jobs:
        if not isinstance(job, dict):
            issues.append("vercel_ssr_job_not_object")
            continue
        missing = [field for field in LISTING_JOB_FIELDS if field not in job]
        if missing:
            issues.append(f"vercel_ssr_job_fields_missing:{','.join(missing)}")
            continue
        identity = str(job.get("id", ""))
        if not re.fullmatch(r"\d+", identity):
            issues.append("vercel_ssr_job_missing_numeric_id")
            continue
        absolute = ABSOLUTE_URL.fullmatch(job.get("absolute_url") or "")
        if absolute is None or absolute.group(1) != identity:
            issues.append(f"vercel_ssr_job_absolute_url_unexpected:{identity}")
            continue
        if job.get("company_name") != EMPLOYER_NAME:
            issues.append(f"vercel_ssr_job_wrong_company:{identity}")
            continue
        if not normalized_title(job.get("title")):
            issues.append(f"vercel_ssr_job_missing_title:{identity}")
            continue
        if not isinstance(job.get("location"), dict) or not normalized_title(job["location"].get("name")):
            issues.append(f"vercel_ssr_job_missing_location:{identity}")
            continue
        if not isinstance(job.get("departments"), list) or not isinstance(job.get("offices"), list):
            issues.append(f"vercel_ssr_job_missing_departments_or_offices:{identity}")
            continue
        if not isinstance(job.get("content"), str) or not job["content"].startswith("$"):
            issues.append(f"vercel_ssr_job_content_is_not_flight_reference:{identity}")
        if identity in by_id:
            issues.append(f"vercel_ssr_job_duplicate_id:{identity}")
            continue
        by_id[identity] = job
        order.append(identity)
        rows.append({
            "id": identity,
            "slug": None,
            "title": normalized_title(job["title"]),
            "absolute_url": job["absolute_url"],
            "requisition_id": job.get("requisition_id"),
            "location": normalized_title(job["location"]["name"]),
            "departments": job.get("departments") or [],
            "offices": job.get("offices") or [],
            "metadata": job.get("metadata") or [],
            "first_published": job.get("first_published"),
            "updated_at": job.get("updated_at"),
            "application_deadline": job.get("application_deadline"),
            "language": job.get("language"),
            "company_name": job.get("company_name"),
            "internal_job_id": job.get("internal_job_id"),
            "data_compliance": job.get("data_compliance"),
            "content": job.get("content"),
            "content_ref": job.get("content"),
        })

    if len(cards) != len(rows):
        issues.append("vercel_visible_cards_and_ssr_jobs_count_mismatch")
    card_by_id = {card["id"]: card for card in cards}
    for row in rows:
        card = card_by_id.get(row["id"])
        if card is None:
            issues.append(f"vercel_ssr_job_without_visible_card:{row['id']}")
            continue
        row["slug"] = card["slug"]
        row["index"] = card["index"]
        row["href"] = card["href"]
        row["url"] = card["url"]
        row["location_display"] = card["location_display"]
        if normalized_title(card["title"]) != row["title"]:
            row["display_title_differs"] = True
    for card in cards:
        if card["id"] not in by_id:
            issues.append(f"vercel_visible_card_without_ssr_job:{card['id']}")
    if [card["id"] for card in cards] != order:
        issues.append("vercel_visible_card_order_differs_from_ssr_jobs")

    filter_labels = [
        normalized_title(button.get_text(" ", strip=True))
        for button in soup.select("button")
        if normalized_title(button.get_text(" ", strip=True)) in ("Location", "Department")
    ]
    pagination = [
        normalized_title(node.get_text(" ", strip=True)) for node in soup.select("button,a")
        if normalized_title(node.get_text(" ", strip=True)).lower() in ("next", "previous", "load more", "show more")
    ]
    return {
        "url": response_url,
        "title": normalized_title(soup.title.get_text(" ", strip=True)),
        "og_title": og.get("og:title"),
        "cards": cards,
        "rows": rows,
        "job_ids": order,
        "count": len(rows),
        "filter_labels": sorted(set(filter_labels)),
        "pagination_controls": pagination,
        "issues": issues,
    }


def parse_board_feed(value: dict, *, require_content: bool) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("meta"), dict):
        raise ValueError("Vercel Greenhouse board feed is not a meta/jobs object")
    meta = value["meta"]
    jobs = value.get("jobs")
    total = meta.get("total")
    if not isinstance(total, int) or not isinstance(jobs, list):
        raise ValueError("Vercel Greenhouse board feed meta/jobs schema changed")
    if len(jobs) != total:
        raise ValueError("Vercel Greenhouse board feed row count does not match meta.total")
    rows, issues, seen = [], [], set()
    for job in jobs:
        if not isinstance(job, dict):
            issues.append("vercel_board_row_not_object")
            continue
        identity = str(job.get("id", ""))
        if not re.fullmatch(r"\d+", identity):
            issues.append("vercel_board_row_missing_numeric_id")
            continue
        if identity in seen:
            issues.append(f"vercel_board_duplicate_id:{identity}")
            continue
        if job.get("company_name") != EMPLOYER_NAME:
            issues.append(f"vercel_board_row_wrong_company:{identity}")
            continue
        if ABSOLUTE_URL.fullmatch(job.get("absolute_url") or "") is None:
            issues.append(f"vercel_board_row_absolute_url_unexpected:{identity}")
            continue
        if require_content and not isinstance(job.get("content"), str):
            issues.append(f"vercel_board_row_missing_content:{identity}")
            continue
        seen.add(identity)
        rows.append(job)
    return {"total": total, "rows": rows, "job_ids": [str(row["id"]) for row in rows], "issues": issues}


def _detail_sections(html: str) -> tuple[list[dict], list[dict]]:
    """Split the rendered first-party body into employer sections and blocks."""
    soup = BeautifulSoup(f'<div data-vercel-body="1">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-vercel-body]")
    blocks = [node for node in root.find_all("div", recursive=False)] if root else []
    sections, current = [], None
    for block in blocks:
        heading_node = block.find(HEADING_TAGS)
        heading = normalized_title(heading_node.get_text(" ", strip=True)) if heading_node else ""
        body_html = str(block)
        if heading:
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": body_html, "text": text_html(body_html)}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += body_html
    if current is not None:
        current["text"] = text_html(current["html"])
        sections.append(current)
    if not sections:
        sections.append({"heading": None, "html": html or "", "text": text_html(html or "")})
    return sections, [{"html": str(block), "text": text_html(str(block))} for block in blocks]


def parse_detail(html: str, expected_row: dict, response_url: str) -> dict:
    expected_url = expected_row.get("url") or ""
    for url in (expected_url, response_url):
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.netloc != "vercel.com"
                or not JOB_LINK_PATH.fullmatch(parts.path)):
            raise ValueError("Vercel representative detail URL is not an official careers job route")
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if h1 is None:
        raise ValueError("Vercel job detail page has no h1 title")
    title = normalized_title(h1.get_text(" ", strip=True))
    if title != normalized_title(expected_row.get("title")):
        raise ValueError(f"Vercel job detail title changed: {title!r} != {expected_row.get('title')!r}")
    lazy = soup.select_one("div.AMTIxG_gridSystemLazyContent")
    if lazy is None:
        raise ValueError("Vercel job detail page no longer renders its lazy content grid")
    sections = lazy.find_all("section", recursive=False)
    header_section = next(
        (section for section in sections if section.select_one("ul li") and not section.select_one("h2")),
        None,
    )
    if header_section is None:
        raise ValueError("Vercel job detail page header meta list is missing")
    meta_items = [normalized_title(item.get_text(" ", strip=True)) for item in header_section.select("ul li")]
    if len(meta_items) < 3:
        raise ValueError("Vercel job detail page header meta list changed")

    content_section = next(
        (section for section in sections
         if section.select_one('div.AMTIxG_block[data-grid-cell] h2')
         or section.select_one(f'div[class*="{DESCRIPTION_BLOCK_CLASS}"] h2')),
        None,
    )
    if content_section is None:
        raise ValueError("Vercel job detail page description section is missing")
    blocks, stopped = [], False
    for block in content_section.select("div[data-grid-cell]"):
        text = normalized_title(block.get_text(" ", strip=True))
        if not text:
            continue
        if text == APPLY_HEADING:
            stopped = True
            break
        if block.select_one("input,select,textarea,button"):
            stopped = True
            break
        if not (block.select_one("h1,h2,h3,h4") or block.select_one("p,ul,ol")):
            continue
        blocks.append(block)
    description_html = "".join(str(block) for block in blocks)
    description_text = text_html(description_html)
    body_sections, _ = _detail_sections(description_html)
    markers = [marker for marker in APPLICATION_MARKERS if marker in description_text]
    if not stopped:
        raise ValueError("Vercel job detail description never reached the application-form boundary")
    if markers:
        raise ValueError(f"Vercel job detail description leaked application-form text: {markers[:2]}")
    if len(normalized_body_text(description_html)) < 100:
        raise ValueError("Vercel job detail description is empty or truncated")
    department, location_label, employment_type = meta_items[0], meta_items[1], meta_items[2]
    links = [{"text": a.get_text(" ", strip=True), "url": a.get("href")}
             for block in blocks for a in block.select("a[href]")]
    attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
    return {
        "url": response_url,
        "title": title,
        "department": department,
        "location_label": location_label,
        "employment_type": employment_type,
        "header_meta": meta_items,
        "description_html": description_html,
        "description_text": description_text,
        "description_normalized_chars": len(normalized_body_text(description_html)),
        "description_fnv1a64": fnv1a64(normalized_body_text(description_html)),
        "description_blocks": len(blocks),
        "description_sections": body_sections,
        "description_links": links,
        "role_attachments": attachments,
        "application_headings": [meta for meta in meta_items if meta == APPLY_HEADING],
        "application_form_excluded": True,
        "application_form_boundary_reached": stopped,
    }


def employer_content_text(row: dict) -> str:
    return normalized_body_text(unescape(row.get("content") or ""))


class Vercel(Adapter):
    """Collect Vercel's own careers listing and first-party job pages."""

    VERIFICATION_REVISION = "vercel.careers.first-party-ssr.1"

    async def _listing(self, label: str):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_careers_listing(response.text, str(response.url))
        parsed.update({
            "label": label,
            "status": response.status_code,
            "final_url": str(response.url),
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "seconds": seconds,
        })
        return parsed

    async def _board_feed(self, *, content: bool, label: str):
        started = time.perf_counter()
        url = BOARD_CONTENT_API if content else BOARD_API
        response = await self.http.get(url)
        seconds = max(0.0, time.perf_counter() - started)
        if urlsplit(str(response.url)).netloc != "boards-api.greenhouse.io":
            raise ValueError("Vercel board feed redirected outside the company-scoped board host")
        parsed = parse_board_feed(response.json(), require_content=content)
        parsed.update({
            "label": label,
            "url": str(response.url),
            "status": response.status_code,
            "seconds": seconds,
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "includes_employer_content": content,
        })
        return parsed

    def _make_job(self, row: dict):
        content_ref = row.get("content")
        department = (row.get("departments") or [{}])[0].get("name")
        location = normalized_title(row.get("location"))
        location_prefix = location.split(" - ", 1)[0] if " - " in location else None
        job = self.job(
            {"listing_row": {key: value for key, value in row.items() if key != "content"},
             "listing_content_ref": content_ref,
             "visible_card": {
                 "index": row.get("index"),
                 "href": row.get("href"),
                 "url": row.get("url"),
                 "title": row.get("title"),
                 "location_display": row.get("location_display"),
             }},
            title=row["title"],
            url=row["url"],
            job_id=row["id"],
            requisition_id=row.get("requisition_id"),
            apply_url=row.get("absolute_url"),
            description="",
            description_url=row["url"],
            locations=[location] if location else [],
            department=department,
            employment_type=None,
            workplace_type=location_prefix if location_prefix in ("Hybrid", "Remote", "On-Site") else None,
            date_posted=row.get("first_published"),
            date_posted_raw=row.get("first_published"),
            date_updated=row.get("updated_at"),
            valid_through=None,
            salary=None,
            employer=row.get("company_name") or EMPLOYER_NAME,
            language=row.get("language"),
        )
        return job

    def _detail_handler(self, jobs_by_url: dict, board_by_id: dict, stats: dict):
        async def detail(job):
            row = jobs_by_url[job.url]
            board_row = board_by_id[str(job.job_id)]
            started = time.perf_counter()
            response = await self.http.get(job.url)
            seconds = max(0.0, time.perf_counter() - started)
            if str(response.url) != job.url:
                raise ValueError(f"Vercel job detail redirected away from its listing URL: {response.url}")
            parsed = parse_detail(response.text, row, str(response.url))
            parse_started = time.perf_counter()
            source_text = employer_content_text(board_row)
            detail_text = normalized_body_text(parsed["description_html"])
            matches = detail_text == source_text
            job.description_html = parsed["description_html"]
            job.description_text = parsed["description_text"]
            job.description_method = "vercel.careers.job_detail.rendered_description_blocks"
            job.description_source_url = job.url
            job.employment_type = parsed["employment_type"]
            job.date_posted = row.get("first_published")
            job.date_posted_raw = row.get("first_published")
            job.date_updated = row.get("updated_at")
            department = parsed["department"]
            job.department = department or job.department
            job.raw["detail"] = {
                "url": parsed["url"],
                "fetched_at": now(),
                "request_seconds": seconds,
                "response_sha256": sha256(response.content).hexdigest(),
                "response_bytes": len(response.content),
                "title": parsed["title"],
                "header_meta": parsed["header_meta"],
                "department": parsed["department"],
                "location_label": parsed["location_label"],
                "employment_type": parsed["employment_type"],
                "description_blocks": parsed["description_blocks"],
                "description_normalized_chars": parsed["description_normalized_chars"],
                "description_fnv1a64": parsed["description_fnv1a64"],
                "description_sections": parsed["description_sections"],
                "description_links": parsed["description_links"],
                "role_attachments": parsed["role_attachments"],
                "application_form_excluded": parsed["application_form_excluded"],
                "application_form_boundary_reached": parsed["application_form_boundary_reached"],
                "employer_content_match": matches,
                "employer_content_normalized_chars": len(source_text),
                "employer_content_sha256": sha256(source_text.encode()).hexdigest(),
                "source_field": "board feed content (HTML entity-escaped employer body)",
            }
            job.raw["listing_row"]["content"] = board_row.get("content")
            stats["detail_parse_seconds"] += max(0.0, time.perf_counter() - parse_started)
            stats["detail_request_seconds"] += seconds
            if not matches:
                job.warnings.append("description_not_full_field")
                self.result.errors.append(f"vercel_detail_description_mismatch:{job.job_id}")
            if parsed["role_attachments"]:
                self.result.errors.append(f"vercel_role_attachments_require_exact_review:{job.job_id}")
            listing_departments = [normalized_title(d.get("name")) for d in row.get("departments") or [] if d.get("name")]
            career_categories = [normalized_title(m.get("value")) for m in row.get("metadata") or []
                                 if m.get("name") == "Career Site Categories" and m.get("value")]
            if department and department not in listing_departments + career_categories:
                job.warnings.append("detail_department_not_in_listing_department_or_category")
            job.raw["detail"]["listing_departments"] = listing_departments
            job.raw["detail"]["career_site_categories"] = career_categories
            return job
        return detail

    async def run(self):
        source_started = time.perf_counter()
        stats = {"detail_parse_seconds": 0.0, "detail_request_seconds": 0.0}
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_ENTRY,
            "listing_authority": "Vercel's own /careers document: 87 visible cards and the SSR jobs array it embeds.",
            "detail_method": "Each card's own first-party route /careers/<slug>-<id>; rendered employer description blocks, stopping before the Apply for Role form.",
            "employer_content_cross_check": "Company-scoped Greenhouse board feed for board token 'vercel' (meta.total + content of the exact 87 ids embedded by the official page).",
            "request_scope": "Only vercel.com/careers* and the vercel board feed for the ids the official page publishes; no other company or board.",
            "filter_observation": "Client-side Location and Department accordions; no server-side filter or pagination parameters.",
        })
        try:
            initial = await self._listing("initial")
            if not self.page_guard():
                return self.result
            board = await self._board_feed(content=True, label="initial")
            if not self.page_guard():
                return self.result
        except Exception as exc:
            self.result.errors.append(f"Vercel source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.metadata["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.expected_total = initial["count"]
        self.result.metadata["initial_listing"] = {k: v for k, v in initial.items() if k not in ("rows", "cards")}
        self.result.metadata["initial_visible_card_ids"] = [card["id"] for card in initial["cards"]]
        self.result.metadata["initial_board_feed"] = {k: v for k, v in board.items() if k != "rows"}
        self.result.errors.extend(initial["issues"])
        self.result.errors.extend(board["issues"])
        board_by_id = {str(row["id"]): row for row in board["rows"]}
        rows_by_id = {row["id"]: row for row in initial["rows"]}
        board_ids = set(board["job_ids"])
        listing_ids = set(initial["job_ids"])
        self.result.metadata["board_order_matches_listing"] = board["job_ids"] == initial["job_ids"]
        if listing_ids != board_ids:
            self.result.errors.append("vercel_official_listing_and_board_feed_membership_mismatch")
        for identity in sorted(listing_ids & board_ids):
            row = rows_by_id[identity]
            feed_row = board_by_id[identity]
            if (normalized_title(feed_row.get("title")) != row["title"]
                    or feed_row.get("absolute_url") != row["absolute_url"]
                    or normalized_title((feed_row.get("location") or {}).get("name")) != row["location"]):
                self.result.errors.append(f"vercel_official_listing_and_board_feed_row_mismatch:{identity}")

        jobs = []
        for row in initial["rows"]:
            if row["id"] not in board_by_id:
                self.result.errors.append(f"vercel_official_job_missing_from_board_feed:{row['id']}")
                continue
            jobs.append(self._make_job(row))
        for job in jobs:
            self.result.validate_job(job)

        jobs_by_url = {row["url"]: row for row in initial["rows"]}
        await self.hydrate(jobs, self._detail_handler(jobs_by_url, board_by_id, stats))

        final = None
        final_board = None
        try:
            final = await self._listing("final")
            final_board = await self._board_feed(content=False, label="final")
        except Exception as exc:
            self.result.errors.append(f"Vercel final source recheck failed: {type(exc).__name__}: {exc}")
        if final is not None and final_board is not None:
            final_ids = final["job_ids"]
            final_rows = {row["id"]: row for row in final["rows"]}
            changed = {}
            for identity, initial_row in rows_by_id.items():
                final_row = final_rows.get(identity)
                if final_row is None:
                    continue
                keys = sorted(
                    key for key in set(initial_row) | set(final_row)
                    if initial_row.get(key) != final_row.get(key)
                )
                if keys:
                    changed[identity] = keys
            same_ids = final_ids == initial["job_ids"]
            same_cards = [card["id"] for card in final["cards"]] == [card["id"] for card in initial["cards"]]
            counts_match = (final["count"], final_board["total"]) == (initial["count"], board["total"])
            self.result.metadata["final_listing"] = {k: v for k, v in final.items() if k not in ("rows", "cards")}
            self.result.metadata["final_board_feed"] = {k: v for k, v in final_board.items() if k != "rows"}
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": initial["job_ids"],
                "final_ids": final_ids,
                "initial_count": initial["count"],
                "final_count": final["count"],
                "initial_visible_cards": len(initial["cards"]),
                "final_visible_cards": len(final["cards"]),
                "board_feed_total_initial": board["total"],
                "board_feed_total_final": final_board["total"],
                "same_ids_and_order": same_ids,
                "same_visible_cards_and_order": same_cards,
                "counts_match": counts_match,
                "changed_fields_by_job_id": changed,
                "new_job_ids": [i for i in final_ids if i not in initial["job_ids"]],
                "removed_job_ids": [i for i in initial["job_ids"] if i not in final_ids],
                "complete": bool(same_ids and same_cards and counts_match and not changed),
            }
            self.result.errors.extend(final["issues"])
            self.result.errors.extend(final_board["issues"])
            if not same_ids or not same_cards or not counts_match:
                self.result.errors.append("vercel_listing_membership_changed_during_collection")
            if changed:
                self.result.errors.append("vercel_source_fields_changed_during_collection")

        self.result.jobs = jobs
        verified = sum(1 for job in jobs if job.description_verified)
        membership = self.result.metadata.get("membership_reconciliation", {})
        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.metadata["visible_card_count"] = len(initial["cards"])
        self.result.metadata["ssr_jobs_count"] = len(initial["rows"])
        self.result.metadata["board_feed_total"] = board["total"]
        self.result.metadata["descriptions_verified"] = verified
        self.result.metadata["detail_request_seconds_sum"] = round(stats["detail_request_seconds"], 6)
        self.result.metadata["detail_parse_seconds_sum"] = round(stats["detail_parse_seconds"], 6)
        self.result.timings["detail_phase_seconds"] = self.result.timings.get("detail_phase_seconds", 0.0) + stats["detail_parse_seconds"]
        self.result.timings["detail_network_seconds_sum"] = stats["detail_request_seconds"]
        self.result.listing_complete = bool(
            membership.get("complete")
            and len(jobs) == initial["count"] == board["total"]
            and verified == len(jobs)
        )
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result
