"""Microsoft's official careers journey: careers.microsoft.com -> Eightfold PCSX.

The Microsoft AEM home page publishes ``https://apply.careers.microsoft.com/careers``
as its job-search destination. That company-branded PCSX site (``domain=microsoft.com``)
is the authority for the unfiltered, global listing; exact ``position_details``
records are read for the ids that listing exposes. The site's public sitemap is a
completeness cross-check only: a sitemap-only id is retained only when its detail
succeeds and its published requisition id is returned by the same PCSX search.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
import re
import time
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..http import FetchError
from ..models import text_html
from .base import Adapter

OFFICIAL_ENTRY = "https://careers.microsoft.com/"
LISTING_URL = "https://apply.careers.microsoft.com/careers"
DOMAIN = "microsoft.com"
API_BASE = "https://apply.careers.microsoft.com/api/pcsx"
SEARCH_URL = API_BASE + "/search"
DETAIL_URL = API_BASE + "/position_details"
SITEMAP_INDEX_URL = LISTING_URL + "/sitemap_index.xml?domain=microsoft.com"
SITEMAP_URL = LISTING_URL + "/sitemap.xml?domain=microsoft.com"
ROBOTS_URL = "https://apply.careers.microsoft.com/robots.txt"
PAGE_SIZE = 10
CHALLENGE_TOKENS = ("403", "405", "429", "CAPTCHA", "captcha", "Human Verification", "awswaf")

SALARY_KEY = re.compile(
    r"(?:\b(?:base\s+)?pay\s+range\b|\bsalary\s+range\b|"
    r"échelle\s+salariale|rémunération\s+de\s+base)",
    re.I,
)
CURRENCY_CODES = (
    "USD", "CAD", "EUR", "GBP", "AUD", "SGD", "INR", "JPY", "CHF",
    "BRL", "MXN", "NZD", "SEK", "NOK", "DKK", "PLN", "ZAR", "AED",
    "CNY", "TWD", "KRW", "HKD",
)
CURRENCY_SYMBOLS = {"$": None, "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
AMOUNT = re.compile(
    r"(?:(?P<pre>[A-Z]{3})\s*)?"
    r"(?P<symbol>[$€£¥₹])?\s*"
    r"(?P<number>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(?P<symbol2>[$€£¥₹]))?"
    r"\s*(?:(?P<post>[A-Z]{3})\b)?"
)


def epoch(value):
    """Convert PCSX's second-based ``postedTs``; missing/zero stays null."""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"Microsoft postedTs is not a valid epoch second: {value!r}")
    if value == 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def compact(value):
    """Whitespace-free NFC text used for independent rendered-body hashes."""
    value = unicodedata.normalize("NFC", text_html(value or ""))
    return re.sub(r"[\s\ufeff]", "", value)


