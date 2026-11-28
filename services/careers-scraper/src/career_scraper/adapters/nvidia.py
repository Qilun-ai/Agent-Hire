"""NVIDIA's official careers journey: nvidia.com -> jobs.nvidia.com Eightfold PCSX.

The NVIDIA corporate careers page publishes ``https://jobs.nvidia.com/careers``
as its open-positions destination. That NVIDIA-branded PCSX site
(``domain=nvidia.com``) is the authority for the unfiltered, global listing.
Its published search configuration has no membership filter and excludes
private positions. The public sitemap is used only as a completeness
cross-check: a sitemap-only id is retained only when its exact detail succeeds
and the same PCSX search returns its requisition id.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..http import FetchError
from ..models import text_html
from .base import Adapter

OFFICIAL_ENTRY = "https://www.nvidia.com/en-us/about-nvidia/careers/"
LISTING_URL = "https://jobs.nvidia.com/careers"
DOMAIN = "nvidia.com"
API_BASE = "https://jobs.nvidia.com/api/pcsx"
SEARCH_URL = API_BASE + "/search"
DETAIL_URL = API_BASE + "/position_details"
SITEMAP_INDEX_URL = LISTING_URL + "/sitemap_index.xml?domain=nvidia.com"
SITEMAP_URL = LISTING_URL + "/sitemap.xml?domain=nvidia.com"
ROBOTS_URL = "https://jobs.nvidia.com/robots.txt"
PAGE_SIZE = 10
CHALLENGE_TOKENS = ("403", "405", "429", "CAPTCHA", "captcha",
                    "Human Verification", "awswaf", "Access Denied")

SALARY_KEY = re.compile(
    r"(?:\b(?:base\s+)?(?:pay|salary)\s+ranges?\b|"
    r"\bcompensation\s+range\b)",
    re.I,
)
CURRENCY_CODES = (
    "USD", "CAD", "EUR", "GBP", "AUD", "SGD", "INR", "JPY", "CHF",
    "BRL", "MXN", "NZD", "SEK", "NOK", "DKK", "PLN", "ZAR", "AED",
    "CNY", "TWD", "KRW", "HKD", "ILS",
)
CURRENCY_SYMBOLS = {"$": None, "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
AMOUNT = re.compile(
    r"(?:(?P<pre>[A-Z]{3})\s*)?"
    r"(?P<symbol>[$€£¥₹])?\s*"
    r"(?P<number>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(?P<symbol2>[$€£¥₹]))?"
    r"\s*(?:(?P<post>[A-Z]{3})\b)?",
    re.I,
)


def epoch(value):
    """Convert PCSX's second-based ``postedTs``; missing/zero stays null."""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"NVIDIA postedTs is not a valid epoch second: {value!r}")
    if value == 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def detail_url(position_id):
    return f"{DETAIL_URL}?position_id={position_id}&domain={DOMAIN}&hl=en"


