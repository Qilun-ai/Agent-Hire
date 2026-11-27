"""HackerRank's own careers page, the Greenhouse board it publishes, and that board's jobs.

The official ``https://www.hackerrank.com/careers`` entry 301-redirects to
``https://www.hackerrank.com/careers/home``, a Sherpa/Framer page whose
``#open-roles`` section contains one client-rendered widget::

    <div class="careers-container" data-greenhouse-jobs aria-busy="true">
      <div class="jobs-column" data-jobs-list>Loading open roles...</div>
    </div>

The page's own module (``/_sherpa/assets/generated/CareersJobs...js`` and the
``greenhouse...js`` module it imports, both served by ``www.hackerrank.com``)
hard-codes the company-scoped board and its exact requests:

* listing  ``https://boards-api.greenhouse.io/v1/boards/hackerrank/departments?render_as=list``
* detail   ``https://boards-api.greenhouse.io/v1/boards/hackerrank/jobs/<id>``
* apply    ``https://job-boards.greenhouse.io/hackerrank/jobs/<id>?gh_src=embed``
* route    ``/careers/job?id=<id>``

So the ATS is used only because HackerRank's own page requests it, and only for
the ``hackerrank`` board it names. The departments request returns all 29
postings in one response (no pagination, search or filters; the UI drops empty
departments and sorts departments by name and jobs by title). The rendered
widget shows the same 29 rows and has no pagination control.

Each posting's full body is the ``content`` field of the exact per-job request
that the company's own detail route makes; the rendered ``#job-content`` body
matches it. The application iframe (the apply URL above) is never opened or
submitted.
"""
from __future__ import annotations

from datetime import datetime
import html
from hashlib import sha256
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .base import Adapter
from ..models import text_html

OFFICIAL_ENTRY = "https://www.hackerrank.com/careers"
CAREERS_HOME = "https://www.hackerrank.com/careers/home"
LISTING_SECTION = "open-roles"
BOARD = "hackerrank"
LISTING_API = "https://boards-api.greenhouse.io/v1/boards/hackerrank/departments?render_as=list"
BOARD_JOBS_API = "https://boards-api.greenhouse.io/v1/boards/hackerrank/jobs?content=false"
DETAIL_API = "https://boards-api.greenhouse.io/v1/boards/hackerrank/jobs/{job_id}"
DETAIL_PAGE = "https://www.hackerrank.com/careers/job?id={job_id}"
APPLY_URL = "https://job-boards.greenhouse.io/hackerrank/jobs/{job_id}?gh_src=embed"
BOARD_API_ROOT = "https://boards-api.greenhouse.io/v1/boards/hackerrank/"
APPLY_ROOT = "https://job-boards.greenhouse.io/hackerrank/jobs/"
METHOD = "hackerrank.official_careers_embed.greenhouse_job_content"

JOB_ABSOLUTE_URL = re.compile(r"^https://job-boards\.greenhouse\.io/hackerrank/jobs/(\d+)$")
SALARY_RANGE = re.compile(
    r"\$\s?([\d][\d,]{3,})\s*(?:[\u2013\u2014\u2212-]|to)\s*\$?\s?([\d][\d,]{3,})"
)
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?)(?:[?#]|$)", re.I)
WORKPLACE_PREFIXES = ("hybrid", "onsite", "on-site", "remote", "in-office", "in office", "office")
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
RESPONSIBILITY_TOKENS = ("responsibilit", "what you will do", "what you\u2019ll do",
                         "what you'll do", "what you will be doing", "your role")
QUALIFICATION_TOKENS = ("qualif", "requirement", "who you are", "what we need",
                        "even better if you have", "you will thrive", "you'll fit right in",
                        "you\u2019ll fit right in", "what you bring", "skills")


def normalized_title(value) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value or ""))).strip()


def normalized_body_text(value: str | None) -> str:
    """Visible body text with layout whitespace and no-break spaces normalised."""
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = soup.get_text(" ")
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()


def compact_body(value: str | None) -> str:
    """Whitespace-insensitive body identity; layout whitespace is not content."""
    return re.sub(r"\s", "", (value or "").replace("\xa0", " "))


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _iso_or_none(value, field: str, issues: list[str]):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        issues.append(f"detail_date_not_string:{field}")
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        issues.append(f"detail_date_unparseable:{field}")
        return None
    return value


