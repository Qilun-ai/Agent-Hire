"""Mad Street Den's own careers page, its job-list/detail PHP endpoints, and exact apply route."""
from __future__ import annotations

import asyncio
import gzip
import json
from hashlib import sha256
from html import unescape
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from .base import Adapter
from ..http import FetchError


OFFICIAL_ENTRY = "https://www.madstreetden.com/careers/"
CAREERS_ORIGIN = "https://www.madstreetden.com"
JOB_LIST_API = CAREERS_ORIGIN + "/api/joblist.php"
JOB_DETAIL_API = CAREERS_ORIGIN + "/api/jobdetail.php"
APPLY_BASE = "https://msd.darwinbox.in/ms/candidate/careers/"
DARWINBOX_CAREERS = APPLY_BASE
DARWINBOX_OTHERS = APPLY_BASE + "others?apply=1"
CAREER_SCRIPT = CAREERS_ORIGIN + "/static/js/career.min.js"
MEMBERSHIP_FIELD = "post_on_carrers_page"
EMPLOYER = "Mad Street Den"
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,80}$")
SCRIPT_ENDPOINT_RE = re.compile(r'const\s+%s\s*=\s*["\']([^"\']+)["\']')

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "caption", "dd", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1",
    "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol",
    "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "ul",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
