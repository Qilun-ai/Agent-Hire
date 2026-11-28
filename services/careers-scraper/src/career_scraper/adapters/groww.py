"""Groww's own careers page and the single Greenhouse EU board it publishes.

groww.in/careers does not host postings itself: its header, hero, teams CTA and
footer all publish https://job-boards.eu.greenhouse.io/groww.  That board is
server-rendered (Remix) and exposes its paginated membership plus one exact
detail route per posting.  The board's public API is used only to read source
fields the page does not render (requisition id, Designation custom field,
first_published/updated timestamps); descriptions and dates always come from
the exact job page.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..models import text_html


OFFICIAL_ENTRY = "https://groww.in/careers"
BOARD_ORIGIN = "https://job-boards.eu.greenhouse.io"
BOARD_LISTING_URL = BOARD_ORIGIN + "/groww"
BOARD_API_JOBS = "https://boards-api.greenhouse.io/v1/boards/groww/jobs"
BOARD_TOKEN = "groww"
BOARD_NAME = "Groww"
MAX_POSTS_PER_PAGE = 50
JOB_PATH = re.compile(r"^/groww/jobs/(\d+)$")
JOB_LINK = re.compile(r"^https://job-boards\.eu\.greenhouse\.io/groww/jobs/(\d+)$")
ENTRY_BOARD_LINK_TEXT = re.compile(r"^(?:view (?:all )?openings|job openings)$", re.I)
REMIX_PREFIX = "window.__remixContext = "
RESPONSIBILITY_HEADINGS = re.compile(
    r"(?:^|\b)(?:key |job )?responsibilit(?:y|ies)\b|role\s*(?:&|and)\s*responsibilities"
    r"|what you(?:'|’)ll do\b|what you will do\b", re.I)
QUALIFICATION_HEADINGS = re.compile(
    r"qualifications?\b|(?:candidate |preferred )?requirements?\b|skills(?: and| &) qualifications"
    r"|what we(?:'|’)re looking for\b|what makes you a great fit\b"
    r"|preferred candidate profile\b|good to have\b|nice to have\b|^experience$", re.I)
BLOCK_CONTAINERS = {"div", "section", "article", "main", "header", "footer", "aside"}
LIST_BLOCKS = {"ul", "ol", "table"}
SECTION_HEADING_MAX_CHARS = 90


def compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(html: str | None) -> str:
    """Whitespace-normalised visible text of a description fragment."""
    return compact(text_html(html))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _require(condition: bool, message: str):
    if not condition:
        raise ValueError(message)


def _remix_context(html: str) -> dict:
    start = html.find(REMIX_PREFIX)
    _require(start >= 0, "Groww Greenhouse page no longer embeds window.__remixContext")
    brace = html.find("{", start + len(REMIX_PREFIX))
    _require(brace >= 0, "Groww Greenhouse __remixContext has no JSON object")
    try:
        value, _ = json.JSONDecoder().raw_decode(html[brace:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Groww Greenhouse __remixContext is not valid JSON: {exc}") from exc
    _require(isinstance(value, dict), "Groww Greenhouse __remixContext is not an object")
    return value


def _route_loader(context: dict, *, detail: bool) -> dict:
    loader = (context.get("state") or {}).get("loaderData") or {}
    suffix = ".$job_post_id" if detail else "$url_token"
    keys = [key for key in loader if key.endswith(suffix)
            and (key.count(".") >= 1 if detail else key == "routes/$url_token")]
    _require(len(keys) == 1, f"Groww Greenhouse loader data route changed: {sorted(loader)}")
    return loader[keys[0]]


def _iter_leaf_blocks(node: Tag):
    """Yield visible text blocks in order, descending into layout wrappers.

    Greenhouse's description field sometimes starts flat and later wraps the
    remaining sections in ``div.job-preview`` wrappers, so heading detection
    must see the same blocks a reader sees rather than only top-level nodes.
    """
    for child in node.children:
        if isinstance(child, NavigableString):
            text = compact(str(child))
            if text:
                yield None, text
            continue
        if not isinstance(child, Tag):
            continue
        if child.name in LIST_BLOCKS:
            yield child, None
            continue
        if child.name in BLOCK_CONTAINERS and any(
                isinstance(inner, Tag) and (inner.name in BLOCK_CONTAINERS or inner.name in LIST_BLOCKS
                                            or inner.name in {"p", "h1", "h2", "h3", "h4", "h5", "h6",
                                                              "blockquote", "pre"})
                for inner in child.children):
            yield from _iter_leaf_blocks(child)
            continue
        yield child, None


def _pure_heading(block: Tag) -> str | None:
    """Return heading text only when a whole top-level block is just a heading."""
    if block.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        text = compact(block.get_text(" ", strip=True))
        return text if 0 < len(text) <= SECTION_HEADING_MAX_CHARS else None
    emphasis = block.find_all(["strong", "b"])
    if len(emphasis) != 1:
        return None
    text = compact(block.get_text(" ", strip=True))
    label = compact(emphasis[0].get_text(" ", strip=True))
    if (text and len(text) <= SECTION_HEADING_MAX_CHARS
            and re.fullmatch(re.escape(label) + r"\s*:?", text)):
        return text
    return None


def description_sections(html: str | None) -> list[dict]:
    """Split the employer's description into ordered heading/body sections.

    Only whole top-level heading blocks start a section.  Inline bold labels
    inside paragraphs or list items stay inside the surrounding section, so no
    posted text is dropped from the full description.
    """
    soup = BeautifulSoup(html or "", "lxml")
    root = soup.body or soup
    sections: list[dict] = []
    current: dict | None = None
    for block, text_value in _iter_leaf_blocks(root):
        text = text_value if text_value is not None else compact(block.get_text("\n", strip=True))
        if not text:
            continue
        heading = _pure_heading(block) if isinstance(block, Tag) else None
        if heading:
            current = {"heading": heading, "parts": []}
            sections.append(current)
        else:
            if current is None:
                current = {"heading": None, "parts": []}
                sections.append(current)
            current["parts"].append(text)
    return [
        {"heading": section["heading"],
         "text": "\n".join(part for part in section["parts"] if part).strip()}
        for section in sections
    ]


def _matching_sections(sections: list[dict], pattern: re.Pattern) -> list[dict] | None:
    rows = [section for section in sections
            if section["heading"] and pattern.search(section["heading"].strip().rstrip(":"))
            and section["text"]]
    return rows or None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    _require(parsed.scheme == "https" and parsed.netloc == "groww.in"
             and parsed.path.rstrip("/") == "/careers",
             "Groww official careers entry redirected away from the inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = compact(soup.title.get_text(" ", strip=True)) if soup.title else ""
    _require("Careers at Groww" in title, "Groww careers page title changed")
    board_links = []
    for anchor in soup.select(f'a[href="{BOARD_LISTING_URL}"]'):
        label = compact(anchor.get_text(" ", strip=True))
        board_links.append({
            "label": label,
            "rel": anchor.get("rel"),
            "target": anchor.get("target"),
            "matches_expected_cta": bool(ENTRY_BOARD_LINK_TEXT.match(label.replace("  ", " "))),
        })
    _require(len(board_links) >= 2,
             "Groww official careers page no longer publishes the Greenhouse board links")
    _require(any(link["matches_expected_cta"] for link in board_links),
             "Groww official careers page no longer labels its Greenhouse board link as openings")
    return {
        "url": response_url,
        "title": title,
        "board_url": BOARD_LISTING_URL,
        "board_link_count": len(board_links),
        "board_links": board_links,
    }


def _listing_url_for_page(page: int) -> str:
    return BOARD_LISTING_URL if page <= 1 else f"{BOARD_LISTING_URL}?page={page}"


def parse_board_listing(html: str, response_url: str = BOARD_LISTING_URL) -> dict:
    parsed = urlsplit(response_url)
    query = parse_qs(parsed.query)
    page = int(query["page"][0]) if "page" in query else 1
    _require(parsed.scheme == "https" and parsed.netloc == "job-boards.eu.greenhouse.io"
             and parsed.path.rstrip("/") == "/groww",
             "Groww Greenhouse listing came from an unexpected route")
    _require(not set(query) - {"page", "departments[]", "offices[]"},
             f"Groww Greenhouse listing carried unexpected filters: {sorted(query)}")
    context = _remix_context(html)
    loader = _route_loader(context, detail=False)
    board = loader.get("board") or {}
    _require(board.get("name") == BOARD_NAME, "Groww Greenhouse board name changed")
    _require(board.get("public_url", "").rstrip("/") == BOARD_LISTING_URL,
             "Groww Greenhouse board public URL changed")
    posts = loader.get("jobPosts") or {}
    count, total, total_pages = (posts.get(key) for key in ("count", "total", "total_pages"))
    _require(all(isinstance(value, int) and value >= 0 for value in (count, total, total_pages)),
             "Groww Greenhouse listing is missing numeric counts")
    _require(posts.get("page") == page, "Groww Greenhouse listing page parameter does not match")
    rows = posts.get("data") or []
    _require(len(rows) == count, "Groww Greenhouse listing row count does not match its metadata")
    _require(count <= MAX_POSTS_PER_PAGE, "Groww Greenhouse listing page exceeded its published page size")

    soup = BeautifulSoup(html, "lxml")
    headings = [compact(node.get_text(" ", strip=True)) for node in soup.select("h1,h2")]
    _require(any("Current job openings at Groww" in heading for heading in headings),
             "Groww Greenhouse listing heading is missing")
    heading_counts = [int(match.group(1)) for heading in headings
                      if (match := re.fullmatch(r"(\d+) jobs?", heading))]
    _require(heading_counts == [total],
             f"Groww Greenhouse rendered job count {heading_counts} does not match listing total {total}")
    rendered = []
    for anchor in soup.select('a[href*="/groww/jobs/"]'):
        match = JOB_LINK.fullmatch(anchor.get("href", ""))
        if match is None:
            continue
        if match.group(1) not in rendered:
            rendered.append(match.group(1))
    _require(len(rendered) == count,
             "Groww Greenhouse rendered job links do not match the listing rows")

    departments = [{"id": department.get("id"), "name": compact(department.get("name"))}
                   for department in loader.get("departments") or []]
    offices = loader.get("offices") or []
    slim_rows = []
    for row in rows:
        identity = str(row.get("id"))
        slim_rows.append({
            "id": identity,
            "title": row.get("title"),
            "requisition_id": row.get("requisition_id"),
            "location": row.get("location"),
            "absolute_url": row.get("absolute_url"),
            "published_at": row.get("published_at"),
            "updated_at": row.get("updated_at"),
            "department": row.get("department"),
            "is_featured": row.get("is_featured"),
            "internal_job_id": row.get("internal_job_id"),
        })
    return {
        "page": page,
        "pages": total_pages,
        "total": total,
        "count": count,
        "rows": slim_rows,
        "job_ids": [row["id"] for row in slim_rows],
        "rendered_job_ids": rendered,
        "heading_counts": heading_counts,
        "departments": departments,
        "offices": offices,
        "department_filter_ids": loader.get("departmentIds") or [],
        "office_filter_ids": loader.get("officeIds") or [],
        "filters_visible": {
            "keyword_search": bool(soup.select_one('input[placeholder="Search"]')),
            "filter_labels": [compact(label.get_text(" ", strip=True))
                              for label in soup.select("label.select__label")],
            "combobox_count": len(soup.select("input.select__input")),
        },
        "url": response_url,
    }


def parse_board_api(value: dict) -> dict:
    _require(isinstance(value, dict), "Groww Greenhouse jobs API response is not an object")
    jobs = value.get("jobs")
    meta = value.get("meta") or {}
    _require(isinstance(jobs, list), "Groww Greenhouse jobs API response is missing jobs")
    _require(isinstance(meta.get("total"), int), "Groww Greenhouse jobs API response is missing meta.total")
    _require(meta["total"] == len(jobs),
             "Groww Greenhouse jobs API meta.total does not match its rows")
    rows = {}
    for row in jobs:
        identity = str(row.get("id"))
        _require(row.get("absolute_url", "").startswith(BOARD_LISTING_URL + "/jobs/"),
                 f"Groww Greenhouse API job {identity} left the published EU board")
        _require(row.get("company_name") == BOARD_NAME,
                 f"Groww Greenhouse API job {identity} company name changed")
        _require(isinstance(row.get("content"), str) and row["content"].strip(),
                 f"Groww Greenhouse API job {identity} has no full description content")
        rows[identity] = row
    return {"rows": rows, "total": meta["total"], "job_ids": sorted(rows)}


def parse_job_detail(html: str, response_url: str) -> dict:
    parsed = urlsplit(response_url)
    match = JOB_PATH.fullmatch(parsed.path)
    _require(parsed.scheme == "https" and parsed.netloc == "job-boards.eu.greenhouse.io"
             and match is not None,
             "Groww Greenhouse job detail came from an unexpected route")
    identity = match.group(1)
    context = _remix_context(html)
    loader = _route_loader(context, detail=True)
    post = loader.get("jobPost") or {}
    _require(str(loader.get("jobPostId") or identity) == identity,
             "Groww Greenhouse detail route job id does not match its loader data")
    _require(post.get("post_type") == "job_post",
             "Groww Greenhouse detail is not a published job post")
    _require(post.get("public_url", "").rstrip("/") == response_url.rstrip("/"),
             "Groww Greenhouse detail public_url does not match the requested job")
    title = compact(post.get("title"))
    content = post.get("content") or ""
    _require(bool(title), "Groww Greenhouse detail has no title")
    _require(bool(content.strip()), "Groww Greenhouse detail has no description content")
    location = compact(post.get("job_post_location"))
    sections = description_sections(content)
    return {
        "id": identity,
        "title": title,
        "location": location,
        "content_html": content,
        "description_text": text_html(content),
        "normalized_text": normalized_body_text(content),
        "sections": sections,
        "responsibilities": _matching_sections(sections, RESPONSIBILITY_HEADINGS),
        "qualifications": _matching_sections(sections, QUALIFICATION_HEADINGS),
        "published_at": post.get("published_at"),
        "application_deadline": post.get("application_deadline"),
        "pay_ranges": post.get("pay_ranges") or [],
        "employment_flag": post.get("employment"),
        "language": post.get("language"),
        "company_name": post.get("company_name"),
        "questions_count": len(post.get("questions") or []),
        "apply_form_embedded": True,
        "description_sha256": sha256((post.get("content") or "").encode()).hexdigest(),
        "response_url": response_url,
    }


class Groww(Adapter):
    """Collect only the 9-row board Groww publishes from groww.in/careers."""

    ENTRY_URL = OFFICIAL_ENTRY

    async def run(self):
        started = time.perf_counter()
        entry_response = await self.http.get(OFFICIAL_ENTRY)
        entry = parse_official_entry(entry_response.text, str(entry_response.url))
        _require(entry["board_url"] == BOARD_LISTING_URL,
                 "Groww official careers page publishes a different Greenhouse board")

        pages, rows, page_urls = {}, {}, []
        page = 1
        while self.page_guard():
            url = _listing_url_for_page(page)
            response = await self.http.get(url)
            listing = parse_board_listing(response.text, str(response.url))
            _require(listing["page"] == page, "Groww Greenhouse returned a different listing page")
            page_urls.append({"page": page, "url": str(response.url), "count": listing["count"],
                              "total": listing["total"], "pages": listing["pages"]})
            for row in listing["rows"]:
                _require(row["id"] not in rows,
                         f"Groww Greenhouse repeated job id {row['id']} across pages")
                rows[row["id"]] = row
            pages[page] = {"total": listing["total"], "pages": listing["pages"],
                           "count": listing["count"], "rendered_job_ids": listing["rendered_job_ids"]}
            if page == 1:
                self.result.metadata["entry"] = entry
                self.result.metadata["board_listing"] = {
                    "url": listing["url"], "total": listing["total"], "pages": listing["pages"],
                    "departments": listing["departments"],
                    "offices": listing["offices"],
                    "filters_visible": listing["filters_visible"],
                    "department_filter_ids": listing["department_filter_ids"],
                    "office_filter_ids": listing["office_filter_ids"],
                    "heading_counts": listing["heading_counts"],
                }
                self.result.expected_total = listing["total"]
            expected_pages = listing["pages"]
            if page >= expected_pages:
                break
            page += 1
        _require(len(rows) == self.result.expected_total,
                 f"Groww board advertised {self.result.expected_total} roles but listed {len(rows)}")
        first = pages.get(1) or {}
        _require(set(first.get("rendered_job_ids") or []) == set(rows),
                 "Groww rendered listing links do not match the board's job ids")
        self.result.pages = len(pages)

        api_response = await self.http.get(BOARD_API_JOBS, params={"content": "true"})
        api = parse_board_api(api_response.json())
        _require(set(api["job_ids"]) == set(rows),
                 "Groww Greenhouse API membership does not match the rendered board")
        _require(api["total"] == self.result.expected_total,
                 "Groww Greenhouse API total does not match the rendered board")
        self.result.metadata["board_api"] = {
            "url": str(api_response.url),
            "total": api["total"],
            "request_url": str(api_response.request.url),
            "response_sha256": sha256(api_response.content).hexdigest(),
        }

        for identity, row in rows.items():
            api_row = api["rows"][identity]
            api_text = compact(text_html(api_row.get("content")))
            metadata = [item for item in api_row.get("metadata") or [] if item.get("name")]
            designation = next((item.get("value") for item in metadata
                                if compact(item.get("name")).lower() == "designation"), None)
            job = self.job(
                {
                    "board_listing_row": row,
                    "board_api_row": {key: value for key, value in api_row.items() if key != "content"},
                    "custom_fields": metadata,
                    "entry_url": OFFICIAL_ENTRY,
                    "detail_route": row["absolute_url"],
                    "api_content_normalized_chars": len(api_text),
                    "api_content_sha256": sha256(api_text.encode()).hexdigest(),
                    "apply_form_embedded": True,
                },
                title=compact(row.get("title")),
                url=row["absolute_url"],
                job_id=identity,
                requisition_id=api_row.get("requisition_id"),
                apply_url=row["absolute_url"],
                locations=[row.get("location")] if row.get("location") else [],
                department=(row.get("department") or {}).get("name"),
                employer=api_row.get("company_name") or BOARD_NAME,
                date_posted=row.get("published_at"),
                date_posted_raw=row.get("published_at"),
                date_updated=row.get("updated_at"),
                valid_through=api_row.get("application_deadline"),
                salary=None,
                language=api_row.get("language"),
            )
            job.raw["designation"] = designation
            job.raw["offices"] = api_row.get("offices") or []
            job.raw["departments"] = api_row.get("departments") or []
            self.add(job)
            if self.limit():
                break

        self.result.metadata["company_listing"] = {
            "source": "groww.in/careers -> job-boards.eu.greenhouse.io/groww",
            "board_total": self.result.expected_total,
            "pages": page_urls,
            "job_ids": list(rows),
        }
        self.result.listing_complete = len(self.result.jobs) == self.result.expected_total
        if not self.result.listing_complete:
            self.result.errors.append("groww_listing_membership_mismatch")
        self.result.timings["source_collection_seconds"] = max(0.0, time.perf_counter() - started)
        return self.result

    async def _detail(self, job):
        response = await self.http.get(job.url)
        detail = parse_job_detail(response.text, str(response.url))
        if detail["id"] != job.job_id:
            raise ValueError(f"Groww detail id {detail['id']} does not match listing id {job.job_id}")
        if detail["title"] != compact(job.title):
            raise ValueError(f"Groww detail title changed for {job.job_id}")
        listed_location = compact((job.locations or [""])[0])
        if detail["location"] and listed_location and detail["location"] != listed_location:
            raise ValueError(f"Groww detail location changed for {job.job_id}")
        job.description_html = detail["content_html"]
        job.description_text = detail["description_text"]
        job.description_method = "groww.greenhouse_eu.job_page_description"
        job.description_source_url = detail["response_url"]
        job.date_posted = detail["published_at"] or job.date_posted
        job.valid_through = detail["application_deadline"]
        job.salary = detail["pay_ranges"] or None
        job.responsibilities = detail["responsibilities"]
        job.qualifications = detail["qualifications"]
        job.raw["detail"] = {
            "url": detail["response_url"],
            "response_sha256": sha256(response.content).hexdigest(),
            "description_sha256": detail["description_sha256"],
            "description_normalized_chars": len(detail["normalized_text"]),
            "description_fnv1a64": fnv1a64(detail["normalized_text"]),
            "sections": detail["sections"],
            "section_headings": [section["heading"] for section in detail["sections"]],
            "pay_ranges": detail["pay_ranges"],
            "employment_flag": detail["employment_flag"],
            "application_questions_count": detail["questions_count"],
            "apply_form_embedded": detail["apply_form_embedded"],
            "dates": {"published_at": detail["published_at"],
                      "application_deadline": detail["application_deadline"]},
        }
        if sha256(detail["normalized_text"].encode()).hexdigest() != job.raw.get("api_content_sha256"):
            job.warnings.append("greenhouse_api_content_differs_from_exact_job_page")
        if job.raw.get("designation") is None:
            job.warnings.append("designation_custom_field_missing")
        return job

    async def run_details(self):
        return await self.hydrate(self.result.jobs, self._detail)
