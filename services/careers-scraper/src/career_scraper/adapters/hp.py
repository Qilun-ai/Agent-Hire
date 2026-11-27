"""HP's official careers journey: jobs.hp.com -> apply.hp.com PCSX listing.

Membership is HP's own published Eightfold PCSX listing (domain ``hp.com``,
company name ``HP``) reached from the official ``jobs.hp.com`` entry. The
listing is unfiltered and global; details are the exact PCSX
``position_details`` records for the ids exposed by that listing.

The PCSX offset pager is sorted by day-granularity ``postedTs`` and can return
duplicate rows at page boundaries while missing others when postings move. The
same-host ``/careers/sitemap.txt`` published by HP's careers site (and linked
from its robots.txt) is used only as a completeness cross-check. A sitemap-only
id is retained only after its exact detail succeeds and an unfiltered search by
its published requisition id returns that same PCSX id.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html import escape
import json
import re
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..http import FetchError
from ..models import text_html
from .base import Adapter

OFFICIAL_ENTRY = "https://jobs.hp.com/"
LISTING_URL = "https://apply.hp.com/careers"
DOMAIN = "hp.com"
API_BASE = "https://apply.hp.com/api/pcsx"
SEARCH_URL = API_BASE + "/search"
DETAIL_URL = API_BASE + "/position_details"
ROBOTS_URL = "https://apply.hp.com/robots.txt"
SITEMAP_INDEX_URL = "https://apply.hp.com/careers/sitemap_index.xml?domain=hp.com"
SITEMAP_URL = "https://apply.hp.com/careers/sitemap.txt?domain=hp.com"
PAGE_SIZE = 10
APPLY_HOST = "hp.wd5.myworkdayjobs.com"
BASE_POSITION_FQ = (
    "(position.type:ats AND -position.ats_data.restrict_access:true "
    "AND is_externally_posted:true)"
)
JOB_DETAIL_SECTIONS = ["position_fields", "jd", "position_custom_content"]
CHALLENGE_TOKENS = ("403", "405", "429", "CAPTCHA", "captcha", "Human Verification", "awswaf")


def epoch(value):
    """Convert PCSX's second-based posting timestamp; missing/zero stays null."""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"HP postedTs is not a valid epoch second: {value!r}")
    if value == 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def compact(value):
    """Whitespace-free NFC text used for the independent rendered-body hash."""
    value = unicodedata.normalize("NFC", text_html(value or ""))
    return re.sub(r"[\s\ufeff]", "", value)


def fnv1a64(value):
    digest = 14695981039346656037
    for character in value:
        digest ^= ord(character)
        digest = (digest * 1099511628211) & ((1 << 64) - 1)
    return f"{digest:016x}"


def official_entry_links(html):
    """Return exact apply.hp.com/careers calls to action published by jobs.hp.com."""
    soup = BeautifulSoup(html or "", "lxml")
    links = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(OFFICIAL_ENTRY, href)
        parsed = urlsplit(absolute)
        if parsed.hostname == "apply.hp.com" and parsed.path.rstrip("/") == "/careers":
            links.add(absolute)
    return sorted(links)


def parse_pcsx_config(html):
    """Validate the HP-branded PCSX configuration that defines listing scope."""
    node = BeautifulSoup(html or "", "lxml").select_one("code#pcsx-data")
    if node is None:
        raise ValueError("HP listing page no longer exposes its PCSX configuration")
    try:
        data = json.loads(node.get_text())
    except ValueError as exc:
        raise ValueError("HP PCSX configuration is not valid JSON") from exc
    if data.get("domain") != DOMAIN:
        raise ValueError(f"HP PCSX domain changed: {data.get('domain')!r}")
    pcs = (data.get("configs") or {}).get("pcsxConfig") or {}
    if (pcs.get("branding") or {}).get("companyName") != "HP":
        raise ValueError("HP PCSX branding is no longer HP")
    search = pcs.get("searchConfig") or {}
    if search.get("membershipFq", "") != "":
        raise ValueError("HP PCSX listing now has a membership filter")
    if search.get("basePositionFq") != BASE_POSITION_FQ:
        raise ValueError("HP PCSX base listing filters changed")
    if search.get("excludePrivatePositions") is not True:
        raise ValueError("HP PCSX private-position exclusion changed")
    tabs = (pcs.get("positionDetailsConfig") or {}).get("tabsDisplayConfig") or []
    if len(tabs) != 1 or tabs[0].get("sections") != JOB_DETAIL_SECTIONS:
        raise ValueError("HP PCSX job-description sections changed")
    return {
        "domain": data["domain"],
        "companyName": pcs["branding"]["companyName"],
        "searchConfig": {
            "basePositionFq": search["basePositionFq"],
            "membershipFq": search.get("membershipFq", ""),
            "excludePrivatePositions": search.get("excludePrivatePositions"),
            "smartFilters": search.get("smartFilters", []),
            "allFilters": search.get("allFilters", []),
            "includeRemoteDefault": search.get("includeRemoteDefault"),
        },
        "positionDetailsConfig": pcs["positionDetailsConfig"],
        "positionCardsConfig": pcs.get("positionCardsConfig"),
    }


