"""Tower Research Capital's official /open-positions/ page and its exact Greenhouse embed.

The official page mounts one ``#grnhse_app`` element and publishes exactly one
loader, ``https://boards.greenhouse.io/embed/job_board/js?for=towerresearchcapital``.
That loader inserts an iframe for the company-scoped board

    https://job-boards.greenhouse.io/embed/job_board?for=towerresearchcapital

which paginates 50 postings per page (observed: 50 + 41 = 91, ``total_pages=2``).
Each posting's published URL is ``https://www.tower-research.com/open-positions/?gh_jid=<id>``;
the official page renders that posting's full body from

    https://job-boards.greenhouse.io/embed/job_app?for=towerresearchcapital&token=<id>

The board's own public feed
``https://boards-api.greenhouse.io/v1/boards/towerresearchcapital/jobs?content=true``
is fetched once as an independent membership/field cross-check; its 91 IDs and
row fields match the two board pages exactly. Membership authority stays with
the published embed pages, and the embedded application form is never submitted.
"""
from __future__ import annotations

from hashlib import sha256
import re
import time
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from .base import Adapter
from ..models import Job, text_html
from ..structured import embedded_page_object

OFFICIAL_ENTRY = "https://tower-research.com/open-positions/"
PUBLISHED_PAGE = "https://www.tower-research.com/open-positions/"
EMBED_LOADER_URL = "https://boards.greenhouse.io/embed/job_board/js?for=towerresearchcapital"
BOARD_TOKEN = "towerresearchcapital"
BOARD_URI = "https://job-boards.greenhouse.io/embed/job_board?for=towerresearchcapital"
APPLICATION_URI = "https://job-boards.greenhouse.io/embed/job_app?for=towerresearchcapital"
BOARD_API_URL = "https://boards-api.greenhouse.io/v1/boards/towerresearchcapital/jobs?content=true"
BOARD_HOST = "job-boards.greenhouse.io"
API_HOST = "boards-api.greenhouse.io"
COMPANY_NAME = "Tower Research Capital"
JOB_URL_PATTERN = r"^https://www\.tower-research\.com/open-positions/\?gh_jid=(\d+)$"
JOB_URL_RE = re.compile(JOB_URL_PATTERN)
EMBED_TOKEN = re.compile(r"embedToken:\s*'([^']+)'")
MAX_POSTS_PER_PAGE = 50
METHOD = "tower_research.official_careers_greenhouse_embed.job_app.content"
SALARY_LINE = re.compile(r"base salary", re.I)
SALARY_RANGE = re.compile(
    r"(?:USD\s*)?\$\s*(\d{1,3}(?:,\d{3})*)\s*[-\u2013\u2014]\s*\$?\s*(\d{1,3}(?:,\d{3})*)"
)
SECTION_LABELS = {
    "responsibilities": {"responsibilities", "key responsibilities"},
    "qualifications": {"qualifications", "requirements", "technical requirements"},
}


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_body_text(html: str | None) -> str:
    """Collapse rendered inline text and whitespace for body comparisons."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+([,;:!?])", r"\1", value)
    return re.sub(r"[\s\u00a0]+", " ", value).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _clean_heading(value: str | None) -> str:
    return re.sub(r"[\s\u00a0]+", " ", value or "").strip().rstrip(":").strip().lower()


def _heading_label(block: Tag) -> tuple[str | None, int | None]:
    """Return (label, level) for real headings, including bold-only paragraphs."""
    if block.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return normalized_title(block.get_text(" ", strip=True)) or None, int(block.name[1])
    if block.name in {"p", "div"}:
        strongs = block.find_all(["strong", "b"])
        if len(strongs) == 1:
            whole = normalized_title(block.get_text(" ", strip=True))
            inner = normalized_title(strongs[0].get_text(" ", strip=True))
            if whole and whole == inner and len(whole) <= 80:
                return whole, None
    return None, None


def section_outline(html: str | None) -> list[dict]:
    """Split the published body at its own headings, retaining each section HTML."""
    soup = BeautifulSoup(html or "", "lxml")
    body = soup.body or soup
    while True:
        children = body.find_all(recursive=False)
        if len(children) == 1 and children[0].name == "div":
            body = children[0]
            continue
        break
    sections = [{"heading": None, "level": None, "parts": []}]
    for block in body.find_all(recursive=False):
        label, level = _heading_label(block)
        if label is not None:
            sections.append({"heading": label, "level": level, "parts": []})
        else:
            sections[-1]["parts"].append(str(block))
    return [
        {"heading": section["heading"], "level": section["level"],
         "html": "".join(section["parts"]).strip()}
        for section in sections
        if section["heading"] or section["parts"]
    ]


def structured_sections(html: str | None) -> dict:
    outline = section_outline(html)
    result = {"responsibilities": None, "qualifications": None, "sections": outline}
    for field, labels in SECTION_LABELS.items():
        matches = [
            section for section in outline
            if section["heading"] and _clean_heading(section["heading"]) in labels
        ]
        if len(matches) == 1 and matches[0]["html"]:
            result[field] = matches[0]["html"]
    return result


def parse_salary(description_text: str | None) -> dict | None:
    """Keep the exact published base-salary sentence; never correct or convert it."""
    for raw_line in (description_text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or not SALARY_LINE.search(line):
            continue
        match = SALARY_RANGE.search(line)
        period = (
            "weekly" if re.search(r"\bweekly\b", line, re.I)
            else "annual" if re.search(r"\bannual\b", line, re.I)
            else None
        )
        return {
            "source_text": line,
            "source_field": "job_app.content full description text",
            "range_raw": match.group(0) if match else None,
            "minimum": match.group(1) if match else None,
            "maximum": match.group(2) if match else None,
            "period": period,
            "currency": "USD" if ("$" in line or re.search(r"\bUSD\b", line)) else None,
            "semantics": (
                "Exact published base-salary sentence from the full description; no conversion, "
                "inference or correction of source typos."
            ),
        }
    return None


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.hostname not in {"tower-research.com", "www.tower-research.com"}
            or parsed.path.rstrip("/") != "/open-positions"):
        raise ValueError("Tower Research official entry is not the inspected /open-positions/ page")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if "Tower Research Capital" not in title:
        raise ValueError("Tower Research official entry no longer identifies Tower Research Capital")
    mounts = soup.select("#grnhse_app")
    if len(mounts) != 1:
        raise ValueError("Tower Research official entry no longer exposes exactly one Greenhouse mount")
    loaders = [
        (script.get("src") or "").strip()
        for script in soup.select("script[src]")
        if "greenhouse" in (script.get("src") or "").lower()
    ]
    if loaders != [EMBED_LOADER_URL]:
        raise ValueError(f"Tower Research Greenhouse loader changed: {loaders!r}")
    canonical = soup.select_one('link[rel="canonical"]')
    return {
        "url": response_url,
        "title": title,
        "mount_id": "grnhse_app",
        "embed_loader_url": EMBED_LOADER_URL,
        "canonical": canonical.get("href") if canonical else None,
    }


def parse_embed_loader(script: str) -> dict:
    """Read the board/application URLs the official loader publishes for this company."""
    if "for=towerresearchcapital" not in script:
        raise ValueError("Tower Research embed loader is not scoped to towerresearchcapital")
    values = {}
    for key in ("boardURI", "applicationURI", "baseURI"):
        match = re.search(rf"{key}:\s*'([^']+)'", script)
        if match is None:
            raise ValueError(f"Tower Research embed loader no longer publishes {key}")
        values[key] = match.group(1)
    token = EMBED_TOKEN.search(script)
    values["embed_token"] = token.group(1) if token else None
    if values["boardURI"] != BOARD_URI:
        raise ValueError(f"Tower Research embed board URI changed: {values['boardURI']}")
    if values["applicationURI"] != APPLICATION_URI:
        raise ValueError(f"Tower Research embed application URI changed: {values['applicationURI']}")
    if not _same_official_page(values["baseURI"]):
        raise ValueError(f"Tower Research embed base URI changed: {values['baseURI']}")
    return values


def _same_official_page(url: str | None) -> bool:
    parsed = urlsplit(str(url or ""))
    return (parsed.scheme == "https"
            and parsed.hostname in {"tower-research.com", "www.tower-research.com"}
            and parsed.path.rstrip("/") == "/open-positions")


def _board_query(response_url: str) -> tuple[int, str]:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != BOARD_HOST or parsed.path != "/embed/job_board":
        raise ValueError(f"Tower Research board response came from an unexpected URL: {response_url}")
    query = parse_qs(parsed.query)
    if query.get("for") != [BOARD_TOKEN]:
        raise ValueError(f"Tower Research board response is not scoped to {BOARD_TOKEN}: {response_url}")
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError as exc:
        raise ValueError(f"Tower Research board page parameter is not an integer: {response_url}") from exc
    return page, parsed.query


def parse_board_page(html: str, response_url: str) -> dict:
    page, _ = _board_query(response_url)
    context = embedded_page_object(html, "window.__remixContext = ")
    try:
        loader = context["state"]["loaderData"]["routes/embed.job_board"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Tower Research board page no longer exposes its Remix loader data") from exc
    board = loader.get("board") or {}
    if board.get("name") != COMPANY_NAME:
        raise ValueError(f"Tower Research board identity changed: {board.get('name')!r}")
    if not _same_official_page(board.get("public_url")):
        raise ValueError(f"Tower Research board public_url changed: {board.get('public_url')!r}")
    posts = loader.get("jobPosts") or {}
    try:
        count, total, total_pages = int(posts["count"]), int(posts["total"]), int(posts["total_pages"])
        reported_page = int(posts["page"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Tower Research board page metadata is missing or not numeric") from exc
    rows = posts.get("data")
    if not isinstance(rows, list):
        raise ValueError("Tower Research board page has no job row list")
    if reported_page != page:
        raise ValueError(f"Tower Research board returned page {reported_page} for requested page {page}")
    if count != len(rows):
        raise ValueError(f"Tower Research board page {page} count={count} but returned {len(rows)} rows")
    if total_pages < 1 or total < count or total_pages > 500:
        raise ValueError(f"Tower Research board pagination is implausible: {posts}")
    parsed_rows, seen = [], set()
    for row in rows:
        identity = str(row.get("id", ""))
        if not identity.isdigit():
            raise ValueError(f"Tower Research board row has no numeric id: {row!r}")
        if identity in seen:
            raise ValueError(f"Tower Research board page {page} repeats job ID {identity}")
        seen.add(identity)
        title = normalized_title(row.get("title"))
        if not title:
            raise ValueError(f"Tower Research board row {identity} has no title")
        absolute_url = str(row.get("absolute_url") or "")
        match = JOB_URL_RE.fullmatch(absolute_url)
        if match is None or match.group(1) != identity:
            raise ValueError(f"Tower Research board row {identity} has a non-exact published URL: {absolute_url}")
        department = row.get("department") or {}
        parsed_rows.append({
            "id": identity,
            "title": title,
            "location": normalized_title(row.get("location")),
            "absolute_url": absolute_url,
            "requisition_id": str(row["requisition_id"]) if row.get("requisition_id") not in (None, "") else None,
            "published_at": row.get("published_at"),
            "updated_at": row.get("updated_at"),
            "internal_job_id": row.get("internal_job_id"),
            "department": department,
            "department_name": normalized_title(department.get("name")) or None,
            "is_featured": row.get("is_featured"),
            "page": page,
        })
    return {
        "page": page,
        "count": count,
        "total": total,
        "total_pages": total_pages,
        "rows": parsed_rows,
        "departments": loader.get("departments") or [],
        "offices": loader.get("offices") or [],
        "featured_count": len((loader.get("featuredPosts") or {}).get("data") or []),
    }


def parse_job_app(html: str, response_url: str, job_id: str) -> dict:
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.hostname != BOARD_HOST or parsed.path != "/embed/job_app":
        raise ValueError(f"Tower Research job {job_id} no longer renders its application page: {response_url}")
    query = parse_qs(parsed.query)
    if query.get("for") != [BOARD_TOKEN] or query.get("token") != [str(job_id)]:
        raise ValueError(f"Tower Research job {job_id} detail URL is not company-scoped: {response_url}")
    context = embedded_page_object(html, "window.__remixContext = ")
    loader = context.get("state", {}).get("loaderData", {})
    if "routes/embed.job_app" not in loader:
        raise ValueError(
            f"Tower Research job {job_id} was closed or unpublished before detail fetch "
            f"(redirected to {response_url})"
        )
    post = loader["routes/embed.job_app"].get("jobPost") or {}
    if post.get("post_type") != "job_post":
        raise ValueError(f"Tower Research job {job_id} no longer returns a job_post record")
    title = normalized_title(post.get("title"))
    if not title:
        raise ValueError(f"Tower Research job {job_id} detail has no title")
    expected_url = f"https://www.tower-research.com/open-positions/?gh_jid={job_id}"
    if post.get("public_url") != expected_url:
        raise ValueError(f"Tower Research job {job_id} public_url changed: {post.get('public_url')!r}")
    if post.get("company_name") != COMPANY_NAME:
        raise ValueError(f"Tower Research job {job_id} employer changed: {post.get('company_name')!r}")
    return post


def parse_board_api(payload: dict) -> tuple[list[dict], int]:
    if not isinstance(payload, dict):
        raise ValueError("Tower Research board API response is not a JSON object")
    jobs = payload.get("jobs")
    meta = payload.get("meta") or {}
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Tower Research board API returned no jobs")
    try:
        total = int(meta.get("total", len(jobs)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Tower Research board API meta.total is not an integer: {meta!r}") from exc
    if total != len(jobs):
        raise ValueError(f"Tower Research board API is incomplete: meta.total={total} but returned {len(jobs)}")
    seen = set()
    for job in jobs:
        identity = str(job.get("id", ""))
        if not identity.isdigit():
            raise ValueError(f"Tower Research board API job has no numeric id: {job!r}")
        if identity in seen:
            raise ValueError(f"Tower Research board API repeats job ID {identity}")
        seen.add(identity)
        if job.get("company_name") != COMPANY_NAME:
            raise ValueError(f"Tower Research board API job {identity} has an unexpected employer")
        match = JOB_URL_RE.fullmatch(str(job.get("absolute_url") or ""))
        if match is None or match.group(1) != identity:
            raise ValueError(f"Tower Research board API job {identity} has a non-exact URL")
        if not normalized_title(job.get("title")):
            raise ValueError(f"Tower Research board API job {identity} has no title")
    return jobs, total


class TowerResearch(Adapter):
    """Collect exactly the postings in Tower Research Capital's published embed board."""

    VERIFICATION_REVISION = "tower_research.official-open-positions-greenhouse-embed.1"

    @staticmethod
    def _assert_entry_url(url):
        parsed = urlsplit(str(url))
        if (parsed.scheme != "https"
                or parsed.hostname not in {"tower-research.com", "www.tower-research.com"}
                or parsed.path.rstrip("/") != "/open-positions"):
            raise ValueError(f"Tower Research flow must start from its official /open-positions/ entry: {url}")
        return str(url)

    def _listing_url(self, page: int) -> str:
        return f"{BOARD_URI}&page={page}"

    def _detail_url(self, job_id: str) -> str:
        return f"{APPLICATION_URI}&token={job_id}"

    def _make_job(self, row: dict, api_job: dict | None) -> Job:
        identity = row["id"]
        api_job = api_job or {}
        offices = api_job.get("offices") or []
        location = row["location"] or normalized_title((api_job.get("location") or {}).get("name"))
        raw = {
            "listing_row": row,
            "listing_request": {
                "url": self._listing_url(row["page"]),
                "page": row["page"],
                "source": "published Greenhouse embed board page",
            },
            "board_api_job": api_job or None,
            "source_ui": {
                "official_entry_url": OFFICIAL_ENTRY,
                "embed_loader_url": EMBED_LOADER_URL,
                "board_uri": BOARD_URI,
                "absolute_url": row["absolute_url"],
                "detail_route": self._detail_url(identity),
                "departments": api_job.get("departments") or ([row["department"]] if row.get("department") else []),
                "offices": offices,
                "first_published_raw": row.get("published_at"),
                "updated_at_raw": api_job.get("updated_at"),
                "application_deadline_raw": api_job.get("application_deadline"),
                "data_compliance": api_job.get("data_compliance"),
                "ai_disclaimer": api_job.get("ai_disclaimer"),
                "ai_opt_out_request_url": api_job.get("ai_opt_out_request_url"),
                "metadata": api_job.get("metadata"),
            },
        }
        job = self.job(
            raw,
            title=row["title"],
            url=row["absolute_url"],
            job_id=identity,
            requisition_id=row.get("requisition_id"),
            apply_url=self._detail_url(identity),
            description="",
            method=None,
            description_url=None,
            locations=[{"location": location, "offices": offices}] if location else [],
            department=row.get("department_name"),
            employment_type=None,
            workplace_type=None,
            date_posted=str(row["published_at"]) if row.get("published_at") else None,
            date_posted_raw=row.get("published_at"),
            date_updated=str(api_job["updated_at"]) if api_job.get("updated_at") else None,
            valid_through=(
                str(api_job["application_deadline"])
                if api_job.get("application_deadline") else None
            ),
            salary=None,
            employer=api_job.get("company_name") or COMPANY_NAME,
            language=api_job.get("language") or "en",
        )
        job.record_identity = f"tower-research:{identity}"
        return job

    async def _detail(self, job):
        url = self._detail_url(str(job.job_id))
        response = await self.http.get(url)
        try:
            post = parse_job_app(response.text, str(response.url), str(job.job_id))
        except ValueError:
            job.description_unavailable = True
            raise
        description_html = post.get("content") or ""
        job.description_html = description_html
        job.description_text = text_html(description_html)
        job.description_method = METHOD
        job.description_source_url = url
        job.description_unavailable = not description_html.strip()
        job.salary = parse_salary(job.description_text)
        sections = structured_sections(description_html)
        job.responsibilities = sections["responsibilities"]
        job.qualifications = sections["qualifications"]
        job.raw["job_app"] = {
            "request_url": url,
            "response_url": str(response.url),
            "status": response.status_code,
            "post_type": post.get("post_type"),
            "language": post.get("language"),
            "title": post.get("title"),
            "job_post_location": post.get("job_post_location"),
            "public_url": post.get("public_url"),
            "company_name": post.get("company_name"),
            "pay_ranges": post.get("pay_ranges"),
            "employment_config": post.get("employment"),
            "published_at": post.get("published_at"),
            "application_deadline": post.get("application_deadline"),
            "introduction": post.get("introduction"),
            "conclusion": post.get("conclusion"),
            "fingerprint": post.get("fingerprint"),
            "redirect_to": post.get("redirect_to"),
            "is_featured": post.get("is_featured"),
            "show_quick_apply_cta": post.get("show_quick_apply_cta"),
            "ai_disclaimer": post.get("ai_disclaimer"),
            "include_ai_disclaimer": post.get("include_ai_disclaimer"),
            "ai_opt_out_request_url": post.get("ai_opt_out_request_url"),
            "application_question_count": len(post.get("questions") or []),
            "description_html_sha256": sha256(description_html.encode()).hexdigest(),
            "description_normalized_chars": len(normalized_body_text(description_html)),
            "description_fnv1a64": fnv1a64(normalized_body_text(description_html)),
        }
        job.raw["description_sections"] = sections["sections"]
        if not description_html.strip():
            self.result.errors.append(f"{job.job_id}: Tower Research detail content is empty")
        elif len(job.description_text.strip()) < 100:
            self.result.errors.append(f"{job.job_id}: Tower Research detail content is implausibly short")
        return job

    async def run(self):
        started = time.perf_counter()
        entry_url = self._assert_entry_url(self.company.careers_url)

        entry_started = time.perf_counter()
        entry_response = await self.http.get(entry_url)
        self._assert_entry_url(str(entry_response.url))
        entry = parse_official_entry(entry_response.text, str(entry_response.url))
        entry_seconds = time.perf_counter() - entry_started

        loader_started = time.perf_counter()
        loader_response = await self.http.get(entry["embed_loader_url"])
        loader = parse_embed_loader(loader_response.text)
        loader_seconds = time.perf_counter() - loader_started

        listing_started = time.perf_counter()
        pages, rows = [], []
        page_number = 1
        while True:
            if not self.page_guard():
                break
            response = await self.http.get(self._listing_url(page_number))
            parsed = parse_board_page(response.text, str(response.url))
            pages.append(parsed)
            rows.extend(parsed["rows"])
            if page_number >= parsed["total_pages"]:
                break
            page_number += 1
        listing_seconds = time.perf_counter() - listing_started

        ids = [row["id"] for row in rows]
        unique_ids = list(dict.fromkeys(ids))
        duplicates = sorted({identity for identity in ids if ids.count(identity) > 1})
        total = pages[0]["total"] if pages else None
        page_counts = [page["count"] for page in pages]
        listing_complete = bool(
            pages
            and total is not None
            and len(pages) == pages[0]["total_pages"]
            and sum(page_counts) == total
            and len(ids) == total
            and len(unique_ids) == total
            and not duplicates
        )
        if not listing_complete:
            self.result.errors.append(
                "tower_research_board_pages_not_reconciled:"
                f" total={total} pages={len(pages)} counts={page_counts} rows={len(ids)} unique={len(unique_ids)}"
            )

        api_started = time.perf_counter()
        api_payload = await self.http.json(BOARD_API_URL)
        api_jobs, api_total = parse_board_api(api_payload)
        api_seconds = time.perf_counter() - api_started
        api_by_id = {str(job["id"]): job for job in api_jobs}
        api_ids = list(api_by_id)
        api_mismatches = []
        if api_total != total or set(api_ids) != set(unique_ids):
            api_mismatches.append({
                "kind": "membership",
                "board_total": total,
                "api_total": api_total,
                "api_only": sorted(set(api_ids) - set(unique_ids)),
                "board_only": sorted(set(unique_ids) - set(api_ids)),
            })
        for row in rows:
            api_job = api_by_id.get(row["id"])
            if api_job is None:
                continue
            if normalized_title(api_job.get("title")) != row["title"]:
                api_mismatches.append({"kind": "title", "id": row["id"],
                                       "board": row["title"], "api": api_job.get("title")})
            if normalized_title((api_job.get("location") or {}).get("name")) != row["location"]:
                api_mismatches.append({"kind": "location", "id": row["id"],
                                       "board": row["location"],
                                       "api": (api_job.get("location") or {}).get("name")})
            api_requisition = str(api_job["requisition_id"]) if api_job.get("requisition_id") not in (None, "") else None
            if api_requisition != row.get("requisition_id"):
                api_mismatches.append({"kind": "requisition_id", "id": row["id"],
                                       "board": row.get("requisition_id"), "api": api_requisition})
        if api_mismatches:
            self.result.errors.append(
                f"tower_research_board_api_cross_check_failed: {len(api_mismatches)} mismatch(es)"
            )

        jobs = [self._make_job(row, api_by_id.get(row["id"])) for row in rows]
        for job in jobs:
            self.result.jobs.append(job)
            self.result.dirty_jobs.append(job)
            self.result.validate_job(job)

        await self.hydrate(jobs, self._detail)

        final_started = time.perf_counter()
        final_entry_verified = final_board_verified = False
        final_total, final_ids = None, []
        try:
            final_entry = await self.http.get(entry_url)
            self._assert_entry_url(str(final_entry.url))
            parse_official_entry(final_entry.text, str(final_entry.url))
            final_entry_verified = True
            final_page = await self.http.get(self._listing_url(1))
            final_parsed = parse_board_page(final_page.text, str(final_page.url))
            final_total = final_parsed["total"]
            final_ids = [row["id"] for row in final_parsed["rows"]]
            final_board_verified = True
        except Exception as exc:  # noqa: BLE001
            self.result.errors.append(
                "tower_research_final_official_recheck_failed:"
                f" {type(exc).__name__}: {exc}"
            )
        final_seconds = time.perf_counter() - final_started
        initial_page_one_ids = [row["id"] for row in pages[0]["rows"]] if pages else []
        added = sorted(set(final_ids) - set(initial_page_one_ids)) if final_board_verified else []
        removed = sorted(set(initial_page_one_ids) - set(final_ids)) if final_board_verified else []
        changed = bool(
            final_board_verified
            and (final_total != total or final_ids != initial_page_one_ids)
        )
        if changed:
            self.result.errors.append(
                "tower_research_board_membership_changed_between_initial_and_final_snapshots:"
                f" initial_total={total} final_total={final_total}"
                f" added={added} removed={removed}"
            )

        self.result.expected_total = total
        self.result.listing_complete = bool(
            listing_complete and not api_mismatches and not changed and final_board_verified
        )
        department_counts = {}
        for job in jobs:
            department_counts[job.department] = department_counts.get(job.department, 0) + 1
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": str(entry_response.url),
            "embed_loader_url": entry["embed_loader_url"],
            "embed_loader": {
                "board_uri": loader["boardURI"],
                "application_uri": loader["applicationURI"],
                "base_uri": loader["baseURI"],
                "embed_token_present": bool(loader["embed_token"]),
            },
            "board_uri": BOARD_URI,
            "application_uri": APPLICATION_URI,
            "board_api_url": BOARD_API_URL,
            "membership_authority": (
                "The exact company-scoped Greenhouse embed board published by "
                "tower-research.com/open-positions/: job-boards.greenhouse.io/embed/job_board"
                "?for=towerresearchcapital, pages 1..total_pages. Page counts and the union of "
                "row IDs define membership; the board API is an independent cross-check."
            ),
            "request_shape": {
                "entry": {"method": "GET", "url": OFFICIAL_ENTRY, "query": {},
                          "authentication": None,
                          "redirects": [str(r.url) for r in entry_response.history]},
                "embed_loader": {"method": "GET", "url": entry["embed_loader_url"]},
                "listing": {
                    "method": "GET",
                    "url": BOARD_URI,
                    "query": {"for": BOARD_TOKEN, "page": "1..total_pages"},
                    "pagination": "page query parameter; 50 postings per page",
                    "authentication": None,
                    "observed_from": "iframe src inserted by the official page's Greenhouse loader",
                },
                "detail": {
                    "method": "GET",
                    "url": APPLICATION_URI,
                    "query": {"for": BOARD_TOKEN, "token": "<job id>"},
                    "authentication": None,
                    "observed_from": "iframe rendered by the official ?gh_jid=<id> page",
                },
                "cross_check_api": {
                    "method": "GET", "url": BOARD_API_URL,
                    "query": {"content": "true"}, "authentication": None,
                },
            },
            "ui_pagination": {
                "max_posts_per_page": MAX_POSTS_PER_PAGE,
                "pages": [
                    {"page": page["page"], "count": page["count"], "total": page["total"],
                     "total_pages": page["total_pages"],
                     "first_id": page["rows"][0]["id"] if page["rows"] else None,
                     "last_id": page["rows"][-1]["id"] if page["rows"] else None}
                    for page in pages
                ],
                "page_controls_observed": ["1", "2"],
            },
            "ui_filters": {
                "keyword_search": True,
                "department_select": True,
                "office_select": True,
                "job_alert": True,
                "applied": [],
                "note": "All 91 postings are shown by default; no country/seniority filter applied.",
            },
            "membership_reconciliation": {
                "complete": listing_complete and not api_mismatches and not changed,
                "total": total,
                "pages": len(pages),
                "page_counts": page_counts,
                "sum_page_counts": sum(page_counts),
                "row_count": len(ids),
                "unique_ids": len(unique_ids),
                "duplicates": duplicates,
                "ids": unique_ids,
            },
            "board_api_cross_check": {
                "total": api_total,
                "ids_match": set(api_ids) == set(unique_ids),
                "order_matches": api_ids == unique_ids,
                "mismatches": api_mismatches,
            },
            "final_recheck": {
                "entry_verified": final_entry_verified,
                "board_page_1_verified": final_board_verified,
                "initial_total": total,
                "final_total": final_total,
                "final_page_1_ids": final_ids,
                "membership_changed": changed,
            },
            "department_counts": department_counts,
            "field_policy": {
                "job_id": "embed board row id",
                "requisition_id": "embed board row requisition_id (cross-checked against board API)",
                "date_posted": "embed board row published_at (explicit publication timestamp)",
                "date_updated": "board API updated_at",
                "valid_through": "board API application_deadline",
                "employer": "board API company_name (Tower Research Capital)",
                "language": "board API language",
                "employment_type": "not exposed as a job field; job_app employment is the 'hidden' config flag, kept in raw",
                "workplace_type": "not exposed",
                "salary": "exact base-salary sentence when the full description publishes one",
                "locations": "embed board row location plus board API offices",
                "qualifications": "single exact Qualifications/Requirements section when unambiguous; full body always retained",
                "responsibilities": "single exact Responsibilities section when unambiguous; full body always retained",
            },
            "description_source": (
                "jobPost.content from the exact company-scoped job_app page rendered by the "
                "official ?gh_jid=<id> URL; full HTML retained, application questions excluded."
            ),
            "detail_route_policy": (
                "Each posting's published URL is its absolute_url on tower-research.com; the "
                "rendered body is the Greenhouse embed job_app iframe. The form is never submitted."
            ),
            "out_of_journey_boards": [
                {"url": "https://tower-research.com/roles/", "token": BOARD_TOKEN,
                 "reason": "same embed board, duplicate route"},
                {"url": "https://tower-research.ca/postes-vacants/", "token": "capitalderecherchetower",
                 "reason": "French-Canadian locale mirror of Montréal roles; different IDs, duplicate roles"},
                {"url": "https://tower-research.com/careers-ventures/", "token": "towerresearchventures",
                 "reason": "separate Tower Research Ventures board, 0 jobs, not linked from this journey"},
                {"url": "https://tower-research.com/internships/", "token": BOARD_TOKEN,
                 "reason": "curated links into the same board; one stale closed gh_jid (8212158)"},
            ],
        })
        self.result.timings.update({
            "entry_phase_seconds": round(entry_seconds, 6),
            "embed_loader_seconds": round(loader_seconds, 6),
            "listing_phase_seconds": round(listing_seconds, 6),
            "api_cross_check_seconds": round(api_seconds, 6),
            "final_recheck_seconds": round(final_seconds, 6),
            "source_snapshot_phase_seconds": round(
                entry_seconds + loader_seconds + listing_seconds + api_seconds, 6),
        })
        self.result.metadata["collection_wall_seconds"] = round(time.perf_counter() - started, 6)
        return self.result
