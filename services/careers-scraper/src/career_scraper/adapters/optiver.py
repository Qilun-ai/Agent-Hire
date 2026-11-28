"""Optiver's own Optimizely (Episerver) careers listing and its job detail pages.

The official careers URL redirects to the company's own ``/join-us/jobs/``
listing. That page server-renders 16 of 160 cards and hydrates
``Components.JobsFiltered`` with the exact pagination endpoint
``/en/api/v1/jobs`` (``from``/``size``), the unfiltered ``totalCount`` and the
filter groups. Every retained posting is one card from that exact endpoint and
its company-published detail page; the full description is the single
``Components.RichText`` body on that detail page.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from hashlib import sha256
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from .base import Adapter
from ..http import FetchError

OFFICIAL_ENTRY = "https://optiver.com/working-at-optiver/career-opportunities/"
LISTING_URL = "https://www.optiver.com/join-us/jobs/"
ORIGIN = "https://www.optiver.com"
JOBS_API = ORIGIN + "/en/api/v1/jobs"
API_ENDPOINT = "/en/api/v1/jobs"
PAGE_SIZE = 16
LIST_COMPONENT = "Components.JobsFiltered"
JOB_PATH = re.compile(r"^/join-us/jobs/[a-z0-9-]+/[a-z0-9.-]+/[a-z0-9-]+/$", re.I)
EMPLOYER = "Optiver"
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "ul",
}
RESPONSIBILITY_TOKENS = ("responsibilit", "what you'll do", "what you will do", "what you’ll do",
                         "the role", "your role", "day to day", "day-to-day")
QUALIFICATION_TOKENS = ("qualif", "requirement", "what you'll need", "what you will need",
                        "what you’ll need", "who you are", "about you", "skills",
                        "experience and skills")


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def rendered_text(html: str | None) -> str:
    """Browser-like ``innerText``: block boundaries become breaks, inline nodes join."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    parts: list[str] = []

    def walk(node):
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        block = node.name in BLOCK_TAGS
        if block:
            parts.append("\n")
        for child in node.children:
            walk(child)
        if block:
            parts.append("\n")

    walk(soup)
    value = "".join(parts).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def extract_react_props(html: str, component: str) -> list[dict]:
    """Read the exact server-side props of a hydrated React component."""
    found = []
    pattern = re.compile(r"React\.createElement\(" + re.escape(component) + r",")
    for match in pattern.finditer(html or ""):
        start = html.find("{", match.end())
        if start < 0:
            continue
        depth, end = 0, None
        for index in range(start, len(html)):
            char = html[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            raise ValueError(f"Unterminated props for {component}")
        found.append(json.loads(html[start:end]))
    return found


def parse_official_entry(html: str, response_url: str = LISTING_URL) -> dict:
    """Validate the company careers entry and the listing state it publishes."""
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc != "www.optiver.com"
            or parts.path.rstrip("/") != "/join-us/jobs"):
        raise ValueError("Optiver official careers entry redirected away from its own job listing")
    if str(parts.query):
        raise ValueError("Optiver job listing was not requested unfiltered")
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "Optiver" not in title:
        raise ValueError("Optiver official job listing page title changed")
    states = extract_react_props(html, LIST_COMPONENT)
    if len(states) != 1:
        raise ValueError("Optiver listing page no longer exposes one JobsFiltered state")
    state = states[0]
    if state.get("apiEndpoint") != API_ENDPOINT:
        raise ValueError("Optiver listing pagination endpoint changed")
    total = state.get("totalCount")
    if not isinstance(total, int) or total < 0:
        raise ValueError("Optiver listing totalCount is missing")
    items = state.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("Optiver listing page exposes no server-rendered job cards")
    filter_config = state.get("filterConfig") or {}
    groups = filter_config.get("filterGroups") or []
    return {
        "url": response_url,
        "page_title": title,
        "component": LIST_COMPONENT,
        "api_endpoint": state["apiEndpoint"],
        "total_count": total,
        "server_rendered_items": len(items),
        "filter_groups": [
            {"id": group.get("id"), "label": group.get("label"),
             "multi_select": group.get("multiSelect"),
             "options": [option.get("value") for option in group.get("options") or []]}
            for group in groups
        ],
        "search_enabled": bool(state.get("showSearch")),
        "load_more_labels": (state.get("labels") or {}).get("loadMore"),
        "listing_component_id": state.get("componentID"),
        "listing_rows": [normalized_row(item) for item in items],
    }