def parse_robots_sitemaps(text):
    return re.findall(r"(?im)^\s*Sitemap:\s*(\S+)\s*$", text or "")


def parse_sitemap_index(text):
    return [value.strip() for value in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text or "")]


def parse_sitemap_job_ids(text):
    """Extract the exact HP job ids from the published careers sitemap text."""
    ids = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in (LISTING_URL, LISTING_URL + "?domain=" + DOMAIN):
            continue
        match = re.fullmatch(
            r"https://apply\.hp\.com/careers/job/(\d+)-[^?\s]+(?:\?domain=hp\.com)?",
            line,
        )
        if match is None:
            raise ValueError(f"HP sitemap contains an unexpected URL: {line}")
        ids.append(int(match.group(1)))
    if len(ids) != len(set(ids)):
        raise ValueError("HP sitemap repeats a job id")
    return ids


def listing_row(row, page_start=None):
    """Validate one HP PCSX search row without inventing missing fields."""
    if not isinstance(row, dict):
        raise ValueError("HP listing row is not an object")
    identity = row.get("id")
    if isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0:
        raise ValueError(f"HP listing row has no valid PCSX id: {row!r}")
    display = row.get("displayJobId")
    if not isinstance(display, str) or not display.strip():
        raise ValueError(f"HP listing row has no displayJobId: {identity}")
    if row.get("atsJobId") != display:
        raise ValueError(f"HP listing row atsJobId differs: {identity}")
    if not isinstance(row.get("name"), str) or not row["name"].strip():
        raise ValueError(f"HP listing row has no title: {identity}")
    if row.get("positionUrl") != f"/careers/job/{identity}":
        raise ValueError(f"HP listing row positionUrl differs: {identity}")
    if row.get("postedTs") not in (None, "", 0) and epoch(row.get("postedTs")) is None:
        raise ValueError(f"HP listing row has invalid postedTs: {identity}")
    if row.get("locations") is not None and not isinstance(row.get("locations"), list):
        raise ValueError(f"HP listing row locations is not a list: {identity}")
    return row


def parse_listing_payload(payload, *, page_start=None):
    if not isinstance(payload, dict) or payload.get("status") != 200:
        raise ValueError(f"HP PCSX listing failed: {payload!r}")
    error = payload.get("error") or {}
    if error.get("message"):
        raise ValueError(f"HP PCSX listing error: {error}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("HP PCSX listing response has no data object")
    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"HP PCSX listing count is invalid: {count!r}")
    if data.get("appliedFilters") not in ({}, None):
        raise ValueError(f"HP PCSX listing unexpectedly has filters: {data.get('appliedFilters')!r}")
    if data.get("sortBy") not in (None, "timestamp"):
        raise ValueError(f"HP PCSX listing sort changed: {data.get('sortBy')!r}")
    positions = data.get("positions")
    if not isinstance(positions, list):
        raise ValueError("HP PCSX listing positions is not a list")
    for row in positions:
        listing_row(row, page_start=page_start)
    return count, positions


