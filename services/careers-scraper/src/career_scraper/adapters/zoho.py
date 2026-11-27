"""Zoho's official careers page and the Zoho Recruit endpoints it publishes.

The page at https://www.zoho.com/careers/ renders its vacancies in
``#rec_job_listing_div`` from nine public Zoho Recruit ``Job_Openings``
endpoints selected by its own region dropdown.  The six regions partition the
global published membership, so the union of the nine endpoint responses is
the membership authority.  Each listed row links to an exact detail page on
the same career-site host; the matching public detail record supplies the full
``Job_Description`` (the listing's own description is a 153-character snippet).
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from hashlib import sha256
import re
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .base import Adapter


OFFICIAL_ENTRY = "https://www.zoho.com/careers/"
OFFICIAL_HOST = "www.zoho.com"
LISTING_PATH = "/recruit/v2/public/Job_Openings"
LISTING_QUERY = (
    "?pagename=Careers&source=CareerSite"
    "&extra_fields=%5B%22Remote_Job%22%2C%22Job_Description%22%5D"
)
DETAIL_QUERY = "?pagename=Careers&source=CareerSite"
WIDGET_SCRIPT_PATH = re.compile(r"/sites/zweb/js/translation/zoho_general_pages/\d+\.js$")
JOB_PAGE_PATH = re.compile(r"^/jobs/Careers/(\d+)(?:/[^/]*)?$")
DETAIL_RECORD_PATH = re.compile(r"^/recruit/v2/public/Job_Openings/(\d+)$")

# Region order and hosts exactly as published by the page widget script.  A
# change here is treated as a scope change and stops the collection until a
# worker re-inspects the site; it is never silently extended to a new host.
EXPECTED_REGION_ENDPOINTS = OrderedDict([
    ("Middle East and Africa", ["zohomea.zohorecruit.in"]),
    ("Europe", ["eu.zohorecruit.eu"]),
    ("North America", ["us-careers.zohorecruit.in", "mx-careers.zohorecruit.in"]),
    ("Australia and New Zealand", ["zoho-au.zohorecruit.in"]),
    ("South America", ["zoho-br.zohorecruit.in", "zoho-co.zohorecruit.in"]),
    ("APAC", ["zohoapac.zohorecruit.in", "careers.zohocorp.com"]),
])

# Organisation label shown in each career site's job header / page title.
# These are public labels, not guessed legal entities; the raw record keeps the
# portal host and the evidence is in companies/zoho/source/browser/.
PORTAL_ORGANISATIONS = {
    "zohomea.zohorecruit.in": "Zoho MEA",
    "eu.zohorecruit.eu": "Zoho Europe",
    "us-careers.zohorecruit.in": "Zoho USA",
    "mx-careers.zohorecruit.in": "Zohocorp Mexico",
    "zoho-au.zohorecruit.in": "Zoho AU",
    "zoho-br.zohorecruit.in": "ZOHO CORPORATION BRASIL TECNOLOGIA LTDA",
    "zoho-co.zohorecruit.in": "Zoho Corporation Colombia S.A.S",
    "zohoapac.zohorecruit.in": "Zoho Corporation Pte Ltd.",
    "careers.zohocorp.com": "Zoho Corporation",
}

RESPONSIBILITY_TOKENS = (
    "responsibilit", "what you'll do", "what you’ll do", "what you will do",
    "key duties", "duties",
)
QUALIFICATION_TOKENS = (
    "requirement", "qualification", "skills", "experience", "what you'll need",
    "what you’ll need", "what we're looking for", "what we’re looking for",
    "who you are", "nice to have", "preferred",
)
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "div", "dl", "dt", "dd", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).replace("\xa0", " ").strip()
    return value or None


def normalized_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def rendered_body_text(html: str | None) -> str:
    """Read text like a browser: no space at inline tag boundaries, breaks at blocks."""
    soup = BeautifulSoup(f'<div data-zoho-text="1">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-zoho-text]")
    if root is None:
        return ""
    parts: list[str] = []

    def walk(node):
        for child in node.children:
            if isinstance(child, NavigableString):
                parts.append(str(child))
            elif isinstance(child, Tag):
                if child.name in ("script", "style", "noscript"):
                    continue
                if child.name == "br":
                    parts.append("\n")
                elif child.name in BLOCK_TAGS:
                    parts.append("\n")
                    walk(child)
                    parts.append("\n")
                else:
                    walk(child)

    walk(root)
    value = "".join(parts).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def normalized_body_text(html: str | None) -> str:
    return re.sub(r"\s+", " ", rendered_body_text(html)).strip()


def query_params(value: str) -> dict:
    """Parse a query with or without its leading '?' exactly as the browser sends it."""
    return parse_qs(value.lstrip("?"))


def parse_listing_date(value: str | None) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    value = str(value).strip()
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", value):
        raise ValueError(f"Zoho listing date is not MM/DD/YYYY: {value!r}")
    return datetime.strptime(value, "%m/%d/%Y").date().isoformat()


def parse_detail_date(value: str | None) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    value = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"Zoho detail date is not YYYY-MM-DD: {value!r}")
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def parse_official_entry(html: str, response_url: str = OFFICIAL_ENTRY) -> dict:
    parsed = urlsplit(response_url)
    if (parsed.scheme != "https" or parsed.netloc != OFFICIAL_HOST
            or parsed.path.rstrip("/") != "/careers"):
        raise ValueError("Zoho official careers entry redirected away from its inspected URL")
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("#rec_job_listing_div")
    group = soup.select_one("#rec_job_listing_div ul.rec-group")
    jobs_section = soup.select_one("section#jobs")
    region_list = soup.select_one(".zwc-region-list")
    country_list = soup.select_one(".zwc-contry-list")
    if not all((container, group, jobs_section, region_list, country_list)):
        raise ValueError("Zoho official careers page no longer exposes its job-listing widget")
    scripts = [urljoin(response_url, node.get("src", "")) for node in soup.select("script[src]")]
    widget_scripts = [url for url in scripts if WIDGET_SCRIPT_PATH.search(urlsplit(url).path)]
    if len(widget_scripts) != 1:
        raise ValueError("Zoho careers page no longer publishes exactly one job-widget script")
    board_links = sorted({
        anchor.get("href")
        for anchor in soup.select("a[href]")
        if re.search(r"greenhouse|lever\.co|workday|ashby|linkedin|indeed|naukri", anchor.get("href") or "", re.I)
    })
    registration = sorted({
        anchor.get("href")
        for anchor in soup.select('a[href*="candidateportal"]')
    })
    return {
        "url": response_url,
        "title": soup.title.get_text(" ", strip=True) if soup.title else None,
        "listing_container_id": container.get("id"),
        "listing_group_class": " ".join(group.get("class") or []),
        "listing_section_id": jobs_section.get("id"),
        "region_filter_element": ".zwc-region-list",
        "country_filter_element": ".zwc-contry-list",
        "widget_script_url": widget_scripts[0],
        "candidate_portal_links": registration,
        "third_party_job_board_links": board_links,
    }


def _split_top_level(value: str, delimiter: str = ",") -> list[str]:
    parts, start, depth, quote, escaped = [], 0, 0, None, False
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == delimiter and depth == 0:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _first_top_level_colon(value: str) -> int:
    depth, quote, escaped = 0, None, False
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
        elif char == ":" and depth == 0:
            return index
    return -1


def _object_block(js: str, marker: str) -> str:
    start = js.find(marker)
    if start < 0:
        raise ValueError(f"Zoho widget script no longer contains {marker!r}")
    brace = js.find("{", start)
    if brace < 0:
        raise ValueError("Zoho widget region map has no opening brace")
    depth, quote, escaped = 0, None, False
    for index in range(brace, len(js)):
        char = js[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return js[brace + 1:index]
    raise ValueError("Zoho widget region map is not closed")


def parse_region_config(js: str) -> dict:
    """Extract and validate the region -> Job_Openings map from the page widget."""
    query_match = re.search(r'const e="(\?pagename=Careers[^"]*)"', js)
    if not query_match:
        raise ValueError("Zoho widget no longer defines its Careers listing query")
    query = query_match.group(1)
    if query != LISTING_QUERY:
        raise ValueError(f"Zoho widget listing query changed: {query}")
    block = _object_block(js, 'const r={"Middle East and Africa"')
    regions: OrderedDict[str, list[str]] = OrderedDict()
    for item in _split_top_level(block):
        if not item.strip():
            continue
        index = _first_top_level_colon(item)
        if index < 0:
            raise ValueError("Zoho widget region map entry is malformed")
        key = item[:index].strip().strip("\"'")
        value = item[index + 1:]
        urls = re.findall(r'"(https://[^"]+/recruit/v2/public/Job_Openings)"\+e', value)
        if not urls:
            raise ValueError(f"Zoho widget region {key!r} has no static Job_Openings endpoint")
        if len(re.findall(r"https://", value)) != len(urls):
            raise ValueError(f"Zoho widget region {key!r} contains an unexpected URL")
        regions[key] = urls
    if list(regions) != list(EXPECTED_REGION_ENDPOINTS):
        raise ValueError(f"Zoho widget region order/names changed: {list(regions)}")
    for region, hosts in EXPECTED_REGION_ENDPOINTS.items():
        urls = regions[region]
        got_hosts = [urlsplit(url).netloc for url in urls]
        if got_hosts != hosts:
            raise ValueError(f"Zoho widget endpoints for {region!r} changed: {got_hosts}")
        for url in urls:
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.path != LISTING_PATH:
                raise ValueError(f"Zoho widget endpoint has an unexpected shape: {url}")
    return {"query": query, "regions": dict(regions)}


def parse_listing_payload(value: dict, *, host: str, region: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Zoho Job_Openings response is not an object")
    if value.get("code") != "success":
        raise ValueError(f"Zoho Job_Openings response failed: {value.get('code')}")
    data = value.get("data")
    info = value.get("info")
    if not isinstance(data, list) or not isinstance(info, dict):
        raise ValueError("Zoho Job_Openings response is missing data or info")
    if info.get("page_name") != "Careers":
        raise ValueError(f"Zoho Job_Openings response page_name changed: {info.get('page_name')}")
    rows, issues, seen = [], [], set()
    for raw in data:
        if not isinstance(raw, dict):
            issues.append("zoho_row_not_object")
            continue
        identity = str(raw.get("id", ""))
        if not re.fullmatch(r"\d+", identity):
            issues.append("zoho_row_missing_numeric_id")
            continue
        if identity in seen:
            issues.append(f"zoho_row_duplicate_id:{identity}")
            continue
        seen.add(identity)
        title = normalized_title(raw.get("Posting_Title") or raw.get("Job_Opening_Name"))
        if not title:
            issues.append(f"zoho_row_missing_title:{identity}")
            continue
        if raw.get("Publish") is not True:
            issues.append(f"zoho_row_not_published:{identity}")
            continue
        if not _clean(raw.get("Job_Type")):
            issues.append(f"zoho_row_missing_job_type:{identity}")
        url = str(raw.get("$url") or "")
        parts = urlsplit(url)
        match = JOB_PAGE_PATH.fullmatch(parts.path)
        if (parts.scheme != "https" or parts.netloc != host or match is None
                or match.group(1) != identity
                or parse_qs(parts.query) != {"source": ["CareerSite"]}):
            issues.append(f"zoho_row_job_url_not_exact:{identity}")
            continue
        snippet = raw.get("Job_Description")
        if not isinstance(snippet, str) or not normalized_body_text(snippet):
            issues.append(f"zoho_row_missing_description_snippet:{identity}")
        if raw.get("Date_Opened") is not None:
            try:
                parse_listing_date(raw.get("Date_Opened"))
            except ValueError as exc:
                issues.append(f"zoho_row_date_invalid:{identity}:{exc}")
        rows.append(dict(raw))
    if not rows and data:
        issues.append("zoho_endpoint_returned_no_valid_rows")
    return {"rows": rows, "count": len(rows), "issues": issues, "region": region, "host": host}


def parse_detail_payload(value: dict, expected_row: dict, *, request_url: str) -> dict:
    if not isinstance(value, dict) or value.get("code") != "success":
        raise ValueError("Zoho detail response is not a successful object")
    data = value.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("Zoho detail response does not contain exactly one job record")
    detail = data[0]
    identity = str(expected_row.get("id"))
    issues = []
    if str(detail.get("id")) != identity:
        raise ValueError(f"Zoho detail id mismatch: {detail.get('id')!r} != {identity!r}")
    title = normalized_title(detail.get("Posting_Title") or detail.get("Job_Opening_Name"))
    if title != normalized_title(expected_row.get("Posting_Title")):
        issues.append(f"zoho_detail_title_mismatch:{identity}")
    if detail.get("Publish") is not True:
        issues.append(f"zoho_detail_not_published:{identity}")
    description = detail.get("Job_Description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"Zoho detail has no Job_Description: {identity}")
    body = normalized_body_text(description)
    if len(body) < 100:
        issues.append(f"zoho_detail_description_too_short:{identity}")
    if re.search(r"(?:\.\.\.|…)\s*$", body):
        issues.append(f"zoho_detail_description_ends_truncated:{identity}")
    snippet_words = re.findall(r"[a-z0-9]+", normalized_body_text(
        expected_row.get("Job_Description") or "").lower())
    body_words = re.findall(r"[a-z0-9]+", body.lower())
    # The preview is cut mid-word and is re-rendered with different spacing
    # around punctuation; compare its complete words in order, ignoring the
    # possibly truncated final word.
    probe = snippet_words[:-1][:18]
    if len(probe) >= 4:
        found = any(body_words[i:i + len(probe)] == probe
                    for i in range(len(body_words) - len(probe) + 1))
        if not found:
            issues.append(f"zoho_listing_snippet_not_in_detail:{identity}")
    apply_url = str(detail.get("$appy_url") or "")
    parts = urlsplit(apply_url)
    apply_match = JOB_PAGE_PATH.fullmatch(parts.path)
    apply_query = parse_qs(parts.query)
    if (parts.scheme != "https" or parts.netloc != urlsplit(request_url).netloc
            or apply_match is None or apply_match.group(1) != identity
            or apply_query.get("$apply") != ["true"] or apply_query.get("source") != ["CareerSite"]):
        issues.append(f"zoho_detail_apply_url_not_exact:{identity}")
    detail_date = parse_detail_date(detail.get("Date_Opened"))
    listing_date = parse_listing_date(expected_row.get("Date_Opened"))
    if detail_date and listing_date and detail_date != listing_date:
        issues.append(f"zoho_listing_and_detail_date_mismatch:{identity}:{listing_date}:{detail_date}")
    return {
        "detail": detail,
        "issues": issues,
        "posted_date": detail_date or listing_date,
        "posted_date_raw": {"listing": expected_row.get("Date_Opened"), "detail": detail.get("Date_Opened")},
        "apply_url": apply_url if not any("apply_url" in issue for issue in issues) else None,
    }


def description_sections(html: str) -> list[dict]:
    """Split the published body into employer headings and their exact text."""
    soup = BeautifulSoup(f'<div data-zoho-body="1">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-zoho-body]")
    if root is None:
        return []
    for span in root.select('span[id="spandesc"], span[id="spanreq"]'):
        span.unwrap()
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    sections: list[dict] = []
    current: dict | None = None
    for block in root.contents:
        if isinstance(block, NavigableString):
            if not str(block).strip():
                continue
            if current is None:
                current = {"heading": None, "html": "", "text": ""}
            current["html"] += str(block)
            continue
        if not isinstance(block, Tag):
            continue
        heading = ""
        if block.name in heading_tags:
            heading = normalized_title(block.get_text(" ", strip=True))
        elif block.name == "p":
            strong = block.find(["strong", "b"])
            full_text = normalized_title(block.get_text(" ", strip=True))
            strong_text = normalized_title(strong.get_text(" ", strip=True)) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text
        elif block.name == "div":
            first = block.find(heading_tags)
            if first is not None and normalized_title(first.get_text(" ", strip=True)) == normalized_title(
                    block.get_text(" ", strip=True)) and len(normalized_title(first.get_text())) <= 120:
                heading = normalized_title(first.get_text(" ", strip=True))
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
        if section["html"]:
            section["text"] = rendered_body_text(section["html"])
        section["text"] = section["text"].strip()
    return sections


def select_sections(sections: list[dict], tokens: tuple[str, ...]) -> list[dict] | None:
    selected = [
        {"heading": section["heading"], "text": section["text"]}
        for section in sections
        if section.get("heading") and any(token in section["heading"].lower() for token in tokens)
        and section.get("text")
    ]
    return selected or None


def location_record(row: dict) -> dict:
    city = _clean(row.get("City"))
    state = _clean(row.get("State"))
    country = _clean(row.get("Country") or row.get("Country1"))
    zip_code = _clean(row.get("Zip_Code"))
    formatted = ", ".join(part for part in (city, state, country) if part)
    return {"city": city, "state": state, "country": country,
            "zip_code": zip_code, "formatted": formatted or None, "portal_region": None}


def workplace_type(row: dict) -> str | None:
    value = row.get("Remote_Job")
    if value is True:
        return "Remote"
    if value is False:
        return "On-site"
    return None


class Zoho(Adapter):
    """Collect every role published by Zoho's own careers page and its regions."""

    VERIFICATION_REVISION = "zoho.careers.region-partition-recruit-public-api.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.detail_timings: list[float] = []

    async def _official_entry(self) -> dict:
        started = time.perf_counter()
        response = await self.http.get(OFFICIAL_ENTRY)
        seconds = max(0.0, time.perf_counter() - started)
        entry = parse_official_entry(response.text, str(response.url))
        entry.update(status=response.status_code, seconds=seconds,
                     response_sha256=sha256(response.content).hexdigest(),
                     response_bytes=len(response.content))
        return entry

    async def _widget_config(self, script_url: str) -> dict:
        started = time.perf_counter()
        response = await self.http.get(script_url)
        seconds = max(0.0, time.perf_counter() - started)
        config = parse_region_config(response.text)
        config.update(script_url=script_url, status=response.status_code, seconds=seconds,
                      script_sha256=sha256(response.content).hexdigest(),
                      response_bytes=len(response.content))
        return config

    async def _listing_snapshot(self, config: dict, *, phase: str) -> dict:
        started = time.perf_counter()
        endpoints, rows, by_region, seen, issues = [], [], OrderedDict(), {}, []
        row_sources = {}
        failed = 0
        for region, hosts in config["regions"].items():
            region_ids = []
            for base in hosts:
                url = base + config["query"]
                request_started = time.perf_counter()
                try:
                    response = await self.http.get(url, headers={"Referer": OFFICIAL_ENTRY})
                    seconds = max(0.0, time.perf_counter() - request_started)
                    parts = urlsplit(str(response.url))
                    if (parts.scheme != "https" or parts.netloc != urlsplit(base).netloc
                            or parts.path != LISTING_PATH
                            or query_params(parts.query) != query_params(config["query"])):
                        raise ValueError(f"Zoho listing endpoint redirected outside its published route: {response.url}")
                    parsed = parse_listing_payload(response.json(), host=urlsplit(base).netloc, region=region)
                    issues.extend(parsed["issues"])
                    for row in parsed["rows"]:
                        identity = str(row["id"])
                        if identity in seen:
                            issues.append(f"zoho_duplicate_id_across_endpoints:{identity}")
                            continue
                        seen[identity] = (region, base)
                        row_sources[identity] = {"region": region, "host": urlsplit(base).netloc}
                        rows.append(row)
                        region_ids.append(identity)
                    endpoints.append({
                        "phase": phase, "region": region, "host": urlsplit(base).netloc,
                        "request_url": url, "final_url": str(response.url), "status": response.status_code,
                        "request_seconds": seconds, "row_count": parsed["count"],
                        "job_ids": [str(row["id"]) for row in parsed["rows"]],
                        "response_bytes": len(response.content),
                        "response_sha256": sha256(response.content).hexdigest(),
                    })
                except Exception as exc:  # noqa: BLE001 - recorded exactly
                    failed += 1
                    self.result.errors.append(
                        f"Zoho listing {region} {base} failed: {type(exc).__name__}: {exc}")
                    endpoints.append({
                        "phase": phase, "region": region, "host": urlsplit(base).netloc,
                        "request_url": url, "status": "failed",
                        "request_seconds": max(0.0, time.perf_counter() - request_started),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
            by_region[region] = region_ids
        return {
            "rows": rows,
            "by_region": dict(by_region),
            "row_sources": row_sources,
            "endpoints": endpoints,
            "issues": list(dict.fromkeys(issues)),
            "failed_endpoints": failed,
            "total_seconds": max(0.0, time.perf_counter() - started),
        }

    def _listing_job(self, row: dict, region: str, portal_host: str):
        date_posted = parse_listing_date(row.get("Date_Opened"))
        return self.job(
            {"listing_row": row, "region": region, "portal_host": portal_host},
            title=normalized_title(row.get("Posting_Title")),
            url=row["$url"],
            job_id=str(row["id"]),
            requisition_id=None,
            apply_url=None,
            description="",
            method=None,
            description_url=None,
            locations=[{**location_record(row), "portal_region": region}],
            department=None,
            employment_type=_clean(row.get("Job_Type")),
            workplace_type=workplace_type(row),
            date_posted=date_posted,
            date_posted_raw={"listing": row.get("Date_Opened"), "detail": None},
            date_updated=None,
            valid_through=None,
            salary=None,
            qualifications=None,
            responsibilities=None,
            experience=_clean(row.get("Work_Experience")),
            education=None,
            employer=PORTAL_ORGANISATIONS.get(portal_host),
            language="en",
        )

    async def _hydrate_job(self, job):
        started = time.perf_counter()
        parts = urlsplit(job.url)
        host = parts.netloc
        detail_url = f"https://{host}{LISTING_PATH}/{job.job_id}{DETAIL_QUERY}"
        try:
            response = await self.http.get(detail_url, headers={"Referer": OFFICIAL_ENTRY})
            seconds = max(0.0, time.perf_counter() - started)
            self.detail_timings.append(seconds)
            response_parts = urlsplit(str(response.url))
            if (response_parts.scheme != "https" or response_parts.netloc != host
                    or DETAIL_RECORD_PATH.fullmatch(response_parts.path) is None
                    or query_params(response_parts.query) != query_params(DETAIL_QUERY)):
                raise ValueError(f"Zoho detail endpoint redirected outside its published route: {response.url}")
            parsed = parse_detail_payload(
                response.json(), job.raw["listing_row"], request_url=detail_url)
            detail = parsed["detail"]
            description_html = detail["Job_Description"]
            sections = description_sections(description_html)
            job.title = normalized_title(detail.get("Posting_Title") or job.title)
            job.apply_url = parsed["apply_url"] or detail.get("$appy_url")
            job.description_html = description_html
            job.description_text = rendered_body_text(description_html)
            job.description_method = "zoho_recruit.public.Job_Openings.id.Job_Description"
            job.description_source_url = detail_url
            job.locations = [{**location_record(detail), "portal_region": job.raw["region"]}]
            job.employment_type = _clean(detail.get("Job_Type")) or job.employment_type
            job.workplace_type = workplace_type(detail) or job.workplace_type
            job.date_posted = parsed["posted_date"]
            job.date_posted_raw = parsed["posted_date_raw"]
            job.salary = detail.get("Salary")
            job.experience = _clean(detail.get("Work_Experience")) or job.experience
            job.employer = PORTAL_ORGANISATIONS.get(host) or job.employer
            job.responsibilities = select_sections(sections, RESPONSIBILITY_TOKENS)
            job.qualifications = select_sections(sections, QUALIFICATION_TOKENS)
            job.raw["detail_row"] = detail
            job.raw["detail"] = {
                "request_url": detail_url,
                "final_url": str(response.url),
                "status": response.status_code,
                "request_seconds": seconds,
                "response_bytes": len(response.content),
                "response_sha256": sha256(response.content).hexdigest(),
                "portal_organisation": job.employer,
                "description_sections": sections,
                "description_html_sha256": sha256(description_html.encode()).hexdigest(),
                "description_normalized_chars": len(normalized_body_text(description_html)),
                "description_normalized_sha256": sha256(
                    normalized_body_text(description_html).encode()).hexdigest(),
                "posted_date_raw": parsed["posted_date_raw"],
                "apply_url": detail.get("$appy_url"),
                "industry": detail.get("Industry"),
                "designation": detail.get("Designation"),
                "currency": detail.get("Currency"),
                "zip_code": detail.get("Zip_Code"),
                "remote_job_raw": detail.get("Remote_Job"),
            }
            for issue in parsed["issues"]:
                if issue not in job.warnings:
                    job.warnings.append(issue)
                self.result.errors.append(issue)
        except Exception as exc:  # noqa: BLE001 - left unverified and recorded
            self.result.errors.append(f"Zoho detail {job.job_id} failed: {type(exc).__name__}: {exc}")
            job.warnings.append("detail_fetch_failed")
            job.description_unavailable = True
        return job

    async def run(self):
        source_started = time.perf_counter()
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "listing_url": OFFICIAL_ENTRY + "#jobs",
            "listing_authority": (
                "The region dropdown published by Zoho's own careers page; each "
                "region maps to the Job_Openings endpoints in the page's widget "
                "script, and the six regions partition the global listing."
            ),
            "listing_membership_request": {
                "method": "GET",
                "path": LISTING_PATH,
                "query": LISTING_QUERY,
                "regions": {region: hosts for region, hosts in EXPECTED_REGION_ENDPOINTS.items()},
                "filters_applied": False,
                "scope": "Only the nine Zoho career-site hosts requested by the official page widget.",
            },
            "detail_method": (
                "Exact public detail record on the same career-site host "
                "(/recruit/v2/public/Job_Openings/{id}); Job_Description is the full body."
            ),
            "portal_organisations": PORTAL_ORGANISATIONS,
        })
        try:
            entry = await self._official_entry()
            config = await self._widget_config(entry["widget_script_url"])
        except Exception as exc:  # noqa: BLE001
            self.result.errors.append(f"Zoho official entry/widget failed: {type(exc).__name__}: {exc}")
            self.result.metadata["source_collection_wall_seconds"] = max(
                0.0, time.perf_counter() - source_started)
            return self.result

        self.result.metadata["initial_official_entry"] = entry
        self.result.metadata["widget_config"] = {k: v for k, v in config.items() if k != "regions"}
        self.result.timings["official_entry_widget_seconds"] = entry["seconds"] + config["seconds"]
        self.result.metadata["region_endpoints"] = config["regions"]

        initial = await self._listing_snapshot(config, phase="initial")
        self.result.metadata["initial_listing"] = {
            "by_region": initial["by_region"],
            "endpoints": initial["endpoints"],
            "issues": initial["issues"],
            "failed_endpoints": initial["failed_endpoints"],
            "count": len(initial["rows"]),
            "seconds": initial["total_seconds"],
        }
        self.result.pages = len(initial["endpoints"])
        self.result.timings["initial_listing_seconds"] = initial["total_seconds"]
        self.result.expected_total = len(initial["rows"]) if initial["failed_endpoints"] == 0 else None
        self.result.errors.extend(initial["issues"])
        if initial["failed_endpoints"]:
            self.result.errors.append("zoho_listing_endpoint_failure_makes_total_unknown")

        for row in initial["rows"]:
            source = initial["row_sources"][str(row["id"])]
            self.add(self._listing_job(row, source["region"], source["host"]))
        jobs = list(self.result.jobs)
        if len(jobs) != len(initial["rows"]):
            self.result.errors.append("zoho_listing_rows_do_not_match_initial_membership")

        if jobs:
            await self.hydrate(jobs, self._hydrate_job)
        self.result.jobs = jobs
        detail_seconds = sum(self.detail_timings)
        self.result.metadata["detail_request_seconds_sum"] = detail_seconds
        self.result.timings["detail_request_seconds_sum"] = detail_seconds
        self.result.metadata["detail_requests"] = len(self.detail_timings)
        self.result.metadata["detail_hydration_mode"] = (
            "One exact detail request per listed ID; full Job_Description field. "
            "Listing Job_Description is a 153-character snippet and is not used as the body."
        )

        final = None
        final_entry = None
        final_config = None
        recheck_started = time.perf_counter()
        try:
            final_entry = await self._official_entry()
            final_config = await self._widget_config(final_entry["widget_script_url"])
            final = await self._listing_snapshot(final_config, phase="final")
        except Exception as exc:  # noqa: BLE001
            self.result.errors.append(f"Zoho final source recheck failed: {type(exc).__name__}: {exc}")
        recheck_seconds = max(0.0, time.perf_counter() - recheck_started)
        self.result.timings["membership_recheck_seconds"] = recheck_seconds
        if final is not None:
            self.result.timings["final_listing_seconds"] = final["total_seconds"]

        membership_ok = False
        if final is None or final_entry is None or final_config is None:
            self.result.errors.append("zoho_final_membership_unavailable")
        else:
            initial_rows = {str(row["id"]): row for row in initial["rows"]}
            final_rows = {str(row["id"]): row for row in final["rows"]}
            changed = {}
            for identity in initial_rows:
                if identity in final_rows and initial_rows[identity] != final_rows[identity]:
                    changed[identity] = sorted(
                        key for key in set(initial_rows[identity]) | set(final_rows[identity])
                        if initial_rows[identity].get(key) != final_rows[identity].get(key))
            same_initial = [str(row["id"]) for row in initial["rows"]]
            same_final = [str(row["id"]) for row in final["rows"]]
            entry_same = (
                entry["widget_script_url"] == final_entry["widget_script_url"]
                and entry["listing_container_id"] == final_entry["listing_container_id"]
                and entry["third_party_job_board_links"] == final_entry["third_party_job_board_links"]
                and config["query"] == final_config["query"]
                and config["script_sha256"] == final_config["script_sha256"]
                and config["regions"] == final_config["regions"]
            )
            region_match = initial["by_region"] == final["by_region"]
            membership_ok = bool(
                not initial["failed_endpoints"] and not final["failed_endpoints"]
                and same_initial == same_final and not changed and entry_same and region_match
                and not initial["issues"] and not final["issues"]
                and len(jobs) == len(initial["rows"])
            )
            self.result.metadata["final_official_entry"] = final_entry
            self.result.metadata["final_listing"] = {
                "by_region": final["by_region"],
                "endpoints": final["endpoints"],
                "issues": final["issues"],
                "failed_endpoints": final["failed_endpoints"],
                "count": len(final["rows"]),
                "seconds": final["total_seconds"],
            }
            self.result.metadata["membership_reconciliation"] = {
                "initial_ids": same_initial,
                "final_ids": same_final,
                "initial_count": len(same_initial),
                "final_count": len(same_final),
                "initial_by_region": initial["by_region"],
                "final_by_region": final["by_region"],
                "same_ids_and_order": same_initial == same_final,
                "region_membership_match": region_match,
                "all_row_fields_match": not changed,
                "changed_fields_by_job_id": changed,
                "new_job_ids": [i for i in same_final if i not in initial_rows],
                "removed_job_ids": [i for i in same_initial if i not in final_rows],
                "official_entry_and_widget_still_exact": entry_same,
                "initial_endpoint_failures": initial["failed_endpoints"],
                "final_endpoint_failures": final["failed_endpoints"],
                "complete": membership_ok,
            }
            if not entry_same:
                self.result.errors.append("zoho_official_entry_or_widget_changed_during_collection")
            if same_initial != same_final or changed or not region_match:
                self.result.errors.append("zoho_listing_membership_changed_during_collection")
            self.result.errors.extend(final["issues"])
            if final["failed_endpoints"]:
                self.result.errors.append("zoho_final_listing_endpoint_failure")

        self.result.metadata["job_ids"] = [str(job.job_id) for job in jobs]
        self.result.metadata["jobs_by_region"] = {
            region: [str(job.job_id) for job in jobs if job.raw.get("region") == region]
            for region in EXPECTED_REGION_ENDPOINTS
        }
        self.result.metadata["current_native_membership_reconciled"] = membership_ok
        self.result.metadata["source_collection_wall_seconds"] = max(
            0.0, time.perf_counter() - source_started)
        self.result.timings["source_collection_wall_seconds"] = self.result.metadata[
            "source_collection_wall_seconds"]
        self.result.listing_complete = bool(membership_ok)
        return self.result