def normalized_row(item: dict) -> dict:
    """Keep every published list field of one exact posting card."""
    href = item.get("href") or ""
    if not isinstance(href, str) or not JOB_PATH.fullmatch(href):
        raise ValueError(f"Optiver listing card has an unexpected job path: {href!r}")
    for field in ("title", "location", "experience", "domain"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise ValueError(f"Optiver listing card is missing {field}: {href}")
    component_id = item.get("componentID")
    if not isinstance(component_id, int) or component_id <= 0:
        raise ValueError(f"Optiver listing card is missing its component id: {href}")
    row = dict(item)
    row["title"] = normalized_title(item["title"])
    row["job_path"] = href
    row["url"] = ORIGIN + href
    row["slug"] = urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1]
    return row


def parse_jobs_page(payload: dict, from_index: int) -> dict:
    """Validate one exact pagination response of the company listing endpoint."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Optiver jobs endpoint response is missing items")
    total = payload.get("totalCount")
    if not isinstance(total, int) or total < 0:
        raise ValueError("Optiver jobs endpoint response is missing totalCount")
    rows, issues = [], []
    for item in payload["items"]:
        try:
            rows.append(normalized_row(item))
        except ValueError as exc:
            issues.append(str(exc))
    return {
        "from": from_index,
        "size": PAGE_SIZE,
        "count": total,
        "items": len(payload["items"]),
        "rows": rows,
        "issues": issues,
    }


def _json_ld_job_posting(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select('script[type="application/ld+json"]'):
        text = (node.string or node.get_text() or "").strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                return candidate
    raise ValueError("Optiver job detail page has no JobPosting JSON-LD")


def description_sections(html: str) -> list[dict]:
    """Keep the employer's own bold-paragraph headings and the body below them."""
    soup = BeautifulSoup(f'<div data-optiver-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-optiver-body]")
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
        sections.append(current)
    for section in sections:
        section["text"] = rendered_text(section["html"])
    return sections


def parse_detail_page(html: str, expected_url: str) -> dict:
    """Read one exact company job page: identity, summary fields, apply route, body."""
    parts = urlsplit(expected_url)
    if parts.scheme != "https" or parts.netloc != "www.optiver.com" or not JOB_PATH.fullmatch(parts.path):
        raise ValueError("Optiver detail URL is not an exact company job path")
    if parts.query or parts.fragment:
        raise ValueError("Optiver detail URL carries an unexpected query or fragment")
    soup = BeautifulSoup(html, "lxml")
    ld = _json_ld_job_posting(html)
    if str(ld.get("url") or "") != expected_url:
        raise ValueError(f"Optiver JobPosting JSON-LD url mismatch: {ld.get('url')!r}")
    if (ld.get("hiringOrganization") or {}).get("name") != EMPLOYER:
        raise ValueError("Optiver JobPosting hiring organisation changed")

    intros = extract_react_props(html, "Components.ContentIntro")
    summaries = extract_react_props(html, "Components.SummaryCard")
    bodies = extract_react_props(html, "Components.RichText")
    if len(intros) != 1 or len(summaries) != 1:
        raise ValueError("Optiver job page no longer exposes one intro/summary component")
    if not bodies:
        raise ValueError("Optiver job page exposes no RichText description")

    intro_title = normalized_title(intros[0].get("title"))
    h1 = soup.select_one("h1")
    page_title = normalized_title(h1.get_text(" ", strip=True)) if h1 else ""
    ld_title = normalized_title(ld.get("title"))
    if not intro_title or intro_title != ld_title or (page_title and page_title != intro_title):
        raise ValueError("Optiver job page title fields disagree")

    metadata = {}
    for item in summaries[0].get("metadata") or []:
        label = normalized_title(item.get("label"))
        if label:
            metadata[label] = item.get("value")
    cta = summaries[0].get("cta") or {}
    apply_href = cta.get("href") or ""
    if apply_href != parts.path + "apply":
        raise ValueError(f"Optiver job page apply route changed: {apply_href!r}")

    body = max(bodies, key=lambda item: len(item.get("text") or ""))
    description_html = body.get("text") or ""
    description_text = rendered_text(description_html)
    if len(description_text) < 100:
        raise ValueError("Optiver job page description body is empty or truncated")
    location = (ld.get("jobLocation") or {}).get("name")
    return {
        "url": expected_url,
        "title": intro_title,
        "json_ld_title": ld_title,
        "page_title": page_title,
        "date_posted": ld.get("datePosted"),
        "date_posted_raw": {"json_ld": {"datePosted": ld.get("datePosted")}} if ld.get("datePosted") else None,
        "employer": EMPLOYER,
        "json_ld_location": location,
        "metadata": metadata,
        "apply_label": cta.get("label"),
        "apply_href": apply_href,
        "apply_url": ORIGIN + apply_href,
        "is_registration_closed": summaries[0].get("isRegistrationClosed"),
        "signup_deadline": summaries[0].get("signupDeadline") or None,
        "richtext_blocks": len(bodies),
        "richtext_lengths": [len(item.get("text") or "") for item in bodies],
        "description_html": description_html,
        "description_text": description_text,
        "description_sections": description_sections(description_html),
        "description_normalized_chars": len(description_text),
        "description_fnv1a64": fnv1a64(description_text),
        "description_links": [
            {"text": anchor.get_text(" ", strip=True), "url": anchor.get("href")}
            for anchor in BeautifulSoup(description_html, "lxml").select("a[href]")
        ],
    }


class Optiver(Adapter):
    """Collect exactly the postings published by Optiver's own job listing."""

    VERIFICATION_REVISION = "optiver.optimizely-jobs-api.1"

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY, headers={"Accept": "text/html,application/xhtml+xml"})
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != LISTING_URL:
            raise ValueError(f"Optiver careers entry final URL changed: {response.url}")
        entry = parse_official_entry(response.text, str(response.url))
        entry.update(
            request_url=OFFICIAL_ENTRY,
            final_url=str(response.url),
            redirects=[str(item.url) for item in response.history],
            status=response.status_code,
            seconds=round(seconds, 6),
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
        )
        return entry

    async def _listing(self):
        started = time.perf_counter()
        rows, pages, issues = [], [], []
        seen: set[str] = set()
        expected = None
        from_index = 0
        while True:
            if not self.page_guard():
                issues.append("optiver_listing_page_budget_exhausted")
                break
            request_started = time.perf_counter()
            response = await self.http.request(
                "GET", JOBS_API, params={"from": from_index, "size": PAGE_SIZE},
                headers={"Accept": "application/json", "Referer": LISTING_URL},
            )
            request_seconds = max(0.0, time.perf_counter() - request_started)
            parts = urlsplit(str(response.url))
            if parts.scheme != "https" or parts.netloc != "www.optiver.com" or parts.path != API_ENDPOINT:
                raise ValueError("Optiver jobs endpoint redirected outside the company listing route")
            page = parse_jobs_page(response.json(), from_index)
            if expected is None:
                expected = page["count"]
            elif page["count"] != expected:
                issues.append("optiver_listing_total_changed_during_pagination")
            issues.extend(page["issues"])
            for row in page["rows"]:
                if row["job_path"] in seen:
                    issues.append(f"optiver_duplicate_job_path:{row['job_path']}")
                    continue
                seen.add(row["job_path"])
                rows.append(row)
            pages.append({
                "from": from_index,
                "items": page["items"],
                "count": page["count"],
                "request_url": str(response.request.url),
                "final_url": str(response.url),
                "request_seconds": round(request_seconds, 6),
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
                "job_paths": [row["job_path"] for row in page["rows"]],
            })
            from_index += PAGE_SIZE
            if not page["rows"]:
                break
            if expected is not None and len(rows) >= expected:
                break
        if expected is not None and len(rows) != expected:
            issues.append("optiver_listing_total_does_not_match_retained_rows")
        return {
            "rows": rows,
            "count": expected,
            "pages": pages,
            "issues": list(dict.fromkeys(issues)),
            "request_seconds": max(0.0, time.perf_counter() - started),
        }

    async def _detail(self, job):
        started = time.perf_counter()
        try:
            response = await self.http.get(job.url, headers={"Accept": "text/html,application/xhtml+xml"})
        except FetchError as exc:
            if "HTTP 404" in str(exc):
                # The listing card is published but its company detail page is gone.
                job.raw.setdefault("detail", {})["http_status"] = 404
                job.raw["detail"]["failure"] = "listing_link_returns_http_404"
                raise ValueError(
                    f"Optiver listing link returns HTTP 404 and no detail page is published: {job.url}"
                ) from exc
            raise
        seconds = max(0.0, time.perf_counter() - started)
        if str(response.url) != job.url:
            raise ValueError(f"Optiver detail page redirected: {job.url} -> {response.url}")
        parsed = parse_detail_page(response.text, job.url)
        listing = job.raw["listing"]
        if normalized_title(parsed["title"]) != normalized_title(listing.get("title")):
            raise ValueError(f"Optiver detail title differs from its listing card: {job.url}")
        location = parsed["metadata"].get("Location")
        if location and normalized_title(location) != normalized_title(listing.get("location")):
            raise ValueError(f"Optiver detail location differs from its listing card: {job.url}")
        level = parsed["metadata"].get("Level")
        if level and normalized_title(level) != normalized_title(listing.get("experience")):
            raise ValueError(f"Optiver detail level differs from its listing card: {job.url}")
        department = parsed["metadata"].get("Department")
        if department and normalized_title(department) != normalized_title(listing.get("domain")):
            raise ValueError(f"Optiver detail department differs from its listing card: {job.url}")
        job.title = parsed["title"]
        job.apply_url = parsed["apply_url"]
        job.description_html = parsed["description_html"]
        job.description_text = parsed["description_text"]
        job.description_method = "optiver.job_detail.richtext"
        job.description_source_url = job.url
        job.date_posted = parsed["date_posted"]
        job.date_posted_raw = parsed["date_posted_raw"]
        job.locations = [location] if location else []
        job.department = department
        job.experience = level
        job.employer = parsed["employer"]
        job.language = "en"
        sections = parsed["description_sections"]
        job.responsibilities = [section for section in sections
                                if section["heading"] and _matches(section["heading"], RESPONSIBILITY_TOKENS)] or None
        job.qualifications = [section for section in sections
                              if section["heading"] and _matches(section["heading"], QUALIFICATION_TOKENS)] or None
        job.raw["detail"] = {
            "request_url": job.url,
            "final_url": str(response.url),
            "status": response.status_code,
            "request_seconds": round(seconds, 6),
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "json_ld": {"datePosted": parsed["date_posted"], "title": parsed["json_ld_title"],
                        "jobLocation": parsed["json_ld_location"], "employer": parsed["employer"]},
            "summary_metadata": parsed["metadata"],
            "apply_label": parsed["apply_label"],
            "apply_href": parsed["apply_href"],
            "is_registration_closed": parsed["is_registration_closed"],
            "signup_deadline": parsed["signup_deadline"],
            "richtext_blocks": parsed["richtext_blocks"],
            "richtext_lengths": parsed["richtext_lengths"],
            "description_normalized_chars": parsed["description_normalized_chars"],
            "description_fnv1a64": parsed["description_fnv1a64"],
            "description_sections": sections,
            "description_links": parsed["description_links"],
        }
        job.validate()
        return job

    def _make_job(self, row: dict):
        return self.job(
            {"listing": row},
            title=row["title"],
            url=row["url"],
            job_id=str(row["componentID"]),
            requisition_id=None,
            apply_url=row["url"] + "apply",
            locations=[row["location"]],
            department=row["domain"],
            experience=row["experience"],
            employment_type=None,
            workplace_type=None,
            date_posted=None,
            date_posted_raw=None,
            date_updated=None,
            valid_through=None,
            salary=None,
            employer=EMPLOYER,
            language="en",
        )

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": LISTING_URL,
            "listing_authority": "Optiver's own /join-us/jobs/ page and the exact JobsFiltered "
                                 "pagination endpoint it publishes.",
            "listing_membership_request": {
                "method": "GET",
                "path": API_ENDPOINT,
                "query_shape": {"from": "listing offset, step 16", "size": PAGE_SIZE},
                "filters_applied": False,
                "scope": "Optiver's own site only; no ATS board or aggregator is enumerated.",
            },
            "detail_method": "Each exact company job page's single Components.RichText body.",
            "request_scope": "Only www.optiver.com listing pages and the exact job paths it publishes.",
        })
        try:
            entry = await self._official_entry()
        except Exception as exc:
            self.result.errors.append(f"Optiver official entry failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result
        self.result.metadata["official_entry"] = entry

        try:
            listing = await self._listing()
        except Exception as exc:
            self.result.errors.append(f"Optiver listing failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
            return self.result

        self.result.expected_total = listing["count"]
        self.result.metadata["listing"] = {key: value for key, value in listing.items() if key != "rows"}
        self.result.errors.extend(listing["issues"])
        if entry["total_count"] != listing["count"]:
            self.result.errors.append("optiver_entry_total_does_not_match_listing_api")
        if entry["api_endpoint"] != API_ENDPOINT:
            self.result.errors.append("optiver_entry_pagination_endpoint_mismatch")

        jobs = []
        for row in listing["rows"]:
            try:
                jobs.append(self._make_job(row))
            except Exception as exc:
                self.result.errors.append(f"Optiver listing row failed: {row.get('job_path')}: {exc}")
        if len(jobs) != (listing["count"] or 0):
            self.result.errors.append("optiver_retained_rows_do_not_match_listing_count")

        if jobs:
            await self.hydrate(jobs, self._detail)
        jobs = [job for job in jobs if job is not None]

        final = None
        try:
            final_entry = await self._official_entry()
            final = await self._listing()
        except Exception as exc:
            self.result.errors.append(f"Optiver final listing recheck failed: {type(exc).__name__}: {exc}")
            final_entry = None
        if final is None or final_entry is None:
            reconciled = False
            self.result.metadata["membership_reconciliation"] = {"complete": False}
        else:
            initial_paths = [row["job_path"] for row in listing["rows"]]
            final_paths = [row["job_path"] for row in final["rows"]]
            initial_by_path = {row["job_path"]: row for row in listing["rows"]}
            final_by_path = {row["job_path"]: row for row in final["rows"]}
            changed = {
                path: sorted(key for key in set(initial_by_path[path]) | set(final_by_path[path])
                             if initial_by_path[path].get(key) != final_by_path[path].get(key))
                for path in initial_paths if path in final_by_path
                and initial_by_path[path] != final_by_path[path]
            }
            same_paths = initial_paths == final_paths
            counts_match = listing["count"] == final["count"] == final_entry["total_count"]
            reconciled = bool(same_paths and counts_match and not changed and not final["issues"])
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_listing"] = {key: value for key, value in final.items() if key != "rows"}
            self.result.metadata["membership_reconciliation"] = {
                "initial_paths": initial_paths,
                "final_paths": final_paths,
                "initial_count": listing["count"],
                "final_count": final["count"],
                "entry_total": final_entry["total_count"],
                "same_paths_and_order": same_paths,
                "all_card_fields_match": not changed,
                "changed_fields_by_path": changed,
                "new_paths": [path for path in final_paths if path not in initial_by_path],
                "removed_paths": [path for path in initial_paths if path not in final_by_path],
                "complete": reconciled,
            }
            if not same_paths or not counts_match:
                self.result.errors.append("optiver_listing_membership_changed_during_collection")
            if changed:
                self.result.errors.append("optiver_listing_fields_changed_during_collection")
            self.result.errors.extend(final["issues"])

        self.result.jobs = jobs
        self.result.metadata["job_ids"] = [job.job_id for job in jobs]
        self.result.metadata["listing_total_count"] = listing["count"]
        self.result.metadata["listing_pages_fetched"] = len(listing["pages"])
        self.result.metadata["current_native_membership_reconciled"] = reconciled
        self.result.metadata["detail_hydration_mode"] = (
            "One exact company job page per retained card; the full description is that page's "
            "single Components.RichText body.")
        self.result.listing_complete = bool(reconciled and jobs and len(jobs) == listing["count"])
        self.result.timings["source_collection_wall_seconds"] = max(0.0, time.perf_counter() - source_started)
        return self.result


def _matches(heading: str, tokens: tuple[str, ...]) -> bool:
    value = normalized_title(heading).lower().replace("’", "'")
    return any(token in value for token in tokens)