def workplace_label(location: str | None) -> str | None:
    """The work-mode word the company publishes as the location prefix."""
    text = normalized_title(location)
    lowered = text.lower()
    for prefix in WORKPLACE_PREFIXES:
        if lowered.startswith(prefix):
            return text[:len(prefix)].strip()
    return None


def parse_official_entry(html: str, response_url: str = CAREERS_HOME) -> dict:
    """Validate the company's own careers page and its published jobs widget."""
    parsed = urlsplit(str(response_url))
    if (parsed.scheme != "https" or parsed.netloc != "www.hackerrank.com"
            or parsed.path.rstrip("/") != "/careers/home"):
        raise ValueError("HackerRank official careers entry redirected away from /careers/home")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if title != "HackerRank Careers":
        raise ValueError(f"HackerRank official careers page title changed: {title!r}")
    containers = soup.select("[data-greenhouse-jobs]")
    if len(containers) != 1:
        raise ValueError("HackerRank careers page no longer exposes exactly one jobs widget")
    container = containers[0]
    list_nodes = container.select("[data-jobs-list]")
    if len(list_nodes) != 1:
        raise ValueError("HackerRank jobs widget no longer has one jobs-list container")
    section = soup.select_one(f"section#{LISTING_SECTION}") or soup.select_one(f"#{LISTING_SECTION}")
    scripts = [normalized_title(script.get("src")) for script in soup.select("script[src]")
               if "CareersJobs" in (script.get("src") or "")]
    if len(scripts) != 1:
        raise ValueError("HackerRank careers page no longer loads exactly one CareersJobs module")
    canonical = soup.select_one('link[rel="canonical"]')
    return {
        "request_url": OFFICIAL_ENTRY,
        "final_url": str(response_url),
        "page_title": title,
        "canonical": canonical.get("href") if canonical else None,
        "jobs_widget_selector": "[data-greenhouse-jobs]",
        "jobs_widget_class": container.get("class"),
        "jobs_widget_aria_busy": container.get("aria-busy"),
        "jobs_widget_server_rendered_job_anchors": len(container.select("a[data-job-id]")),
        "jobs_widget_placeholder": normalized_title(list_nodes[0].get_text(" ", strip=True))[:120],
        "listing_section_present": section is not None,
        "listing_section_id": section.get("id") if section else None,
        "careers_jobs_module": scripts[0],
    }


def parse_ui_bundles(careers_js: str, greenhouse_js: str, careers_js_url: str) -> dict:
    """Confirm the company-served modules publish the exact board endpoints."""
    if "[data-greenhouse-jobs]" not in careers_js:
        raise ValueError("HackerRank CareersJobs module no longer drives the jobs widget")
    if "/careers/job?" not in careers_js:
        raise ValueError("HackerRank CareersJobs module no longer links its /careers/job route")
    if "greenhouse" not in careers_js:
        raise ValueError("HackerRank CareersJobs module no longer imports the Greenhouse module")
    required = {
        "board_api_root": BOARD_API_ROOT,
        "listing_request": "departments?render_as=list",
        "detail_request_prefix": "jobs/",
        "apply_root": APPLY_ROOT,
        "embed_source_parameter": "gh_src",
        "embed_source_default": "embed",
    }
    missing = [name for name, needle in required.items() if needle not in greenhouse_js]
    if missing:
        raise ValueError(f"HackerRank Greenhouse module no longer names: {missing}")
    if f"boards/{BOARD}/" not in greenhouse_js:
        raise ValueError("HackerRank Greenhouse module no longer names its own board token")
    import_match = re.search(r"""["']\./(greenhouse\.[A-Za-z0-9_-]+\.js)["']""", careers_js)
    return {
        "careers_jobs_module_url": careers_js_url,
        "careers_jobs_module_sha256": sha256(careers_js.encode()).hexdigest(),
        "greenhouse_module": import_match.group(1) if import_match else None,
        "greenhouse_module_sha256": sha256(greenhouse_js.encode()).hexdigest(),
        "board_token_published_by_company_page": BOARD,
        "listing_request_published": LISTING_API,
        "detail_request_published": BOARD_API_ROOT + "jobs/<id>",
        "apply_url_published": APPLY_ROOT + "<id>?gh_src=embed",
        "detail_route_published": "/careers/job?id=<id>",
    }