def fnv1a64(value):
    digest = 14695981039346656037
    for character in value:
        digest ^= ord(character)
        digest = (digest * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def detail_url(position_id):
    return f"{DETAIL_URL}?position_id={position_id}&domain={DOMAIN}&hl=en"


def apply_url(position_id):
    return f"{LISTING_URL}/apply?pid={position_id}"


def parse_official_entry(html, response_url=OFFICIAL_ENTRY):
    """Validate the current Microsoft home page and its published search target."""
    parsed = urlsplit(response_url)
    if parsed.scheme != "https" or parsed.netloc != "careers.microsoft.com":
        raise ValueError("Microsoft official careers entry left the official host")
    if not parsed.path.rstrip("/").endswith(("/home.html", "/home")):
        raise ValueError(f"Microsoft official careers entry redirected unexpectedly: {response_url}")
    soup = BeautifulSoup(html or "", "lxml")
    node = soup.select_one("#careers-eightFoldCareerUrl")
    if node is None:
        raise ValueError("Microsoft official page no longer publishes its Eightfold search target")
    target = node.get_text(" ", strip=True)
    if target != LISTING_URL:
        raise ValueError(f"Microsoft official search target changed: {target!r}")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return {
        "url": OFFICIAL_ENTRY,
        "final_url": response_url,
        "title": title,
        "search_target": target,
        "search_target_selector": "#careers-eightFoldCareerUrl",
    }


def parse_listing_config(html):
    """Validate the Microsoft-branded PCSX configuration that defines scope."""
    soup = BeautifulSoup(html or "", "lxml")
    node = soup.select_one("code#pcsx-data")
    if node is None:
        raise ValueError("Microsoft listing page no longer exposes its PCSX configuration")
    try:
        data = json.loads(node.get_text())
    except ValueError as exc:
        raise ValueError("Microsoft PCSX configuration is not valid JSON") from exc
    if data.get("domain") != DOMAIN:
        raise ValueError(f"Microsoft PCSX domain changed: {data.get('domain')!r}")
    pcs = (data.get("configs") or {}).get("pcsxConfig") or {}
    branding = pcs.get("branding") or {}
    if branding.get("companyName") != "Microsoft":
        raise ValueError("Microsoft PCSX branding is no longer Microsoft")
    search = pcs.get("searchConfig") or {}
    if search.get("membershipFq", "") != "":
        raise ValueError("Microsoft PCSX listing now has a membership filter")
    if search.get("excludePrivatePositions") is not True:
        raise ValueError("Microsoft PCSX private-position exclusion changed")
    base = search.get("basePositionFq") or ""
    folded = base.casefold()
    for token in ("position.system_id:successfactors", "is_externally_posted:1",
                  "position.ats_data.status:open", "position.type:ats"):
        if token not in folded:
            raise ValueError(f"Microsoft PCSX base listing filter changed; missing {token!r}")
    tabs = (pcs.get("positionDetailsConfig") or {}).get("tabsDisplayConfig") or []
    jd = next((tab for tab in tabs if tab.get("id") == "jd"), None)
    if jd is None or jd.get("sections") != ["position_fields", "jd", "position_custom_content"]:
        raise ValueError("Microsoft PCSX job-description sections changed")
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
    }


def listing_row(row):
    """Validate one Microsoft PCSX search row without inventing missing fields."""
    if not isinstance(row, dict):
        raise ValueError("Microsoft listing row is not an object")
    identity = row.get("id")
    if isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0:
        raise ValueError(f"Microsoft listing row has no valid PCSX id: {row!r}")
    display = row.get("displayJobId")
    if not isinstance(display, str) or not display.strip():
        raise ValueError(f"Microsoft listing row has no displayJobId: {identity}")
    if row.get("atsJobId") != display:
        raise ValueError(f"Microsoft listing row atsJobId differs: {identity}")
    if not isinstance(row.get("name"), str) or not row["name"].strip():
        raise ValueError(f"Microsoft listing row has no title: {identity}")
    if row.get("positionUrl") != f"/careers/job/{identity}":
        raise ValueError(f"Microsoft listing row positionUrl differs: {identity}")
    if row.get("postedTs") not in (None, "", 0) and epoch(row.get("postedTs")) is None:
        raise ValueError(f"Microsoft listing row has invalid postedTs: {identity}")
    for key in ("locations", "standardizedLocations"):
        if row.get(key) is not None and not isinstance(row.get(key), list):
            raise ValueError(f"Microsoft listing row {key} is not a list: {identity}")
    return row


