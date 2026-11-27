"""Signify (Philips Lighting)'s own Phenom careers search and native job pages.

The official company journey is ``www.careers.signify.com/global/en/search-results``
(Phenom CareerConnect).  Its server-rendered search page embeds the complete
``eagerLoadRefineSearch`` DDO for the current page (10 rows) and reports
``totalHits``.  The site's own pagination links use ``?from=<offset>&s=1`` and
the same widget also answers a ``refineSearch`` POST on ``/widgets``.  The
company sitemap publishes one canonical ``/global/en/job/<jobId>/<slug>`` URL per
listed posting.  Only those exact native job pages are read for the full body;
the Workday apply URLs stay application destinations and are never enumerated.
"""
from __future__ import annotations

import re
import time
from hashlib import sha256
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter
from ..structured import embedded_page_object

OFFICIAL_ENTRY = "https://www.careers.signify.com/global/en/search-results"
BASE_URL = "https://www.careers.signify.com/global/en/"
WIDGET_ENDPOINT = "https://www.careers.signify.com/widgets"
SITEMAP_URL = "https://www.careers.signify.com/global/en/sitemap.xml"
BASE_HOST = "www.careers.signify.com"
REFNUM = "SIGNGLOBAL"
PAGE_SIZE = 10
LISTING_TEMPLATE = OFFICIAL_ENTRY + "?from={offset}&s=1"
JOB_SEQUENCE = re.compile(r"^SIGNGLOBAL(\d+)ENGLOBALEXTERNAL$")
SITEMAP_JOB_URL = re.compile(
    r"^https://www\.careers\.signify\.com/global/en/job/(\d+)/([^/?#]+)$"
)
ATTACHMENT_SUFFIX = re.compile(r"\.(?:pdf|docx?|rtf|xlsx?|pptx?)(?:[?#]|$)", re.I)
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}

RESPONSIBILITY_TOKENS = (
    "responsibilit", "key areas", "what you will do", "what you'll do", "what you do",
    "your tasks", "main tasks", "duties", "the role", "more about the role",
)
QUALIFICATION_TOKENS = (
    "critical experiences", "qualification", "requirement", "more about you",
    "what you bring", "who you are", "experience and skills", "skills and experience",
    "your profile", "candidate profile", "about you",
)


def normalized_title(value: str | None) -> str:
    import unicodedata
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def normalized_body_text(value: str | None) -> str:
    """Approximate rendered innerText: block boundaries separate, inline nodes join.

    A plain ``get_text(" ")`` inserts spaces before inline punctuation
    (``<b>Advisor</b>.`` becomes ``Advisor .``), which the browser does not.
    """
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    chunks: list[str] = []

    def visit(node):
        if isinstance(node, NavigableString):
            chunks.append(str(node))
            return
        name = getattr(node, "name", None)
        if name in {"script", "style", "noscript"}:
            return
        block = name in BLOCK_TAGS
        if block and chunks and not chunks[-1].endswith((" ", "\n", "\t")):
            chunks.append("\n")
        for child in getattr(node, "children", ()):
            visit(child)
        if block and chunks and not chunks[-1].endswith((" ", "\n", "\t")):
            chunks.append("\n")

    visit(soup)
    return re.sub(r"\s+", " ", "".join(chunks)).strip()