def parse_departments(payload) -> dict:
    """Parse the exact listing request the company page makes."""
    if not isinstance(payload, dict):
        raise ValueError("HackerRank departments response is not a JSON object")
    departments = payload.get("departments")
    if not isinstance(departments, list) or not departments:
        raise ValueError("HackerRank departments response has no departments")
    rows, seen, issues = [], set(), []
    for department in departments:
        if not isinstance(department, dict):
            raise ValueError("HackerRank departments response contains a non-object department")
        name = normalized_title(department.get("name"))
        jobs = department.get("jobs")
        if not isinstance(name, str) or not name:
            raise ValueError("HackerRank department has no name")
        if not isinstance(jobs, list):
            raise ValueError(f"HackerRank department {name!r} has no jobs array")
        department_rows = []
        for job in jobs:
            if not isinstance(job, dict):
                raise ValueError(f"HackerRank department {name!r} contains a non-object job")
            identity = str(job.get("id", ""))
            if not identity.isdigit():
                raise ValueError(f"HackerRank department {name!r} job has no numeric id: {identity!r}")
            title_raw = job.get("title")
            title = normalized_title(title_raw)
            if not title:
                raise ValueError(f"HackerRank job {identity} has no title")
            location = normalized_title((job.get("location") or {}).get("name"))
            absolute = normalized_title(job.get("absolute_url"))
            match = JOB_ABSOLUTE_URL.fullmatch(absolute)
            if not match or match.group(1) != identity:
                issues.append(f"listing_row_non_exact_absolute_url:{identity}")
            if identity in seen:
                issues.append(f"listing_row_duplicate_job_id:{identity}")
                continue
            seen.add(identity)
            row = dict(job)
            row["id"] = identity
            row["title"] = title
            row["title_raw"] = title_raw
            row["location"] = {"name": location}
            row["department"] = name
            department_rows.append(row)
        rows.append({
            "id": str(department.get("id")),
            "name": name,
            "job_count": len(department_rows),
            "jobs": department_rows,
        })
    non_empty = [department for department in rows if department["jobs"]]
    if not non_empty:
        raise ValueError("HackerRank departments response exposes no posting")
    ui_order = ui_order_of(non_empty)
    return {
        "departments": non_empty,
        "empty_departments": [d["name"] for d in rows if not d["jobs"]],
        "all_department_count": len(rows),
        "job_ids": [row["id"] for department in non_empty for row in department["jobs"]],
        "ui_order": ui_order,
        "ui_order_rule": (
            "Company bundle sorts non-empty departments by name (localeCompare) and each "
            "department's jobs by title (localeCompare); reproduced here with the same rule."
        ),
        "issues": issues,
    }


def ui_order_of(departments) -> list[str]:
    """Reproduce the widget's render order from the API payload."""
    ordered = sorted(departments, key=lambda department: department["name"])
    return [row["id"] for department in ordered
            for row in sorted(department["jobs"], key=lambda job: job["title_raw"])]


def parse_board_jobs(payload) -> dict:
    """Board-scoped membership cross-check (same company board, all rows)."""
    if not isinstance(payload, dict):
        raise ValueError("HackerRank board jobs response is not a JSON object")
    jobs = payload.get("jobs")
    meta = payload.get("meta") or {}
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("HackerRank board jobs response has no jobs")
    total = meta.get("total", len(jobs))
    try:
        total = int(total)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"HackerRank board meta.total is not an integer: {total!r}") from exc
    ids, issues = [], []
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("HackerRank board jobs response contains a non-object job")
        identity = str(job.get("id", ""))
        if not identity.isdigit():
            raise ValueError(f"HackerRank board job has no numeric id: {identity!r}")
        ids.append(identity)
    if total != len(jobs):
        issues.append("board_meta_total_does_not_match_rows")
    if len(set(ids)) != len(ids):
        issues.append("board_jobs_repeat_an_id")
    return {"job_ids": ids, "total": total, "row_count": len(jobs), "issues": issues}