def parse_detail_payload(payload, expected_id=None):
    if not isinstance(payload, dict) or payload.get("status") != 200:
        raise ValueError(f"HP PCSX detail failed: {payload!r}")
    error = payload.get("error") or {}
    if error.get("message"):
        raise ValueError(f"HP PCSX detail error: {error}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("HP PCSX detail response has no data object")
    identity = data.get("id")
    if isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0:
        raise ValueError("HP PCSX detail has no valid posting id")
    if expected_id is not None and str(identity) != str(expected_id):
        raise ValueError(f"HP PCSX detail id differs: {identity} != {expected_id}")
    display = data.get("displayJobId")
    if not isinstance(display, str) or not display.strip():
        raise ValueError(f"HP PCSX detail has no displayJobId: {identity}")
    if data.get("atsJobId") != display:
        raise ValueError(f"HP PCSX detail atsJobId differs: {identity}")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError(f"HP PCSX detail has no title: {identity}")
    if data.get("positionUrl") != f"/careers/job/{identity}":
        raise ValueError(f"HP PCSX detail positionUrl differs: {identity}")
    if data.get("publicUrl") != f"{LISTING_URL}/job/{identity}":
        raise ValueError(f"HP PCSX detail publicUrl differs: {identity}")
    body = data.get("jobDescription")
    if not isinstance(body, str) or len(text_html(body).strip()) < 100:
        raise ValueError(f"HP PCSX detail has no complete jobDescription: {identity}")
    extra = data.get("positionExtraDetails")
    if extra is not None and not isinstance(extra, dict):
        raise ValueError(f"HP PCSX detail positionExtraDetails is not an object: {identity}")
    custom = (extra or {}).get("customContent")
    if custom is not None and not isinstance(custom, list):
        raise ValueError(f"HP PCSX detail customContent is not a list: {identity}")
    if data.get("postedTs") not in (None, "", 0) and epoch(data.get("postedTs")) is None:
        raise ValueError(f"HP PCSX detail has invalid postedTs: {identity}")
    return data


def description_html(detail):
    """Full Job description tab: role body plus its configured custom sections."""
    parts = [detail["jobDescription"]]
    for item in (detail.get("positionExtraDetails") or {}).get("customContent") or []:
        if not isinstance(item, dict):
            raise ValueError("HP position custom content item is not an object")
        body = item.get("body")
        if not isinstance(body, str) or not body.strip():
            continue
        title = item.get("title")
        if title:
            parts.append(f'<div class="hp-position-custom-title">{escape(str(title))}</div>')
        parts.append(f'<div class="hp-position-custom-body">{body}</div>')
    return "\n".join(parts)


def description_headings(html):
    soup = BeautifulSoup(html or "", "lxml")
    headings = []
    for node in soup.select("b, strong"):
        value = " ".join(node.get_text(" ", strip=True).split())
        if value and value not in headings:
            headings.append(value)
    return headings


def published_labels(html):
    """Retain HP's published Job/Schedule/Shift/Travel/Relocation labels."""
    lines = text_html(html).splitlines()
    labels = {}
    for index, line in enumerate(lines[:-1]):
        key = re.sub(r"\s*-\s*$", "", line.strip())
        if key not in {"Job", "Schedule", "Shift", "Travel", "Relocation"}:
            continue
        value = lines[index + 1].strip()
        if not value or value in {"Equal Opportunity Employer (EEO)"}:
            continue
        if re.fullmatch(r"(Job|Schedule|Shift|Travel|Relocation)\s*-", value):
            continue
        labels.setdefault(key, value)
    return labels


SALARY_CURRENCIES = (
    "USD", "CAD", "EUR", "GBP", "AUD", "SGD", "INR", "PLN", "CHF",
    "JPY", "BRL", "MXN",
)
SALARY_SYMBOLS = {"$": None, "€": "EUR", "£": "GBP"}
SALARY_LABEL = (
    r"(?P<label>"
    r"The\s+(?:typical\s+)?(?:base\s+)?(?:pay|salary)\s+range\s+for\s+this\s+(?:role|position)\s+is"
    r"|The\s+on[- ]target\s+earnings(?:\s*\(OTE\))?\s+range\s+for\s+this\s+role\s+is"
    r"|Salary\s+Range\s*:"
    r"|Salary\s*:"
    r"|Hourly\s+Pay\s+Range\s*:"
    r"|Pay\s+Range\s*:"
    r")"
)
SALARY_CURRENCY_CODES = "|".join(SALARY_CURRENCIES)
SALARY_PERIOD = (
    r"annually|annual|per\s+year|yearly|per\s+annum|a\s+year"
    r"|per\s+hour|hourly|an\s+hour|/\s*hour|/hr"
)
SALARY_PUBLISHED = re.compile(
    SALARY_LABEL
    + r"\s*(?:put\s+)?"
    + r"(?:(?P<min_symbol>[$€£])\s*)?(?P<min_number>\d[\d.,]*)(?:\s*(?P<min_short>[Kk]))?"
    + rf"\s*(?P<min_code>{SALARY_CURRENCY_CODES})?"
    + r"\s*(?:up\s+to|to|through|[-–—])\s*"
    + r"(?:(?P<max_symbol>[$€£])\s*)?(?P<max_number>\d[\d.,]*)(?:\s*(?P<max_short>[Kk]))?"
    + rf"\s*(?P<max_code>{SALARY_CURRENCY_CODES})?"
    + rf"(?:\s*(?:{SALARY_CURRENCY_CODES}))?"
    + rf"\s*(?P<period>{SALARY_PERIOD})?"
    + r"\s*[,.;]?"
    + r"\s*(?P<mix>with\s+an?\s+\d{2}\s*%?\s*/\s*\d{2}\s*%?\s*\(salary\s*/\s*incentive\)\s*mix)?"
    + r"\s*(?P<applies>\(applies\s+to[^)]*candidates\s+only\))?"
    + r"\s*[.]?",
    re.I,
)
SALARY_MIX = re.compile(
    r"with\s+an?\s+(?P<mix>\d{2}\s*%?\s*/\s*\d{2}\s*%?)\s*"
    r"\(salary\s*/\s*incentive\)\s*mix",
    re.I,
)
SALARY_TAIL = re.compile(
    r"(?:\s*(?:"
    r"with\s+additional\s+opportunities[^.]*"
    r"|with\s+an?\s+\d{2}\s*%?\s*/\s*\d{2}\s*%?\s*\(salary\s*/\s*incentive\)\s*mix"
    r"|\(applies\s+to[^)]*candidates\s+only\)"
    r"))+\s*[.]?",
    re.I,
)
SALARY_PERIOD_TOKENS = {
    "annually": "year", "annual": "year", "per year": "year",
    "yearly": "year", "per annum": "year", "a year": "year",
    "per hour": "hour", "hourly": "hour", "an hour": "hour",
    "/ hour": "hour", "/hour": "hour", "/hr": "hour",
}
SALARY_LABEL_PERIODS = {"salary range:", "salary:"}


def normalize_salary_text(value):
    """Collapse HTML line breaks/NBSP so the published pay sentence is one line."""
    value = unicodedata.normalize("NFC", value or "")
    for space in ("\u00a0", "\u202f", "\u2009", "\u200b"):
        value = value.replace(space, " ")
    return re.sub(r"\s+", " ", value).strip()


def salary_amount(token, shorthand=False):
    """Parse one published amount without converting currency or inventing digits."""
    token = (token or "").strip().rstrip(",.;")
    if not token:
        return None
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", token):
        value = float(token.replace(",", ""))
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", token):
        # European published format, e.g. EUR "55.050,00".
        value = float(token.replace(".", "").replace(",", "."))
    elif re.fullmatch(r"\d+(?:\.\d+)?", token):
        value = float(token)
    else:
        return None
    if shorthand:
        value *= 1000
    return int(value) if value.is_integer() else round(value, 2)


def salary_published_text(flat, match):
    """Exact pay sentence from the description, with only whitespace collapsed."""
    start = match.start()
    end = match.end()
    tail = SALARY_TAIL.match(flat, end)
    if tail:
        end = tail.end()
    tail = flat[end:end + 2]
    if tail.startswith("."):
        end += 1
    while end > start and flat[end - 1] in " ,;":
        end -= 1
    return flat[start:end].strip()


def salary_currency(match, flat, locations):
    """Keep the published currency; infer only from an explicit US/Canada context.

    ``$`` is shared by several currencies, so a US applicability clause or a US
    location is required before reading it as USD. Records with neither a
    currency token nor a location hint stay null; nothing is guessed.
    """
    code = (match.group("min_code") or match.group("max_code") or "").upper()
    if code:
        return code, "published_code", False
    symbol = match.group("min_symbol") or match.group("max_symbol")
    if SALARY_SYMBOLS.get(symbol):
        return SALARY_SYMBOLS[symbol], "published_symbol", False
    context = match.group(0)
    applied = (match.group("applies") or "").lower()
    us_clause = bool(
        re.search(r"(?:US|United States(?: of America)?)\s+candidates?\s+only", context, re.I)
        or applied
    )
    joined = " | ".join(str(item) for item in (locations or []))
    us_location = "United States" in joined
    canada = "HP Canada" in flat or "Canada" in joined
    prefix = "dollar_symbol_" if symbol == "$" else ""
    if us_clause:
        return "USD", prefix + "us_applicability_clause", True
    if us_location:
        return "USD", prefix + "us_location", True
    if canada:
        return "CAD", prefix + "canada_posting", True
    return None, prefix + "unresolved", False


def parse_salary(text, locations=None):
    """Parse every published HP pay-range spelling into exact min/max/currency/period.

    HP publishes ranges as base-pay ranges, on-target earnings (OTE) with a
    salary/incentive mix, or hourly rates under several labels. ``min``/``max``
    always repeat the published numbers: for OTE they are the published total
    on-target-earnings range, not a base-salary conversion. The exact published
    sentence is retained in ``published_text`` and the base/OTE label in
    ``pay_type``; nothing is converted or extrapolated.
    """
    if not text:
        return None
    flat = normalize_salary_text(text)
    ranges = []
    for match in SALARY_PUBLISHED.finditer(flat):
        groups = match.groupdict()
        minimum = salary_amount(groups["min_number"], bool(groups["min_short"]))
        maximum = salary_amount(groups["max_number"], bool(groups["max_short"]))
        if minimum is None or maximum is None:
            continue
        note = None
        # A single published dot before three digits next to a comma-thousands
        # amount is a thousands separator, not a fractional value.
        if (
            isinstance(minimum, float)
            and minimum < 1000 <= maximum
            and re.fullmatch(r"\d{1,3}\.\d{3}", groups["min_number"])
        ):
            minimum = int(groups["min_number"].replace(".", ""))
            note = "published dot-thousands amount normalised to its integer value"
        currency, currency_source, inferred = salary_currency(match, flat, locations)
        label = re.sub(r"\s+", " ", groups["label"]).strip()
        period_raw = re.sub(r"\s+", " ", groups["period"] or "").lower()
        period = SALARY_PERIOD_TOKENS.get(period_raw)
        period_source = "published" if period else None
        if period is None and label.lower() in SALARY_LABEL_PERIODS:
            # HP's bare "Salary Range:"/"Salary:" lines disclose annual salary.
            period, period_source = "year", "implied_annual_salary_range"
        published = salary_published_text(flat, match)
        mix = groups["mix"]
        if mix is None:
            mix_match = SALARY_MIX.search(published)
            mix = mix_match.group(0) if mix_match else None
        mix_match = SALARY_MIX.search(mix) if mix else None
        entry = {
            "min": minimum,
            "max": maximum,
            "currency": currency,
            "period": period,
            "pay_type": "ote" if "on-target" in label.lower() or "on target" in label.lower() else "base",
            "mix": re.sub(r"\s+", "", mix_match.group("mix")) if mix_match else None,
            "currency_source": currency_source,
            "currency_inferred": inferred,
            "period_source": period_source,
            "pay_label": label,
            "published_text": published,
            "source": "jobDescription",
        }
        if note:
            entry["parsing_note"] = note
        if period == "hour" and minimum >= 1000:
            entry["published_anomaly"] = (
                "published hourly unit carries annual-scale amounts; retained exactly "
                "as published, not converted"
            )
        ranges.append(entry)
    if not ranges:
        return None
    primary = ranges[0]
    additional = [
        item for item in ranges[1:]
        if (item["min"], item["max"], item["currency"], item["period"], item["published_text"])
        != (primary["min"], primary["max"], primary["currency"], primary["period"], primary["published_text"])
    ]
    if additional:
        primary["additional_ranges"] = additional
        primary["multiple_published_ranges"] = True
    return primary


def detail_url(position_id):
    return f"{DETAIL_URL}?position_id={position_id}&domain={DOMAIN}&hl=en"


class HP(Adapter):
    VERIFICATION_REVISION = "hp.official-pcsx-listing-sitemap-reconciled.1"

    def __init__(self, http, company, source, options):
        super().__init__(http, company, source, options)
        self.listing_by_id = {}
        self.failures = {}
        self._detail_progress = None

    async def snapshot(self, phase):
        """Read the full unfiltered UI listing; duplicate page-boundary rows are recorded."""
        started = time.perf_counter()
        rows, pages, seen, duplicate_ids = [], [], set(), []
        total = None
        offset = 0
        while True:
            if not self.page_guard():
                raise ValueError("HP PCSX listing pagination budget exhausted")
            params = {
                "domain": DOMAIN,
                "query": "",
                "location": "",
                "start": offset,
                "sort_by": "timestamp",
            }
            payload = await self.http.json(SEARCH_URL, params=params, headers={"Referer": LISTING_URL})
            count, batch = parse_listing_payload(payload, page_start=offset)
            if total is None:
                total = count
            elif count != total:
                self.result.metadata.setdefault("listing_count_changes", []).append(
                    {"phase": phase, "previous": total, "current": count, "at_start": offset}
                )
                total = count
            if not batch and offset < total:
                raise ValueError(f"HP PCSX listing returned an empty page at {offset} of {total}")
            ids = []
            for row in batch:
                identity = row["id"]
                ids.append(identity)
                if identity in seen:
                    duplicate_ids.append(identity)
                    continue
                seen.add(identity)
                rows.append(row)
            pages.append({"start": offset, "count": len(batch), "ids": ids})
            offset += len(batch)
            if offset >= total:
                break
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
            "pages": pages,
            "seconds": round(seconds, 6),
        }
        self.result.metadata.setdefault("listing_snapshots", []).append(snapshot)
        return rows, total, snapshot

    async def sitemap_ids(self):
        """Read the careers site's own robots-linked sitemap as a cross-check."""
        started = time.perf_counter()
        robots = await self.http.get(ROBOTS_URL, headers={"Referer": LISTING_URL})
        sitemaps = parse_robots_sitemaps(robots.text)
        index_url = next((url for url in sitemaps if "sitemap_index.xml" in url), None)
        if index_url is None:
            raise ValueError("HP robots.txt no longer publishes its careers sitemap index")
        index = await self.http.get(index_url, headers={"Referer": LISTING_URL})
        sitemap_url = next(
            (url for url in parse_sitemap_index(index.text) if "sitemap.txt" in url), None
        )
        if sitemap_url is None:
            raise ValueError("HP sitemap index no longer publishes sitemap.txt")
        sitemap = await self.http.get(sitemap_url, headers={"Referer": LISTING_URL})
        ids = parse_sitemap_job_ids(sitemap.text)
        self.result.metadata["sitemap"] = {
            "robots_url": ROBOTS_URL,
            "sitemap_index_url": index_url,
            "sitemap_url": sitemap_url,
            "jobs": len(ids),
            "seconds": round(max(0.0, time.perf_counter() - started), 6),
        }
        return set(ids)

    async def fetch_details(self, ids, *, source):
        """Fetch exact details with two workers and stop on 429/403 challenges."""
        started = time.perf_counter()
        semaphore = asyncio.Semaphore(max(1, self.options.detail_concurrency))
        stop = asyncio.Event()
        details, failures = {}, {}
        stats = {
            "total": len(ids),
            "completed": 0,
            "fetched": 0,
            "failed": 0,
            "stopped": False,
        }
        self.result.metadata.setdefault("detail_phases", []).append(stats)

        async def one(position_id):
            async with semaphore:
                if stop.is_set():
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
                    failures[position_id] = {"url": url, "reason": message, "source": source}
                    stats["failed"] += 1
                    if any(token in message for token in CHALLENGE_TOKENS):
                        stop.set()
                        stats["stopped"] = True
                        stats["challenge"] = message
                except Exception as exc:
                    failures[position_id] = {
                        "url": url,
                        "reason": f"{type(exc).__name__}: {exc}",
                        "source": source,
                    }
                    stats["failed"] += 1
                finally:
                    stats["completed"] += 1
                    if self._detail_progress and stats["completed"] % 25 == 0:
                        self._detail_progress(stats)

        await asyncio.gather(*(one(position_id) for position_id in ids))
        seconds = max(0.0, time.perf_counter() - started)
        stats["duration_seconds"] = round(seconds, 6)
        self.result.timings["detail_phase_seconds"] = (
            self.result.timings.get("detail_phase_seconds", 0.0) + seconds
        )
        return details, failures, stats

    async def search_membership(self, detail):
        """Prove a sitemap-only id still belongs to HP's unfiltered search index."""
        identity = detail["id"]
        query = detail["displayJobId"]
        offset = 0
        total = None
        found = False
        pages = []
        while True:
            payload = await self.http.json(
                SEARCH_URL,
                params={
                    "domain": DOMAIN,
                    "query": query,
                    "location": "",
                    "start": offset,
                    "sort_by": "timestamp",
                },
                headers={"Referer": LISTING_URL},
            )
            count, batch = parse_listing_payload(payload, page_start=offset)
            if total is None:
                total = count
            elif count != total:
                raise ValueError(f"HP search count changed for {query!r}")
            pages.append({"start": offset, "count": len(batch)})
            if any(str(row["id"]) == str(identity) for row in batch):
                found = True
                break
            offset += len(batch)
            if offset >= total or not batch:
                break
        return found, {"query": query, "count": total, "pages": pages}

    def job_from_detail(self, detail, listing=None):
        body = description_html(detail)
        text = text_html(body)
        labels = published_labels(body)
        action = ((detail.get("positionUserActions") or {}).get("applyAction") or {})
        apply_url = action.get("applyUrl")
        warnings = []
        if apply_url:
            parsed = urlsplit(apply_url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError(f"HP apply URL is not valid: {apply_url!r}")
            if parsed.hostname != APPLY_HOST:
                warnings.append("apply_host_unexpected")
        row = listing or {
            key: detail.get(key)
            for key in (
                "id",
                "displayJobId",
                "name",
                "locations",
                "standardizedLocations",
                "postedTs",
                "stars",
                "department",
                "creationTs",
                "isHot",
                "workLocationOption",
                "locationFlexibility",
                "atsJobId",
                "positionUrl",
            )
        }
        job = self.job(
            {
                "listing": row,
                "detail": detail,
                "detail_url": detail_url(detail["id"]),
                "published_labels": labels,
                "description_headings": description_headings(body),
                "membership_source": "listing" if listing else "sitemap_detail_search",
            },
            title=detail["name"],
            job_id=str(detail["id"]),
            requisition_id=detail["displayJobId"],
            url=detail["publicUrl"],
            apply_url=apply_url,
            locations=detail.get("locations") or row.get("locations") or [],
            department=detail.get("department"),
            employment_type=labels.get("Schedule"),
            workplace_type=detail.get("workLocationOption"),
            date_posted=epoch(detail.get("postedTs")),
            date_posted_raw=detail.get("postedTs"),
            salary=parse_salary(text, detail.get("locations") or row.get("locations") or []),
            employer="HP Inc.",
            language="en",
            description=body,
            method="hp.pcsx.position_details.jobDescription+position_custom_content",
            description_url=detail_url(detail["id"]),
        )
        job.raw["standardized_locations"] = detail.get("standardizedLocations")
        job.raw["date_fields"] = {
            "postedTs": detail.get("postedTs"),
            "externallyPostedTs": detail.get("externallyPostedTs"),
            "creationTs": detail.get("creationTs"),
        }
        job.raw["apply_action"] = action
        job.raw["membership_search"] = detail.get("_hp_membership_search")
        job.warnings.extend(warnings)
        return job

    def fallback_job(self, row, reason):
        """Keep an advertised listing row when its exact detail could not be read."""
        job = self.job(
            {"listing": row, "detail_error": reason},
            title=row["name"],
            job_id=str(row["id"]),
            requisition_id=row["displayJobId"],
            url=urljoin(LISTING_URL, row["positionUrl"]),
            locations=row.get("locations") or [],
            department=row.get("department"),
            workplace_type=row.get("workLocationOption"),
            date_posted=epoch(row.get("postedTs")),
            date_posted_raw=row.get("postedTs"),
            employer="HP Inc.",
            language="en",
            method="hp.pcsx.position_details",
            description_url=detail_url(row["id"]),
        )
        job.description_unavailable = True
        job.warnings.append("detail_fetch_failed")
        return job

    async def run(self):
        # 1. Official entry and HP-branded listing configuration.
        entry = await self.http.get(OFFICIAL_ENTRY)
        entry_links = official_entry_links(entry.text)
        if not entry_links:
            raise ValueError("HP official careers entry no longer links to apply.hp.com/careers")
        listing = await self.http.get(LISTING_URL, headers={"Referer": OFFICIAL_ENTRY})
        config = parse_pcsx_config(listing.text)
        self.result.metadata.update(
            {
                "verification_revision": self.VERIFICATION_REVISION,
                "official_entry_url": OFFICIAL_ENTRY,
                "official_entry_status": entry.status_code,
                "official_entry_final_url": str(entry.url),
                "official_entry_links_to_listing": entry_links,
                "listing_url": LISTING_URL,
                "listing_authority": "HP-branded PCSX listing reached from jobs.hp.com",
                "pcsx_config": config,
                "request_scope": "domain=hp.com unfiltered PCSX listing; no country/seniority filters",
            }
        )

        # 2. Listing pages and the site's own sitemap cross-check.
        initial_rows, initial_total, initial_snapshot = await self.snapshot("initial")
        sitemap = await self.sitemap_ids()
        initial_ids = {row["id"] for row in initial_rows}
        self.listing_by_id = {row["id"]: row for row in initial_rows}
        self.result.metadata["membership_reconciliation"] = {
            "initial_advertised": initial_total,
            "initial_listing_rows": initial_snapshot["rows"],
            "initial_listing_unique": initial_snapshot["unique_ids"],
            "initial_duplicates": initial_snapshot["duplicate_ids"],
            "sitemap_jobs": len(sitemap),
            "sitemap_only": sorted(sitemap - initial_ids),
            "listing_only": sorted(initial_ids - sitemap),
        }

        # 3. Exact detail for every listing id and every sitemap cross-check id.
        candidates = sorted(initial_ids | sitemap)
        details, failures, _ = await self.fetch_details(candidates, source="initial_listing_or_sitemap")

        # 4. A second listing snapshot catches additions during the detail phase.
        try:
            final_rows, final_total, final_snapshot = await self.snapshot("final")
            final_snapshot_error = None
        except Exception as exc:
            final_snapshot_error = f"{type(exc).__name__}: {exc}"
            final_rows, final_total = initial_rows, initial_total
            final_snapshot = {
                "phase": "final",
                "error": final_snapshot_error,
                "total": initial_total,
                "rows": initial_snapshot["rows"],
                "unique_ids": initial_snapshot["unique_ids"],
                "duplicate_rows": initial_snapshot["duplicate_rows"],
                "duplicate_ids": initial_snapshot["duplicate_ids"],
                "pages": initial_snapshot["pages"],
                "reused_initial_snapshot": True,
            }
            self.result.errors.append(f"Final HP listing snapshot failed: {final_snapshot_error}")
        final_ids = {row["id"] for row in final_rows}
        for row in final_rows:
            self.listing_by_id.setdefault(row["id"], row)
        new_final_ids = sorted(final_ids - set(details) - set(failures))
        if new_final_ids:
            more_details, more_failures, _ = await self.fetch_details(
                new_final_ids, source="final_listing"
            )
            details.update(more_details)
            failures.update(more_failures)

        # 5. Sitemap-only rows must still be found by exact requisition search.
        sitemap_only = sorted(sitemap - initial_ids - final_ids)
        sitemap_active, sitemap_rejected = [], []
        for position_id in sitemap_only:
            detail = details.get(position_id)
            if detail is None:
                sitemap_rejected.append(
                    {"id": position_id, "reason": failures.get(position_id, {}).get("reason", "no detail")}
                )
                continue
            try:
                found, evidence = await self.search_membership(detail)
            except Exception as exc:
                sitemap_rejected.append({"id": position_id, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            detail.setdefault("_hp_membership_search", evidence)
            if found:
                sitemap_active.append(position_id)
            else:
                sitemap_rejected.append({"id": position_id, "reason": "displayJobId search did not return exact id", "evidence": evidence})

        # 6. Initial ids missing from the final snapshot stay only if still in the
        # exact search index; this distinguishes a live posting from a removal.
        final_active = set(final_ids)
        kept_initial_only, removed_during_run = [], []
        for position_id in sorted(initial_ids - final_ids):
            if position_id in sitemap_active:
                kept_initial_only.append(position_id)
                continue
            detail = details.get(position_id)
            if detail is None:
                removed_during_run.append({"id": position_id, "reason": "no final listing row and no detail"})
                continue
            try:
                found, evidence = await self.search_membership(detail)
            except Exception as exc:
                removed_during_run.append({"id": position_id, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if found:
                detail.setdefault("_hp_membership_search", evidence)
                kept_initial_only.append(position_id)
            else:
                removed_during_run.append({"id": position_id, "reason": "not in final listing or exact search", "evidence": evidence})

        # 7. Build final records in published listing order.
        final_members = (final_active | set(sitemap_active) | set(kept_initial_only))
        order = []
        for row in initial_rows + final_rows:
            if row["id"] in final_members and row["id"] not in order:
                order.append(row["id"])
        for position_id in sorted(set(sitemap_active) - set(order)):
            order.append(position_id)

        jobs = {}
        for position_id in order:
            detail = details.get(position_id)
            if detail is not None:
                jobs[position_id] = self.job_from_detail(
                    detail, self.listing_by_id.get(position_id)
                )
            elif position_id in self.listing_by_id:
                reason = failures.get(position_id, {}).get("reason", "detail unavailable")
                jobs[position_id] = self.fallback_job(self.listing_by_id[position_id], reason)
                self.failures[position_id] = failures.get(position_id, {"reason": reason})
                self.result.errors.append(f"Detail {detail_url(position_id)}: {reason}")
            else:
                self.failures[position_id] = failures.get(
                    position_id, {"reason": "sitemap detail unavailable"}
                )

        for job in jobs.values():
            self.add(job)

        # 8. Reconcile every page/id/count and keep failures explicit.
        collected = len(self.result.jobs)
        expected = final_total if final_total is not None else initial_total
        self.result.expected_total = expected
        membership_ok = collected == expected and not removed_during_run
        self.result.listing_complete = bool(membership_ok and initial_total == final_total)
        if not membership_ok:
            self.result.errors.append(
                f"HP membership mismatch: advertised {expected}, collected {collected}"
            )
        if initial_total != final_total:
            self.result.warnings.append("listing_count_changed_during_run")
        if removed_during_run:
            self.result.warnings.append("posting_removed_during_run")

        self.result.metadata["membership_reconciliation"].update(
            {
                "final_advertised": final_total,
                "final_listing_rows": final_snapshot["rows"],
                "final_listing_unique": final_snapshot["unique_ids"],
                "final_duplicates": final_snapshot["duplicate_ids"],
                "final_snapshot_error": final_snapshot_error,
                "final_members": len(final_members),
                "collected": collected,
                "listing_only_initial": sorted(initial_ids - sitemap),
                "sitemap_only_initial": sorted(sitemap - initial_ids),
                "sitemap_active_recovered": sorted(sitemap_active),
                "sitemap_rejected": sitemap_rejected,
                "kept_initial_only": sorted(kept_initial_only),
                "removed_during_run": removed_during_run,
                "detail_failures": {
                    str(key): value for key, value in sorted(self.failures.items())
                },
                "listing_and_sitemap_reconciled": membership_ok,
            }
        )
        self.result.metadata["detail_stats"] = {
            "requested": len(candidates) + len(new_final_ids),
            "succeeded": len(details),
            "failed": len(failures),
        }
        return self.result