def parse_official_entry(html, response_url=OFFICIAL_ENTRY):
    """Validate NVIDIA's corporate careers page and its published search target."""
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc not in {"www.nvidia.com", "nvidia.com"}:
        raise ValueError("NVIDIA official careers entry left the official host")
    if parsed.path.rstrip("/") not in {
        "/en-us/about-nvidia/careers",
        "/about-nvidia/careers",
    }:
        raise ValueError(f"NVIDIA official careers entry redirected unexpectedly: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "NVIDIA" not in title:
        raise ValueError(f"NVIDIA official careers page title changed: {title!r}")
    hero = soup.select_one("#career-hero-cta")
    if hero is None:
        raise ValueError("NVIDIA official careers page no longer publishes its jobs CTA")
    target = hero.get("href", "")
    if target != LISTING_URL:
        raise ValueError(f"NVIDIA official jobs target changed: {target!r}")
    links = [a.get("href") for a in soup.find_all("a", href=True)
             if a.get("href") == LISTING_URL]
    if len(links) < 2:
        raise ValueError("NVIDIA official page no longer links to jobs.nvidia.com/careers in nav and CTA")
    return {
        "url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "title": title,
        "search_target": target,
        "search_target_selector": "#career-hero-cta",
        "listing_links": len(links),
    }


def parse_listing_config(html):
    """Validate the NVIDIA-branded PCSX configuration that defines scope."""
    soup = BeautifulSoup(html or "", "lxml")
    node = soup.select_one("code#pcsx-data")
    if node is None:
        raise ValueError("NVIDIA listing page no longer exposes its PCSX configuration")
    try:
        data = json.loads(node.get_text())
    except ValueError as exc:
        raise ValueError("NVIDIA PCSX configuration is not valid JSON") from exc
    if data.get("domain") != DOMAIN:
        raise ValueError(f"NVIDIA PCSX domain changed: {data.get('domain')!r}")
    pcs = (data.get("configs") or {}).get("pcsxConfig") or {}
    branding = pcs.get("branding") or {}
    if branding.get("companyName") != "NVIDIA Corporation":
        raise ValueError("NVIDIA PCSX branding is no longer NVIDIA Corporation")
    search = pcs.get("searchConfig") or {}
    if search.get("membershipFq", "") != "":
        raise ValueError("NVIDIA PCSX listing now has a membership filter")
    if search.get("excludePrivatePositions") is not True:
        raise ValueError("NVIDIA PCSX private-position exclusion changed")
    base = search.get("basePositionFq") or ""
    folded = base.casefold()
    for token in ("position.type:ats", "position.ats_data.is_posted:true",
                  "position.ats_data.restrict_access:false"):
        if token not in folded:
            raise ValueError(f"NVIDIA PCSX base listing filter changed; missing {token!r}")
    tabs = (pcs.get("positionDetailsConfig") or {}).get("tabsDisplayConfig") or []
    jd = next((tab for tab in tabs if tab.get("id") == "jd"), None)
    if jd is None or jd.get("sections") != ["position_fields", "jd"]:
        raise ValueError("NVIDIA PCSX job-description tab sections changed")
    company = next((tab for tab in tabs if tab.get("id") == "company"), None)
    canonical = soup.find("link", rel="canonical")
    og_url = soup.find("meta", property="og:url")
    if not canonical or canonical.get("href") != LISTING_URL:
        raise ValueError("NVIDIA listing canonical URL changed")
    if not og_url or og_url.get("content") != LISTING_URL:
        raise ValueError("NVIDIA listing og:url changed")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return {
        "domain": data["domain"],
        "company_name": branding["companyName"],
        "page_title": title,
        "membership_fq": search.get("membershipFq", ""),
        "base_position_fq": base,
        "exclude_private_positions": search.get("excludePrivatePositions"),
        "smart_filters": search.get("smartFilters", []),
        "all_filters": search.get("allFilters", []),
        "include_remote_default": search.get("includeRemoteDefault"),
        "job_description_sections": jd["sections"],
        "company_tab_sections": company.get("sections") if company else None,
    }


def listing_row(row):
    """Validate one NVIDIA PCSX search row without inventing missing fields."""
    if not isinstance(row, dict):
        raise ValueError("NVIDIA listing row is not an object")
    identity = row.get("id")
    if isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0:
        raise ValueError(f"NVIDIA listing row has no valid PCSX id: {row!r}")
    display = row.get("displayJobId")
    if not isinstance(display, str) or not display.strip():
        raise ValueError(f"NVIDIA listing row has no displayJobId: {identity}")
    if row.get("atsJobId") != display:
        raise ValueError(f"NVIDIA listing row atsJobId differs: {identity}")
    if not isinstance(row.get("name"), str) or not row["name"].strip():
        raise ValueError(f"NVIDIA listing row has no title: {identity}")
    if row.get("positionUrl") != f"/careers/job/{identity}":
        raise ValueError(f"NVIDIA listing row positionUrl differs: {identity}")
    if row.get("postedTs") not in (None, "", 0) and epoch(row.get("postedTs")) is None:
        raise ValueError(f"NVIDIA listing row has invalid postedTs: {identity}")
    for key in ("locations", "standardizedLocations"):
        if row.get(key) is not None and not isinstance(row.get(key), list):
            raise ValueError(f"NVIDIA listing row {key} is not a list: {identity}")
    return row


def parse_search_payload(payload, *, requested_start):
    """Validate an unfiltered NVIDIA PCSX search page and return its rows."""
    if not isinstance(payload, dict) or payload.get("status") != 200:
        raise ValueError(f"NVIDIA PCSX search failed: {payload!r}")
    error = payload.get("error") or {}
    if error.get("message"):
        raise ValueError(f"NVIDIA PCSX search error: {error}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("NVIDIA PCSX search response has no data object")
    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"NVIDIA PCSX search count is invalid: {count!r}")
    if data.get("appliedFilters") not in ({}, None):
        raise ValueError(f"NVIDIA PCSX search unexpectedly has filters: {data.get('appliedFilters')!r}")
    if data.get("sortBy") not in (None, "timestamp"):
        raise ValueError(f"NVIDIA PCSX search sort changed: {data.get('sortBy')!r}")
    positions = data.get("positions")
    if not isinstance(positions, list):
        raise ValueError("NVIDIA PCSX search positions is not a list")
    if len(positions) > PAGE_SIZE:
        raise ValueError(f"NVIDIA PCSX search returned {len(positions)} rows for a {PAGE_SIZE}-row pager")
    seen = set()
    for row in positions:
        listing_row(row)
        identity = row["id"]
        if identity in seen:
            raise ValueError(f"NVIDIA PCSX search page repeats id {identity}")
        seen.add(identity)
    expected_rows = min(PAGE_SIZE, max(0, count - requested_start))
    issues = []
    if len(positions) != expected_rows:
        issues.append(
            f"row_count_differs_from_advertised:start={requested_start}:"
            f"expected={expected_rows}:received={len(positions)}:count={count}"
        )
    used_fuzz = (data.get("resultsMetaData") or {}).get("usedFuzzSearch")
    if used_fuzz is True:
        issues.append("search_reported_fuzzy_results")
    return count, positions, issues


def parse_detail_payload(payload, *, expected_id):
    """Validate the exact public PCSX detail record for a listing id."""
    if not isinstance(payload, dict) or payload.get("status") != 200:
        raise ValueError(f"NVIDIA PCSX detail failed: {payload!r}")
    error = payload.get("error") or {}
    if error.get("message"):
        raise ValueError(f"NVIDIA PCSX detail error: {error}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("NVIDIA PCSX detail response has no data object")
    identity = data.get("id")
    if isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0:
        raise ValueError("NVIDIA PCSX detail has no valid posting id")
    if str(identity) != str(expected_id):
        raise ValueError(f"NVIDIA PCSX detail id differs: {identity} != {expected_id}")
    display = data.get("displayJobId")
    if not isinstance(display, str) or not display.strip():
        raise ValueError(f"NVIDIA PCSX detail has no displayJobId: {identity}")
    if data.get("atsJobId") != display:
        raise ValueError(f"NVIDIA PCSX detail atsJobId differs: {identity}")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError(f"NVIDIA PCSX detail has no title: {identity}")
    if data.get("positionUrl") != f"/careers/job/{identity}":
        raise ValueError(f"NVIDIA PCSX detail positionUrl differs: {identity}")
    if data.get("publicUrl") != f"{LISTING_URL}/job/{identity}":
        raise ValueError(f"NVIDIA PCSX detail publicUrl differs: {identity}")
    body = data.get("jobDescription")
    if not isinstance(body, str) or len(text_html(body).strip()) < 100:
        raise ValueError(f"NVIDIA PCSX detail has no complete jobDescription: {identity}")
    if data.get("postedTs") not in (None, "", 0) and epoch(data.get("postedTs")) is None:
        raise ValueError(f"NVIDIA PCSX detail has invalid postedTs: {identity}")
    if data.get("locations") is not None and not isinstance(data.get("locations"), list):
        raise ValueError(f"NVIDIA PCSX detail locations is not a list: {identity}")
    extra = data.get("positionExtraDetails")
    if extra is not None and not isinstance(extra, dict):
        raise ValueError(f"NVIDIA PCSX detail positionExtraDetails is not an object: {identity}")
    custom = (extra or {}).get("customContent")
    if custom is not None and not isinstance(custom, list):
        raise ValueError(f"NVIDIA PCSX detail customContent is not a list: {identity}")
    time_types = data.get("efcustomTextTimeType")
    if time_types is not None and not isinstance(time_types, list):
        raise ValueError(f"NVIDIA PCSX detail efcustomTextTimeType is not a list: {identity}")
    return data


def apply_route(detail):
    """Return the public Workday application route published by NVIDIA's detail."""
    action = ((detail.get("positionUserActions") or {}).get("applyAction") or {})
    value = action.get("applyUrl")
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https":
        return None
    if not parsed.netloc.endswith(".myworkdayjobs.com"):
        return None
    if "NVIDIAExternalCareerSite" not in parsed.path:
        return None
    return value


def description_sections(html):
    """Keep employer-authored headings and the exact body below each heading."""
    soup = BeautifulSoup(f'<div data-nvidia-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-nvidia-body]")
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    blocks = [node for node in root.contents if getattr(node, "name", None)] if root else []
    sections, current = [], None
    for block in blocks:
        heading = ""
        if block.name in heading_tags:
            heading = block.get_text(" ", strip=True)
        elif block.name in {"b", "strong"}:
            heading = block.get_text(" ", strip=True)
        elif block.name == "p":
            strong = block.find(["strong", "b"], recursive=False)
            full_text = " ".join(block.get_text(" ", strip=True).split())
            strong_text = " ".join(strong.get_text(" ", strip=True).split()) if strong else ""
            if strong_text and strong_text == full_text and len(full_text) <= 120:
                heading = full_text
        if heading:
            heading = " ".join(heading.split())
            if current is not None:
                sections.append(current)
            current = {"heading": heading, "html": "", "text": ""}
            continue
        if current is None:
            current = {"heading": None, "html": "", "text": ""}
        current["html"] += str(block)
    if current is not None:
        sections.append(current)
    else:
        sections.append({"heading": None, "html": "", "text": ""})
    for section in sections:
        if section["html"] and not section["text"]:
            section["text"] = text_html(section["html"])
    return sections


def _amount_value(token):
    token = (token or "").strip().rstrip(",.;")
    if not token:
        return None
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", token):
        value = float(token.replace(",", ""))
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", token):
        value = float(token.replace(".", "").replace(",", "."))
    elif re.fullmatch(r"\d+(?:\.\d+)?", token):
        value = float(token)
    else:
        return None
    return int(value) if value.is_integer() else round(value, 2)


def _salary_sentences(html):
    flat = re.sub(r"\s+", " ", text_html(html or "")).strip()
    if not flat:
        return []
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", flat) if part.strip()]
    merged = []
    for part in parts:
        if merged and re.match(r"^(?:[a-z]|(?:is|are|was|were|has|have|will|can|the|a|an)\b)", part):
            merged[-1] = merged[-1] + " " + part
        else:
            merged.append(part)
    return merged


def extract_salary(html):
    """Retain NVIDIA's exact published pay-range sentences and parsed bounds.

    Amounts are never converted or inferred. A bare ``$`` stays currency=null;
    only explicit ISO codes or unambiguous symbols are retained. Ranges with
    one amount keep ``amount`` rather than inventing a second bound.
    """
    ranges = []
    published = []
    for sentence in _salary_sentences(html):
        if not SALARY_KEY.search(sentence):
            continue
        matches = []
        for match in AMOUNT.finditer(sentence):
            number = match.group("number")
            if not re.search(r"\d{3}", number) and not re.search(r"\.\d", number):
                continue
            value = _amount_value(number)
            if value is None:
                continue
            code = (match.group("pre") or match.group("post") or "").upper()
            if code and code not in CURRENCY_CODES:
                code = ""
            symbol = match.group("symbol") or match.group("symbol2")
            matches.append({"value": value, "code": code or None, "symbol": symbol})
        if not matches:
            continue
        code = next((item["code"] for item in matches if item["code"]), None)
        symbol = next((item["symbol"] for item in matches if item["symbol"]), None)
        if code:
            currency, currency_source = code, "published_code"
        elif symbol and CURRENCY_SYMBOLS.get(symbol):
            currency, currency_source = CURRENCY_SYMBOLS[symbol], "published_symbol"
        else:
            currency, currency_source = None, None
        period = None
        if re.search(r"per\s+year|annually|a\s+year", sentence, re.I):
            period = "year"
        elif re.search(r"per\s+hour|hourly|an\s+hour", sentence, re.I):
            period = "hour"
        entry = {
            "min": matches[0]["value"] if len(matches) >= 2 else None,
            "max": matches[1]["value"] if len(matches) >= 2 else None,
            "amount": matches[0]["value"] if len(matches) == 1 else None,
            "currency": currency,
            "currency_source": currency_source,
            "period": period,
            "published_text": sentence,
            "amounts": [{"value": item["value"], "currency": item["code"]}
                        for item in matches],
            "source": "jobDescription",
        }
        if entry["published_text"] not in published:
            published.append(entry["published_text"])
            ranges.append(entry)
    if not ranges:
        return None
    return {
        "source_field": "jobDescription",
        "published_text": published,
        "ranges": ranges,
        "multiple_published_ranges": len(ranges) > 1,
    }


def parse_sitemap_index(text):
    return [value.strip() for value in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text or "")]


def parse_sitemap_job_ids(text):
    """Extract exact NVIDIA PCSX ids from the public careers sitemap."""
    ids = set()
    for loc in parse_sitemap_index(text):
        parsed = urlsplit(loc)
        if parsed.scheme != "https" or parsed.netloc != "jobs.nvidia.com":
            continue
        match = re.search(r"/careers/job/(\d+)(?:-[^/]*)?$", parsed.path)
        if match:
            ids.add(int(match.group(1)))
    return ids


class Nvidia(Adapter):
    VERIFICATION_REVISION = "nvidia.official-pcsx-listing-sitemap-crosscheck.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.listing_by_id = {}
        self.details = {}
        self.failures = {}

    async def snapshot(self, phase):
        """Read the full unfiltered NVIDIA listing; record page-boundary drift."""
        started = time.perf_counter()
        rows, pages, seen = [], [], set()
        duplicate_ids, issues, count_changes = [], [], []
        total = None
        offset = 0
        while True:
            if not self.page_guard():
                raise ValueError("NVIDIA PCSX listing pagination budget exhausted")
            params = {
                "domain": DOMAIN,
                "query": "",
                "location": "",
                "start": offset,
                "sort_by": "timestamp",
            }
            response = await self.http.get(SEARCH_URL, params=params,
                                            headers={"Referer": LISTING_URL})
            payload = response.json()
            count, batch, page_issues = parse_search_payload(payload, requested_start=offset)
            issues.extend(page_issues)
            if total is None:
                total = count
            elif count != total:
                count_changes.append({"phase": phase, "previous": total,
                                      "current": count, "at_start": offset})
                total = count
            if not batch:
                if total is not None and offset < total:
                    raise ValueError(f"NVIDIA PCSX listing returned an empty page at {offset} of {total}")
                break
            ids = []
            for row in batch:
                identity = row["id"]
                ids.append(identity)
                if identity in seen:
                    duplicate_ids.append(identity)
                    continue
                seen.add(identity)
                rows.append(row)
            pages.append({
                "start": offset,
                "rows": len(batch),
                "ids": ids,
                "advertised_count": count,
                "request_url": str(response.request.url),
                "response_sha256": sha256(response.content).hexdigest(),
                "response_bytes": len(response.content),
            })
            offset += len(batch)
            if offset >= total:
                break
        if total is None or total <= 0:
            raise ValueError(f"NVIDIA PCSX listing advertised no usable jobs: {total!r}")
        seconds = max(0.0, time.perf_counter() - started)
        self.result.timings[phase + "_listing_seconds"] = (
            self.result.timings.get(phase + "_listing_seconds", 0.0) + seconds
        )
        snapshot = {
            "phase": phase,
            "total": total,
            "rows": len(rows),
            "unique_ids": len(seen),
            "duplicate_rows": len(duplicate_ids),
            "duplicate_ids": sorted(set(duplicate_ids)),
            "count_changes": count_changes,
            "issues": list(dict.fromkeys(issues)),
            "pages": pages,
            "seconds": round(seconds, 6),
        }
        self.result.metadata.setdefault("listing_snapshots", []).append(snapshot)
        return rows, total, snapshot

    async def sitemap(self):
        """Read the careers site's public sitemap as a completeness cross-check."""
        started = time.perf_counter()
        robots_sitemaps = []
        try:
            robots = await self.http.get(ROBOTS_URL, headers={"Referer": LISTING_URL})
            robots_sitemaps = re.findall(r"(?im)^\s*Sitemap:\s*(\S+)\s*$", robots.text or "")
        except Exception:
            robots_sitemaps = []
        index = await self.http.get(SITEMAP_INDEX_URL, headers={"Referer": LISTING_URL})
        index_locs = parse_sitemap_index(index.text)
        sitemap_url = next((url for url in index_locs
                            if url == SITEMAP_URL), None)
        if sitemap_url is None:
            raise ValueError("NVIDIA sitemap index no longer publishes sitemap.xml")
        sitemap = await self.http.get(sitemap_url, headers={"Referer": LISTING_URL})
        ids = parse_sitemap_job_ids(sitemap.text)
        metadata = {
            "robots_url": ROBOTS_URL,
            "robots_sitemaps": robots_sitemaps,
            "sitemap_index_url": SITEMAP_INDEX_URL,
            "sitemap_url": sitemap_url,
            "jobs": len(ids),
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        }
        self.result.metadata["sitemap"] = metadata
        return ids, metadata

    async def search_membership(self, detail):
        """Prove a sitemap-only id is still returned by NVIDIA's own PCSX search."""
        identity = detail["id"]
        query = detail["displayJobId"]
        offset = 0
        total = None
        found = False
        pages = []
        while True:
            response = await self.http.get(
                SEARCH_URL,
                params={"domain": DOMAIN, "query": query, "location": "",
                        "start": offset, "sort_by": "timestamp"},
                headers={"Referer": LISTING_URL},
            )
            count, batch, issues = parse_search_payload(response.json(), requested_start=offset)
            if total is None:
                total = count
            elif count != total:
                raise ValueError(f"NVIDIA search count changed for {query!r}")
            pages.append({"start": offset, "rows": len(batch), "issues": issues})
            if any(str(row["id"]) == str(identity) for row in batch):
                found = True
                break
            offset += len(batch)
            if offset >= total or not batch:
                break
        return found, {"query": query, "count": total, "pages": pages}

    async def fetch_details(self, ids, *, source):
        """Fetch exact details with two workers and stop on a challenge/429."""
        started = time.perf_counter()
        semaphore = asyncio.Semaphore(max(1, self.options.detail_concurrency))
        stop = asyncio.Event()
        details, failures, skipped = {}, {}, []
        stats = {
            "total": len(ids), "completed": 0, "fetched": 0, "failed": 0,
            "skipped": 0, "verified_cache_hits": 0, "stopped": False, "source": source,
        }
        self.result.metadata.setdefault("detail_phases", []).append(stats)

        async def one(position_id):
            async with semaphore:
                if stop.is_set():
                    skipped.append(position_id)
                    stats["skipped"] += 1
                    return
                cached = self.options.cached_jobs.get(detail_url(position_id))
                if cached and cached.description_verified and cached.raw.get("detail"):
                    details[position_id] = cached.raw["detail"]
                    stats["verified_cache_hits"] += 1
                    stats["completed"] += 1
                    return
                url = detail_url(position_id)
                try:
                    payload = await self.http.json(
                        DETAIL_URL,
                        params={"position_id": position_id, "domain": DOMAIN, "hl": "en"},
                        headers={"Referer": f"{LISTING_URL}/job/{position_id}"},
                    )
                    details[position_id] = parse_detail_payload(payload, expected_id=position_id)
                    stats["fetched"] += 1
                except FetchError as exc:
                    message = str(exc)
                    failures[position_id] = {"id": position_id, "url": url,
                                             "reason": message, "source": source}
                    stats["failed"] += 1
                    if any(token in message for token in CHALLENGE_TOKENS):
                        stop.set()
                        stats["stopped"] = True
                        stats["challenge"] = message
                except Exception as exc:
                    failures[position_id] = {
                        "id": position_id, "url": url,
                        "reason": f"{type(exc).__name__}: {exc}", "source": source,
                    }
                    stats["failed"] += 1
                finally:
                    stats["completed"] += 1

        await asyncio.gather(*(one(position_id) for position_id in ids))
        seconds = max(0.0, time.perf_counter() - started)
        stats["duration_seconds"] = round(seconds, 6)
        if skipped:
            stats["skipped_ids"] = skipped[:100]
        self.result.timings["detail_phase_seconds"] = (
            self.result.timings.get("detail_phase_seconds", 0.0) + seconds
        )
        if stats["verified_cache_hits"]:
            self.result.metadata["resumed_verified_detail_cache_hits"] = stats["verified_cache_hits"]
            self.result.warnings.append("resumed_verified_details_reused")
        return details, failures, stats

    def job_from_detail(self, detail, listing=None, membership_source="listing"):
        body = detail["jobDescription"]
        sections = description_sections(body)
        responsibilities = [
            section for section in sections
            if section["heading"] and any(token in section["heading"].casefold()
                                          for token in ("what you'll be doing", "what you will do",
                                                        "responsibilities", "key responsibilities"))
        ]
        qualifications = [
            section for section in sections
            if section["heading"] and any(token in section["heading"].casefold()
                                          for token in ("what we need to see", "qualifications",
                                                        "requirements", "what you'll need"))
        ]
        time_types = detail.get("efcustomTextTimeType") or []
        action = ((detail.get("positionUserActions") or {}).get("applyAction") or {})
        row = listing or {
            key: detail.get(key)
            for key in (
                "id", "displayJobId", "name", "locations", "standardizedLocations",
                "postedTs", "stars", "department", "creationTs", "isHot",
                "workLocationOption", "locationFlexibility", "atsJobId", "positionUrl",
            )
        }
        return self.job(
            {
                "listing": row,
                "detail": detail,
                "detail_url": detail_url(detail["id"]),
                "standardized_locations": detail.get("standardizedLocations"),
                "date_fields": {
                    "postedTs": detail.get("postedTs"),
                    "creationTs": detail.get("creationTs"),
                },
                "apply_action": action,
                "description_sections": sections,
                "membership_source": membership_source,
                "salary_source": "jobDescription.published_pay_range_text",
                "company_tab_sections": (detail.get("positionExtraDetails") or {}).get("customContent"),
            },
            title=detail["name"],
            job_id=str(detail["id"]),
            requisition_id=detail["displayJobId"],
            url=detail["publicUrl"],
            apply_url=apply_route(detail),
            locations=detail.get("locations") or row.get("locations") or [],
            department=detail.get("department"),
            employment_type=time_types[0] if time_types else None,
            workplace_type=detail.get("workLocationOption"),
            date_posted=epoch(detail.get("postedTs")),
            date_posted_raw=detail.get("postedTs"),
            salary=extract_salary(body),
            qualifications=qualifications or None,
            responsibilities=responsibilities or None,
            employer="NVIDIA",
            language="en",
            description=body,
            method="nvidia.pcsx.position_details.jobDescription",
            description_url=detail_url(detail["id"]),
        )

    def fallback_job(self, row, reason):
        """Keep an advertised listing row when its exact detail could not be read."""
        job = self.job(
            {"listing": row, "detail_error": reason},
            title=row["name"],
            job_id=str(row["id"]),
            requisition_id=row["displayJobId"],
            url=f"{LISTING_URL}/job/{row['id']}",
            apply_url=None,
            locations=row.get("locations") or [],
            department=row.get("department"),
            workplace_type=row.get("workLocationOption"),
            date_posted=epoch(row.get("postedTs")),
            date_posted_raw=row.get("postedTs"),
            employer="NVIDIA",
            language="en",
            method="nvidia.pcsx.position_details",
            description_url=detail_url(row["id"]),
        )
        job.description_unavailable = True
        job.warnings.append("detail_fetch_failed")
        return job

    async def run(self):
        source_started = time.perf_counter()

        # 1. NVIDIA's own entry and the published search destination.
        entry = await self.http.get(OFFICIAL_ENTRY)
        official = parse_official_entry(entry.text, str(entry.url))
        listing = await self.http.get(LISTING_URL, headers={"Referer": OFFICIAL_ENTRY})
        config = parse_listing_config(listing.text)
        self.result.metadata.update({
            "verification_revision": self.VERIFICATION_REVISION,
            "official_entry_url": OFFICIAL_ENTRY,
            "official_entry_status": entry.status_code,
            "official_entry_final_url": str(entry.url),
            "official_entry": official,
            "listing_url": LISTING_URL,
            "listing_status": listing.status_code,
            "listing_authority": "NVIDIA-branded Eightfold PCSX listing published by nvidia.com",
            "pcsx_config": config,
            "request_scope": "domain=nvidia.com unfiltered PCSX search; no country/seniority filters",
            "search_request": {
                "method": "GET", "path": "/api/pcsx/search",
                "query_shape": {"domain": DOMAIN, "query": "", "location": "",
                                "start": "0-based offset", "sort_by": "timestamp"},
                "page_size": PAGE_SIZE, "filters_applied": False,
            },
            "detail_request": {"method": "GET", "path": "/api/pcsx/position_details",
                               "query_shape": {"position_id": "listing id", "domain": DOMAIN, "hl": "en"}},
            "apply_route": "Workday applyUrl published in positionUserActions.applyAction",
        })

        # 2. Full unfiltered listing.
        initial_rows, initial_total, initial_snapshot = await self.snapshot("initial")
        initial_ids = [row["id"] for row in initial_rows]
        self.listing_by_id = {row["id"]: row for row in initial_rows}
        listing_complete = (
            initial_snapshot["unique_ids"] == initial_total
            and not initial_snapshot["duplicate_ids"]
            and not initial_snapshot["count_changes"]
            and not initial_snapshot["issues"]
        )

        # 3. Public sitemap cross-check.
        try:
            sitemap, sitemap_meta = await self.sitemap()
            sitemap_error = None
        except Exception as exc:
            sitemap, sitemap_meta = set(), {"error": f"{type(exc).__name__}: {exc}"}
            sitemap_error = sitemap_meta["error"]
            self.result.metadata["sitemap"] = sitemap_meta
        sitemap_only = sorted(sitemap - set(initial_ids))

        # 4. Exact detail for every listing id and every sitemap cross-check id.
        candidates = initial_ids + sitemap_only
        self.details, self.failures, detail_stats = await self.fetch_details(
            candidates, source="initial_listing_or_sitemap"
        )

        # 5. Final advertised count from the same unfiltered search.
        final_started = time.perf_counter()
        final_count = initial_total
        final_issues = []
        final_count_error = None
        try:
            final_response = await self.http.get(
                SEARCH_URL,
                params={"domain": DOMAIN, "query": "", "location": "",
                        "start": 0, "sort_by": "timestamp"},
                headers={"Referer": LISTING_URL},
            )
            final_count, final_batch, final_issues = parse_search_payload(
                final_response.json(), requested_start=0
            )
            self.result.metadata["final_search_page"] = {
                "count": final_count, "rows": len(final_batch), "issues": final_issues,
                "request_url": str(final_response.request.url),
            }
        except Exception as exc:
            final_count_error = f"{type(exc).__name__}: {exc}"
            self.result.metadata["final_search_page_error"] = final_count_error
        self.result.timings["final_search_seconds"] = max(0.0, time.perf_counter() - final_started)

        # 6. Sitemap-only rows must still be found by exact requisition search.
        membership_started = time.perf_counter()
        sitemap_active, sitemap_rejected = [], []
        for position_id in sitemap_only:
            detail = self.details.get(position_id)
            if detail is None:
                failure = self.failures.get(position_id, {})
                sitemap_rejected.append({
                    "id": position_id,
                    "reason": failure.get("reason", "no exact detail record"),
                })
                continue
            try:
                found, evidence = await self.search_membership(detail)
            except Exception as exc:
                sitemap_rejected.append({
                    "id": position_id,
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                continue
            detail.setdefault("_nvidia_membership_search", evidence)
            if found:
                sitemap_active.append(position_id)
            else:
                sitemap_rejected.append({
                    "id": position_id,
                    "reason": "displayJobId search did not return exact PCSX id",
                    "evidence": evidence,
                })
        self.result.timings["membership_checks_seconds"] = max(
            0.0, time.perf_counter() - membership_started
        )

        member_ids = initial_ids + sitemap_active
        expected_total = initial_total + len(sitemap_active)
        if len(member_ids) != expected_total:
            self.result.errors.append(
                f"membership_count_mismatch: advertised={expected_total} members={len(member_ids)}"
            )
            listing_complete = False
        if sitemap_active:
            self.result.warnings.append("membership_grew_during_run")
        if final_count_error:
            self.result.errors.append(f"final_search_failed: {final_count_error}")
        elif final_count != expected_total:
            self.result.errors.append(
                f"final_search_count_changed: {initial_total} -> {final_count}"
            )

        # 7. Build records in listing order, then verified sitemap additions.
        for row in initial_rows:
            detail = self.details.get(row["id"])
            if detail is None:
                failure = self.failures.get(row["id"], {})
                reason = failure.get("reason", "detail not fetched (phase stopped)")
                self.result.errors.append(
                    f"Detail {detail_url(row['id'])}: {reason}"
                )
                self.add(self.fallback_job(row, reason))
            else:
                self.add(self.job_from_detail(detail, row))
        for position_id in sitemap_active:
            self.add(self.job_from_detail(
                self.details[position_id], None, membership_source="sitemap_detail_search"
            ))

        self.result.listing_complete = listing_complete
        self.result.expected_total = expected_total
        self.result.metadata["membership_reconciliation"] = {
            "advertised_total": initial_total,
            "expected_total": expected_total,
            "initial_listing_rows": len(initial_rows),
            "initial_unique_ids": len(initial_ids),
            "member_ids": member_ids,
            "member_ids_count": len(member_ids),
            "sitemap_jobs": len(sitemap),
            "sitemap_only": sitemap_only,
            "sitemap_active_additions": sitemap_active,
            "sitemap_rejected": sitemap_rejected,
            "sitemap_error": sitemap_error,
            "detail_failures": self.failures,
            "detail_stats": detail_stats,
            "final_search_count": final_count,
            "final_search_issues": final_issues,
            "final_search_error": final_count_error,
        }
        self.result.metadata["source_collection_wall_seconds"] = round(
            max(0.0, time.perf_counter() - source_started), 6
        )
        return self.result