def parse_job_detail(payload, expected_id: str) -> dict:
    """Validate one exact per-job response requested by the company detail route."""
    if not isinstance(payload, dict):
        raise ValueError(f"HackerRank job {expected_id} response is not a JSON object")
    identity = str(payload.get("id", ""))
    if identity != str(expected_id):
        raise ValueError(f"HackerRank job detail id mismatch: expected {expected_id}, got {identity}")
    title = normalized_title(payload.get("title"))
    if not title:
        raise ValueError(f"HackerRank job {identity} has no title")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"HackerRank job {identity} has no content field")
    # Greenhouse returns the post HTML escaped once; the company page decodes it
    # before rendering #job-content, so membership and body identity are checked
    # against that same decoded HTML.
    content_html = html.unescape(content)
    issues = []
    absolute = normalized_title(payload.get("absolute_url"))
    match = JOB_ABSOLUTE_URL.fullmatch(absolute)
    if not match or match.group(1) != identity:
        issues.append("detail_non_exact_absolute_url")
    dates = {field: _iso_or_none(payload.get(field), field, issues)
             for field in ("first_published", "updated_at", "application_deadline")}
    departments = payload.get("departments")
    if departments is None:
        departments = []
    if not isinstance(departments, list) or any(not isinstance(d, dict) for d in departments):
        raise ValueError(f"HackerRank job {identity} departments field changed shape")
    offices = payload.get("offices")
    if offices is None:
        offices = []
    if not isinstance(offices, list):
        raise ValueError(f"HackerRank job {identity} offices field changed shape")
    location = payload.get("location")
    if location is not None and not isinstance(location, dict):
        raise ValueError(f"HackerRank job {identity} location field changed shape")
    return {
        "id": identity,
        "title": title,
        "content": content,
        "content_html": content_html,
        "absolute_url": absolute or None,
        "requisition_id": payload.get("requisition_id"),
        "internal_job_id": payload.get("internal_job_id"),
        "company_name": payload.get("company_name"),
        "language": payload.get("language"),
        "location_name": normalized_title((location or {}).get("name")),
        "departments": [{"id": d.get("id"), "name": normalized_title(d.get("name"))}
                        for d in departments if normalized_title(d.get("name"))],
        "offices": offices,
        "metadata": payload.get("metadata") or [],
        "data_compliance": payload.get("data_compliance"),
        "ai_disclaimer": payload.get("ai_disclaimer"),
        "include_ai_disclaimer": payload.get("include_ai_disclaimer"),
        "ai_opt_out_request_url": payload.get("ai_opt_out_request_url"),
        "first_published": dates["first_published"],
        "updated_at": dates["updated_at"],
        "application_deadline": dates["application_deadline"],
        "issues": issues,
    }