def fnv1a64(value: str) -> str:
    digest = 14695981039346656037
    for char in value:
        digest = ((digest ^ ord(char)) * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def _page_config(html: str) -> dict:
    config = embedded_page_object(html, "var phApp = phApp || ")
    if (config.get("refNum") != REFNUM
            or config.get("widgetApiEndpoint") != WIDGET_ENDPOINT
            or config.get("baseUrl") != BASE_URL
            or config.get("siteType") != "external"):
        raise ValueError("Signify native careers configuration changed")
    return config


def _validate_listing_row(row: dict) -> dict:
    if not isinstance(row, dict):
        raise ValueError("Signify search row is not an object")
    job_id = row.get("jobId")
    sequence = row.get("jobSeqNo")
    match = JOB_SEQUENCE.fullmatch(sequence or "")
    if (not isinstance(job_id, str) or not job_id.isdigit() or not match
            or match.group(1) != job_id or not normalized_title(row.get("title"))):
        raise ValueError("Signify search row has a missing or inconsistent identity")
    if row.get("visibilityType") != "External" or row.get("siteType") != "external":
        raise ValueError("Signify search row is not a public external posting")
    apply_url = row.get("applyUrl")
    if not isinstance(apply_url, str) or urlsplit(apply_url).scheme != "https":
        raise ValueError("Signify search row has no public apply URL")
    return row


def parse_search_page(html: str, requested_offset: int) -> dict:
    """Read one exact server-rendered search-results page and its paging state."""
    config = _page_config(html)
    ddo = embedded_page_object(html, "phApp.ddo = ")
    value = ddo.get("eagerLoadRefineSearch")
    if not isinstance(value, dict) or value.get("status") != 200:
        raise ValueError("Signify search page has no successful eagerLoadRefineSearch")
    rows = value.get("data", {}).get("jobs") if isinstance(value.get("data"), dict) else None
    total = value.get("totalHits")
    hits = value.get("hits")
    if (not isinstance(rows, list) or not isinstance(total, int) or total < 0
            or not isinstance(hits, int) or hits != len(rows) or len(rows) > PAGE_SIZE):
        raise ValueError("Signify search page count/page size does not reconcile")
    session = embedded_page_object(html, "phApp.sessionParams = ")
    if str(session.get("from")) != str(requested_offset):
        raise ValueError(
            f"Signify search page state from={session.get('from')!r} does not match request {requested_offset}"
        )
    return {
        "offset": requested_offset,
        "hits": hits,
        "total_hits": total,
        "rows": [_validate_listing_row(row) for row in rows],
        "config": config,
        "page_name": session.get("pageName"),
    }


def parse_sitemap(xml: str) -> dict:
    """Return the company-published canonical job URL for every sitemap job ID."""
    locs = re.findall(r"<loc>([^<]+)</loc>", xml or "")
    if not locs:
        raise ValueError("Signify sitemap has no URL entries")
    urls, duplicates = {}, []
    non_job = 0
    for loc in locs:
        loc = loc.strip()
        if "/global/en/job/" not in loc:
            non_job += 1
            continue
        match = SITEMAP_JOB_URL.fullmatch(loc)
        if match is None:
            raise ValueError(f"Signify sitemap job URL has an unknown shape: {loc}")
        job_id = match.group(1)
        if job_id in urls:
            duplicates.append(job_id)
            continue
        urls[job_id] = loc
    if duplicates:
        raise ValueError(f"Signify sitemap repeats job IDs: {duplicates[:5]}")
    return {"urls": urls, "job_count": len(urls), "non_job_urls": non_job,
            "total_urls": len(locs)}


def parse_widget_search(value: dict) -> dict:
    """Validate the same widget's JSON refineSearch response (membership cross-check)."""
    body = value.get("refineSearch") if isinstance(value, dict) else None
    if not isinstance(body, dict) or body.get("status") != 200:
        raise ValueError("Signify widget refineSearch response is not a successful object")
    rows = body.get("data", {}).get("jobs") if isinstance(body.get("data"), dict) else None
    total = body.get("totalHits")
    hits = body.get("hits")
    if (not isinstance(rows, list) or not isinstance(total, int) or total < 0
            or not isinstance(hits, int) or hits != len(rows)):
        raise ValueError("Signify widget refineSearch count does not reconcile")
    return {"rows": [_validate_listing_row(row) for row in rows], "hits": hits,
            "total_hits": total}


def description_sections(html: str) -> list[dict]:
    """Keep each employer-authored heading (bold paragraph or tag) and its body."""
    soup = BeautifulSoup(f'<div data-signify-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-signify-body]")
    blocks = [node for node in root.contents if isinstance(node, Tag)] if root else []
    sections, current = [], None

    def bold_heading(block: Tag) -> str:
        if block.name in HEADING_TAGS:
            return normalized_title(block.get_text(" ", strip=True))
        if block.name not in {"p", "div"}:
            return ""
        text = normalized_title(block.get_text(" ", strip=True))
        if not text or len(text) > 140:
            return ""
        bold = block.find(["b", "strong"])
        emphasis = block.select_one(".emphasis-2")
        holder = bold or emphasis
        if holder is None:
            return ""
        holder_text = normalized_title(holder.get_text(" ", strip=True))
        return text if holder_text and holder_text == text else ""

    for block in blocks:
        heading = bold_heading(block)
        if heading:
            if current is not None:
                current["text"] = normalized_body_text(current["html"])
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        current["text"] = normalized_body_text(current["html"])
        sections.append(current)
    if not sections:
        sections = [{"heading": None, "html": html or "", "text": normalized_body_text(html)}]
    return sections


def _matching_sections(sections: list[dict], tokens: tuple[str, ...]) -> list[dict] | None:
    found = [section for section in sections
             if section.get("heading") and section.get("text")
             and any(token in section["heading"].casefold() for token in tokens)]
    return found or None


def parse_detail_page(html: str, listing_row: dict) -> dict:
    """Validate one exact native job page against its published listing row."""
    config = _page_config(html)
    ddo = embedded_page_object(html, "phApp.ddo = ")
    value = ddo.get("jobDetail")
    data = value.get("data", {}).get("job") if isinstance(value, dict) and isinstance(value.get("data"), dict) else None
    if not isinstance(value, dict) or value.get("status") != 200 or not isinstance(data, dict):
        raise ValueError("Signify native job page has no jobDetail object")
    job_id = str(listing_row["jobId"])
    if (str(data.get("jobId")) != job_id
            or data.get("jobSeqNo") != listing_row["jobSeqNo"]
            or str(data.get("reqId")) != job_id
            or data.get("refNum") != REFNUM):
        raise ValueError("Signify native job page identity does not match the listed posting")
    if normalized_title(data.get("title")) != normalized_title(listing_row.get("title")):
        raise ValueError(
            f"Signify title conflict listing={listing_row.get('title')!r} detail={data.get('title')!r}"
        )
    if (data.get("visibilityType") != "External" or data.get("siteType") != "external"
            or data.get("displayCareerSite") is False):
        raise ValueError("Signify native job page is not a public external posting")
    employer = normalized_title(data.get("companyName"))
    if "signify" not in employer.casefold():
        raise ValueError(f"Signify native job page employer is unexpected: {employer!r}")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Signify native job page description field is missing")
    description_text = normalized_body_text(description)
    if len(description_text) < 100:
        raise ValueError("Signify native job page description is empty or truncated")
    listing_apply = listing_row.get("applyUrl")
    if listing_apply and data.get("applyUrl") and data["applyUrl"] != listing_apply:
        raise ValueError("Signify native job page apply URL differs from the listed apply URL")
    listing_posted = listing_row.get("postedDate")
    if listing_posted and data.get("postedDate") and data["postedDate"] != listing_posted:
        raise ValueError("Signify native job page posted date differs from the listed date")
    sections = description_sections(description)
    return {
        "job": data,
        "sections": sections,
        "description_text": description_text,
        "description_chars": len(description_text),
        "description_fnv1a64": fnv1a64(description_text),
        "config": config,
        "html_sha256": sha256(html.encode()).hexdigest(),
    }


def _iso_date(value) -> str | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    candidate = value[:10]
    return candidate if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate) else None


def _salary_from_sections(sections: list[dict]) -> dict | None:
    """Only an explicit employer pay sentence is used; no range is invented."""
    for section in sections:
        heading = normalized_title(section.get("heading"))
        if re.match(r"^(?:pay\s+)?(?:rate|salary|compensation|hourly\s+pay)\b", heading, re.I):
            return {"source_text": heading, "source_field": "description_heading"}
    return None


def _attachment_links(description_html: str) -> list[dict]:
    soup = BeautifulSoup(description_html or "", "lxml")
    links = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if ATTACHMENT_SUFFIX.search(href):
            links.append({"url": href, "label": anchor.get_text(" ", strip=True) or None})
    return links


class PhilipsLightingSignify(Adapter):
    """Follow only Signify's published search pages, sitemap and native job pages."""

    VERIFICATION_REVISION = "signify.native-phenom-search-and-jobs.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self._invalidated_cache_ids: list[str] = []

    def add(self, job):
        row = job.raw.get("listing") or {}
        cached = self.options.cached_jobs.get(job.url)
        if cached:
            old = cached.raw.get("listing") or {}
            critical = ("jobId", "jobSeqNo", "title", "applyUrl", "postedDate", "location", "type")
            if any(old.get(field) != row.get(field) for field in critical):
                self.options.cached_jobs.pop(job.url, None)
                self._invalidated_cache_ids.append(str(cached.job_id or job.url))
            else:
                # Same exact native detail URL and unchanged listing fields.
                cached.record_identity = None
        super().add(job)

    async def _search_page(self, offset: int) -> dict:
        if not self.page_guard():
            raise ValueError("Signify listing page budget exhausted")
        started = time.perf_counter()
        url = OFFICIAL_ENTRY if offset == 0 else LISTING_TEMPLATE.format(offset=offset)
        response = await self.http.get(url)
        parsed = parse_search_page(response.text, offset)
        parsed.update(
            request_url=url,
            final_url=str(response.url),
            status=response.status_code,
            seconds=max(0.0, time.perf_counter() - started),
            response_bytes=len(response.content),
            response_sha256=sha256(response.content).hexdigest(),
        )
        parsed.pop("config", None)
        return parsed

    async def _listing_snapshot(self) -> dict:
        started = time.perf_counter()
        pages, rows, seen, issues = [], [], {}, []
        total = None
        offset = 0
        while True:
            page = await self._search_page(offset)
            if total is None:
                total = page["total_hits"]
            elif page["total_hits"] != total:
                issues.append(
                    f"signify_listing_total_changed:{total}->{page['total_hits']}@{offset}"
                )
            if page["hits"] != len(page["rows"]):
                issues.append(f"signify_listing_hit_count_mismatch@{offset}")
            page_ids = []
            for row in page["rows"]:
                identity = row["jobId"]
                page_ids.append(identity)
                if identity in seen:
                    issues.append(f"signify_listing_duplicate_job_id:{identity}")
                    continue
                seen[identity] = dict(row, listing_offset=offset)
                rows.append(seen[identity])
            pages.append({
                "offset": offset,
                "hits": page["hits"],
                "total_hits": page["total_hits"],
                "job_ids": page_ids,
                "request_url": page["request_url"],
                "final_url": page["final_url"],
                "request_seconds": page["seconds"],
                "response_bytes": page["response_bytes"],
                "response_sha256": page["response_sha256"],
            })
            if not page["rows"]:
                break
            offset += page["hits"]
            if total is not None and offset >= total:
                break
        if total is not None and len(rows) != total:
            issues.append(f"signify_listing_rows_{len(rows)}_do_not_match_total_{total}")
        return {
            "rows": rows,
            "job_ids": [row["jobId"] for row in rows],
            "total_hits": total,
            "pages": pages,
            "issues": list(dict.fromkeys(issues)),
            "seconds": max(0.0, time.perf_counter() - started),
        }

    async def _sitemap(self) -> dict:
        started = time.perf_counter()
        response = await self.http.get(SITEMAP_URL)
        if str(response.url) != SITEMAP_URL:
            raise ValueError("Signify sitemap redirected away from its published URL")
        parsed = parse_sitemap(response.text)
        parsed.update(url=SITEMAP_URL, status=response.status_code,
                      seconds=max(0.0, time.perf_counter() - started),
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    async def _widget_search(self) -> dict:
        started = time.perf_counter()
        body = {
            "lang": "en_global", "deviceType": "desktop", "country": "global",
            "pageName": "search-results", "ddoKey": "refineSearch", "from": 0,
            "size": 500, "global": True, "selected_fields": {},
            "all_fields": ["city", "state", "category", "type", "orgFunction", "country"],
            "clearAll": True, "keywords": "", "subsearch": "", "sortBy": "",
            "jobs": True, "counts": True,
        }
        response = await self.http.request(
            "POST", WIDGET_ENDPOINT, json=body,
            headers={"Referer": OFFICIAL_ENTRY, "Origin": "https://www.careers.signify.com"},
        )
        parsed = parse_widget_search(response.json())
        parsed.update(seconds=max(0.0, time.perf_counter() - started),
                      status=response.status_code,
                      response_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return parsed

    def _make_job(self, row: dict, detail_url: str):
        return self.job(
            {"listing": row},
            title=normalized_title(row.get("title")),
            url=detail_url,
            job_id=str(row["jobId"]),
            requisition_id=str(row.get("reqId") or row["jobId"]),
            apply_url=row.get("applyUrl"),
            locations=list(row.get("multi_location") or [row.get("location")]),
            department=row.get("category"),
            employment_type=row.get("type"),
            date_posted=_iso_date(row.get("postedDate")),
            date_posted_raw=row.get("postedDate"),
            employer=None,
            language=row.get("locale"),
        )

    async def detail(self, job):
        started = time.perf_counter()
        response = await self.http.get(job.url)
        parsed_url = urlsplit(str(response.url))
        match = SITEMAP_JOB_URL.fullmatch(str(response.url))
        if parsed_url.hostname != BASE_HOST or match is None or match.group(1) != str(job.job_id):
            raise ValueError("Signify posting redirected away from its exact native job URL")
        parsed = parse_detail_page(response.text, job.raw["listing"])
        data = parsed["job"]
        job.description_html = data["description"]
        job.description_text = parsed["description_text"]
        job.description_method = "signify.native_phapp.jobDetail.data.job.description"
        job.description_source_url = job.url
        job.title = normalized_title(data.get("title")) or job.title
        job.requisition_id = str(data.get("reqId") or job.requisition_id)
        job.apply_url = data.get("applyUrl") or job.apply_url
        multi = data.get("multi_location")
        if isinstance(multi, list) and multi:
            job.locations = [loc.get("location") for loc in multi if isinstance(loc, dict) and loc.get("location")]
        elif data.get("location"):
            job.locations = [data["location"]]
        job.department = data.get("category") or job.department
        job.employment_type = data.get("type") or job.employment_type
        job.employer = data.get("companyName")
        job.language = data.get("locale") or job.language
        job.date_posted = _iso_date(data.get("postedDate")) or job.date_posted
        job.date_posted_raw = data.get("postedDate") or job.date_posted_raw
        job.date_updated = data.get("jobUpdatedDate") or data.get("jobUpdated") or None
        structured = data.get("structureData") if isinstance(data.get("structureData"), dict) else {}
        job.salary = structured.get("baseSalary") or _salary_from_sections(parsed["sections"])
        job.qualifications = _matching_sections(parsed["sections"], QUALIFICATION_TOKENS)
        job.responsibilities = _matching_sections(parsed["sections"], RESPONSIBILITY_TOKENS)
        job.raw["detail"] = data
        job.raw["detail_source"] = {
            "url": str(response.url),
            "status": response.status_code,
            "method": "GET native /global/en/job/<jobId>/<slug> page; phApp.ddo.jobDetail.data.job",
            "response_sha256": sha256(response.content).hexdigest(),
            "response_bytes": len(response.content),
            "description_sha256": sha256(data["description"].encode()).hexdigest(),
            "description_normalized_chars": parsed["description_chars"],
            "description_fnv1a64": parsed["description_fnv1a64"],
            "description_sections": parsed["sections"],
            "role_attachments": _attachment_links(data["description"]),
            "salary_from_description_heading": _salary_from_sections(parsed["sections"]),
            "request_seconds": max(0.0, time.perf_counter() - started),
            "structure_data": structured,
            "source_visibility": {
                "visibilityType": data.get("visibilityType"),
                "siteType": data.get("siteType"),
                "displayCareerSite": data.get("displayCareerSite"),
                "jobVisibility": data.get("jobVisibility"),
            },
            "dates": {
                "postedDate": data.get("postedDate"),
                "postedDateTrack": data.get("postedDateTrack"),
                "dateCreated": data.get("dateCreated"),
                "jobUpdated": data.get("jobUpdated"),
                "jobUpdatedDate": data.get("jobUpdatedDate"),
                "jobDeletedOn": data.get("jobDeletedOn"),
                "jobReopenedOn": data.get("jobReopenedOn"),
                "posting_date_mapping": "postedDate",
                "creation_and_processing_timestamps_not_used_as_posting_dates": True,
            },
            "employment": {
                "workerType": data.get("workerType"),
                "applicantTypeRequired": data.get("applicantTypeRequired"),
                "type": data.get("type"),
                "actionType": data.get("actionType"),
                "region": data.get("region"),
                "ats": data.get("ats"),
                "source": data.get("source"),
            },
        }
        return job

    async def run(self):
        collection_started = time.perf_counter()
        if self.company.slug not in {"philips-lighting-signify", "signify"}:
            raise ValueError("This adapter is only for Signify (Philips Lighting)")
        if self.source.url != OFFICIAL_ENTRY or self.source.evidence_url != OFFICIAL_ENTRY:
            raise ValueError("Signify source must be its inspected official search-results entry")
        entry_started = time.perf_counter()
        try:
            entry = await self.http.get(OFFICIAL_ENTRY)
            if str(entry.url) != OFFICIAL_ENTRY:
                raise ValueError("Signify official search entry redirected away from its inspected URL")
            entry_config = _page_config(entry.text)
            entry_parsed = parse_search_page(entry.text, 0)
        except Exception as exc:
            self.result.errors.append(f"Signify source request failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = round(
                max(0.0, time.perf_counter() - collection_started), 6
            )
            return self.result
        entry_seconds = max(0.0, time.perf_counter() - entry_started)

        try:
            listing = await self._listing_snapshot()
        except Exception as exc:
            self.result.errors.append(f"Signify listing pagination failed: {type(exc).__name__}: {exc}")
            self.result.listing_complete = False
            self.result.timings["source_collection_wall_seconds"] = round(
                max(0.0, time.perf_counter() - collection_started), 6
            )
            return self.result
        if listing["total_hits"] != entry_parsed["total_hits"]:
            listing["issues"].append(
                f"signify_entry_total_{entry_parsed['total_hits']}_differs_from_pagination_total_{listing['total_hits']}"
            )
        self.result.expected_total = listing["total_hits"]
        self.result.metadata["initial_official_entry"] = {
            "url": str(entry.url),
            "status": entry.status_code,
            "response_sha256": sha256(entry.content).hexdigest(),
            "response_bytes": len(entry.content),
            "seconds": round(entry_seconds, 6),
            "refNum": entry_config.get("refNum"),
            "widgetApiEndpoint": entry_config.get("widgetApiEndpoint"),
            "baseUrl": entry_config.get("baseUrl"),
            "siteType": entry_config.get("siteType"),
            "embedded_total_hits": entry_parsed["total_hits"],
            "embedded_page_hits": entry_parsed["hits"],
            "page_size": PAGE_SIZE,
        }
        self.result.metadata["initial_listing"] = listing
        self.result.errors.extend(listing["issues"])

        try:
            sitemap = await self._sitemap()
        except Exception as exc:
            self.result.errors.append(f"Signify sitemap failed: {type(exc).__name__}: {exc}")
            sitemap = None
        try:
            widget = await self._widget_search()
        except Exception as exc:
            self.result.errors.append(f"Signify widget cross-check failed: {type(exc).__name__}: {exc}")
            widget = None

        if sitemap is not None:
            self.result.metadata["sitemap"] = sitemap
            listed_ids = set(listing["job_ids"])
            sitemap_ids = set(sitemap["urls"])
            if sitemap_ids != listed_ids:
                self.result.errors.append(
                    "signify_sitemap_listing_membership_mismatch:"
                    f"listed_only={sorted(listed_ids - sitemap_ids)[:10]}:"
                    f"sitemap_only={sorted(sitemap_ids - listed_ids)[:10]}"
                )
            if sitemap["job_count"] != listing["total_hits"]:
                self.result.errors.append("signify_sitemap_job_count_differs_from_listing_total")
        if widget is not None:
            widget_ids = [row["jobId"] for row in widget["rows"]]
            self.result.metadata["widget_cross_check"] = {
                key: value for key, value in widget.items() if key != "rows"
            }
            self.result.metadata["widget_cross_check"]["job_ids"] = widget_ids
            if widget["total_hits"] != listing["total_hits"] or set(widget_ids) != set(listing["job_ids"]):
                self.result.errors.append("signify_widget_membership_differs_from_search_pages")
            if len(widget_ids) != len(set(widget_ids)):
                self.result.errors.append("signify_widget_has_duplicate_job_ids")

        sitemap_urls = sitemap["urls"] if sitemap else {}
        detail_url = {}
        for row in listing["rows"]:
            job_id = row["jobId"]
            url = sitemap_urls.get(job_id)
            if not url:
                self.result.errors.append(f"signify_listed_job_absent_from_sitemap:{job_id}")
                url = f"{BASE_URL}job/{job_id}"
            detail_url[job_id] = url
            self.add(self._make_job(row, url))
        if len(self.result.jobs) != listing["total_hits"]:
            self.result.errors.append("signify_retained_rows_do_not_match_listing_total")
        self.result.pages = len(listing["pages"])
        if self.result.errors:
            self.result.listing_complete = False
        else:
            self.result.listing_complete = True

        detail_started = time.perf_counter()
        self.result.jobs = await self.hydrate(self.result.jobs, self.detail)
        detail_seconds = max(0.0, time.perf_counter() - detail_started)
        failed = [job for job in self.result.jobs if not job.description_verified]
        if failed:
            self.result.errors.extend(
                f"signify_detail_incomplete:{job.job_id}:{job.url}" for job in failed[:30]
            )

        final_started = time.perf_counter()
        try:
            final_listing = await self._listing_snapshot()
        except Exception as exc:
            self.result.errors.append(f"Signify final listing recheck failed: {type(exc).__name__}: {exc}")
            final_listing = None
        try:
            final_sitemap = await self._sitemap()
        except Exception as exc:
            self.result.errors.append(f"Signify final sitemap recheck failed: {type(exc).__name__}: {exc}")
            final_sitemap = None
        final_seconds = max(0.0, time.perf_counter() - final_started)

        reconciliation = {
            "initial_count": listing["total_hits"],
            "initial_job_ids": listing["job_ids"],
            "sitemap_job_ids": sorted(sitemap_urls) if sitemap else None,
            "final_count": final_listing["total_hits"] if final_listing else None,
            "final_job_ids": final_listing["job_ids"] if final_listing else None,
            "final_sitemap_job_ids": sorted(final_sitemap["urls"]) if final_sitemap else None,
            "initial_ids_equal_final": bool(final_listing)
            and set(listing["job_ids"]) == set(final_listing["job_ids"])
            and listing["total_hits"] == final_listing["total_hits"],
            "same_relevance_order": bool(final_listing)
            and listing["job_ids"] == final_listing["job_ids"],
            "initial_sitemap_equal_final_sitemap": bool(final_sitemap and sitemap)
            and sitemap["urls"] == final_sitemap["urls"],
            "collected_ids_equal_initial": (
                {job.job_id for job in self.result.jobs} == set(listing["job_ids"])
                and len(self.result.jobs) == listing["total_hits"]
            ),
        }
        if final_listing is None or final_sitemap is None:
            reconciliation["complete"] = False
            self.result.listing_complete = False
        else:
            if not reconciliation["initial_ids_equal_final"]:
                self.result.errors.append("signify_listing_membership_changed_during_collection")
            elif not reconciliation["same_relevance_order"]:
                self.result.warnings.append("signify_relevance_order_changed_between_listing_passes")
            if not reconciliation["initial_sitemap_equal_final_sitemap"]:
                self.result.errors.append("signify_sitemap_membership_changed_during_collection")
            reconciliation["complete"] = (
                reconciliation["initial_ids_equal_final"]
                and reconciliation["initial_sitemap_equal_final_sitemap"]
                and reconciliation["collected_ids_equal_initial"]
                and not self.result.errors
            )
            if not reconciliation["complete"]:
                self.result.listing_complete = False
        if final_listing is not None:
            self.result.metadata["final_listing"] = final_listing
        if final_sitemap is not None:
            self.result.metadata["final_sitemap"] = final_sitemap
        self.result.metadata["membership_reconciliation"] = reconciliation

        self.result.pages = len(listing["pages"])
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_ENTRY,
            "listing_authority": (
                "Signify's own search-results widget: server-rendered pages at "
                "?from=<offset>&s=1 (10 rows/page) and the same widget's refineSearch JSON "
                "response; the company sitemap is a membership cross-check and canonical "
                "detail URL source."
            ),
            "listing_membership_request": {
                "method": "GET",
                "path": "/global/en/search-results",
                "query_shape": {"from": "UI page offset, every 10 rows", "s": 1},
                "page_size": PAGE_SIZE,
                "filters_applied": False,
                "sort": "most relevant (site default)",
                "scope": "SIGNGLOBAL external company site only",
            },
            "widget_cross_check_request": {
                "method": "POST",
                "path": "/widgets",
                "body_shape": {
                    "ddoKey": "refineSearch", "from": 0, "size": 500, "global": True,
                    "clearAll": True, "keywords": "", "subsearch": "", "sortBy": "",
                    "pageName": "search-results", "lang": "en_global", "country": "global",
                },
            },
            "detail_method": (
                "GET each exact canonical /global/en/job/<jobId>/<slug> URL published by the "
                "company sitemap; full body is phApp.ddo.jobDetail.data.job.description."
            ),
            "date_policy": (
                "postedDate is the source posting date. dateCreated, jobDeletedOn, jobReopenedOn, "
                "metadata.runDate and request times are retained in raw but never used as posting dates."
            ),
            "apply_policy": (
                "Workday apply URLs are retained exactly as published and are never fetched or "
                "enumerated; job membership comes only from the Signify careers listing."
            ),
            "description_source": "phApp.ddo.jobDetail.data.job.description on each native job page",
            "reused_cache_invalidations": self._invalidated_cache_ids,
            "collection_wall_seconds": round(max(0.0, time.perf_counter() - collection_started), 6),
        })
        self.result.timings.update({
            "official_entry_seconds": round(entry_seconds, 6),
            "signify_initial_listing_seconds": round(listing["seconds"], 6),
            "signify_sitemap_seconds": round(sitemap["seconds"] if sitemap else 0.0, 6),
            "signify_widget_cross_check_seconds": round(widget["seconds"] if widget else 0.0, 6),
            "signify_detail_seconds": round(detail_seconds, 6),
            "signify_final_recheck_seconds": round(final_seconds, 6),
            "source_collection_wall_seconds": round(max(0.0, time.perf_counter() - collection_started), 6),
        })
        return self.result


Signify = PhilipsLightingSignify