RESPONSIBILITY_TOKENS = ("responsibilit", "what you will do", "what you'll do", "duties", "the role")
QUALIFICATION_TOKENS = ("qualif", "requirement", "skills we need", "what you will need",
                        "what you'll need", "who you are", "required skills")


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def visible_text(html: str | None) -> str:
    """Block-aware visible text; inline spans stay joined like a browser's innerText."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    pieces: list[str] = []

    def walk(node):
        if isinstance(node, NavigableString):
            pieces.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        if node.name == "br":
            pieces.append("\n")
            return
        block = node.name in BLOCK_TAGS
        if block:
            pieces.append("\n")
        for child in node.children:
            walk(child)
        if block:
            pieces.append("\n")

    walk(soup)
    value = "".join(pieces)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return re.sub(r"\n{2,}", "\n", value).strip()


def unescape_description(value: str) -> str:
    """Decode the employer's escaped HTML wrapper without turning literal code into tags."""
    for _ in range(2):
        if re.search(
            r"&(?:amp;)*lt;(?:p|div|span|section|article|ul|ol|h[1-6]|br)(?:\s|&(?:amp;)*gt;)",
            value,
            re.I,
        ):
            value = unescape(value)
        else:
            break
    return value


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parts = urlsplit(response_url)
    if (parts.scheme != "https" or parts.netloc not in {"www.madstreetden.com", "madstreetden.com"}
            or parts.path.rstrip("/") != "/careers"):
        raise ValueError("Mad Street Den official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    title = normalized_title(soup.title.get_text(" ", strip=True) if soup.title else "")
    if "mad street den" not in title.lower() or "career" not in title.lower():
        raise ValueError("Mad Street Den official careers page identity changed")
    job_list = soup.select_one("#jobList")
    desc_modal = soup.select_one("#descModal")
    script = next(
        (node for node in soup.select("script[src]")
         if urlsplit(urljoin(response_url, node.get("src", ""))).path == "/static/js/career.min.js"),
        None,
    )
    if job_list is None or desc_modal is None or script is None:
        raise ValueError("Mad Street Den careers page no longer exposes its job list, modal, or career script")
    darwinbox = [
        urljoin(response_url, anchor.get("href", ""))
        for anchor in soup.select("a[href]")
        if "msd.darwinbox.in/ms/candidate/careers/" in anchor.get("href", "")
    ]
    if DARWINBOX_OTHERS not in darwinbox:
        raise ValueError("Mad Street Den careers page no longer links its Darwinbox application route")
    return {
        "url": response_url,
        "title": title,
        "job_list_selector": "#jobList",
        "desc_modal_selector": "#descModal",
        "career_script_url": urljoin(response_url, script.get("src", "")),
        "darwinbox_application_url": DARWINBOX_OTHERS,
        "darwinbox_links": list(dict.fromkeys(darwinbox)),
        "visible_heading": next(
            (normalized_title(node.get_text(" ", strip=True)) for node in soup.select("h1,h2,h3")
             if "current openings" in normalized_title(node.get_text(" ", strip=True)).lower()),
            "",
        ),
    }


def parse_career_script(js: str, response_url: str = CAREER_SCRIPT) -> dict:
    def endpoint(name: str) -> str:
        match = re.search(SCRIPT_ENDPOINT_RE.pattern % re.escape(name), js)
        if match is None:
            raise ValueError(f"Mad Street Den career script no longer defines {name}")
        return match.group(1)

    joblist = urljoin(CAREERS_ORIGIN + "/", endpoint("jobListEndPoint"))
    jobdetail = urljoin(CAREERS_ORIGIN + "/", endpoint("jobDetailsEndPoint"))
    apply_base = endpoint("jobApplyURL")
    if joblist != JOB_LIST_API or jobdetail != JOB_DETAIL_API:
        raise ValueError("Mad Street Den career script job endpoints changed")
    if apply_base.rstrip("/") != APPLY_BASE.rstrip("/"):
        raise ValueError("Mad Street Den career script apply base changed")
    if MEMBERSHIP_FIELD not in js or "?job_id=" not in js:
        raise ValueError("Mad Street Den career script membership/detail request shape changed")
    return {
        "url": response_url,
        "joblist_api": joblist,
        "jobdetail_api": jobdetail,
        "apply_base": apply_base,
        "membership_field": MEMBERSHIP_FIELD,
        "membership_filter": f"{MEMBERSHIP_FIELD} == 1",
        "detail_query": "?job_id=<job_id>",
        "request_method": "POST",
        "request_body": "empty (jQuery contentType application/json, data '')",
    }


def parse_joblist(value) -> dict:
    if not isinstance(value, list):
        raise ValueError("Mad Street Den job-list response is not a JSON array")
    rows, seen, issues = [], set(), []
    unpublished_ids: list[str] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            issues.append(f"madstreetden_joblist_row_not_object:{index}")
            continue
        identity = str(row.get("job_id") or "").strip()
        if not JOB_ID_RE.fullmatch(identity):
            issues.append(f"madstreetden_joblist_invalid_job_id:{index}")
            continue
        if identity in seen:
            issues.append(f"madstreetden_joblist_duplicate_id:{identity}")
            continue
        seen.add(identity)
        title = normalized_title(row.get("title"))
        if not title:
            issues.append(f"madstreetden_joblist_missing_title:{identity}")
            continue
        published = row.get(MEMBERSHIP_FIELD)
        if published not in (1, "1", True):
            unpublished_ids.append(identity)
            continue
        group = normalized_title(row.get("group_company"))
        if group and group.casefold() != EMPLOYER.casefold():
            issues.append(f"madstreetden_joblist_wrong_group_company:{identity}:{group}")
            continue
        for field in ("job_code", "department", "location", "location_city", "location_country", "employee_type"):
            if row.get(field) in (None, ""):
                issues.append(f"madstreetden_joblist_missing_{field}:{identity}")
        apply_url = APPLY_BASE + identity + "?apply=1"
        rows.append({
            "id": identity,
            "title": title,
            "apply_url": apply_url,
            "detail_url": JOB_DETAIL_API + "?job_id=" + identity,
            "row": dict(row),
        })
    return {
        "rows": rows,
        "job_ids": [row["id"] for row in rows],
        "count": len(rows),
        "all_count": len(value),
        "unpublished_ids": unpublished_ids,
        "departments": sorted({normalized_title(row["row"].get("department")) for row in rows if row["row"].get("department")}),
        "locations": sorted({normalized_title(row["row"].get("location")) for row in rows if row["row"].get("location")}),
        "issues": list(dict.fromkeys(issues)),
    }


def _iter_blocks(node):
    for child in node.children:
        if not isinstance(child, Tag):
            continue
        if child.name in {"script", "style", "noscript"}:
            continue
        if child.name in {"ul", "ol"}:
            for item in child.find_all("li", recursive=False):
                text = normalized_title(item.get_text(" ", strip=True))
                if text:
                    yield "li", text
            continue
        if child.name in HEADING_TAGS or child.name in {"p", "li"}:
            text = normalized_title(child.get_text(" ", strip=True))
            if text:
                yield child.name, text
            continue
        yield from _iter_blocks(child)


def description_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(f'<div data-madstreetden-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-madstreetden-body]")
    sections: list[dict] = []
    current = {"heading": None, "text": ""}
    if root is None:
        return [current]
    for tag, text in _iter_blocks(root):
        heading = None
        if tag in HEADING_TAGS:
            heading = text
        elif len(text) <= 120 and text.endswith(":"):
            heading = text
        if heading:
            if current["text"]:
                sections.append(current)
            current = {"heading": heading, "text": ""}
        else:
            current["text"] = (current["text"] + "\n" + text).strip()
    if current["text"]:
        sections.append(current)
    return sections or [{"heading": None, "text": ""}]


def _sections_matching(sections: list[dict], tokens: tuple[str, ...]) -> list[dict] | None:
    matched = [
        section for section in sections
        if section.get("heading") and any(token in section["heading"].lower() for token in tokens)
    ]
    return matched or None


def parse_job_detail(value, expected_row: dict, response_url: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Mad Street Den job-detail response is not a JSON object")
    title = normalized_title(value.get("title"))
    if not title:
        raise ValueError("Mad Street Den job-detail response has no title")
    if title != expected_row["title"]:
        raise ValueError(f"Mad Street Den job-detail title changed: {title!r} != {expected_row['title']!r}")
    company = normalized_title(value.get("company"))
    if company and company.casefold() != EMPLOYER.casefold():
        raise ValueError(f"Mad Street Den job-detail company changed: {company!r}")
    description = value.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Mad Street Den job-detail response has an empty description")
    html = unescape_description(description)
    text = visible_text(html)
    if len(text) < 100:
        raise ValueError("Mad Street Den job-detail description is empty or truncated")
    sections = description_sections(html)
    salary = None
    if any(value.get(field) not in (None, "", []) for field in ("salary_min", "salary_max", "salary_currency")):
        salary = {
            "min": value.get("salary_min") or None,
            "max": value.get("salary_max") or None,
            "currency": value.get("salary_currency") or None,
        }
    dates = {
        "job_created_timestamp": value.get("job_created_timestamp"),
        "job_updated_timestamp": value.get("job_updated_timestamp"),
        "posting_date_mapping": None,
        "deadline_mapping": None,
    }
    return {
        "url": response_url,
        "title": title,
        "company": company or EMPLOYER,
        "description_html": html,
        "description_text": text,
        "description_normalized_chars": len(normalized_text(text)),
        "description_fnv1a64": fnv1a64(normalized_text(text)),
        "description_sha256": sha256(html.encode()).hexdigest(),
        "description_sections": sections,
        "qualifications": _sections_matching(sections, QUALIFICATION_TOKENS),
        "responsibilities": _sections_matching(sections, RESPONSIBILITY_TOKENS),
        "salary": salary,
        "experience": value.get("experience") or None,
        "date_posted_raw": dates,
        "raw": dict(value),
    }


class MadStreetDen(Adapter):
    """Read only Mad Street Den's own careers page and its own job-list/detail endpoints."""

    VERIFICATION_REVISION = "madstreetden.official-joblist-detail.1"

    def _request_headers(self) -> dict:
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": CAREERS_ORIGIN,
            "Referer": OFFICIAL_ENTRY,
        }

    def _saved_response_evidence(self, url: str) -> dict | None:
        output = getattr(self.http, "output", None)
        if output is None:
            return None
        requests_path = output / "requests.jsonl"
        if not requests_path.is_file():
            return None
        records = []
        for line in requests_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("url") == url:
                records.append(record)
        if not records:
            return None
        record = records[-1]
        compressed = output / "responses" / f"{record.get('sha256')}.gz"
        body = None
        if compressed.is_file():
            body = gzip.decompress(compressed.read_bytes()).decode("utf-8", "replace")
        return {
            "status": record.get("status"),
            "sha256": record.get("sha256"),
            "content_type": record.get("content_type"),
            "body": body,
        }

    async def _post_json(self, url: str, phase: str):
        started = time.perf_counter()
        try:
            response = await self.http.request("POST", url, content=b"", headers=self._request_headers())
        except FetchError as exc:
            seconds = max(0.0, time.perf_counter() - started)
            evidence = self._saved_response_evidence(url)
            failure = {
                "phase": phase,
                "url": url,
                "method": "POST",
                "reason": str(exc),
                "seconds": round(seconds, 6),
                "evidence": evidence,
            }
            self.result.metadata.setdefault("source_failures", []).append(failure)
            self.result.errors.append(f"Mad Street Den {phase} request failed: {exc}")
            return None, seconds
        seconds = max(0.0, time.perf_counter() - started)
        if response.status_code != 200:
            failure = {
                "phase": phase,
                "url": url,
                "method": "POST",
                "reason": f"HTTP {response.status_code}",
                "seconds": round(seconds, 6),
                "evidence": {"status": response.status_code, "body": response.text},
            }
            self.result.metadata.setdefault("source_failures", []).append(failure)
            self.result.errors.append(f"Mad Street Den {phase} request returned HTTP {response.status_code}")
            return None, seconds
        try:
            value = response.json()
        except ValueError as exc:
            failure = {
                "phase": phase,
                "url": url,
                "method": "POST",
                "reason": f"invalid JSON: {exc}",
                "seconds": round(seconds, 6),
                "evidence": {"status": response.status_code, "body": response.text[:2000]},
            }
            self.result.metadata.setdefault("source_failures", []).append(failure)
            self.result.errors.append(f"Mad Street Den {phase} response was not JSON")
            return None, seconds
        return {
            "value": value,
            "status": response.status_code,
            "url": str(response.url),
            "request_url": url,
            "response_bytes": len(response.content),
            "response_sha256": sha256(response.content).hexdigest(),
            "seconds": round(seconds, 6),
        }, seconds

    async def _official_entry(self):
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_official_entry(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=round(seconds, 6),
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed, seconds

    async def _career_script(self):
        started = time.perf_counter()
        response = await self.http.get(CAREER_SCRIPT)
        seconds = max(0.0, time.perf_counter() - started)
        parsed = parse_career_script(response.text, str(response.url))
        parsed.update(
            status=response.status_code,
            seconds=round(seconds, 6),
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
        )
        return parsed, seconds

    async def _listing(self, phase: str):
        if not self.page_guard():
            return None, 0.0
        payload, seconds = await self._post_json(JOB_LIST_API, phase)
        if payload is None:
            return None, seconds
        try:
            parsed = parse_joblist(payload["value"])
        except ValueError as exc:
            self.result.errors.append(f"Mad Street Den {phase} job-list schema changed: {exc}")
            self.result.metadata.setdefault("source_failures", []).append({
                "phase": phase,
                "url": JOB_LIST_API,
                "method": "POST",
                "reason": str(exc),
                "seconds": round(seconds, 6),
                "evidence": {"status": payload["status"], "body": json.dumps(payload["value"])[:2000]},
            })
            return None, seconds
        parsed.update(
            status=payload["status"],
            url=payload["url"],
            request_url=payload["request_url"],
            response_bytes=payload["response_bytes"],
            response_sha256=payload["response_sha256"],
            seconds=round(seconds, 6),
        )
        return parsed, seconds

    async def _detail(self, row: dict):
        payload, seconds = await self._post_json(row["detail_url"], f"detail:{row['id']}")
        if payload is None:
            self.result.metadata.setdefault("failed_details", []).append({
                "id": row["id"],
                "url": row["detail_url"],
                "reason": "detail request failed",
            })
            return None, seconds
        try:
            parsed = parse_job_detail(payload["value"], row, payload["url"])
        except ValueError as exc:
            self.result.errors.append(f"Mad Street Den detail {row['id']} failed: {exc}")
            self.result.metadata.setdefault("failed_details", []).append({
                "id": row["id"],
                "url": row["detail_url"],
                "reason": str(exc),
            })
            return None, seconds
        parsed.update(
            status=payload["status"],
            request_url=payload["request_url"],
            response_bytes=payload["response_bytes"],
            response_sha256=payload["response_sha256"],
            seconds=round(seconds, 6),
        )
        return parsed, seconds

    def _make_job(self, row: dict, detail: dict):
        listing = row["row"]
        locations = []
        if listing.get("location"):
            locations = [normalized_title(listing["location"])]
        elif listing.get("location_city") or listing.get("location_country"):
            locations = [", ".join(
                part for part in (normalized_title(listing.get("location_city")),
                                  normalized_title(listing.get("location_country"))) if part
            )]
        job = self.job(
            {
                "listing": listing,
                "detail": detail["raw"],
                "detail_url": detail["url"],
                "career_script_url": CAREER_SCRIPT,
                "description_sections": detail["description_sections"],
                "description_sha256": detail["description_sha256"],
                "description_normalized_chars": detail["description_normalized_chars"],
                "description_fnv1a64": detail["description_fnv1a64"],
                "application_questions_excluded_from_description": True,
                "dates": detail["date_posted_raw"],
            },
            title=detail["title"],
            url=row["apply_url"],
            job_id=row["id"],
            requisition_id=listing.get("job_code"),
            apply_url=row["apply_url"],
            description=detail["description_html"],
            method="madstreetden.official.jobdetail.description",
            description_url=detail["url"],
            locations=locations,
            department=listing.get("department"),
            employment_type=listing.get("employee_type"),
            employer=listing.get("group_company") or EMPLOYER,
            experience=detail["experience"],
            salary=detail["salary"],
            qualifications=detail["qualifications"],
            responsibilities=detail["responsibilities"],
            date_posted=None,
            date_updated=None,
            valid_through=None,
            date_posted_raw=detail["date_posted_raw"],
            language="en",
        )
        job.description_text = detail["description_text"]
        job.company_scope_verified = self.company.scope == "company"
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_ENTRY,
            "job_list_api": JOB_LIST_API,
            "job_detail_api": JOB_DETAIL_API,
            "apply_base": APPLY_BASE,
            "listing_authority": "Mad Street Den's own /careers/ page and its own /api/joblist.php membership request.",
            "membership_filter": f"{MEMBERSHIP_FIELD} == 1",
            "request_shape": {
                "method": "POST",
                "body": "empty (observed jQuery contentType application/json, data '')",
                "headers": self._request_headers(),
            },
            "detail_method": "The company page's own POST /api/jobdetail.php?job_id=<id> JSON description field.",
            "company_scope": "Only rows whose group_company is Mad Street Den; no parent M2P board, no guessed ATS slug.",
        })
        entry_seconds = script_seconds = initial_seconds = detail_seconds = final_seconds = 0.0
        try:
            entry, entry_seconds = await self._official_entry()
            script, script_seconds = await self._career_script()
        except Exception as exc:
            self.result.errors.append(f"Mad Street Den official entry/script failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_seconds"] = round(max(0.0, time.perf_counter() - source_started), 6)
            self.result.timings["total_adapter_seconds"] = round(max(0.0, time.perf_counter() - source_started), 6)
            return self.result
        self.result.metadata["official_entry"] = entry
        self.result.metadata["career_script"] = script
        initial, initial_seconds = await self._listing("initial")
        if initial is None:
            self.result.expected_total = None
            self.result.listing_complete = False
            self.result.timings["source_collection_seconds"] = round(
                entry_seconds + script_seconds + initial_seconds, 6
            )
            self.result.timings["official_entry_seconds"] = round(entry_seconds, 6)
            self.result.timings["career_script_seconds"] = round(script_seconds, 6)
            self.result.timings["initial_listing_seconds"] = round(initial_seconds, 6)
            self.result.timings["detail_phase_seconds"] = 0.0
            self.result.timings["final_recheck_seconds"] = 0.0
            self.result.timings["total_adapter_seconds"] = round(max(0.0, time.perf_counter() - source_started), 6)
            return self.result

        self.result.expected_total = initial["count"]
        self.result.metadata["initial_listing"] = {key: value for key, value in initial.items() if key != "rows"}
        self.result.metadata["initial_job_ids"] = initial["job_ids"]
        if initial["issues"]:
            self.result.errors.extend(initial["issues"])
        if not initial["rows"]:
            self.result.errors.append("madstreetden_official_listing_returned_no_published_rows")
            self.result.listing_complete = False
            self.result.timings["source_collection_seconds"] = round(
                entry_seconds + script_seconds + initial_seconds, 6
            )
            self.result.timings["official_entry_seconds"] = round(entry_seconds, 6)
            self.result.timings["career_script_seconds"] = round(script_seconds, 6)
            self.result.timings["initial_listing_seconds"] = round(initial_seconds, 6)
            self.result.timings["detail_phase_seconds"] = 0.0
            self.result.timings["final_recheck_seconds"] = 0.0
            self.result.timings["total_adapter_seconds"] = round(max(0.0, time.perf_counter() - source_started), 6)
            return self.result

        detail_started = time.perf_counter()
        sem = asyncio.Semaphore(max(1, self.options.detail_concurrency))
        async def one(row):
            async with sem:
                return await self._detail(row)
        details = await asyncio.gather(*(one(row) for row in initial["rows"]))
        detail_seconds = max(0.0, time.perf_counter() - detail_started)

        for row, (detail, _seconds) in zip(initial["rows"], details):
            if detail is None:
                continue
            self.add(self._make_job(row, detail))

        final, final_seconds = await self._listing("final")
        reconciliation = {
            "initial_ids": initial["job_ids"],
            "final_ids": final["job_ids"] if final else None,
            "initial_count": initial["count"],
            "final_count": final["count"] if final else None,
            "retained_jobs": len(self.result.jobs),
            "complete": False,
        }
        if final is None:
            self.result.errors.append("madstreetden_final_listing_recheck_failed")
        else:
            self.result.metadata["final_listing"] = {key: value for key, value in final.items() if key != "rows"}
            if final["issues"]:
                self.result.errors.extend(final["issues"])
            if final["job_ids"] != initial["job_ids"]:
                self.result.errors.append("madstreetden_listing_membership_changed_during_collection")
            initial_by_id = {row["id"]: row["row"] for row in initial["rows"]}
            final_by_id = {row["id"]: row["row"] for row in final["rows"]}
            if any(initial_by_id.get(identity) != final_by_id.get(identity) for identity in initial["job_ids"]):
                self.result.errors.append("madstreetden_listing_fields_changed_during_collection")
            reconciliation["complete"] = (
                final["job_ids"] == initial["job_ids"]
                and len(self.result.jobs) == initial["count"]
                and all(job.description_verified for job in self.result.jobs)
            )
        self.result.metadata["membership_reconciliation"] = reconciliation
        self.result.listing_complete = bool(reconciliation["complete"] and not self.result.errors)
        self.result.timings.update({
            "official_entry_seconds": round(entry_seconds, 6),
            "career_script_seconds": round(script_seconds, 6),
            "initial_listing_seconds": round(initial_seconds, 6),
            "detail_phase_seconds": round(detail_seconds, 6),
            "final_recheck_seconds": round(final_seconds, 6),
            "source_collection_seconds": round(
                entry_seconds + script_seconds + initial_seconds + final_seconds, 6
            ),
            "total_adapter_seconds": round(max(0.0, time.perf_counter() - source_started), 6),
        })
        return self.result