def description_sections(html_text: str | None) -> list[dict]:
    """Keep every employer-authored heading and the exact body below it."""
    soup = BeautifulSoup(f'<div data-hackerrank-body="true">{html_text or ""}</div>', "lxml")
    root = soup.select_one("[data-hackerrank-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections: list[dict] = []
    current = {"heading": None, "html": ""}
    for block in blocks:
        heading = None
        if block.name in HEADING_TAGS:
            heading = normalized_title(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find(["strong", "b"], recursive=False)
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text
        if heading:
            if current["html"] or current["heading"]:
                sections.append(_finish_section(current))
            current = {"heading": heading, "html": ""}
            continue
        current["html"] += str(block)
    if current["html"] or current["heading"]:
        sections.append(_finish_section(current))
    if not sections:
        sections.append({"heading": None, "html": html_text or "", "text": text_html(html_text)})
    return sections


def _finish_section(section: dict) -> dict:
    return {"heading": section["heading"], "html": section["html"],
            "text": text_html(section["html"]) if section["html"] else ""}


def sections_matching(sections, tokens) -> list[dict] | None:
    matched = [section for section in sections
               if section["heading"] and any(token in section["heading"].lower() for token in tokens)]
    return matched or None


def compensation_salary(sections) -> dict | None:
    """Only a published numeric range becomes a salary object; nothing is inferred."""
    for section in sections:
        if not section["heading"] or "compensation" not in section["heading"].lower():
            continue
        text = section["text"] or ""
        match = SALARY_RANGE.search(text)
        if not match:
            continue
        return {
            "source_section": section["heading"],
            "source_text": text,
            "range_raw": match.group(0),
            "minimum": match.group(1).replace(",", ""),
            "maximum": match.group(2).replace(",", ""),
            "currency": "USD",
            "semantics": (
                "Numeric range exactly as published in the posting's Compensation section; "
                "no conversion, annualisation or inference."
            ),
        }
    return None


def description_links(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text or "", "lxml")
    return [{"text": anchor.get_text(" ", strip=True) or None, "url": anchor.get("href")}
            for anchor in soup.select("a[href]")]


def parse_detail_page(html: str, job_id: str, response_url: str) -> dict:
    """Check the company's own client-rendered detail route and its apply control."""
    if not str(job_id).isdigit():
        raise ValueError(f"HackerRank detail route needs a numeric job id, got {job_id!r}")
    parsed = urlsplit(str(response_url))
    if (parsed.scheme != "https" or parsed.netloc != "www.hackerrank.com"
            or parsed.path.rstrip("/") != "/careers/job"
            or parsed.query != f"id={job_id}"):
        raise ValueError(f"HackerRank detail route changed for job {job_id}: {response_url}")
    soup = BeautifulSoup(html, "lxml")
    section = soup.select_one("[data-careers-job]")
    heading = soup.select_one("#job-title")
    content = soup.select_one("#job-content")
    apply_control = soup.select_one("#greenhouse-apply-button-center")
    if section is None or heading is None or content is None or apply_control is None:
        raise ValueError(f"HackerRank detail route no longer exposes its job shell for {job_id}")
    tabs = [normalized_title(node.get_text(" ", strip=True))
            for node in soup.select('[role="tab"][data-w-tab]')]
    return {
        "url": str(response_url),
        "job_shell_selector": "[data-careers-job]",
        "job_title_heading_present": heading is not None,
        "job_title_server_rendered": normalized_title(heading.get_text(" ", strip=True)) or None,
        "job_content_container_present": content is not None,
        "job_content_server_rendered_chars": len(normalized_body_text(content.decode_contents())),
        "client_rendered_from_per_job_request": not normalized_body_text(content.decode_contents()),
        "apply_control_present": apply_control is not None,
        "apply_control_label": normalized_title(apply_control.get_text(" ", strip=True)),
        "tabs": tabs,
        "missing_destination": section.get("data-missing-destination"),
        "greenhouse_module_loaded": any("greenhouse" in (script.get("src") or "")
                                        for script in soup.select("script[src]")),
    }


class Hackerrank(Adapter):
    """Collect exactly the postings HackerRank's own careers page publishes."""

    VERIFICATION_REVISION = "hackerrank.official-careers-sherpa-embed.greenhouse-job-content.1"

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        entry = parse_official_entry(response.text, str(response.url))
        module_url = "https://www.hackerrank.com" + entry["careers_jobs_module"]
        bundle_started = time.perf_counter()
        careers_js = await self.http.get(module_url, headers={"Referer": str(response.url)})
        import_match = re.search(r"""["']\./(greenhouse\.[A-Za-z0-9_-]+\.js)["']""", careers_js.text)
        if import_match is None:
            raise ValueError("HackerRank CareersJobs module no longer names its Greenhouse module")
        greenhouse_url = module_url.rsplit("/", 1)[0] + "/" + import_match.group(1)
        greenhouse_js = await self.http.get(greenhouse_url, headers={"Referer": str(response.url)})
        bundle_seconds = max(0.0, time.perf_counter() - bundle_started)
        evidence = parse_ui_bundles(careers_js.text, greenhouse_js.text, module_url)
        evidence.update(greenhouse_module_url=greenhouse_url,
                        greenhouse_status=greenhouse_js.status_code,
                        careers_status=careers_js.status_code)
        entry.update(status=response.status_code, seconds=round(seconds, 6),
                     response_sha256=sha256(response.content).hexdigest(),
                     response_bytes=len(response.content),
                     bundle_evidence=evidence)
        return entry, bundle_seconds

    async def _listing(self):
        started = time.perf_counter()
        response = await self.http.get(LISTING_API, headers={"Referer": CAREERS_HOME})
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != LISTING_API:
            raise ValueError(f"HackerRank listing request redirected: {response.url}")
        parsed = parse_departments(response.json())
        parsed.update(status=response.status_code, seconds=round(seconds, 6),
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _board_crosscheck(self):
        started = time.perf_counter()
        response = await self.http.get(BOARD_JOBS_API, headers={"Referer": CAREERS_HOME})
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != BOARD_JOBS_API:
            raise ValueError(f"HackerRank board jobs request redirected: {response.url}")
        parsed = parse_board_jobs(response.json())
        parsed.update(status=response.status_code, seconds=round(seconds, 6),
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    def _listing_job(self, row, position):
        identity = row["id"]
        return self.job(
            {"listing_row": row, "listing_position": position,
             "detail_api_url": DETAIL_API.format(job_id=identity)},
            title=row["title"],
            url=DETAIL_PAGE.format(job_id=identity),
            job_id=identity,
            apply_url=APPLY_URL.format(job_id=identity),
            locations=[{"location": row["location"]["name"]}] if row["location"]["name"] else [],
            department=row["department"],
            workplace_type=workplace_label(row["location"]["name"]),
        )

    async def _detail(self, job):
        url = DETAIL_API.format(job_id=job.job_id)
        response = await self.http.get(
            url, headers={"Referer": DETAIL_PAGE.format(job_id=job.job_id), "Accept": "application/json"})
        if str(response.url) != url:
            raise ValueError(f"HackerRank job {job.job_id} detail request redirected: {response.url}")
        detail = parse_job_detail(response.json(), job.job_id)
        listing_row = job.raw["listing_row"]
        if detail["title"] != listing_row["title"]:
            self.result.errors.append(f"hackerrank_listing_detail_title_mismatch:{job.job_id}")
        if detail["location_name"] != listing_row["location"]["name"]:
            self.result.errors.append(f"hackerrank_listing_detail_location_mismatch:{job.job_id}")
        if [d["name"] for d in detail["departments"]] and detail["departments"][0]["name"] != listing_row["department"]:
            self.result.errors.append(f"hackerrank_listing_detail_department_mismatch:{job.job_id}")
        self.result.errors.extend(f"{job.job_id}:{issue}" for issue in detail["issues"])
        return self._make_job(detail, listing_row, job.raw.get("listing_position"),
                              response_sha256=sha256(response.content).hexdigest(),
                              response_bytes=len(response.content))

    def _make_job(self, detail, listing_row, position, *, response_sha256=None, response_bytes=None):
        identity = detail["id"]
        description_html = detail["content_html"]
        description_text = text_html(description_html)
        sections = description_sections(description_html)
        salary = compensation_salary(sections)
        links = description_links(description_html)
        attachments = [link for link in links if ATTACHMENT_SUFFIX.search(link["url"] or "")]
        department = (detail["departments"][0]["name"] if detail["departments"]
                      else listing_row["department"])
        team_metadata = next((item.get("value") for item in detail["metadata"]
                              if normalized_title(item.get("name")) == "Team Name" and item.get("value")), None)
        job = self.job(
            {
                "listing_row": listing_row,
                "listing_position": position,
                "detail": {
                    "source_field": "content",
                    "request_url": DETAIL_API.format(job_id=identity),
                    "response_sha256": response_sha256,
                    "response_bytes": response_bytes,
                    "absolute_url": detail["absolute_url"],
                    "internal_job_id": detail["internal_job_id"],
                    "departments": detail["departments"],
                    "offices": detail["offices"],
                    "metadata": detail["metadata"],
                    "team_name": team_metadata,
                    "data_compliance": detail["data_compliance"],
                    "ai_disclaimer": detail["ai_disclaimer"],
                    "include_ai_disclaimer": detail["include_ai_disclaimer"],
                    "ai_opt_out_request_url": detail["ai_opt_out_request_url"],
                    "first_published_raw": detail["first_published"],
                    "updated_at_raw": detail["updated_at"],
                    "application_deadline_raw": detail["application_deadline"],
                    "description_sha256": sha256(description_html.encode()).hexdigest(),
                    "source_content_escaped_sha256": sha256(detail["content"].encode()).hexdigest(),
                    "description_normalized_chars": len(normalized_body_text(description_html)),
                    "description_fnv1a64": fnv1a64(normalized_body_text(description_html)),
                    "description_compact_chars": len(compact_body(normalized_body_text(description_html))),
                    "description_compact_fnv1a64": fnv1a64(compact_body(normalized_body_text(description_html))),
                    "description_sections": sections,
                    "description_links": links,
                    "role_attachments": attachments,
                    "compensation_section_text": next(
                        (s["text"] for s in sections
                         if s["heading"] and "compensation" in s["heading"].lower()), None),
                    "application_route": {
                        "listing_link": f"/careers/job?id={identity}",
                        "company_detail_page": DETAIL_PAGE.format(job_id=identity),
                        "apply_iframe_src": APPLY_URL.format(job_id=identity),
                        "application_form_opened": False,
                    },
                },
            },
            title=detail["title"],
            url=DETAIL_PAGE.format(job_id=identity),
            job_id=identity,
            requisition_id=str(detail["requisition_id"]) if detail["requisition_id"] not in (None, "") else None,
            apply_url=APPLY_URL.format(job_id=identity),
            description=description_html,
            method=METHOD,
            description_url=DETAIL_API.format(job_id=identity),
            locations=[{"location": detail["location_name"]}] if detail["location_name"] else [],
            department=department,
            team=team_metadata,
            employment_type=None,
            workplace_type=workplace_label(detail["location_name"]),
            date_posted=detail["first_published"],
            date_posted_raw=detail["first_published"],
            date_updated=detail["updated_at"],
            valid_through=detail["application_deadline"],
            salary=salary,
            responsibilities=sections_matching(sections, RESPONSIBILITY_TOKENS),
            qualifications=sections_matching(sections, QUALIFICATION_TOKENS),
            employer=detail["company_name"],
            language=detail["language"],
        )
        job.description_text = description_text
        job.description_unavailable = not bool(description_text.strip())
        if attachments:
            self.result.errors.append(f"hackerrank_role_attachments_require_review:{identity}")
        return job

    async def run(self):
        started = time.perf_counter()
        try:
            entry, bundle_seconds = await self._official_entry()
            listing = await self._listing()
            crosscheck = await self._board_crosscheck()
        except Exception as exc:
            self.result.errors.append(f"HackerRank source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
            return self.result

        self.result.expected_total = len(listing["job_ids"])
        self.result.pages = 1
        self.result.metadata["official_entry"] = entry
        self.result.metadata["listing"] = {key: value for key, value in listing.items() if key != "departments"}
        self.result.metadata["department_groups"] = [
            {"department": department["name"], "count": department["job_count"],
             "job_ids": [row["id"] for row in department["jobs"]]}
            for department in listing["departments"]
        ]
        self.result.metadata["board_membership_crosscheck"] = crosscheck
        self.result.errors.extend(listing["issues"])
        self.result.errors.extend(crosscheck["issues"])
        if set(crosscheck["job_ids"]) != set(listing["job_ids"]):
            self.result.errors.append(
                "hackerrank_board_membership_crosscheck_mismatch:"
                f" only_listing={sorted(set(listing['job_ids']) - set(crosscheck['job_ids']))}"
                f" only_board={sorted(set(crosscheck['job_ids']) - set(listing['job_ids']))}")
        if crosscheck["total"] != len(listing["job_ids"]):
            self.result.errors.append("hackerrank_board_total_does_not_match_listing")

        rows = {row["id"]: row for department in listing["departments"] for row in department["jobs"]}
        order = listing["ui_order"]
        jobs = [self._listing_job(rows[identity], position) for position, identity in enumerate(order, 1)]
        if len(jobs) != self.result.expected_total:
            self.result.errors.append("hackerrank_retained_rows_do_not_match_company_listing_count")

        jobs = await self.hydrate(jobs, self._detail)
        self.result.jobs = jobs
        failed = [job for job in jobs if not job.description_verified]
        if failed:
            self.result.errors.append(
                "hackerrank_details_not_verified:" + ",".join(str(job.job_id) for job in failed))

        detail_pages, detail_page_seconds = {}, 0.0
        for label, identity in (("first", order[0]), ("last", order[-1])):
            url = DETAIL_PAGE.format(job_id=identity)
            page_started = time.perf_counter()
            try:
                response = await self.http.get(url, headers={"Referer": CAREERS_HOME})
                detail_pages[identity] = {"position_in_listing": label,
                                          **parse_detail_page(response.text, identity, str(response.url))}
            except Exception as exc:
                detail_pages[identity] = {"position_in_listing": label, "url": url,
                                          "error": f"{type(exc).__name__}: {exc}"}
                self.result.errors.append(f"hackerrank_detail_page_check_failed:{identity}:{type(exc).__name__}")
            finally:
                detail_page_seconds += max(0.0, time.perf_counter() - page_started)
        self.result.metadata["representative_detail_pages"] = detail_pages

        final = None
        try:
            final = await self._listing()
        except Exception as exc:
            self.result.errors.append(f"HackerRank final listing recheck failed: {type(exc).__name__}: {exc}")
        reconciliation = {
            "initial_ids": listing["job_ids"],
            "initial_ui_order": order,
            "final_ids": final["job_ids"] if final else None,
            "final_ui_order": final["ui_order"] if final else None,
            "initial_count": len(listing["job_ids"]),
            "final_count": len(final["job_ids"]) if final else None,
            "initial_final_set_matches": bool(final) and set(final["job_ids"]) == set(listing["job_ids"]),
            "initial_final_order_matches": bool(final) and final["ui_order"] == order,
            "board_crosscheck_matches": set(crosscheck["job_ids"]) == set(listing["job_ids"]),
            "board_crosscheck_total": crosscheck["total"],
            "retained_rows": len(jobs),
            "complete": False,
        }
        if final is not None:
            self.result.metadata["final_listing"] = {key: value for key, value in final.items()
                                                     if key != "departments"}
            self.result.errors.extend(final["issues"])
            if not reconciliation["initial_final_set_matches"]:
                self.result.errors.append(
                    "hackerrank_listing_membership_changed_during_collection:"
                    f" added={sorted(set(final['job_ids']) - set(listing['job_ids']))}"
                    f" removed={sorted(set(listing['job_ids']) - set(final['job_ids']))}")
            elif not reconciliation["initial_final_order_matches"]:
                self.result.errors.append("hackerrank_listing_order_changed_during_collection")
        reconciliation["complete"] = bool(
            reconciliation["initial_final_set_matches"]
            and reconciliation["initial_final_order_matches"]
            and reconciliation["board_crosscheck_matches"]
            and reconciliation["retained_rows"] == reconciliation["initial_count"]
            and not self.result.errors
        )
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.listing_complete = reconciliation["complete"]

        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_final_url": CAREERS_HOME,
            "listing_url": CAREERS_HOME + "#" + LISTING_SECTION,
            "listing_api_url": LISTING_API,
            "membership_authority": (
                "The exact company-scoped listing request made by HackerRank's own careers widget: "
                "boards-api.greenhouse.io/v1/boards/hackerrank/departments?render_as=list. Its 29 "
                "postings, grouped into the 6 non-empty departments, are the membership; the widget "
                "renders all of them with no pagination, search or filters."
            ),
            "request_shape": {
                "entry": {"method": "GET", "url": OFFICIAL_ENTRY, "redirect": "301 -> /careers/home"},
                "listing": {"method": "GET", "url": LISTING_API, "query": {"render_as": "list"},
                            "pagination": "none", "filters": "none", "authentication": "none",
                            "scope": "Greenhouse board 'hackerrank' only, as published by HackerRank's own page"},
                "detail": {"method": "GET", "url": BOARD_API_ROOT + "jobs/<id>",
                           "pagination": "none", "authentication": "none",
                           "scope": "exact ids exposed by the company listing"},
                "board_crosscheck": {"method": "GET", "url": BOARD_JOBS_API,
                                     "purpose": "same company board, meta.total membership cross-check"},
            },
            "ui_pagination": (
                "None. The rendered widget lists all 29 postings grouped by department; no "
                "next/previous, load-more, search or filter control exists and scrolling loads nothing new."
            ),
            "field_policy": {
                "job_id": "Greenhouse job id exposed by the company listing and used by /careers/job?id=",
                "requisition_id": "per-job requisition_id",
                "date_posted": "per-job first_published (explicit publication timestamp)",
                "date_updated": "per-job updated_at",
                "valid_through": "per-job application_deadline (null for every current posting)",
                "employer": "per-job company_name",
                "language": "per-job language",
                "employment_type": "not exposed by this source; stays null",
                "workplace_type": "work-mode word published as the location prefix (Hybrid/Onsite/Remote/In-office)",
                "salary": "only a numeric range published in a Compensation section",
                "locations": "location.name; offices[] retained in raw",
            },
            "description_source": (
                "Per-job content field requested by the company's own detail route "
                "(/careers/job?id=<id> fetches boards-api.greenhouse.io/v1/boards/hackerrank/jobs/<id>). "
                "HTML-escaped content is decoded to its body HTML; every section is retained."
            ),
            "detail_route_policy": (
                "job.url is the company detail route; apply_url is the exact iframe src of that page's "
                "Application tab. The application form is never opened or submitted."
            ),
            "detail_fetches": len(jobs),
            "representative_detail_page_checks": 2,
            "collection_wall_seconds": round(max(0.0, time.perf_counter() - started), 6),
        })
        self.result.timings["entry_and_bundle_seconds"] = round(bundle_seconds, 6)
        self.result.timings["listing_seconds"] = listing["seconds"]
        self.result.timings["board_crosscheck_seconds"] = crosscheck["seconds"]
        self.result.timings["representative_detail_page_seconds"] = round(detail_page_seconds, 6)
        self.result.timings["final_listing_seconds"] = final["seconds"] if final else 0.0
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - started)
        return self.result