def parse_search_payload(payload, *, requested_start):
    """Validate an unfiltered Microsoft PCSX search page and return its rows."""
    if not isinstance(payload, dict) or payload.get("status") != 200:
        raise ValueError(f"Microsoft PCSX search failed: {payload!r}")
    error = payload.get("error") or {}
    if error.get("message"):
        raise ValueError(f"Microsoft PCSX search error: {error}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Microsoft PCSX search response has no data object")
    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"Microsoft PCSX search count is invalid: {count!r}")
    if data.get("appliedFilters") not in ({}, None):
        raise ValueError(f"Microsoft PCSX search unexpectedly has filters: {data.get('appliedFilters')!r}")
    if data.get("sortBy") not in (None, "timestamp"):
        raise ValueError(f"Microsoft PCSX search sort changed: {data.get('sortBy')!r}")
    positions = data.get("positions")
    if not isinstance(positions, list):
        raise ValueError("Microsoft PCSX search positions is not a list")
    if len(positions) > PAGE_SIZE:
        raise ValueError(f"Microsoft PCSX search returned {len(positions)} rows for a {PAGE_SIZE}-row pager")
    seen = set()
    for row in positions:
        listing_row(row)
        identity = row["id"]
        if identity in seen:
            raise ValueError(f"Microsoft PCSX search page repeats id {identity}")
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
        raise ValueError(f"Microsoft PCSX detail failed: {payload!r}")
    error = payload.get("error") or {}
    if error.get("message"):
        raise ValueError(f"Microsoft PCSX detail error: {error}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Microsoft PCSX detail response has no data object")
    identity = data.get("id")
    if isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0:
        raise ValueError("Microsoft PCSX detail has no valid posting id")
    if str(identity) != str(expected_id):
        raise ValueError(f"Microsoft PCSX detail id differs: {identity} != {expected_id}")
    display = data.get("displayJobId")
    if not isinstance(display, str) or not display.strip():
        raise ValueError(f"Microsoft PCSX detail has no displayJobId: {identity}")
    if data.get("atsJobId") != display:
        raise ValueError(f"Microsoft PCSX detail atsJobId differs: {identity}")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError(f"Microsoft PCSX detail has no title: {identity}")
    if data.get("positionUrl") != f"/careers/job/{identity}":
        raise ValueError(f"Microsoft PCSX detail positionUrl differs: {identity}")
    if data.get("publicUrl") != f"{LISTING_URL}/job/{identity}":
        raise ValueError(f"Microsoft PCSX detail publicUrl differs: {identity}")
    body = data.get("jobDescription")
    if not isinstance(body, str) or len(text_html(body).strip()) < 100:
        raise ValueError(f"Microsoft PCSX detail has no complete jobDescription: {identity}")
    if data.get("postedTs") not in (None, "", 0) and epoch(data.get("postedTs")) is None:
        raise ValueError(f"Microsoft PCSX detail has invalid postedTs: {identity}")
    if data.get("locations") is not None and not isinstance(data.get("locations"), list):
        raise ValueError(f"Microsoft PCSX detail locations is not a list: {identity}")
    extra = data.get("positionExtraDetails")
    if extra is not None and not isinstance(extra, dict):
        raise ValueError(f"Microsoft PCSX detail positionExtraDetails is not an object: {identity}")
    custom = (extra or {}).get("customContent")
    if custom is not None and not isinstance(custom, list):
        raise ValueError(f"Microsoft PCSX detail customContent is not a list: {identity}")
    return data


def description_html(detail):
    """Full job-description tab: role body plus configured custom sections."""
    parts = [detail["jobDescription"]]
    for item in (detail.get("positionExtraDetails") or {}).get("customContent") or []:
        if not isinstance(item, dict):
            raise ValueError("Microsoft position custom content item is not an object")
        body = item.get("body")
        if not isinstance(body, str) or not body.strip():
            continue
        title = item.get("title")
        if title:
            parts.append(f'<div class="microsoft-position-custom-title">{escape(str(title))}</div>')
        parts.append(f'<div class="microsoft-position-custom-body">{body}</div>')
    return "\n".join(parts)


def description_sections(html):
    """Keep employer-authored headings and the exact body below each heading."""
    soup = BeautifulSoup(f'<div data-microsoft-body="true">{html or ""}</div>', "lxml")
    root = soup.select_one("[data-microsoft-body]")
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
            strong = block.find("strong", recursive=False)
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
    """Retain Microsoft's exact published pay-range sentences and parsed bounds.

    Amounts/currencies are never converted or inferred from a location. A bare
    ``$`` stays currency=null; only explicit ISO codes or unambiguous symbols
    are retained. Ranges with one amount keep ``amount`` rather than inventing
    a second bound.
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
        if re.search(r"per\s+year|annually|a\s+year|par\s+année", sentence, re.I):
            period = "year"
        elif re.search(r"per\s+hour|hourly|an\s+hour|par\s+heure", sentence, re.I):
            period = "hour"
        entry = {
            "min": matches[0]["value"] if len(matches) >= 2 else None,
            "max": matches[1]["value"] if len(matches) >= 2 else None,
            "amount": matches[0]["value"] if len(matches) == 1 else None,
            "currency": currency,
            "currency_source": currency_source,
            "period": period,
            "published_text": sentence,
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
    """Extract exact Microsoft PCSX job ids from the public careers sitemap."""
    values = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text or "")
    if not values:
        values = [line.strip() for line in (text or "").splitlines() if line.strip()]
    ids = []
    for line in values:
        if line in (LISTING_URL, LISTING_URL + "?domain=" + DOMAIN):
            continue
        match = re.fullmatch(
            r"https://apply\.careers\.microsoft\.com/careers/job/(\d+)-[^?\s]+(?:\?domain=microsoft\.com)?",
            line,
        )
        if match is None:
            raise ValueError(f"Microsoft sitemap contains an unexpected URL: {line}")
        ids.append(int(match.group(1)))
    if len(ids) != len(set(ids)):
        raise ValueError("Microsoft sitemap repeats a job id")
    return ids


class Microsoft(Adapter):
    VERIFICATION_REVISION = "microsoft.official-pcsx-listing-sitemap-crosscheck.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.listing_by_id = {}
        self.failures = {}
        self._detail_progress = None

    async def snapshot(self, phase):
        """Read the full unfiltered Microsoft listing; record page-boundary drift."""
        started = time.perf_counter()
        rows, pages, seen = [], [], set()
        duplicate_ids, issues, count_changes = [], [], []
        total = None
        offset = 0
        while True:
            if not self.page_guard():
                raise ValueError("Microsoft PCSX listing pagination budget exhausted")
            params = {
                "domain": DOMAIN,
                "query": "",
                "location": "",
                "start": offset,
                "sort_by": "timestamp",
            }
            response = await self.http.get(SEARCH_URL, params=params, headers={"Referer": LISTING_URL})
            payload = response.json()
            count, batch, page_issues = parse_search_payload(payload, requested_start=offset)
            issues.extend(page_issues)
            if total is None:
                total = count
            elif count != total:
                count_changes.append({"phase": phase, "previous": total, "current": count, "at_start": offset})
                total = count
            if not batch:
                if total is not None and offset < total:
                    raise ValueError(f"Microsoft PCSX listing returned an empty page at {offset} of {total}")
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
            raise ValueError(f"Microsoft PCSX listing advertised no usable jobs: {total!r}")
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
        sitemap_url = next((url for url in index_locs if url.endswith("sitemap.xml?domain=" + DOMAIN)), None)
        if sitemap_url is None:
            raise ValueError("Microsoft sitemap index no longer publishes sitemap.xml")
        parsed = urlsplit(sitemap_url)
        if parsed.scheme != "https" or parsed.netloc != "apply.careers.microsoft.com" or parsed.path != "/careers/sitemap.xml":
            raise ValueError(f"Microsoft sitemap index points outside its careers route: {sitemap_url}")
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
        return set(ids), metadata

    async def search_membership(self, detail):
        """Prove a sitemap-only id is still returned by Microsoft's own PCSX search."""
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
                raise ValueError(f"Microsoft search count changed for {query!r}")
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
            "skipped": 0, "stopped": False, "source": source,
        }
        self.result.metadata.setdefault("detail_phases", []).append(stats)

        async def one(position_id):
            async with semaphore:
                if stop.is_set():
                    skipped.append(position_id)
                    stats["skipped"] += 1
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
                    failures[position_id] = {"id": position_id, "url": url, "reason": message, "source": source}
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
                    if self._detail_progress and stats["completed"] % 25 == 0:
                        self._detail_progress(stats)

        await asyncio.gather(*(one(position_id) for position_id in ids))
        seconds = max(0.0, time.perf_counter() - started)
        stats["duration_seconds"] = round(seconds, 6)
        if skipped:
            stats["skipped_ids"] = skipped[:100]
        self.result.timings["detail_phase_seconds"] = (
            self.result.timings.get("detail_phase_seconds", 0.0) + seconds
        )
        return details, failures, stats

    def job_from_detail(self, detail, listing=None):
        body = description_html(detail)
        sections = description_sections(body)
        responsibilities = [
            section for section in sections
            if section["heading"] and "responsibilit" in section["heading"].casefold()
        ]
        qualifications = [
            section for section in sections
            if section["heading"] and "qualification" in section["heading"].casefold()
        ]
        work_site = detail.get("efcustomTextWorkSite") or []
        travel = detail.get("efcustomTextRequiredTravel") or []
        profession = detail.get("efcustomTextCurrentProfession") or []
        discipline = detail.get("efcustomTextTaDisciplineName") or []
        role_type = detail.get("efcustomTextRoletype") or []
        employment_type = detail.get("efcustomTextEmploymentType") or []
        apply_action = ((detail.get("positionUserActions") or {}).get("applyAction") or {})
        row = listing or {
            key: detail.get(key)
            for key in (
                "id", "displayJobId", "name", "locations", "standardizedLocations",
                "postedTs", "stars", "department", "creationTs", "isHot",
                "workLocationOption", "locationFlexibility", "atsJobId", "positionUrl",
            )
        }
        job = self.job(
            {
                "listing": row,
                "detail": detail,
                "detail_url": detail_url(detail["id"]),
                "standardized_locations": detail.get("standardizedLocations"),
                "date_fields": {
                    "postedTs": detail.get("postedTs"),
                    "creationTs": detail.get("creationTs"),
                },
                "apply_action": apply_action,
                "role_fields": {
                    "work_site": work_site[0] if work_site else None,
                    "required_travel": travel[0] if travel else None,
                    "profession": profession[0] if profession else None,
                    "discipline": discipline[0] if discipline else None,
                    "role_type": role_type[0] if role_type else None,
                    "employment_type": employment_type[0] if employment_type else None,
                },
                "description_sections": sections,
                "membership_source": "listing" if listing else "sitemap_detail_search",
                "salary_source": "jobDescription.published_pay_range_text",
            },
            title=detail["name"],
            job_id=str(detail["id"]),
            requisition_id=detail["displayJobId"],
            url=detail["publicUrl"],
            apply_url=apply_url(detail["id"]),
            locations=detail.get("locations") or row.get("locations") or [],
            department=detail.get("department"),
            employment_type=employment_type[0] if employment_type else None,
            workplace_type=work_site[0] if work_site else None,
            date_posted=epoch(detail.get("postedTs")),
            date_posted_raw=detail.get("postedTs"),
            salary=extract_salary(body),
            qualifications=qualifications or None,
            responsibilities=responsibilities or None,
            employer="Microsoft",
            language="en",
            description=body,
            method="microsoft.pcsx.position_details.jobDescription+position_custom_content",
            description_url=detail_url(detail["id"]),
        )
        return job

    def fallback_job(self, row, reason):
        """Keep an advertised listing row when its exact detail could not be read."""
        job = self.job(
            {"listing": row, "detail_error": reason},
            title=row["name"],
            job_id=str(row["id"]),
            requisition_id=row["displayJobId"],
            url=f"{LISTING_URL}/job/{row['id']}",
            apply_url=apply_url(row["id"]),
            locations=row.get("locations") or [],
            department=row.get("department"),
            workplace_type=row.get("workLocationOption"),
            date_posted=epoch(row.get("postedTs")),
            date_posted_raw=row.get("postedTs"),
            employer="Microsoft",
            language="en",
            method="microsoft.pcsx.position_details",
            description_url=detail_url(row["id"]),
        )
        job.description_unavailable = True
        job.warnings.append("detail_fetch_failed")
        return job

    async def run(self):
        source_started = time.perf_counter()

        # 1. Microsoft's own entry and the published search destination.
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
            "listing_authority": "Microsoft-branded Eightfold PCSX listing published by careers.microsoft.com",
            "pcsx_config": config,
            "request_scope": "domain=microsoft.com unfiltered PCSX search; no country/seniority filters",
            "search_request": {
                "method": "GET", "path": "/api/pcsx/search",
                "query_shape": {"domain": DOMAIN, "query": "", "location": "",
                                "start": "0-based offset", "sort_by": "timestamp"},
                "page_size": PAGE_SIZE, "filters_applied": False,
            },
            "detail_request": {"method": "GET", "path": "/api/pcsx/position_details",
                               "query_shape": {"position_id": "listing id", "domain": DOMAIN, "hl": "en"}},
            "apply_route": f"{LISTING_URL}/apply?pid=<PCSX id>",
        })

        # 2. Full unfiltered listing.
        initial_rows, initial_total, initial_snapshot = await self.snapshot("initial")
        initial_ids = [row["id"] for row in initial_rows]
        self.listing_by_id = {row["id"]: row for row in initial_rows}

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
        details, failures, detail_stats = await self.fetch_details(
            candidates, source="initial_listing_or_sitemap"
        )

        # 5. Re-read the public sitemap to catch additions during the detail phase.
        try:
            final_sitemap, final_sitemap_meta = await self.sitemap()
            final_sitemap_error = None
        except Exception as exc:
            final_sitemap, final_sitemap_meta = sitemap, {"error": f"{type(exc).__name__}: {exc}"}
            final_sitemap_error = final_sitemap_meta["error"]
            self.result.metadata["final_sitemap"] = final_sitemap_meta
        new_sitemap_ids = sorted(final_sitemap - sitemap - set(initial_ids))
        if new_sitemap_ids:
            more_details, more_failures, more_stats = await self.fetch_details(
                new_sitemap_ids, source="final_sitemap"
            )
            details.update(more_details)
            failures.update(more_failures)

        # 6. Final advertised count from the same unfiltered search.
        final_count = initial_total
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

        # 7. Sitemap-only rows must still be found by exact requisition search.
        sitemap_only_all = sorted(set(sitemap_only) | set(new_sitemap_ids))
        sitemap_active, sitemap_rejected = [], []
        for position_id in sitemap_only_all:
            detail = details.get(position_id)
            if detail is None:
                failure = failures.get(position_id, {})
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
            detail.setdefault("_microsoft_membership_search", evidence)
            if found:
                sitemap_active.append(position_id)
            else:
                sitemap_rejected.append({
                    "id": position_id,
                    "reason": "displayJobId search did not return exact PCSX id",
                    "evidence": evidence,
                })

        # 8. Initial ids missing from the final sitemap stay only if exact search still finds them.
        removed_during_run = []
        kept_initial_only = []
        if final_sitemap:
            for position_id in sorted(set(initial_ids) - final_sitemap):
                detail = details.get(position_id)
                if detail is None:
                    kept_initial_only.append(position_id)
                    continue
                try:
                    found, evidence = await self.search_membership(detail)
                except Exception as exc:
                    removed_during_run.append({
                        "id": position_id,
                        "reason": f"final membership check failed: {type(exc).__name__}: {exc}",
                    })
                    continue
                if found:
                    detail.setdefault("_microsoft_membership_search", evidence)
                    kept_initial_only.append(position_id)
                else:
                    removed_during_run.append({
                        "id": position_id,
                        "reason": "not in final sitemap or exact search",
                        "evidence": evidence,
                    })
        removed_ids = {item["id"] for item in removed_during_run}

        # 9. Build final records in published listing order.
        members = []
        for position_id in initial_ids + sitemap_active:
            if position_id in removed_ids or position_id in members:
                continue
            members.append(position_id)
        jobs = {}
        listing_detail_failures = []
        for position_id in members:
            detail = details.get(position_id)
            if detail is not None:
                jobs[position_id] = self.job_from_detail(detail, self.listing_by_id.get(position_id))
            elif position_id in self.listing_by_id:
                reason = failures.get(position_id, {}).get("reason", "detail unavailable")
                jobs[position_id] = self.fallback_job(self.listing_by_id[position_id], reason)
                self.failures[position_id] = failures.get(
                    position_id, {"id": position_id, "reason": reason}
                )
                listing_detail_failures.append(position_id)
                self.result.errors.append(f"Detail {detail_url(position_id)}: {reason}")
            else:
                self.failures[position_id] = failures.get(
                    position_id, {"id": position_id, "reason": "membership detail unavailable"}
                )
        for job in jobs.values():
            self.add(job)

        # 10. Reconcile pages, ids, counts and exact details.
        collected = len(self.result.jobs)
        expected = final_count if final_count is not None else initial_total
        self.result.expected_total = expected
        membership_ok = collected == expected and not listing_detail_failures and not removed_during_run
        self.result.listing_complete = bool(membership_ok)
        if not membership_ok:
            self.result.errors.append(
                f"Microsoft membership mismatch: advertised {expected}, collected {collected}"
            )
        if initial_total != expected:
            self.result.warnings.append("listing_count_changed_during_run")
        if initial_snapshot["duplicate_ids"]:
            self.result.warnings.append("listing_page_boundary_duplicates")
        if sitemap_only_all:
            self.result.warnings.append("sitemap_crosscheck_rows_reviewed")
        if removed_during_run:
            self.result.warnings.append("posting_removed_during_run")
        if sitemap_error:
            self.result.warnings.append("sitemap_crosscheck_unavailable")
        if final_sitemap_error:
            self.result.warnings.append("final_sitemap_crosscheck_unavailable")
        if final_count_error:
            self.result.warnings.append("final_listing_count_unavailable")

        self.result.metadata["membership_reconciliation"] = {
            "initial_advertised": initial_total,
            "final_advertised": final_count,
            "initial_listing_rows": initial_snapshot["rows"],
            "initial_listing_unique": initial_snapshot["unique_ids"],
            "initial_duplicates": initial_snapshot["duplicate_ids"],
            "initial_count_changes": initial_snapshot["count_changes"],
            "initial_page_issues": initial_snapshot["issues"],
            "sitemap_jobs": len(sitemap),
            "sitemap_only": sitemap_only,
            "sitemap_only_count": len(sitemap_only),
            "final_sitemap_jobs": len(final_sitemap),
            "new_sitemap_ids": new_sitemap_ids,
            "sitemap_active_recovered": sorted(sitemap_active),
            "sitemap_rejected": sitemap_rejected,
            "kept_initial_only": kept_initial_only,
            "removed_during_run": removed_during_run,
            "final_search_count": final_count,
            "final_search_error": final_count_error,
            "final_sitemap_error": final_sitemap_error,
            "members": len(members),
            "member_ids": [str(value) for value in members],
            "initial_ids": [str(value) for value in initial_ids],
            "collected": collected,
            "listing_detail_failures": listing_detail_failures,
            "detail_failures": {str(key): value for key, value in sorted(self.failures.items())},
            "listing_sitemap_reconciled": membership_ok,
        }
        self.result.metadata["detail_stats"] = {
            "requested_initial": len(candidates),
            "requested_final_sitemap": len(new_sitemap_ids),
            "succeeded": len(details),
            "failed": len(failures),
            "initial_phase": detail_stats,
        }
        self.result.metadata["source_collection_wall_seconds"] = round(
            max(0.0, time.perf_counter() - source_started), 6
        )
        return self.result
